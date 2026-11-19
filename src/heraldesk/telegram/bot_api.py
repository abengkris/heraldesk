"""Minimal Telegram Bot API adapter.

M6 deliberately avoids adding ``aiogram`` or ``python-telegram-bot``. Only
``sendMessage`` and the ``getUpdates`` polling call are implemented with the standard
library. ``heraldesk serve`` drives polling through
:class:`heraldesk.telegram.poller.TelegramPoller`.

The token is held in memory only. It never appears in a repr, log line, exception or
audit entry, and API failures are reported without the request URL.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from heraldesk.telegram.base import (
    MAX_MESSAGE_LENGTH,
    TelegramError,
    TelegramSendResult,
    TelegramUpdate,
    updates_from_get_updates,
)

DEFAULT_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10.0


class BotApiTelegramAdapter:
    """Send-only adapter with a small polling stub for the future server loop."""

    name = "telegram"

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not token.strip():
            raise TelegramError("telegram bot token must not be empty")
        if timeout_seconds <= 0:
            raise TelegramError("telegram timeout must be positive")
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def __repr__(self) -> str:
        """Never expose the token."""
        return "BotApiTelegramAdapter(<redacted>)"

    def _call(self, method: str, fields: dict[str, str]) -> dict[str, Any]:
        url = f"{self._base_url}/bot{self._token}/{method}"
        data = urlencode(fields).encode("utf-8")
        request = Request(url, data=data)
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise TelegramError(f"telegram API call {method} failed") from exc
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TelegramError(f"telegram API call {method} returned invalid JSON") from exc
        if not isinstance(parsed, dict):
            raise TelegramError(f"telegram API call {method} returned an unexpected payload")
        if not parsed.get("ok"):
            raise TelegramError(f"telegram API rejected {method}")
        return parsed

    def get_me(self) -> str | None:
        """Return the bot username from ``getMe``, or ``None`` when it has none."""
        parsed = self._call("getMe", {})
        result = parsed.get("result")
        if not isinstance(result, dict):
            return None
        username = result.get("username")
        return username if isinstance(username, str) and username else None

    def send_message(self, chat_id: int, text: str) -> TelegramSendResult:
        """Send one plain-text message through ``sendMessage``."""
        body = text.strip()
        if not body:
            raise TelegramError("telegram message must not be empty")
        parsed = self._call(
            "sendMessage",
            {"chat_id": str(chat_id), "text": body[:MAX_MESSAGE_LENGTH]},
        )
        result = parsed.get("result")
        message_id = 0
        if isinstance(result, dict):
            raw_id = result.get("message_id")
            if isinstance(raw_id, int):
                message_id = raw_id
        return TelegramSendResult(chat_id=chat_id, message_id=message_id)

    def poll_updates(self, offset: int | None = None) -> list[TelegramUpdate]:
        """Fetch the next batch of updates, honouring the Bot API offset."""
        fields = {"timeout": "0"}
        if offset is not None:
            fields["offset"] = str(offset)
        return updates_from_get_updates(self._call("getUpdates", fields))
