"""Minimal inbound Telegram inbox.

A message is stored only when its chat id is on the allowlist. Commands call the same
domain services as the CLI: approvals keep the kill-switch gate, the 72-hour expiry check
and the audit entries, with ``channel='telegram'``.

``heraldesk serve`` feeds this module from the webhook endpoint and the background
poller. Telegram can retry a webhook or replay ``getUpdates`` after a restart, so a
recorded ``update_id`` is never processed twice.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import (
    ProfileCatalog,
    TelegramMessageRepository,
    TelegramUpdateLocator,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import (
    approve,
    expire_pending_approvals,
    list_queue,
    reject,
    scope_for_content_item,
)
from heraldesk.domain.errors import ApprovalError, ProfileNotFoundError, ProfileValidationError
from heraldesk.domain.profiles import resolve_scope
from heraldesk.telegram.base import TelegramAdapter, TelegramError, TelegramUpdate
from heraldesk.telegram.formatting import (
    help_text,
    mask_chat_id,
    mask_sender_id,
    preview,
    queue_line,
    queue_reply,
)

MAX_STORED_TEXT = 4096
MAX_AUDIT_TEXT = 500
KNOWN_COMMANDS: Final = frozenset({"approve", "reject", "queue", "help", "start"})


@dataclass(frozen=True, slots=True)
class InboxOutcome:
    """Result of handling one inbound message."""

    message_id: int | None
    command: str | None
    status: str
    reply: str | None = None
    profile_id: int | None = None
    content_item_id: int | None = None


def parse_command(text: str) -> tuple[str | None, list[str]]:
    """Parse ``/command arg`` text, tolerating ``/command@botname``."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None, []
    head, *arguments = stripped.split()
    command = head[1:].split("@", 1)[0].strip().lower()
    if not command:
        return None, []
    return command, arguments


def _parse_item_id(value: str) -> int | None:
    cleaned = value.strip().lstrip("#")
    try:
        item_id = int(cleaned)
    except ValueError:
        return None
    return item_id if item_id > 0 else None


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hint_scope(session: Session, slug: str | None) -> ProfileScope | None:
    if slug is None:
        return None
    try:
        return resolve_scope(session, slug)
    except (ProfileNotFoundError, ProfileValidationError):
        return None


def _default_scope(session: Session) -> ProfileScope | None:
    profiles = ProfileCatalog(session).list()
    if len(profiles) == 1:
        return ProfileScope(profiles[0].id)
    return None


def _resolve_queue_scope(
    session: Session,
    *,
    profile_hint: str | None,
    arguments: list[str],
) -> tuple[ProfileScope | None, str | None]:
    requested = arguments[0].strip() if arguments else profile_hint
    if requested is not None:
        scope = _hint_scope(session, requested)
        if scope is None:
            return None, f"Profile '{requested}' is not loaded."
        return scope, None
    scope = _default_scope(session)
    if scope is not None:
        return scope, None
    slugs = ", ".join(profile.slug for profile in ProfileCatalog(session).list())
    if not slugs:
        return None, "No profiles are registered."
    return None, f"Choose a profile: /queue <profile>. Available: {slugs}"


def handle_update(
    session: Session,
    layout: paths.Layout,
    update: TelegramUpdate,
    *,
    allowed_chat_ids: Sequence[int],
    allowed_user_ids: Sequence[int] = (),
    adapter: TelegramAdapter | None = None,
    profile_hint: str | None = None,
    redactor: SecretRegistry | None = None,
    now: datetime | None = None,
) -> InboxOutcome:
    """Store and execute one inbound Telegram message.

    Non-allowlisted chats are ignored without storing their text or replying. Unknown
    commands get the help text and never raise.
    """
    if update.chat_id not in allowed_chat_ids:
        return InboxOutcome(
            message_id=None,
            command=None,
            status="ignored",
            reply=None,
        )
    if allowed_user_ids and (update.sender_id is None or update.sender_id not in allowed_user_ids):
        return InboxOutcome(
            message_id=None,
            command=None,
            status="ignored",
            reply=None,
        )

    if update.update_id is not None and TelegramUpdateLocator(session).exists(update.update_id):
        return InboxOutcome(
            message_id=None,
            command=None,
            status="duplicate",
            reply=None,
        )

    current = datetime.now(UTC) if now is None else now
    redaction = redactor or SecretRegistry.from_environment()
    text = redaction.redact(update.text or "").strip()[:MAX_STORED_TEXT]
    command, arguments = parse_command(text)
    scope = _hint_scope(session, profile_hint)
    content_item_id: int | None = None
    status = "stored"
    reply: str | None = None

    if command in {"approve", "reject"}:
        if not arguments:
            status = "error"
            reply = f"Usage: /{command} <id>"
        else:
            item_id = _parse_item_id(arguments[0])
            if item_id is None:
                status = "error"
                reply = f"Usage: /{command} <id>"
            else:
                content_item_id = item_id
                item_scope = scope_for_content_item(session, item_id)
                if item_scope is None:
                    status = "error"
                    reply = f"Content item {item_id} does not exist."
                else:
                    scope = item_scope
                    try:
                        if command == "approve":
                            outcome = approve(
                                session,
                                layout,
                                scope,
                                item_id,
                                actor="user",
                                channel="telegram",
                                now=current,
                            )
                            status = "ok"
                            reply = (
                                f"Approved content #{outcome.content_item_id}: "
                                f"{outcome.from_state} -> {outcome.to_state}."
                            )
                        else:
                            reason = " ".join(arguments[1:]).strip() or None
                            outcome = reject(
                                session,
                                scope,
                                item_id,
                                reason=reason,
                                actor="user",
                                channel="telegram",
                                now=current,
                            )
                            status = "ok"
                            reply = (
                                f"Rejected content #{outcome.content_item_id}: "
                                f"{outcome.from_state} -> {outcome.to_state}."
                            )
                    except ApprovalError as exc:
                        status = "error"
                        reply = redaction.redact(str(exc))[:MAX_AUDIT_TEXT]
    elif command == "queue":
        scope, error = _resolve_queue_scope(
            session,
            profile_hint=profile_hint,
            arguments=arguments,
        )
        if error is not None:
            status = "error"
            reply = error
        elif scope is not None:
            expire_pending_approvals(session, scope, now=current)
            entries = list_queue(session, scope, limit=20)
            reply = queue_reply(
                [
                    queue_line(
                        item_id=entry.item_id,
                        platform=entry.platform,
                        state=entry.item_state,
                        risk_level=entry.risk_level,
                        body=entry.body,
                    )
                    for entry in entries
                ]
            )
            status = "ok"
    elif command is not None:
        status = "help"
        reply = help_text()
    else:
        status = "stored"

    redacted_reply = None if reply is None else redaction.redact(reply)[:MAX_STORED_TEXT]
    stored = TelegramMessageRepository(session).add(
        scope,
        chat_id=update.chat_id,
        sender_id=update.sender_id,
        text=text,
        command=command,
        content_item_id=content_item_id,
        response=redacted_reply,
        created_at=current,
        update_id=update.update_id,
    )

    AuditService(session).record(
        scope,
        actor="user",
        action="telegram.received",
        entity="telegram_message",
        entity_id=stored.id,
        payload={
            "message_id": stored.id,
            "chat_id": mask_chat_id(update.chat_id),
            "sender_id": mask_sender_id(update.sender_id),
            "command": command,
            "content_item_id": content_item_id,
            "text_sha256": _text_hash(text),
            "text_length": len(text),
        },
    )
    if command is not None:
        AuditService(session).record(
            scope,
            actor="user",
            action="telegram.command",
            entity="telegram_message",
            entity_id=stored.id,
            payload={
                "message_id": stored.id,
                "command": command,
                "status": status,
                "content_item_id": content_item_id,
            },
        )

    if adapter is not None and redacted_reply is not None:
        with contextlib.suppress(TelegramError):
            adapter.send_message(update.chat_id, redacted_reply)

    return InboxOutcome(
        message_id=stored.id,
        command=command,
        status=status,
        reply=redacted_reply,
        profile_id=None if scope is None else scope.profile_id,
        content_item_id=content_item_id,
    )


def process_polling(
    session: Session,
    layout: paths.Layout,
    adapter: TelegramAdapter,
    *,
    allowed_chat_ids: Sequence[int],
    allowed_user_ids: Sequence[int] = (),
    profile_hint: str | None = None,
    redactor: SecretRegistry | None = None,
    now: datetime | None = None,
    offset: int | None = None,
) -> list[InboxOutcome]:
    """Poll once and handle every update without tracking an offset.

    The server uses :class:`heraldesk.telegram.poller.TelegramPoller`, which advances the
    Bot API offset itself. This helper remains for direct, single-shot callers and tests.
    """
    outcomes: list[InboxOutcome] = []
    for update in adapter.poll_updates(offset):
        outcomes.append(
            handle_update(
                session,
                layout,
                update,
                allowed_chat_ids=allowed_chat_ids,
                allowed_user_ids=allowed_user_ids,
                adapter=adapter,
                profile_hint=profile_hint,
                redactor=redactor,
                now=now,
            )
        )
    return outcomes


def queue_summary(session: Session, scope: ProfileScope, *, limit: int = 5) -> str:
    """Return a short redacted queue summary for CLI/tests."""
    entries = list_queue(session, scope, limit=limit)
    return queue_reply(
        [
            queue_line(
                item_id=entry.item_id,
                platform=entry.platform,
                state=entry.item_state,
                risk_level=entry.risk_level,
                body=entry.body,
            )
            for entry in entries
        ]
    )


def redacted_preview(text: str, *, redactor: SecretRegistry | None = None) -> str:
    """Return a redacted single-line preview."""
    registry = redactor or SecretRegistry.from_environment()
    return preview(registry.redact(text))
