"""Offline Telegram adapter for tests and ``heraldesk telegram test``.

It performs no network I/O. ``fail_times`` makes notification-failure tests
deterministic, and ``enqueue`` feeds the inbound polling/webhook stub.
"""

from __future__ import annotations

from heraldesk.telegram.base import (
    TelegramError,
    TelegramSendResult,
    TelegramUpdate,
)


class FakeTelegramAdapter:
    """Deterministic in-process Telegram adapter."""

    name = "fake"

    def __init__(self, *, fail_times: int = 0, error: str = "fake telegram unavailable") -> None:
        if fail_times < 0:
            raise ValueError("fail_times must not be negative")
        self._fail_times = fail_times
        self._error = error
        self.send_calls = 0
        self.sent: list[tuple[int, str]] = []
        self.incoming: list[TelegramUpdate] = []

    def send_message(self, chat_id: int, text: str) -> TelegramSendResult:
        """Record one outbound message, or fail on purpose."""
        self.send_calls += 1
        if self.send_calls <= self._fail_times:
            raise TelegramError(self._error)
        self.sent.append((chat_id, text))
        return TelegramSendResult(chat_id=chat_id, message_id=len(self.sent))

    def poll_updates(self, offset: int | None = None) -> list[TelegramUpdate]:
        """Return queued updates, honouring the Bot API offset convention."""
        if offset is None:
            pending = list(self.incoming)
            self.incoming.clear()
            return pending
        pending = [
            update
            for update in self.incoming
            if update.update_id is None or update.update_id >= offset
        ]
        self.incoming = [
            update
            for update in self.incoming
            if update.update_id is not None and update.update_id < offset
        ]
        return pending

    def enqueue(
        self,
        text: str,
        chat_id: int,
        *,
        sender_id: int | None = None,
        update_id: int | None = None,
    ) -> TelegramUpdate:
        """Queue one inbound message for ``poll_updates`` or a webhook test."""
        update = TelegramUpdate(
            chat_id=chat_id,
            text=text,
            sender_id=sender_id,
            update_id=update_id,
        )
        self.incoming.append(update)
        return update
