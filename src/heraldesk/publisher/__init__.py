"""The publisher: the only module that writes content to a platform.

The flow is deterministic. The scheduler selects due items; this module prepares the
state (``approved``/``failed`` -> ``scheduled`` when needed), resolves the account,
calls the adapter through its contract, and records the outcome.

Rules:

* success moves ``scheduled`` -> ``published`` and stores ``external_id``/``published_at``;
* a provider failure moves ``scheduled`` -> ``failed``, stores a redacted error and
  schedules a retry after backoff;
* after three attempts (one initial plus two retries) the item stays ``failed`` until a
  human runs ``heraldesk publish <id>``;
* credentials are decrypted only for the adapter call and are added to the redaction
  registry so a chatty provider error cannot leak them.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher, CryptoError
from heraldesk.core.locks import (
    BlockReason,
    OffSwitchState,
    evaluate_off_switches,
    is_global_kill_switch_enabled,
)
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import ContentItem
from heraldesk.db.repositories import (
    ApprovalRepository,
    ContentItemRepository,
    ProfileRepository,
    RecordNotFound,
    SocialAccountRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.errors import ContentError, ContentItemNotFoundError, ProfileValidationError
from heraldesk.domain.profiles import load_policy
from heraldesk.domain.scheduling import (
    POLICY_UNAVAILABLE_REASON,
    PublishGate,
    evaluate_publish_gate,
)
from heraldesk.platforms.base import (
    AccountRef,
    PlatformAdapter,
    PublishRequest,
)

if TYPE_CHECKING:
    from heraldesk.telegram.notifier import NotificationService

MAX_PUBLISH_ATTEMPTS: Final = 3
DEFAULT_RETRY_BACKOFF_SECONDS: Final[tuple[int, ...]] = (60, 300)
MAX_AUDIT_TEXT: Final = 500

PublishTrigger = Literal["manual", "scheduler"]


class PublishStatus(StrEnum):
    """Outcome of one publish call."""

    PUBLISHED = "published"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """Everything the CLI or scheduler needs to report one publish attempt."""

    item_id: int
    platform: str
    status: PublishStatus
    from_state: str
    to_state: str
    external_id: str | None = None
    error: str | None = None
    attempts: int = 0
    retry_at: datetime | None = None
    reason: str | None = None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _current(now: datetime | None) -> datetime:
    return _as_utc(datetime.now(UTC) if now is None else now)


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _backoff_seconds(backoff: Sequence[int], attempt: int) -> int:
    if not backoff:
        return 0
    return max(0, backoff[min(attempt - 1, len(backoff) - 1)])


def publish_block_reason(
    session: Session,
    layout: paths.Layout,
    scope: ProfileScope,
) -> BlockReason | None:
    """Return the off-switch reason that forbids publishing for this profile, if any."""
    try:
        profile = ProfileRepository(session).require(scope)
    except RecordNotFound as exc:
        raise ContentError(str(exc)) from exc
    gate = evaluate_off_switches(
        OffSwitchState(
            global_kill_switch=is_global_kill_switch_enabled(layout),
            profile_kill_switch=profile.kill_switch,
            profile_status=profile.status,
        )
    )
    return gate.reason


def policy_skips_scheduler(item: ContentItem) -> bool:
    """Return ``True`` when the policy says the scheduler must never auto-publish."""
    return item.publish_mode == "draft_only"


def policy_skip_reason(
    session: Session,
    layout: paths.Layout,
    scope: ProfileScope,
    item: ContentItem,
    *,
    trigger: PublishTrigger,
    current: datetime,
    actor: str,
) -> str | None:
    """Apply quiet hours and the daily cap to one publish attempt.

    Returns a stable reason code when an automatic attempt must be skipped. A human
    ``manual`` publish overrides the gate and records ``publish.policy_override`` so the
    exception stays auditable. A missing or malformed ``policy.yaml`` fails closed for
    automatic publishing instead of pretending there is no limit.
    """
    try:
        profile = ProfileRepository(session).require(scope)
    except RecordNotFound as exc:
        raise ContentError(str(exc)) from exc
    try:
        policy = load_policy(layout.profiles / profile.slug)
        gate = evaluate_publish_gate(
            session,
            scope,
            timezone=profile.timezone,
            policy=policy,
            now=current,
        )
    except ProfileValidationError as exc:
        gate = PublishGate(
            False,
            POLICY_UNAVAILABLE_REASON,
            f"cannot read policy.yaml: {exc}",
        )
    if gate.allowed:
        return None
    if trigger == "manual":
        AuditService(session).record(
            scope,
            actor=actor,
            action="publish.policy_override",
            entity="content_item",
            entity_id=item.id,
            payload={
                "content_item_id": item.id,
                "platform": item.platform,
                "reason": gate.reason or POLICY_UNAVAILABLE_REASON,
                "detail": (gate.detail or "")[:MAX_AUDIT_TEXT],
                "trigger": trigger,
                "timezone": profile.timezone,
            },
        )
        return None
    return gate.reason or POLICY_UNAVAILABLE_REASON


def approval_skip_reason(session: Session, scope: ProfileScope, item: ContentItem) -> str | None:
    """Return why the latest approval forbids publishing, or ``None`` when it allows it.

    Auto modes have no approval row, which is allowed. A pending, rejected or expired
    approval always blocks publishing; M4 cancels items on expiry, and this check keeps a
    scheduled row from slipping through if that cleanup was missed.
    """
    latest = ApprovalRepository(session).latest_for_item(scope, item.id)
    if latest is None:
        return None
    if latest.state != "approved":
        return f"latest approval is {latest.state}"
    return None


def _resolve_account(
    session: Session,
    scope: ProfileScope,
    item: ContentItem,
    cipher: CredentialCipher,
) -> AccountRef:
    repository = SocialAccountRepository(session)
    account = None
    if item.account_id is not None:
        account = repository.get(scope, item.account_id)
    if account is None:
        for candidate in repository.list(scope):
            if candidate.platform == item.platform:
                account = candidate
                break
    if account is None:
        if item.platform == "fake":
            return AccountRef(platform="fake", handle="fake", credentials={})
        raise ContentError(f"no {item.platform} account is configured for this profile")
    try:
        credentials = cipher.decrypt_json(account.credentials_encrypted)
    except CryptoError as exc:
        raise ContentError(str(exc)) from exc
    return AccountRef(
        platform=account.platform,
        handle=account.handle,
        credentials=credentials,
    )


def _prepare_state(
    session: Session,
    scope: ProfileScope,
    item: ContentItem,
    *,
    trigger: PublishTrigger,
    current: datetime,
    actor: str,
) -> str:
    """Move the item to ``scheduled`` when the caller is allowed to publish it now."""
    repository = ContentItemRepository(session)
    if item.state == ContentState.SCHEDULED.value:
        return item.state
    if item.state == ContentState.FAILED.value:
        if trigger == "manual":
            repository.record_publish_attempt(scope, item.id, attempts=0, error=None)
        transition(
            session,
            scope,
            item.id,
            ContentState.SCHEDULED,
            actor=actor,
            reason=f"retry requested via {trigger}",
            schedule_at=current,
        )
        return item.state
    if item.state == ContentState.APPROVED.value and trigger == "manual":
        transition(
            session,
            scope,
            item.id,
            ContentState.SCHEDULED,
            actor=actor,
            reason="manual publish",
            schedule_at=current,
        )
        return item.state
    raise ContentError(
        f"content item {item.id} is {item.state}; only approved, scheduled or failed "
        "items can be published"
    )


def _record_failure(
    session: Session,
    scope: ProfileScope,
    item: ContentItem,
    *,
    from_state: str,
    attempts: int,
    error: str,
    current: datetime,
    actor: str,
    trigger: PublishTrigger,
    backoff_seconds: Sequence[int],
) -> PublishOutcome:
    repository = ContentItemRepository(session)
    will_retry = attempts < MAX_PUBLISH_ATTEMPTS
    retry_at: datetime | None = None
    if will_retry:
        retry_at = current + timedelta(seconds=_backoff_seconds(backoff_seconds, attempts))

    repository.record_publish_attempt(scope, item.id, attempts=attempts, error=error)
    transition(
        session,
        scope,
        item.id,
        ContentState.FAILED,
        actor=actor,
        reason=f"publish attempt {attempts} failed via {trigger}",
    )
    repository.set_scheduled_at(scope, item.id, scheduled_at=retry_at)

    AuditService(session).record(
        scope,
        actor=actor,
        action="publish.failed",
        entity="content_item",
        entity_id=item.id,
        payload={
            "content_item_id": item.id,
            "platform": item.platform,
            "attempt": attempts,
            "will_retry": will_retry,
            "retry_at": retry_at.isoformat() if retry_at is not None else None,
            "error": error,
            "trigger": trigger,
        },
    )
    return PublishOutcome(
        item_id=item.id,
        platform=item.platform,
        status=PublishStatus.FAILED,
        from_state=from_state,
        to_state=ContentState.FAILED.value,
        error=error,
        attempts=attempts,
        retry_at=retry_at,
    )


def _profile_slug(session: Session, scope: ProfileScope) -> str | None:
    try:
        return ProfileRepository(session).require(scope).slug
    except RecordNotFound:
        return None


def _notify_published(
    notifier: NotificationService | None,
    session: Session,
    scope: ProfileScope,
    item_id: int,
    *,
    now: datetime,
) -> None:
    if notifier is None:
        return
    item = ContentItemRepository(session).get(scope, item_id)
    slug = _profile_slug(session, scope)
    if item is None or slug is None:
        return
    notifier.notify_published(scope, slug=slug, item=item, now=now)


def _notify_failed_final(
    notifier: NotificationService | None,
    session: Session,
    scope: ProfileScope,
    item_id: int,
    *,
    now: datetime,
) -> None:
    if notifier is None:
        return
    item = ContentItemRepository(session).get(scope, item_id)
    slug = _profile_slug(session, scope)
    if item is None or slug is None:
        return
    notifier.notify_failed_final(scope, slug=slug, item=item, now=now)


def publish_item(
    session: Session,
    layout: paths.Layout,
    scope: ProfileScope,
    item_id: int,
    *,
    adapter: PlatformAdapter | None,
    cipher: CredentialCipher,
    now: datetime | None = None,
    actor: str = "system",
    trigger: PublishTrigger = "scheduler",
    retry_backoff_seconds: Sequence[int] = DEFAULT_RETRY_BACKOFF_SECONDS,
    notifier: NotificationService | None = None,
) -> PublishOutcome:
    """Publish one content item once, recording success or a retryable failure.

    A missing adapter is treated as a provider failure with a documented message, so an
    unimplemented platform leaves a visible ``failed`` row instead of a silent skip.
    """
    current = _current(now)
    repository = ContentItemRepository(session)
    item = repository.get(scope, item_id)
    if item is None:
        raise ContentItemNotFoundError(
            f"content item {item_id} does not exist for profile {scope.profile_id}"
        )

    from_state = item.state
    block = publish_block_reason(session, layout, scope)
    if block is not None:
        return PublishOutcome(
            item_id=item.id,
            platform=item.platform,
            status=PublishStatus.SKIPPED,
            from_state=from_state,
            to_state=from_state,
            reason=block.value,
        )
    if trigger != "manual" and policy_skips_scheduler(item):
        return PublishOutcome(
            item_id=item.id,
            platform=item.platform,
            status=PublishStatus.SKIPPED,
            from_state=from_state,
            to_state=from_state,
            reason="publish mode draft_only is manual only",
        )
    approval_reason = approval_skip_reason(session, scope, item)
    if approval_reason is not None:
        return PublishOutcome(
            item_id=item.id,
            platform=item.platform,
            status=PublishStatus.SKIPPED,
            from_state=from_state,
            to_state=from_state,
            reason=approval_reason,
        )
    if item.external_id:
        return PublishOutcome(
            item_id=item.id,
            platform=item.platform,
            status=PublishStatus.SKIPPED,
            from_state=from_state,
            to_state=from_state,
            reason="item already has an external id",
            external_id=item.external_id,
        )
    policy_reason = policy_skip_reason(
        session,
        layout,
        scope,
        item,
        trigger=trigger,
        current=current,
        actor=actor,
    )
    if policy_reason is not None:
        return PublishOutcome(
            item_id=item.id,
            platform=item.platform,
            status=PublishStatus.SKIPPED,
            from_state=from_state,
            to_state=from_state,
            reason=policy_reason,
        )

    _prepare_state(session, scope, item, trigger=trigger, current=current, actor=actor)
    attempts = item.publish_attempts + 1

    real_adapter = adapter
    redactor = SecretRegistry.from_environment()
    try:
        account = _resolve_account(session, scope, item, cipher)
    except ContentError as exc:
        error = redactor.redact(str(exc))[:MAX_AUDIT_TEXT]
        outcome = _record_failure(
            session,
            scope,
            item,
            from_state=from_state,
            attempts=attempts,
            error=error,
            current=current,
            actor=actor,
            trigger=trigger,
            backoff_seconds=retry_backoff_seconds,
        )
        if outcome.retry_at is None:
            _notify_failed_final(notifier, session, scope, item.id, now=current)
        return outcome

    redactor = SecretRegistry.from_environment(extra_values=account.credentials.values())
    body = redactor.redact(item.body)

    AuditService(session).record(
        scope,
        actor=actor,
        action="publish.requested",
        entity="content_item",
        entity_id=item.id,
        payload={
            "content_item_id": item.id,
            "platform": item.platform,
            "attempt": attempts,
            "trigger": trigger,
            "publish_mode": item.publish_mode,
            "body_sha256": _body_hash(body),
            "body_length": len(body),
        },
    )

    if real_adapter is None:
        error = f"no publisher adapter for platform '{item.platform}'; see docs/platforms/README.md"
        outcome = _record_failure(
            session,
            scope,
            item,
            from_state=from_state,
            attempts=attempts,
            error=error,
            current=current,
            actor=actor,
            trigger=trigger,
            backoff_seconds=retry_backoff_seconds,
        )
        if outcome.retry_at is None:
            _notify_failed_final(notifier, session, scope, item.id, now=current)
        return outcome

    request = PublishRequest(
        content_item_id=item.id,
        platform=item.platform,
        body=body,
        idempotency_key=f"heraldesk:content:{item.id}",
        media=dict(item.media),
    )
    try:
        result = asyncio.run(real_adapter.publish(account, request))
    except Exception as exc:  # adapters must never crash the publisher
        message = redactor.redact(str(exc)).strip()
        error = (message or type(exc).__name__)[:MAX_AUDIT_TEXT]
        outcome = _record_failure(
            session,
            scope,
            item,
            from_state=from_state,
            attempts=attempts,
            error=error,
            current=current,
            actor=actor,
            trigger=trigger,
            backoff_seconds=retry_backoff_seconds,
        )
        if outcome.retry_at is None:
            _notify_failed_final(notifier, session, scope, item.id, now=current)
        return outcome

    transition(
        session,
        scope,
        item.id,
        ContentState.PUBLISHED,
        actor=actor,
        reason=f"published via {trigger}",
    )
    repository.record_publish_attempt(
        scope,
        item.id,
        attempts=attempts,
        error=None,
    )
    repository.mark_published(
        scope,
        item.id,
        external_id=result.external_id,
        published_at=current,
    )
    AuditService(session).record(
        scope,
        actor=actor,
        action="publish.succeeded",
        entity="content_item",
        entity_id=item.id,
        payload={
            "content_item_id": item.id,
            "platform": item.platform,
            "external_id": result.external_id,
            "attempts": attempts,
            "trigger": trigger,
            "body_sha256": _body_hash(body),
        },
    )
    _notify_published(notifier, session, scope, item.id, now=current)
    return PublishOutcome(
        item_id=item.id,
        platform=item.platform,
        status=PublishStatus.PUBLISHED,
        from_state=from_state,
        to_state=ContentState.PUBLISHED.value,
        external_id=result.external_id,
        attempts=attempts,
    )
