"""FastAPI application for ``heraldesk serve``.

The server is deliberately small and loopback-first. It exposes the operational
endpoints only:

* ``GET /health`` — process, version and database status;
* ``GET /status`` — profiles, queue counts, last scheduler sweep and Telegram state;
* ``POST /webhook/telegram`` — Bot API update intake, allowlist enforced.

Responses never contain a bot token, a full chat id, or a content body. The web UI and the
``/api/v1`` namespace are deferred to the web-UI milestone; see ``docs/decisions.md``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from heraldesk import __version__, paths
from heraldesk.config import Config
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.engine import inspect_database
from heraldesk.db.repositories import (
    AuditRepository,
    ContentItemRepository,
    ProfileCatalog,
)
from heraldesk.db.scope import ProfileScope
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

QUEUE_COUNT_STATES = (
    "drafted",
    "reviewed",
    "pending_approval",
    "approved",
    "scheduled",
    "failed",
    "blocked",
    "rejected",
    "needs_review",
)
ACCEPTED_INBOX_STATUSES = frozenset({"ok", "stored", "help"})


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


@dataclass(slots=True)
class AppContext:
    """Objects shared by the routes and the lifespan manager."""

    layout: paths.Layout
    config: Config
    database: ServerDatabase
    jobs: BackgroundJobs
    adapter: TelegramAdapter | None
    redactor: SecretRegistry
    allowed_chat_ids: tuple[int, ...]
    telegram_active: bool


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


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.isoformat()


def _queue_counts(raw: Mapping[str, int]) -> dict[str, int]:
    counts = {state: raw.get(state, 0) for state in QUEUE_COUNT_STATES}
    counts["total"] = sum(counts.values())
    return counts


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


def _context(request: Request) -> AppContext:
    context: AppContext = request.app.state.context
    return context


def create_app(options: AppOptions) -> FastAPI:
    """Build the FastAPI app and its background jobs."""
    adapter, token, telegram_active = _resolve_adapter(options)
    redactor = SecretRegistry.from_environment(
        extra_values=[token] if token is not None and token.strip() else []
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
    context = AppContext(
        layout=options.layout,
        config=options.config,
        database=database,
        jobs=jobs,
        adapter=adapter,
        redactor=redactor,
        allowed_chat_ids=tuple(options.config.telegram.allowed_chat_ids),
        telegram_active=telegram_active,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx: AppContext = app.state.context
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

    @app.get("/health")
    def health(request: Request) -> JSONResponse:
        """Report process, version and database health."""
        ctx = _context(request)
        reachable = ctx.database.is_healthy()
        revision: str | None = None
        if reachable:
            revision = inspect_database(ctx.layout.db_path).revision
        healthy = reachable and revision is not None
        body: dict[str, Any] = {
            "status": "ok" if healthy else "error",
            "version": __version__,
            "db": {"ok": healthy, "revision": revision},
        }
        return JSONResponse(body, status_code=200 if healthy else 503)

    @app.get("/status")
    def status(request: Request) -> dict[str, Any]:
        """Return profile metadata, queue counts and runtime state."""
        ctx = _context(request)
        totals = {state: 0 for state in QUEUE_COUNT_STATES}
        profiles: list[dict[str, Any]] = []
        last_run: dict[str, Any] | None = None
        with ctx.database.session() as session:
            repository = ContentItemRepository(session)
            for summary in ProfileCatalog(session).list():
                counts = repository.state_counts(ProfileScope(summary.id))
                queue = _queue_counts(counts)
                for state in QUEUE_COUNT_STATES:
                    totals[state] += queue[state]
                profiles.append(
                    {
                        "slug": summary.slug,
                        "name": summary.name,
                        "status": summary.status,
                        "kill_switch": summary.kill_switch,
                        "queue": queue,
                    }
                )
            latest = AuditRepository(session).latest(None, action="scheduler.run")
            if latest is not None:
                last_run = {
                    "id": latest.id,
                    "created_at": _iso(latest.created_at),
                    "payload": latest.payload,
                }
        return {
            "version": __version__,
            "profiles": profiles,
            "queue": _queue_counts(totals),
            "scheduler": {
                "last_run_at": None if last_run is None else last_run["created_at"],
                "last_run": last_run,
            },
            "telegram": {
                "enabled": ctx.telegram_active,
                "configured": ctx.config.telegram.enabled,
                "mode": ctx.config.telegram.mode,
                "allowed_chats": len(ctx.allowed_chat_ids),
            },
        }

    @app.post("/webhook/telegram")
    async def telegram_webhook(request: Request) -> dict[str, Any]:
        """Handle one Bot API update. Always answers 200, never echoes chat ids."""
        ctx = _context(request)
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
                adapter=ctx.adapter,
                profile_hint=None,
                redactor=ctx.redactor,
            )
        return _inbox_response(outcome)

    return app
