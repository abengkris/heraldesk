"""Idempotent Telegram notifications.

Every event produces at most one ``telegram_notifications`` row and one audit entry.
The row is written even when the profile policy mutes the event, so a later schedule
sweep cannot decide to send an old event out of the blue.

Notification delivery is at-most-once, matching the project's publishing rule: if the
Bot API errors, the event is recorded as ``failed`` and is not retried automatically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import Approval, ContentItem
from heraldesk.db.repositories import TelegramNotificationRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.errors import ProfileValidationError
from heraldesk.domain.notifications import (
    NotifyEvent,
    approval_key,
    failed_final_key,
    killswitch_key,
    published_key,
    should_notify,
)
from heraldesk.domain.policy import NotifyMode, parse_policy_yaml
from heraldesk.telegram.base import TelegramAdapter, TelegramError
from heraldesk.telegram.formatting import (
    approval_message,
    failed_message,
    killswitch_message,
    mask_chat_id,
    preview,
    published_message,
)

MAX_STORED_PREVIEW = 500


@dataclass(frozen=True, slots=True)
class NotificationOutcome:
    """What happened to one notification event."""

    event: str
    status: str
    notification_id: int
    recipients: tuple[str, ...] = ()
    reason: str | None = None
    duplicate: bool = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class NotificationService:
    """Store and send idempotent Telegram notifications for one session."""

    def __init__(
        self,
        session: Session,
        layout: paths.Layout,
        adapter: TelegramAdapter,
        *,
        allowed_chat_ids: Sequence[int],
        redactor: SecretRegistry | None = None,
        actor: str = "system",
    ) -> None:
        self._session = session
        self._layout = layout
        self._adapter = adapter
        self._allowed_chat_ids = tuple(allowed_chat_ids)
        self._redactor = redactor or SecretRegistry.from_environment()
        self._actor = actor
        self._repository = TelegramNotificationRepository(session)
        self._audit = AuditService(session)

    def _policy_mode(self, slug: str | None) -> NotifyMode:
        if slug is None:
            return NotifyMode.ALL
        path = self._layout.profiles / slug / "policy.yaml"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return NotifyMode.ALL
        try:
            return parse_policy_yaml(text, source=str(path)).notify
        except ProfileValidationError:
            return NotifyMode.ALL

    def _masked_recipients(self) -> tuple[str, ...]:
        return tuple(mask_chat_id(chat_id) for chat_id in self._allowed_chat_ids)

    def _dispatch(
        self,
        scope: ProfileScope | None,
        *,
        event: NotifyEvent,
        idempotency_key: str,
        message: str,
        mode: NotifyMode,
        now: datetime | None,
    ) -> NotificationOutcome:
        existing = self._repository.find(scope, idempotency_key)
        if existing is not None:
            return NotificationOutcome(
                event=existing.event,
                status=existing.status,
                notification_id=existing.id,
                recipients=tuple(existing.recipients),
                reason="duplicate event; notification was already recorded",
                duplicate=True,
            )

        current = _as_utc(datetime.now(UTC) if now is None else now)
        redacted_message = self._redactor.redact(message)
        stored_preview = preview(redacted_message, limit=MAX_STORED_PREVIEW)
        masked = self._masked_recipients()
        reason: str | None = None
        if not should_notify(mode, event):
            reason = f"notify mode '{mode.value}' mutes this event"
        elif not self._allowed_chat_ids:
            reason = "no telegram.allowed_chat_ids are configured"

        if reason is not None:
            row = self._repository.add(
                scope,
                event=event.value,
                idempotency_key=idempotency_key,
                status="suppressed",
                message_preview=stored_preview,
                recipients=list(masked),
                error=None,
                created_at=current,
            )
            self._audit.record(
                scope,
                actor=self._actor,
                action="telegram.suppressed",
                entity="telegram_notification",
                entity_id=row.id,
                payload={
                    "event": event.value,
                    "notification_id": row.id,
                    "recipients": list(masked),
                    "reason": reason,
                },
            )
            return NotificationOutcome(
                event=event.value,
                status="suppressed",
                notification_id=row.id,
                recipients=masked,
                reason=reason,
            )

        try:
            for chat_id in self._allowed_chat_ids:
                self._adapter.send_message(chat_id, redacted_message)
        except TelegramError as exc:
            error = self._redactor.redact(str(exc))[:MAX_STORED_PREVIEW]
            row = self._repository.add(
                scope,
                event=event.value,
                idempotency_key=idempotency_key,
                status="failed",
                message_preview=stored_preview,
                recipients=list(masked),
                error=error,
                created_at=current,
            )
            self._audit.record(
                scope,
                actor=self._actor,
                action="telegram.failed",
                entity="telegram_notification",
                entity_id=row.id,
                payload={
                    "event": event.value,
                    "notification_id": row.id,
                    "recipients": list(masked),
                    "error": error,
                },
            )
            return NotificationOutcome(
                event=event.value,
                status="failed",
                notification_id=row.id,
                recipients=masked,
                reason=error,
            )

        row = self._repository.add(
            scope,
            event=event.value,
            idempotency_key=idempotency_key,
            status="sent",
            message_preview=stored_preview,
            recipients=list(masked),
            error=None,
            created_at=current,
            sent_at=current,
        )
        self._audit.record(
            scope,
            actor=self._actor,
            action="telegram.sent",
            entity="telegram_notification",
            entity_id=row.id,
            payload={
                "event": event.value,
                "notification_id": row.id,
                "recipients": list(masked),
            },
        )
        return NotificationOutcome(
            event=event.value,
            status="sent",
            notification_id=row.id,
            recipients=masked,
        )

    def notify_approval_requested(
        self,
        scope: ProfileScope,
        *,
        slug: str,
        item: ContentItem,
        approval: Approval,
        now: datetime | None = None,
    ) -> NotificationOutcome:
        """Notify the human that an item needs approval."""
        body = self._redactor.redact(item.body)
        message = approval_message(
            item_id=item.id,
            platform=item.platform,
            risk_level=item.risk_level,
            body=body,
        )
        return self._dispatch(
            scope,
            event=NotifyEvent.APPROVAL_REQUESTED,
            idempotency_key=approval_key(scope.profile_id, item.id, approval.id),
            message=message,
            mode=self._policy_mode(slug),
            now=now,
        )

    def notify_published(
        self,
        scope: ProfileScope,
        *,
        slug: str,
        item: ContentItem,
        now: datetime | None = None,
    ) -> NotificationOutcome:
        """Notify the human that an item reached ``published``."""
        message = published_message(
            item_id=item.id,
            platform=item.platform,
            external_id=item.external_id,
        )
        return self._dispatch(
            scope,
            event=NotifyEvent.PUBLISHED,
            idempotency_key=published_key(scope.profile_id, item.id, item.publish_attempts),
            message=message,
            mode=self._policy_mode(slug),
            now=now,
        )

    def notify_failed_final(
        self,
        scope: ProfileScope,
        *,
        slug: str,
        item: ContentItem,
        now: datetime | None = None,
    ) -> NotificationOutcome:
        """Notify the human that publishing exhausted every retry."""
        error = self._redactor.redact(item.last_error or "")
        message = failed_message(
            item_id=item.id,
            platform=item.platform,
            attempts=item.publish_attempts,
            error=error or None,
        )
        return self._dispatch(
            scope,
            event=NotifyEvent.FAILED_FINAL,
            idempotency_key=failed_final_key(
                scope.profile_id,
                item.id,
                item.publish_attempts,
                _as_utc(item.updated_at),
            ),
            message=message,
            mode=self._policy_mode(slug),
            now=now,
        )

    def notify_killswitch(
        self,
        scope: ProfileScope | None,
        *,
        slug: str | None,
        enabled: bool,
        event_id: str,
        now: datetime | None = None,
    ) -> NotificationOutcome:
        """Notify the human that a global or per-profile kill switch changed."""
        profile_id = None if scope is None else scope.profile_id
        return self._dispatch(
            scope,
            event=NotifyEvent.KILLSWITCH,
            idempotency_key=killswitch_key(profile_id, enabled=enabled, event_id=event_id),
            message=killswitch_message(slug=slug, enabled=enabled),
            mode=self._policy_mode(slug),
            now=now,
        )
