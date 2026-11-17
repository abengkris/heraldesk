"""Manual inbox triage for the M8 CLI.

This module deliberately does not generate replies, classify sentiment or call a social
platform API. A human reads one stored inbound message and either sends a reply through
the existing Telegram adapter or ignores it with a reason. Every action writes an audit
entry and every stored or printed text is redacted first.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.config import ConfigError, load_config
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import TelegramMessage
from heraldesk.db.repositories import InboxRepository, ProfileCatalog
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.errors import DomainError
from heraldesk.telegram.base import TelegramAdapter, TelegramError
from heraldesk.telegram.bot_api import BotApiTelegramAdapter
from heraldesk.telegram.credentials import TelegramConfigError, resolve_bot_token
from heraldesk.telegram.formatting import mask_chat_id, mask_sender_id

MAX_REPLY_TEXT = 4096
MAX_AUDIT_TEXT = 500


class InboxError(DomainError):
    """An inbox triage action cannot be completed."""


class InboxItemNotFoundError(InboxError):
    """The requested inbox item does not exist."""


class InboxStateError(InboxError):
    """The inbox item is already handled or ignored."""


class InboxStatus(StrEnum):
    """Manual triage state stored on ``telegram_messages.status``."""

    PENDING = "pending"
    HANDLED = "handled"
    IGNORED = "ignored"


class IgnoreReason(StrEnum):
    """Bounded reasons a human can use when ignoring an inbox item."""

    SPAM = "spam"
    OFF_TOPIC = "off-topic"
    OTHER = "other"


DEFAULT_LIST_LIMIT: Final = 20


@dataclass(frozen=True, slots=True)
class InboxItemView:
    """A detached, safe snapshot of one inbox row."""

    id: int
    profile_id: int | None
    profile_slug: str | None
    chat_id: int
    sender_id: int | None
    text: str
    command: str | None
    content_item_id: int | None
    response: str | None
    status: str
    ignore_reason: str | None
    created_at: datetime
    triaged_at: datetime | None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _profile_slugs(session: Session) -> dict[int, str]:
    return {profile.id: profile.slug for profile in ProfileCatalog(session).list()}


def _view(message: TelegramMessage, slugs: dict[int, str]) -> InboxItemView:
    profile_id = message.profile_id
    return InboxItemView(
        id=message.id,
        profile_id=profile_id,
        profile_slug=None if profile_id is None else slugs.get(profile_id),
        chat_id=message.chat_id,
        sender_id=message.sender_id,
        text=message.text,
        command=message.command,
        content_item_id=message.content_item_id,
        response=message.response,
        status=message.status,
        ignore_reason=message.ignore_reason,
        created_at=message.created_at,
        triaged_at=message.triaged_at,
    )


def _scope(message: TelegramMessage) -> ProfileScope | None:
    if message.profile_id is None:
        return None
    return ProfileScope(message.profile_id)


def _require_message(session: Session, item_id: int) -> TelegramMessage:
    message = InboxRepository(session).get(item_id)
    if message is None:
        raise InboxItemNotFoundError(f"inbox item {item_id} does not exist")
    return message


def _require_pending(message: TelegramMessage) -> None:
    if message.status == InboxStatus.HANDLED.value:
        raise InboxStateError(f"inbox item {message.id} is already handled")
    if message.status == InboxStatus.IGNORED.value:
        raise InboxStateError(f"inbox item {message.id} is already ignored")


def resolve_reply_transport(layout: paths.Layout) -> tuple[TelegramAdapter, SecretRegistry]:
    """Build the configured Telegram adapter and a redactor that knows its token."""
    try:
        config = load_config(layout.home)
    except ConfigError as exc:
        raise InboxError(str(exc)) from exc
    if not config.telegram.enabled:
        raise InboxError(
            "telegram is disabled; enable telegram.enabled and configure a bot token "
            "before replying from the inbox"
        )
    try:
        token = resolve_bot_token(layout)
    except TelegramConfigError as exc:
        raise InboxError(str(exc)) from exc
    if token is None:
        raise InboxError(
            "no telegram bot token is configured; run 'heraldesk telegram token set' first"
        )
    return (
        BotApiTelegramAdapter(token),
        SecretRegistry.from_environment(extra_values=[token]),
    )


def list_items(
    session: Session,
    *,
    status: InboxStatus | str = InboxStatus.PENDING,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[InboxItemView]:
    """Return newest inbox items for one status across every profile."""
    selected = status if isinstance(status, InboxStatus) else InboxStatus(status)
    if limit < 1:
        raise InboxError("limit must be at least 1")
    messages = InboxRepository(session).list(status=selected.value, limit=limit)
    slugs = _profile_slugs(session)
    views = [_view(message, slugs) for message in messages]
    AuditService(session).record(
        None,
        actor="user",
        action="inbox.listed",
        entity="inbox",
        payload={"status": selected.value, "limit": limit, "count": len(views)},
    )
    return views


def show_item(session: Session, item_id: int) -> InboxItemView:
    """Return one inbox item and record that it was viewed."""
    message = _require_message(session, item_id)
    AuditService(session).record(
        _scope(message),
        actor="user",
        action="inbox.shown",
        entity="inbox_item",
        entity_id=message.id,
        payload={
            "message_id": message.id,
            "status": message.status,
            "command": message.command,
            "content_item_id": message.content_item_id,
            "chat_id": mask_chat_id(message.chat_id),
            "sender_id": mask_sender_id(message.sender_id),
        },
    )
    return _view(message, _profile_slugs(session))


def reply_item(
    session: Session,
    layout: paths.Layout,
    item_id: int,
    text: str,
    *,
    adapter: TelegramAdapter | None = None,
    redactor: SecretRegistry | None = None,
    now: datetime | None = None,
) -> InboxItemView:
    """Send one manual reply, then mark the inbox item handled.

    The adapter is resolved from configuration when *adapter* is omitted. Tests inject
    :class:`~heraldesk.telegram.fake.FakeTelegramAdapter` and never touch the network.
    A failed send leaves the item pending.
    """
    if not text.strip():
        raise InboxError("reply text must not be empty")

    message = _require_message(session, item_id)
    _require_pending(message)

    default_redactor: SecretRegistry
    if adapter is None:
        adapter, default_redactor = resolve_reply_transport(layout)
    else:
        default_redactor = SecretRegistry.from_environment()
    redaction = redactor or default_redactor
    body = redaction.redact(text).strip()[:MAX_REPLY_TEXT]
    if not body:
        raise InboxError("reply text must not be empty")

    try:
        adapter.send_message(message.chat_id, body)
    except TelegramError as exc:
        raise InboxError(redaction.redact(str(exc))[:MAX_AUDIT_TEXT]) from exc

    current = datetime.now(UTC) if now is None else now
    updated = InboxRepository(session).mark_handled(
        item_id,
        response=body,
        triaged_at=current,
    )
    if updated is None:
        raise InboxItemNotFoundError(f"inbox item {item_id} does not exist")

    AuditService(session).record(
        _scope(updated),
        actor="user",
        action="inbox.replied",
        entity="inbox_item",
        entity_id=updated.id,
        payload={
            "message_id": updated.id,
            "chat_id": mask_chat_id(updated.chat_id),
            "sender_id": mask_sender_id(updated.sender_id),
            "text_sha256": _hash(body),
            "text_length": len(body),
            "adapter": adapter.name,
        },
    )
    return _view(updated, _profile_slugs(session))


def ignore_item(
    session: Session,
    item_id: int,
    *,
    reason: IgnoreReason | str = IgnoreReason.OTHER,
    now: datetime | None = None,
) -> InboxItemView:
    """Mark one inbox item ignored with a bounded reason."""
    selected = reason if isinstance(reason, IgnoreReason) else IgnoreReason(reason)
    message = _require_message(session, item_id)
    _require_pending(message)

    current = datetime.now(UTC) if now is None else now
    updated = InboxRepository(session).mark_ignored(
        item_id,
        reason=selected.value,
        triaged_at=current,
    )
    if updated is None:
        raise InboxItemNotFoundError(f"inbox item {item_id} does not exist")

    AuditService(session).record(
        _scope(updated),
        actor="user",
        action="inbox.ignored",
        entity="inbox_item",
        entity_id=updated.id,
        payload={
            "message_id": updated.id,
            "reason": selected.value,
            "previous_status": InboxStatus.PENDING.value,
            "chat_id": mask_chat_id(updated.chat_id),
            "sender_id": mask_sender_id(updated.sender_id),
        },
    )
    return _view(updated, _profile_slugs(session))
