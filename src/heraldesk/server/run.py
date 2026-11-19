"""Start ``heraldesk serve`` with Uvicorn.

Uvicorn owns the signal handling. Binding outside loopback requires ``--allow-remote``,
an explicit ``HERALDESK_ADMIN_TOKEN`` and HTTPS termination at a trusted reverse proxy
(``HERALDESK_TRUST_PROXY=1``). The default remains loopback-only.
"""

from __future__ import annotations

import logging

import uvicorn

from heraldesk import paths
from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import is_loopback, load_config
from heraldesk.db.engine import inspect_database
from heraldesk.db.migrations import MigrationError, head_revision
from heraldesk.telegram.credentials import TelegramConfigError
from heraldesk.web.auth import (
    AuthConfigError,
    explicit_admin_token,
    resolve_admin_token,
    resolve_webhook_secret,
    trust_proxy_enabled,
)

logger = logging.getLogger("heraldesk.server")


class ServerError(Exception):
    """Raised when the server cannot start with the requested settings."""


class _QueryRedactionFilter(logging.Filter):
    """Strip query strings from uvicorn access records so tokens cannot be logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        return True


def _install_query_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _QueryRedactionFilter) for item in access.filters):
        access.addFilter(_QueryRedactionFilter())


def run_server(
    *,
    host: str | None = None,
    port: int | None = None,
    enable_telegram: bool = True,
    enable_web: bool = True,
    allow_remote: bool = False,
) -> None:
    """Run the API, Web UI, scheduler and Telegram bot in one process."""
    layout = paths.layout_for(paths.resolve_home())
    config = load_config(layout.home)
    resolved_host = host if host is not None else config.server.host
    resolved_port = port if port is not None else config.server.port
    remote = not is_loopback(resolved_host)
    trust_proxy = trust_proxy_enabled(layout)

    if remote:
        if not allow_remote:
            raise ServerError(
                f"refusing to bind {resolved_host}: Heraldesk serves loopback by default; "
                "use 127.0.0.1 behind a reverse proxy, or pass --allow-remote with HTTPS "
                "proxy hardening"
            )
        explicit = explicit_admin_token(layout)
        if explicit is None:
            raise ServerError(
                "remote mode requires an explicit HERALDESK_ADMIN_TOKEN in the environment "
                "or .env; a secret.key-derived token is not accepted for remote binds"
            )
        if len(explicit.token) < 32:
            raise ServerError(
                "remote mode requires an admin token of at least 32 characters; "
                'generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
            )
        if not trust_proxy:
            raise ServerError(
                "Remote mode requires HTTPS via reverse proxy; set HERALDESK_TRUST_PROXY=1 "
                "behind a TLS-terminating proxy"
            )
        logger.warning(
            "remote mode enabled: keep the port firewalled and terminate HTTPS at a "
            "trusted reverse proxy"
        )

    if (
        config.telegram.enabled
        and (remote or config.telegram.mode == "webhook")
        and resolve_webhook_secret(layout) is None
    ):
        raise ServerError(
            "Telegram webhook mode and remote mode require "
            "HERALDESK_TELEGRAM_WEBHOOK_SECRET so forged /approve commands are rejected"
        )

    if not layout.db_path.is_file():
        raise ServerError(f"database {layout.db_path} does not exist; run 'heraldesk init' first")
    status = inspect_database(layout.db_path)
    try:
        expected = head_revision(layout.db_path)
    except MigrationError as exc:
        raise ServerError(str(exc)) from exc
    if status.error is not None:
        raise ServerError(f"cannot read the database: {status.error}")
    if status.revision != expected:
        raise ServerError(
            f"database schema is {status.revision or 'missing'}, expected {expected}; "
            "run 'heraldesk upgrade'"
        )

    try:
        admin = resolve_admin_token(layout)
        app = create_app(
            AppOptions(
                layout=layout,
                config=config,
                enable_telegram=enable_telegram,
                enable_web=enable_web,
                admin_token=admin.token,
                cookie_secure=trust_proxy,
                trust_proxy=trust_proxy,
            )
        )
    except (TelegramConfigError, AuthConfigError) as exc:
        raise ServerError(str(exc)) from exc

    _install_query_redaction()
    uvicorn.run(app, host=resolved_host, port=resolved_port, log_level="info")
