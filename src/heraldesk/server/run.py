"""Start ``heraldesk serve`` with Uvicorn.

Uvicorn owns the SIGINT/SIGTERM handling. On a signal it stops accepting requests,
runs the FastAPI lifespan shutdown (which stops APScheduler and writes ``serve.stopped``),
and exits. Binding outside loopback is refused until admin authentication exists.
"""

from __future__ import annotations

import uvicorn

from heraldesk import paths
from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import is_loopback, load_config
from heraldesk.telegram.credentials import TelegramConfigError


class ServerError(Exception):
    """Raised when the server cannot start with the requested settings."""


def run_server(
    *,
    host: str | None = None,
    port: int | None = None,
    enable_telegram: bool = True,
) -> None:
    """Run the API, scheduler and Telegram bot in one process."""
    layout = paths.layout_for(paths.resolve_home())
    config = load_config(layout.home)
    resolved_host = host if host is not None else config.server.host
    resolved_port = port if port is not None else config.server.port
    if not is_loopback(resolved_host):
        raise ServerError(
            f"refusing to bind {resolved_host}: Heraldesk serves loopback only until "
            "admin authentication lands; use 127.0.0.1 or a local reverse proxy"
        )
    if not layout.db_path.is_file():
        raise ServerError(f"database {layout.db_path} does not exist; run 'heraldesk init' first")
    try:
        app = create_app(
            AppOptions(
                layout=layout,
                config=config,
                enable_telegram=enable_telegram,
            )
        )
    except TelegramConfigError as exc:
        raise ServerError(str(exc)) from exc
    uvicorn.run(app, host=resolved_host, port=resolved_port, log_level="info")
