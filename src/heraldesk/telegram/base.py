"""Telegram adapter contract for outbound notifications and the inbound stub.

M6 only needs sending plus a polling/webhook stub. Tests use
:class:`heraldesk.telegram.fake.FakeTelegramAdapter` and never touch the network.
The real Bot API adapter lives in :mod:`heraldesk.telegram.bot_api`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

MAX_MESSAGE_LENGTH = 4096


class TelegramError(Exception):
    """Raised when Telegram configuration or an API call fails.

    Messages never contain a bot token or a full API URL. Callers redact them again
    before storing or printing anything.
    """


@dataclass(frozen=True, slots=True)
class TelegramUpdate:
    """One inbound Telegram message, already reduced to the fields Heraldesk needs."""

    chat_id: int
    text: str
    sender_id: int | None = None
    message_id: int | None = None
    update_id: int | None = None
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TelegramSendResult:
    """What Telegram returned after accepting a message."""

    chat_id: int
    message_id: int


class TelegramAdapter(Protocol):
    """The subset of the Telegram contract M6 uses."""

    name: str

    def send_message(self, chat_id: int, text: str) -> TelegramSendResult:
        """Send one plain-text message and return its platform id."""
        ...

    def poll_updates(self, offset: int | None = None) -> list[TelegramUpdate]:
        """Return updates newer than *offset*. The live loop arrives in M7."""
        ...


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _update_from_message(payload: Mapping[str, Any]) -> TelegramUpdate | None:
    message = payload.get("message")
    if not isinstance(message, Mapping):
        message = payload.get("edited_message")
    if not isinstance(message, Mapping):
        return None
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return None
    chat_id = _as_int(chat.get("id"))
    if chat_id is None:
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    sender = message.get("from")
    sender_id = _as_int(sender.get("id")) if isinstance(sender, Mapping) else None
    return TelegramUpdate(
        chat_id=chat_id,
        text=text,
        sender_id=sender_id,
        message_id=_as_int(message.get("message_id")),
        update_id=_as_int(payload.get("update_id")),
    )


def parse_webhook_update(payload: Mapping[str, Any]) -> TelegramUpdate | None:
    """Parse one Bot API webhook payload, or return ``None`` when it is not a message."""
    return _update_from_message(payload)


def updates_from_get_updates(payload: Mapping[str, Any]) -> list[TelegramUpdate]:
    """Parse a ``getUpdates`` response into update objects."""
    raw = payload.get("result")
    if not isinstance(raw, list):
        return []
    updates: list[TelegramUpdate] = []
    for item in raw:
        if isinstance(item, Mapping):
            update = _update_from_message(item)
            if update is not None:
                updates.append(update)
    return updates
