"""Objects shared by the API routes, UI routes and the lifespan manager."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from heraldesk import paths
from heraldesk.config import Config
from heraldesk.core.redact import SecretRegistry
from heraldesk.platforms.base import PlatformAdapter
from heraldesk.server.background import BackgroundJobs
from heraldesk.server.database import ServerDatabase
from heraldesk.telegram.base import TelegramAdapter
from heraldesk.web.auth import AdminAuth

AdapterFactory = Callable[[str], PlatformAdapter]


@dataclass(slots=True)
class WebContext:
    """Everything a request needs. Created once per app for tests and ``serve``."""

    layout: paths.Layout
    config: Config
    database: ServerDatabase
    jobs: BackgroundJobs
    adapter: TelegramAdapter | None
    redactor: SecretRegistry
    allowed_chat_ids: tuple[int, ...]
    allowed_user_ids: tuple[int, ...]
    telegram_active: bool
    auth: AdminAuth
    adapter_factory: AdapterFactory | None = None
    webhook_secret: str | None = None
    web_enabled: bool = True
