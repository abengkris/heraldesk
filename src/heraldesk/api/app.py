"""FastAPI application for ``heraldesk serve``.

M11 adds the authenticated Web UI and ``/api/v1`` JSON API on top of the M7 operational
endpoints. ``/health`` and the Telegram platform webhook remain public; every user-facing
route requires either a Bearer admin token or a signed UI session cookie.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from heraldesk import __version__, paths
from heraldesk.config import Config
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.engine import inspect_database
from heraldesk.db.migrations import MigrationError, head_revision
from heraldesk.server.background import (
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SCHEDULER_INTERVAL_SECONDS,
    BackgroundJobs,
)
from heraldesk.server.database import ServerDatabase
from heraldesk.telegram.base import TelegramAdapter, parse_webhook_update
from heraldesk.telegram.bot_api import BotApiTelegramAdapter
from heraldesk.telegram.credentials import resolve_bot_token
from heraldesk.telegram.inbox import InboxOutcome, handle_update
from heraldesk.web import ui
from heraldesk.web.api import router as api_router
from heraldesk.web.auth import (
    SESSION_COOKIE,
    AdminAuth,
    AuthConfigError,
    AuthError,
    Unauthorized,
    resolve_admin_token,
    resolve_webhook_secret,
    token_in_query,
)
from heraldesk.web.context import AdapterFactory, WebContext
from heraldesk.web.errors import WebError, json_error
from heraldesk.web.service import build_status

ACCEPTED_INBOX_STATUSES = frozenset({"ok", "stored", "help"})
_STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "static"

logger = logging.getLogger("heraldesk.web")


@dataclass(frozen=True, slots=True)
class AppOptions:
    """Everything the server needs, kept injectable for tests."""

    layout: paths.Layout
    config: Config
    enable_telegram: bool = True
    telegram_adapter: TelegramAdapter | None = None
    telegram_token: str | None = None
    scheduler_interval_seconds: float = DEFAULT_SCHEDULER_INTERVAL_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    admin_token: str | None = None
    cookie_secure: bool = False
    trust_proxy: bool = False
    enable_web: bool = True
    adapter_factory: AdapterFactory | None = None
    telegram_webhook_secret: str | None = None


def _resolve_adapter(options: AppOptions) -> tuple[TelegramAdapter | None, str | None, bool]:
    if not options.enable_telegram or not options.config.telegram.enabled:
        return None, options.telegram_token, False
    adapter = options.telegram_adapter
    token = options.telegram_token
    if adapter is None:
        if token is None:
            token = resolve_bot_token(options.layout)
        if token is not None:
            adapter = BotApiTelegramAdapter(token)
    return adapter, token, adapter is not None


def _inbox_response(outcome: InboxOutcome) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ok": outcome.status in ACCEPTED_INBOX_STATUSES,
        "status": outcome.status,
        "command": outcome.command,
        "content_item_id": outcome.content_item_id,
    }
    if outcome.reply is not None:
        response["reply"] = outcome.reply
    return response


def _context(request: Request) -> WebContext:
    context: WebContext = request.app.state.context
    return context


def _auth(request: Request) -> AdminAuth:
    auth: AdminAuth = request.app.state.auth
    return auth


def _wants_json(request: Request) -> bool:
    path = request.url.path
    return path.startswith("/api/") or path == "/status"


def _webhook_secret(options: AppOptions) -> str | None:
    if options.telegram_webhook_secret is not None:
        return options.telegram_webhook_secret.strip() or None
    return resolve_webhook_secret(options.layout)


def require_any_auth(request: Request) -> None:
    """Accept a Bearer token or a valid UI session for data-bearing JSON endpoints."""
    auth = _auth(request)
    if auth.verify_bearer(request.headers.get("authorization")):
        return
    if auth.read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return
    raise Unauthorized("a valid admin token or session is required")


def _error_page(request: Request, exc: WebError) -> HTMLResponse:
    auth = _auth(request)
    session = auth.read_session(request.cookies.get(SESSION_COOKIE))
    context: dict[str, Any] = {
        "request": request,
        "session": session,
        "csrf": "" if session is None else session.csrf,
        "active": "",
        "version": __version__,
        "code": exc.code,
        "message": exc.message,
    }
    return ui.templates.TemplateResponse(
        request=request,
        name="error.html",
        context=context,
        status_code=exc.status_code,
    )


def create_app(options: AppOptions) -> FastAPI:
    """Build the FastAPI app, API, UI and background jobs."""
    adapter, token, telegram_active = _resolve_adapter(options)
    admin_token = options.admin_token
    if admin_token is None:
        try:
            admin_token = resolve_admin_token(options.layout).token
        except AuthConfigError as exc:
            raise AuthConfigError(str(exc)) from exc
    auth = AdminAuth(
        token=admin_token,
        cookie_secure=options.cookie_secure,
        trust_proxy=options.trust_proxy,
    )
    redactor = SecretRegistry.from_environment(
        extra_values=[
            value for value in (token, admin_token) if value is not None and value.strip()
        ]
    )
    webhook_secret = _webhook_secret(options)
    if (
        options.config.telegram.enabled
        and (options.config.telegram.mode == "webhook" or options.trust_proxy)
        and webhook_secret is None
    ):
        raise AuthConfigError(
            "Telegram webhook mode and proxy mode require HERALDESK_TELEGRAM_WEBHOOK_SECRET "
            "so forged /approve commands are rejected"
        )
    database = ServerDatabase(options.layout)
    jobs = BackgroundJobs(
        database,
        options.layout,
        options.config,
        adapter=adapter,
        redactor=redactor,
        scheduler_interval_seconds=options.scheduler_interval_seconds,
        poll_interval_seconds=options.poll_interval_seconds,
    )
    context = WebContext(
        layout=options.layout,
        config=options.config,
        database=database,
        jobs=jobs,
        adapter=adapter,
        redactor=redactor,
        allowed_chat_ids=tuple(options.config.telegram.allowed_chat_ids),
        allowed_user_ids=tuple(options.config.telegram.allowed_user_ids),
        telegram_active=telegram_active,
        auth=auth,
        adapter_factory=options.adapter_factory,
        webhook_secret=webhook_secret,
        web_enabled=options.enable_web,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx: WebContext = app.state.context
        started = False
        try:
            with ctx.database.session() as session:
                AuditService(session).record(
                    None,
                    actor="system",
                    action="serve.started",
                    entity="server",
                    payload={
                        "telegram": ctx.telegram_active,
                        "telegram_mode": ctx.config.telegram.mode,
                        "scheduler_interval_seconds": options.scheduler_interval_seconds,
                        "poll_interval_seconds": options.poll_interval_seconds,
                        "web": ctx.web_enabled,
                    },
                )
            started = True
            ctx.jobs.start()
            yield
        finally:
            ctx.jobs.stop()
            if started:
                with ctx.database.session() as session:
                    AuditService(session).record(
                        None,
                        actor="system",
                        action="serve.stopped",
                        entity="server",
                        payload={"reason": "shutdown"},
                    )
            ctx.database.close()

    app = FastAPI(
        title="Heraldesk",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.auth = auth

    @app.middleware("http")
    async def guard_requests(request: Request, call_next: Any) -> Response:
        if token_in_query(request.url.query):
            logger.warning("admin token supplied in the URL query; request rejected")
            return json_error(400, "token_in_query", "tokens are not accepted in URLs")
        if request.url.path.startswith("/api/v1") and not auth.verify_bearer(
            request.headers.get("authorization")
        ):
            return json_error(401, "unauthorized", "a valid Bearer admin token is required")
        response: Response = await call_next(request)
        if auth.cookie_secure:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'self'; form-action 'self'",
        )
        return response

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> Response:
        if exc.status_code == 401 and not _wants_json(request):
            return RedirectResponse("/login", status_code=303)
        if _wants_json(request):
            return json_error(exc.status_code, exc.code, exc.message)
        return _error_page(request, WebError(exc.status_code, exc.code, exc.message))

    @app.exception_handler(WebError)
    async def web_error_handler(request: Request, exc: WebError) -> Response:
        if exc.status_code == 401 and not _wants_json(request):
            return RedirectResponse("/login", status_code=303)
        if _wants_json(request):
            return json_error(exc.status_code, exc.code, exc.message)
        return _error_page(request, exc)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
        _ = exc
        message = "invalid request"
        if _wants_json(request):
            return json_error(422, "validation_error", message)
        return _error_page(request, WebError(422, "validation_error", message))

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
        if _wants_json(request):
            return json_error(exc.status_code, "http_error", "request failed")
        if exc.status_code == 404:
            return _error_page(request, WebError(404, "not_found", "page not found"))
        return _error_page(request, WebError(exc.status_code, "http_error", "request failed"))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
        logger.error("request failed (%s)", type(exc).__name__)
        message = "internal server error"
        if _wants_json(request):
            return json_error(500, "internal_error", message)
        return _error_page(request, WebError(500, "internal_error", message))

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        """Public liveness: process, version and database health only."""
        ctx = _context(request)
        reachable = ctx.database.is_healthy()
        revision: str | None = None
        if reachable:
            revision = inspect_database(ctx.layout.db_path).revision
        try:
            expected = head_revision(ctx.layout.db_path)
        except MigrationError:
            expected = None
        schema_ok = expected is not None and revision == expected
        healthy = reachable and schema_ok
        body: dict[str, Any] = {
            "status": "ok" if healthy else "error",
            "version": __version__,
            "db": {
                "ok": healthy,
                "revision": revision,
                "expected_revision": expected,
            },
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    @app.get("/status")
    def status(request: Request) -> dict[str, Any]:
        """Return profile metadata, queue counts and runtime state. Auth required."""
        require_any_auth(request)
        ctx = _context(request)
        reachable = ctx.database.is_healthy()
        revision = inspect_database(ctx.layout.db_path).revision if reachable else None
        with ctx.database.session() as session:
            return build_status(
                session,
                db_ok=reachable and revision is not None,
                revision=revision,
                telegram_active=ctx.telegram_active,
                telegram_configured=ctx.config.telegram.enabled,
                telegram_mode=ctx.config.telegram.mode,
                allowed_chats=len(ctx.allowed_chat_ids),
            )

    @app.post("/webhook/telegram")
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        """Handle one Bot API update. Always answers 200, never echoes chat ids.

        This is the explicit platform-callback exception to the admin-auth rule. It is
        only active when Telegram is enabled and still enforces the chat allowlist. When
        ``HERALDESK_TELEGRAM_WEBHOOK_SECRET`` is set, the Bot API secret header is required.
        """
        ctx = _context(request)
        if ctx.webhook_secret is None:
            if ctx.config.telegram.mode == "webhook" or ctx.auth.trust_proxy:
                return {"ok": False, "status": "unauthorized"}
        elif not ctx.auth.verify_webhook_secret(
            request.headers.get("x-telegram-bot-api-secret-token"),
            ctx.webhook_secret,
        ):
            return {"ok": False, "status": "unauthorized"}
        if not ctx.telegram_active:
            return {"ok": False, "status": "ignored"}
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return {"ok": False, "status": "ignored"}
        if not isinstance(payload, Mapping):
            return {"ok": False, "status": "ignored"}
        update = parse_webhook_update(payload)
        if update is None:
            return {"ok": True, "status": "ignored"}
        with ctx.database.session() as session:
            outcome = handle_update(
                session,
                ctx.layout,
                update,
                allowed_chat_ids=ctx.allowed_chat_ids,
                allowed_user_ids=ctx.allowed_user_ids,
                adapter=ctx.adapter,
                profile_hint=None,
                redactor=ctx.redactor,
            )
        return _inbox_response(outcome)

    app.include_router(api_router)
    if options.enable_web:
        app.include_router(ui.router)
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app
