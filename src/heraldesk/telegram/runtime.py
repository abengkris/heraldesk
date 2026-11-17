"""Build Telegram adapters and notification services from configuration.

Real notifications are enabled only when ``telegram.enabled`` is true and a token is
available. A missing token returns ``None`` rather than silently sending to the fake
adapter; ``heraldesk telegram test`` is the explicit offline check.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.config import Config, ConfigError, load_config
from heraldesk.core.redact import SecretRegistry
from heraldesk.telegram.base import TelegramAdapter, TelegramError
from heraldesk.telegram.bot_api import BotApiTelegramAdapter
from heraldesk.telegram.credentials import resolve_bot_token
from heraldesk.telegram.fake import FakeTelegramAdapter
from heraldesk.telegram.notifier import NotificationService


def build_notification_service(
    session: Session,
    layout: paths.Layout,
    config: Config,
) -> NotificationService | None:
    """Return a real notification service, or ``None`` when Telegram is not configured."""
    if not config.telegram.enabled:
        return None
    token = resolve_bot_token(layout)
    if token is None:
        return None
    adapter: TelegramAdapter = BotApiTelegramAdapter(token)
    redactor = SecretRegistry.from_environment(extra_values=[token])
    return NotificationService(
        session,
        layout,
        adapter,
        allowed_chat_ids=tuple(config.telegram.allowed_chat_ids),
        redactor=redactor,
    )


def notifier_for(session: Session, layout: paths.Layout) -> NotificationService | None:
    """Load config and build a notification service, tolerating absent configuration."""
    try:
        config = load_config(layout.home)
        return build_notification_service(session, layout, config)
    except (ConfigError, TelegramError, OSError):
        return None


def offline_adapter() -> FakeTelegramAdapter:
    """Return the fake adapter used by ``telegram test`` and ``telegram send``."""
    return FakeTelegramAdapter()
