"""Server-rendered Web UI: login, dashboard, queue, inbox, reports and profiles.

Forms are normal POST/redirect forms. HTMX progressively enhances queue and inbox rows
by replacing the affected row; without JavaScript the same forms still work.
"""

from __future__ import annotations

import difflib
import hmac
import html
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from heraldesk import __version__
from heraldesk.agents.errors import AgentRunError
from heraldesk.analyst import ReportError, monthly_period, weekly_period
from heraldesk.community import InboxError
from heraldesk.core.audit import AuditService
from heraldesk.core.locks import (
    KillSwitchError,
    engage_global_kill_switch,
    is_global_kill_switch_enabled,
    release_global_kill_switch,
)
from heraldesk.db.repositories import (
    ApprovalLocator,
    ProfileCatalog,
    StudioMessageRepository,
    StudioThreadRepository,
)
from heraldesk.domain.approvals import edit_item
from heraldesk.domain.errors import (
    ApprovalError,
    ContentError,
    ProfileNotFoundError,
    ProfileValidationError,
)
from heraldesk.domain.profiles import list_profiles, load_policy, profile_summary, resolve_scope
from heraldesk.llm.errors import LLMError
from heraldesk.llm.factory import build_llm, resolve_model
from heraldesk.platforms.base import constraints_for
from heraldesk.studio import StudioError, StudioService
from heraldesk.web.auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    AdminAuth,
    AuthConfigError,
    Forbidden,
    UISession,
    Unauthorized,
    rotate_session_epoch,
    session_epoch_path,
)
from heraldesk.web.context import WebContext
from heraldesk.web.errors import BadRequest, NotFound, WebError
from heraldesk.web.profile_editor import (
    PROFILE_FILES,
    ProfileEditError,
    ProfileEditState,
    read_profile_files,
    save_profile_files,
)
from heraldesk.web.rendering import render_markdown
from heraldesk.web.service import (
    approve_by_id,
    audit_login,
    calendar_week,
    cost_summary,
    dashboard_counts,
    dashboard_needs_attention,
    generate_report,
    get_inbox_detail,
    get_queue_detail,
    ignore_inbox,
    list_audit_views,
    list_inbox_views,
    list_queue_views,
    list_report_files,
    profile_mode,
    profiles_payload,
    publish_content,
    read_report,
    reject_by_id,
    reply_inbox,
    report_file,
)

router = APIRouter()

_TEMPLATES = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES))
templates.env.globals["app_version"] = __version__

LOGIN_CSRF_MAX_AGE = 600


def _context(request: Request) -> WebContext:
    context: WebContext = request.app.state.context
    return context


def _auth(request: Request) -> AdminAuth:
    auth: AdminAuth = request.app.state.auth
    return auth


def _session(request: Request) -> UISession:
    session = _auth(request).read_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise Unauthorized("login required")
    return session


def _render(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    payload = {"request": request, **context}
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=payload,
        status_code=status_code,
    )


def _page(
    request: Request,
    session: UISession,
    name: str,
    active: str,
    *,
    status_code: int = 200,
    **extra: Any,
) -> HTMLResponse:
    ctx = _context(request)
    current_profile = request.query_params.get("profile") or None
    profiles: list[dict[str, str]] = []
    queue_pending = 0
    inbox_pending = 0
    try:
        with ctx.database.session() as db:
            profiles = [{"slug": s.slug, "name": s.name} for s in ProfileCatalog(db).list()]
            counts = dashboard_counts(db)
            queue_pending = counts["queue"]["pending_approval"]
            inbox_pending = counts["inbox"]["pending"]
    except Exception:
        profiles = []
    return _render(
        request,
        name,
        {
            "session": session,
            "csrf": session.csrf,
            "active": active,
            "version": __version__,
            "studio_enabled": ctx.config.studio.enabled,
            "killswitch": is_global_kill_switch_enabled(ctx.layout),
            "profiles": profiles,
            "current_profile": current_profile,
            "profile_mode": profile_mode(ctx.layout, current_profile),
            "profile_qs": f"?profile={current_profile}" if current_profile else "",
            "queue_pending": queue_pending,
            "inbox_pending": inbox_pending,
            **extra,
        },
        status_code=status_code,
    )


async def _form(request: Request) -> FormData:
    return await request.form()


def _form_value(form: FormData, key: str) -> str:
    value = form.get(key)
    return value if isinstance(value, str) else ""


def _require_csrf(auth: AdminAuth, session: UISession, form: FormData) -> None:
    if not auth.verify_csrf(session, _form_value(form, "csrf")):
        raise Forbidden("invalid CSRF token; reload the page and try again")


def _set_session_cookie(response: Response, auth: AdminAuth, cookie: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=auth.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=auth.cookie_secure,
        path="/",
    )


def _set_login_csrf_cookie(response: Response, auth: AdminAuth, signed: str) -> None:
    response.set_cookie(
        LOGIN_CSRF_COOKIE,
        signed,
        max_age=LOGIN_CSRF_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=auth.cookie_secure,
        path="/",
    )


def _login_page(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    auth = _auth(request)
    hidden, signed = auth.issue_login_csrf()
    response = _render(
        request,
        "login.html",
        {"login": True, "csrf": hidden, "error": error, "version": __version__},
        status_code=status_code,
    )
    _set_login_csrf_cookie(response, auth, signed)
    return response


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


_RISK_PRIORITY = {"blocked": 0, "high": 1, "medium": 2, "low": 3}


def _sorted_queue(views: list[Any]) -> list[Any]:
    """Order queue entries by risk (blocked/high first), then oldest first."""

    def key(view: Any) -> tuple[int, datetime]:
        created = view.created_at or datetime.max.replace(tzinfo=UTC)
        return (_RISK_PRIORITY.get(view.risk_level or "", 4), created)

    return sorted(views, key=key)


def _monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _parse_week(value: str) -> date:
    return _monday_of(date.fromisoformat(value))


def _word_diff(old: str, new: str) -> str:
    """Return a word-level HTML diff of two texts with ``ins``/``del``."""
    matcher = difflib.SequenceMatcher(a=old.split(), b=new.split())
    parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            parts.append(html.escape(" ".join(old.split()[i1:i2])))
        elif tag == "delete":
            parts.append("<del>" + html.escape(" ".join(old.split()[i1:i2])) + "</del>")
        elif tag == "insert":
            parts.append("<ins>" + html.escape(" ".join(new.split()[j1:j2])) + "</ins>")
        else:
            parts.append("<del>" + html.escape(" ".join(old.split()[i1:i2])) + "</del>")
            parts.append("<ins>" + html.escape(" ".join(new.split()[j1:j2])) + "</ins>")
    return " ".join(part for part in parts if part)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    """Show the token login form."""
    if _auth(request).read_session(request.cookies.get(SESSION_COOKIE)) is not None:
        return RedirectResponse("/", status_code=303)
    return _login_page(request)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> Response:
    """Validate the admin token, rate limit failures and start a UI session."""
    auth = _auth(request)
    form = await _form(request)
    if not auth.verify_login_csrf(
        request.cookies.get(LOGIN_CSRF_COOKIE),
        _form_value(form, "csrf"),
    ):
        return _login_page(request, error="The login form expired. Try again.", status_code=403)

    key = auth.client_key(request)
    if not auth.rate_limiter.allowed(key):
        context = _context(request)
        with context.database.session() as session:
            audit_login(session, "auth.login_rate_limited")
        retry = auth.rate_limiter.retry_after(key)
        rate_response = _login_page(
            request,
            error="Too many failed attempts. Wait a minute and try again.",
            status_code=429,
        )
        rate_response.headers["Retry-After"] = str(retry)
        return rate_response

    entered = _form_value(form, "token")
    if not entered or not hmac.compare_digest(entered, auth.token):
        auth.rate_limiter.record_failure(key)
        context = _context(request)
        with context.database.session() as session:
            audit_login(session, "auth.login_failed", detail="invalid token")
        return _login_page(request, error="Invalid admin token.", status_code=401)

    auth.rate_limiter.reset(key)
    _ui_session, cookie = auth.issue_session()
    context = _context(request)
    with context.database.session() as session:
        audit_login(session, "auth.login")
        landing = "/queue" if dashboard_counts(session)["queue"]["pending_approval"] > 0 else "/"
    response: Response = RedirectResponse(landing, status_code=303)
    _set_session_cookie(response, auth, cookie)
    response.delete_cookie(LOGIN_CSRF_COOKIE, path="/")
    return response


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """Clear the UI session after a CSRF-checked POST."""
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    context = _context(request)
    with context.database.session() as session_db:
        audit_login(session_db, "auth.logout")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/logout-all")
async def logout_all(request: Request) -> Response:
    """Invalidate every UI session by rotating the instance session epoch."""
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    context = _context(request)
    try:
        rotate_session_epoch(session_epoch_path(context.layout))
    except AuthConfigError as exc:
        return _page(
            request,
            session,
            "error.html",
            "dashboard",
            status_code=500,
            code="session_epoch_failed",
            message=context.redactor.redact(str(exc)) or "cannot revoke sessions",
        )
    with context.database.session() as db:
        audit_login(db, "auth.logout_all", detail="epoch rotated")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, ok: str | None = None, profile: str | None = None) -> HTMLResponse:
    """Show the items that need a human's review."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        needs = dashboard_needs_attention(db, context.redactor, profile=profile)
    return _page(
        request,
        session,
        "dashboard.html",
        "dashboard",
        needs=needs,
        notice="Kill switch updated." if ok == "killswitch" else None,
        error=None,
    )


@router.post("/killswitch")
async def killswitch_toggle(request: Request) -> Response:
    """Engage or clear the global kill switch from the dashboard."""
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    action = _form_value(form, "action").strip().lower()
    if action not in {"engage", "clear"}:
        raise BadRequest("kill switch action must be engage or clear", "invalid_action")
    context = _context(request)
    try:
        with context.database.session() as db:
            if action == "engage":
                changed = engage_global_kill_switch(context.layout)
                verb = "engaged"
            else:
                changed = release_global_kill_switch(context.layout)
                verb = "disengaged"
            if changed:
                AuditService(db).record(
                    None,
                    actor="user",
                    action=f"killswitch.{verb}",
                    entity="killswitch",
                    payload={"scope": "global", "enabled": action == "engage", "source": "web"},
                )
    except KillSwitchError as exc:
        return _page(
            request,
            session,
            "error.html",
            "dashboard",
            status_code=500,
            code="killswitch_failed",
            message=context.redactor.redact(str(exc)) or "kill switch update failed",
        )
    return RedirectResponse("/?ok=killswitch", status_code=303)


@router.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    ok: str | None = None,
    profile: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    """List approval-backed queue entries with a review panel."""
    session = _session(request)
    context = _context(request)
    selected_status = status or "pending"
    with context.database.session() as db:
        entries = _sorted_queue(
            list_queue_views(db, context.redactor, status=selected_status, profile=profile)
        )
    return _page(
        request,
        session,
        "queue.html",
        "queue",
        entries=entries,
        detail=None,
        filter_status=selected_status,
        notice=_notice(ok),
        error=None,
    )


@router.get("/queue/{approval_id}", response_class=HTMLResponse)
def queue_detail_page(
    request: Request,
    approval_id: int,
    ok: str | None = None,
    error: str | None = None,
    profile: str | None = None,
) -> HTMLResponse:
    """Show the full redacted draft before a human approves or rejects it."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        try:
            detail = get_queue_detail(db, context.redactor, approval_id)
            entries = _sorted_queue(
                list_queue_views(db, context.redactor, status="pending", profile=profile)
            )
        except WebError as exc:
            return _page(
                request,
                session,
                "error.html",
                "queue",
                status_code=exc.status_code,
                code=exc.code,
                message=exc.message,
            )
    notice: str | None = None
    if ok is not None:
        notice = {
            "edited": "Draft saved.",
            "published": "Item published.",
            "skipped": "Publish skipped.",
        }.get(ok)
    return _page(
        request,
        session,
        "queue.html",
        "queue",
        entries=entries,
        detail=detail,
        filter_status="pending",
        notice=notice,
        error=error,
    )


@router.get("/queue/{approval_id}/panel", response_class=HTMLResponse)
def queue_detail_panel(request: Request, approval_id: int) -> Response:
    """Return the detail panel as an htmx fragment, or redirect to the full page."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        try:
            detail = get_queue_detail(db, context.redactor, approval_id)
        except WebError as exc:
            if _is_htmx(request):
                return _render(
                    request,
                    "partials/error_row.html",
                    {"error": exc.message, "colspan": 1},
                    status_code=200,
                )
            return RedirectResponse("/queue", status_code=303)
    if _is_htmx(request):
        return _render(
            request,
            "partials/queue_detail.html",
            {"detail": detail, "csrf": session.csrf},
        )
    return RedirectResponse(f"/queue/{approval_id}", status_code=303)


@router.post("/queue/{approval_id}/edit/preview", response_class=HTMLResponse)
async def queue_edit_preview(request: Request, approval_id: int) -> Response:
    """Show a read-only word diff before saving an edit. Performs no writes."""
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    body = _form_value(form, "body").strip()
    context = _context(request)
    with context.database.session() as db:
        try:
            detail = get_queue_detail(db, context.redactor, approval_id)
        except WebError as exc:
            return _page(
                request,
                session,
                "error.html",
                "queue",
                status_code=exc.status_code,
                code=exc.code,
                message=exc.message,
            )
    new_redacted = context.redactor.redact(body)
    if not new_redacted.strip():
        error = "edited body must not be empty"
        return _queue_detail_with_error(request, session, approval_id, BadRequest(error))
    diff_html = _word_diff(detail.body, new_redacted)
    payload: dict[str, Any] = {
        "detail": detail,
        "new_body": body,
        "diff_html": diff_html,
    }
    if _is_htmx(request):
        return _render(
            request, "partials/queue_edit_preview.html", {**payload, "csrf": session.csrf}
        )
    return _page(request, session, "queue_edit_preview.html", "queue", **payload)


@router.post("/queue/{approval_id}/edit", response_class=HTMLResponse)
async def queue_edit(request: Request, approval_id: int) -> Response:
    """Edit the draft body through the audited approval service."""
    ui_session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), ui_session, form)
    body = _form_value(form, "body").strip()
    context = _context(request)
    try:
        with context.database.session() as db:
            resolved = ApprovalLocator(db).resolve(approval_id)
            if resolved is None:
                raise NotFound("approval not found")
            scope, item_id = resolved
            edit_item(db, scope, item_id, body=body, actor="user", channel="web")
    except (ApprovalError, ContentError, WebError) as exc:
        return _queue_detail_with_error(request, ui_session, approval_id, exc)
    return RedirectResponse(f"/queue/{approval_id}?ok=edited", status_code=303)


@router.post("/queue/{approval_id}/publish", response_class=HTMLResponse)
async def queue_publish(request: Request, approval_id: int) -> Response:
    """Publish one approved/scheduled/failed item from the Web UI."""
    ui_session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), ui_session, form)
    context = _context(request)
    try:
        with context.database.session() as db:
            resolved = ApprovalLocator(db).resolve(approval_id)
            if resolved is None:
                raise NotFound("approval not found")
            _scope, item_id = resolved
            outcome = publish_content(db, context, item_id, actor="user")
    except (ApprovalError, ContentError, WebError) as exc:
        return _queue_detail_with_error(request, ui_session, approval_id, exc)
    if outcome.status.value == "published":
        return RedirectResponse(f"/queue/{approval_id}?ok=published", status_code=303)
    return RedirectResponse(f"/queue/{approval_id}?ok=skipped", status_code=303)


def _queue_detail_with_error(
    request: Request,
    ui_session: UISession,
    approval_id: int,
    exc: Exception,
) -> HTMLResponse:
    context = _context(request)
    with context.database.session() as db:
        try:
            detail = get_queue_detail(db, context.redactor, approval_id)
            entries = _sorted_queue(list_queue_views(db, context.redactor, status="pending"))
        except WebError as inner:
            return _page(
                request,
                ui_session,
                "error.html",
                "queue",
                status_code=inner.status_code,
                code=inner.code,
                message=inner.message,
            )
    return _page(
        request,
        ui_session,
        "queue.html",
        "queue",
        entries=entries,
        detail=detail,
        filter_status="pending",
        notice=None,
        error=context.redactor.redact(str(exc)) or "action failed",
        status_code=400,
    )


@router.post("/queue/{approval_id}/approve", response_class=HTMLResponse)
async def queue_approve(request: Request, approval_id: int) -> Response:
    """Approve one queue item by approval id."""
    session = _session(request)
    form = await _form(request)
    try:
        _require_csrf(_auth(request), session, form)
    except Forbidden as exc:
        if not _is_htmx(request):
            raise
        return _queue_action_error(request, session, approval_id, exc)
    context = _context(request)
    try:
        with context.database.session() as db:
            approve_by_id(db, context.layout, approval_id, actor="user")
    except (ApprovalError, ContentError, WebError) as exc:
        return _queue_action_error(request, session, approval_id, exc)
    if _is_htmx(request):
        return _queue_action_success(request, session, "Item approved.")
    return RedirectResponse("/queue?ok=approved", status_code=303)


@router.post("/queue/{approval_id}/reject", response_class=HTMLResponse)
async def queue_reject(request: Request, approval_id: int) -> Response:
    """Reject one queue item with a required reason."""
    session = _session(request)
    form = await _form(request)
    try:
        _require_csrf(_auth(request), session, form)
    except Forbidden as exc:
        if not _is_htmx(request):
            raise
        return _queue_action_error(request, session, approval_id, exc)
    reason = _form_value(form, "reason").strip()
    if not reason:
        return _queue_action_error(
            request,
            session,
            approval_id,
            BadRequest("reject reason is required", "reason_required"),
        )
    context = _context(request)
    try:
        with context.database.session() as db:
            reject_by_id(db, approval_id, reason=reason, actor="user")
    except (ApprovalError, ContentError, WebError) as exc:
        return _queue_action_error(request, session, approval_id, exc)
    if _is_htmx(request):
        return _queue_action_success(request, session, "Item rejected.")
    return RedirectResponse("/queue?ok=rejected", status_code=303)


def _queue_action_success(request: Request, session: UISession, label: str) -> Response:
    """Build an htmx response that removes the row and updates badges, toast and detail."""
    context = _context(request)
    with context.database.session() as db:
        remaining = _sorted_queue(list_queue_views(db, context.redactor, status="pending"))
        queue_pending = len(remaining)
        next_id = remaining[0].approval_id if remaining else None
        panel = ""
        if next_id is not None:
            try:
                detail = get_queue_detail(db, context.redactor, next_id)
                panel = templates.env.get_template("partials/queue_detail.html").render(
                    detail=detail, csrf=session.csrf
                )
            except WebError:
                panel = ""
    badge = f'<span id="queue-badge" hx-swap-oob="true">{queue_pending}</span>'
    toast = (
        '<div class="toast-region" id="toast-region" hx-swap-oob="true">'
        f'<div class="toast status-ok"><span>{label}</span>'
        '<button type="button" class="toast-close" aria-label="Dismiss">&#215;</button></div></div>'
    )
    detail_oob = (
        f'<section class="queue-detail" id="queue-detail" hx-swap-oob="innerHTML">{panel}</section>'
    )
    response = Response(badge + toast + detail_oob)
    response.headers["HX-Push-Url"] = f"/queue/{next_id}" if next_id else "/queue"
    return response


def _queue_action_error(
    request: Request,
    session: UISession,
    approval_id: int,
    exc: Exception,
) -> HTMLResponse:
    context = _context(request)
    message = context.redactor.redact(str(exc)) or "action failed"
    with context.database.session() as db:
        entry = next(
            (
                view
                for view in list_queue_views(db, context.redactor, status="pending")
                if view.approval_id == approval_id
            ),
            None,
        )
    if _is_htmx(request):
        if entry is not None:
            return _render(
                request,
                "partials/queue_row.html",
                {"entry": entry, "csrf": session.csrf, "error": message},
                status_code=200,
            )
        return _render(
            request,
            "partials/error_row.html",
            {"error": message, "colspan": 4},
            status_code=200,
        )
    with context.database.session() as db:
        entries = list_queue_views(db, context.redactor, status="pending")
    return _page(
        request,
        session,
        "queue.html",
        "queue",
        entries=entries,
        notice=None,
        error=message,
        status_code=400,
    )


@router.get("/inbox", response_class=HTMLResponse)
def inbox_page(
    request: Request,
    ok: str | None = None,
    status: str | None = None,
    profile: str | None = None,
) -> HTMLResponse:
    """List inbox items for manual triage, with a review panel."""
    session = _session(request)
    context = _context(request)
    selected = status or "pending"
    with context.database.session() as db:
        items = _inbox_list(db, context, status=selected, profile=profile)
    return _page(
        request,
        session,
        "inbox.html",
        "inbox",
        items=items,
        detail=None,
        filter_status=selected,
        retention_days=context.config.telegram.retention_days,
        notice=_notice(ok),
        error=None,
    )


@router.get("/inbox/{item_id}", response_class=HTMLResponse)
def inbox_detail_page(request: Request, item_id: int, profile: str | None = None) -> HTMLResponse:
    """Show the full inbox item and triage actions."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        try:
            detail = get_inbox_detail(db, context.redactor, item_id)
            items = _inbox_list(db, context, status="pending", profile=profile)
        except WebError as exc:
            return _page(
                request,
                session,
                "error.html",
                "inbox",
                status_code=exc.status_code,
                code=exc.code,
                message=exc.message,
            )
    return _page(
        request,
        session,
        "inbox.html",
        "inbox",
        items=items,
        detail=detail,
        filter_status="pending",
        retention_days=context.config.telegram.retention_days,
        notice=None,
        error=None,
    )


@router.get("/inbox/{item_id}/panel", response_class=HTMLResponse)
def inbox_detail_panel(request: Request, item_id: int) -> Response:
    """Return the inbox detail fragment for htmx, or redirect to the full page."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        try:
            detail = get_inbox_detail(db, context.redactor, item_id)
        except WebError as exc:
            if _is_htmx(request):
                return _render(
                    request,
                    "partials/error_row.html",
                    {"error": exc.message, "colspan": 1},
                    status_code=200,
                )
            return RedirectResponse("/inbox", status_code=303)
    if _is_htmx(request):
        return _render(
            request,
            "partials/inbox_detail.html",
            {"detail": detail, "csrf": session.csrf},
        )
    return RedirectResponse(f"/inbox/{item_id}", status_code=303)


def _inbox_list(
    db: Any,
    context: WebContext,
    *,
    status: str,
    profile: str | None,
) -> list[Any]:
    items = list_inbox_views(db, context.redactor, status=status, limit=200)
    if profile is not None:
        items = [item for item in items if item.profile_slug == profile]
    return sorted(items, key=lambda item: item.created_at)


@router.post("/inbox/{item_id}/reply", response_class=HTMLResponse)
async def inbox_reply(request: Request, item_id: int) -> Response:
    """Send a manual reply through the existing Telegram service."""
    session = _session(request)
    form = await _form(request)
    try:
        _require_csrf(_auth(request), session, form)
    except Forbidden as exc:
        if not _is_htmx(request):
            raise
        return _inbox_action_error(request, session, item_id, exc)
    text = _form_value(form, "text").strip()
    if not text:
        return _inbox_action_error(
            request,
            session,
            item_id,
            BadRequest("reply text is required", "text_required"),
        )
    context = _context(request)
    try:
        with context.database.session() as db:
            reply_inbox(db, context, item_id, text)
    except (InboxError, WebError) as exc:
        return _inbox_action_error(request, session, item_id, exc)
    if _is_htmx(request):
        return _inbox_action_success(request, session, "Reply sent.")
    return RedirectResponse("/inbox?ok=replied", status_code=303)


@router.post("/inbox/{item_id}/ignore", response_class=HTMLResponse)
async def inbox_ignore(request: Request, item_id: int) -> Response:
    """Mark one inbox item ignored with a bounded reason."""
    session = _session(request)
    form = await _form(request)
    try:
        _require_csrf(_auth(request), session, form)
    except Forbidden as exc:
        if not _is_htmx(request):
            raise
        return _inbox_action_error(request, session, item_id, exc)
    reason = _form_value(form, "reason") or "other"
    context = _context(request)
    try:
        with context.database.session() as db:
            ignore_inbox(db, context, item_id, reason)
    except (InboxError, WebError) as exc:
        return _inbox_action_error(request, session, item_id, exc)
    if _is_htmx(request):
        return _inbox_action_success(request, session, "Item ignored.")
    return RedirectResponse("/inbox?ok=ignored", status_code=303)


def _inbox_action_success(request: Request, session: UISession, label: str) -> Response:
    """Build an htmx response that removes the row and updates badge, toast and detail."""
    context = _context(request)
    with context.database.session() as db:
        remaining = _inbox_list(db, context, status="pending", profile=None)
        inbox_pending = len(remaining)
        next_id = remaining[0].id if remaining else None
        panel = ""
        if next_id is not None:
            try:
                detail = get_inbox_detail(db, context.redactor, next_id)
                panel = templates.env.get_template("partials/inbox_detail.html").render(
                    detail=detail, csrf=session.csrf
                )
            except WebError:
                panel = ""
    badge = f'<span id="inbox-badge" hx-swap-oob="true">{inbox_pending}</span>'
    toast = (
        '<div class="toast-region" id="toast-region" hx-swap-oob="true">'
        f'<div class="toast status-ok"><span>{label}</span>'
        '<button type="button" class="toast-close" aria-label="Dismiss">&#215;</button></div></div>'
    )
    detail_oob = (
        f'<section class="inbox-detail" id="inbox-detail" hx-swap-oob="innerHTML">{panel}</section>'
    )
    response = Response(badge + toast + detail_oob)
    response.headers["HX-Push-Url"] = f"/inbox/{next_id}" if next_id else "/inbox"
    return response


def _inbox_action_error(
    request: Request,
    session: UISession,
    item_id: int,
    exc: Exception,
) -> HTMLResponse:
    context = _context(request)
    message = context.redactor.redact(str(exc)) or "action failed"
    with context.database.session() as db:
        item = next(
            (
                view
                for view in list_inbox_views(db, context.redactor, status="pending", limit=200)
                if view.id == item_id
            ),
            None,
        )
    if _is_htmx(request):
        if item is not None:
            return _render(
                request,
                "partials/inbox_item.html",
                {"item": item, "csrf": session.csrf, "error": message},
                status_code=200,
            )
        return _render(
            request,
            "partials/error_row.html",
            {"error": message, "colspan": 4},
            status_code=200,
        )
    with context.database.session() as db:
        items = _inbox_list(db, context, status="pending", profile=None)
    return _page(
        request,
        session,
        "inbox.html",
        "inbox",
        items=items,
        detail=None,
        filter_status="pending",
        retention_days=context.config.telegram.retention_days,
        notice=None,
        error=message,
        status_code=400,
    )


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, ok: str | None = None) -> HTMLResponse:
    """List generated weekly/monthly files and offer generation buttons."""
    session = _session(request)
    context = _context(request)
    current = datetime.now(UTC)
    iso = current.isocalendar()
    return _page(
        request,
        session,
        "reports.html",
        "reports",
        files=list_report_files(context.layout),
        current_week=f"{iso.year:04d}-W{iso.week:02d}",
        current_month=f"{current.year:04d}-{current.month:02d}",
        notice=_notice(ok),
        error=None,
    )


@router.post("/reports/weekly", response_class=HTMLResponse)
async def reports_weekly(request: Request) -> Response:
    """Generate the current or requested weekly report file."""
    return await _generate_report(request, "weekly")


@router.post("/reports/monthly", response_class=HTMLResponse)
async def reports_monthly(request: Request) -> Response:
    """Generate the current or requested monthly report file."""
    return await _generate_report(request, "monthly")


async def _generate_report(request: Request, kind: str) -> Response:
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    field = "week" if kind == "weekly" else "month"
    value = _form_value(form, field).strip() or None
    context = _context(request)
    try:
        with context.database.session() as db:
            generate_report(db, context.layout, kind, value)
    except (ReportError, WebError) as exc:
        message = context.redactor.redact(str(exc)) or "report generation failed"
        return _page(
            request,
            session,
            "reports.html",
            "reports",
            files=list_report_files(context.layout),
            current_week=weekly_period().label,
            current_month=monthly_period().label,
            notice=None,
            error=message,
        )
    if _is_htmx(request):
        return Response("", headers={"HX-Redirect": "/reports?ok=generated"})
    return RedirectResponse("/reports?ok=generated", status_code=303)


@router.get("/reports/view/{filename}", response_class=HTMLResponse)
def report_view(request: Request, filename: str) -> HTMLResponse:
    """Render one generated report as sanitized HTML."""
    session = _session(request)
    context = _context(request)
    try:
        markdown = read_report(context.layout, filename)
        path = report_file(context.layout, filename)
    except WebError as exc:
        return _page(
            request,
            session,
            "error.html",
            "reports",
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
        )
    return _page(
        request,
        session,
        "report.html",
        "reports",
        filename=path.name,
        label=path.stem.split("-", 1)[1],
        html=render_markdown(markdown),
    )


@router.get("/reports/download/{filename}")
def report_download(request: Request, filename: str) -> FileResponse:
    """Download one generated Markdown report."""
    _session(request)
    context = _context(request)
    try:
        path = report_file(context.layout, filename)
    except WebError as exc:
        raise NotFound(exc.message) from exc
    return FileResponse(
        path,
        media_type="text/markdown; charset=utf-8",
        filename=path.name,
    )


def _require_studio(request: Request) -> WebContext:
    ctx = _context(request)
    if not ctx.config.studio.enabled:
        raise NotFound("the Draft Room is disabled")
    return ctx


def _studio_service(ctx: WebContext, db: Any, *, with_provider: bool = False) -> StudioService:
    provider = None
    model = ""
    if with_provider:
        provider = build_llm(ctx.config, ctx.layout)
        model = resolve_model(ctx.config, provider)
    return StudioService(
        db,
        ctx.layout,
        provider=provider,
        model=model,
        daily_cap_usd=ctx.config.llm.max_daily_cost_usd,
        thread_cap_usd=ctx.config.studio.max_cost_usd,
        max_messages=ctx.config.studio.max_messages,
        enabled=ctx.config.studio.enabled,
        redactor=ctx.redactor,
    )


def _studio_thread_payload(
    ctx: WebContext,
    db: Any,
    thread_id: int,
    *,
    error: str | None = None,
    draft_text: str = "",
    notice: str | None = None,
) -> dict[str, Any]:
    locator = StudioThreadRepository(db)
    scope = locator.scope_for(thread_id)
    if scope is None:
        raise NotFound(f"studio thread {thread_id} does not exist")
    service = _studio_service(ctx, db)
    thread = service.get_thread(scope, thread_id)
    messages = service.list_messages(scope, thread_id)
    summary = profile_summary(db, scope)
    spent = sum(message.cost_usd for message in messages)
    return {
        "thread": thread,
        "messages": messages,
        "profile": summary,
        "spent": spent,
        "max_cost_usd": ctx.config.studio.max_cost_usd,
        "remaining_messages": max(0, ctx.config.studio.max_messages - len(messages)),
        "error": error,
        "draft_text": draft_text,
        "notice": notice,
        "platforms": ["threads", "nostr", "fake"],
        "profile_files": list(PROFILE_FILES),
    }


@router.get("/studio", response_class=HTMLResponse)
def studio_page(request: Request, profile: str | None = None) -> HTMLResponse:
    """List Draft Room threads and let the user start a conversation."""
    ctx = _require_studio(request)
    session = _session(request)
    with ctx.database.session() as db:
        summaries = list_profiles(db)
        selected = None
        if profile is not None:
            try:
                scope = resolve_scope(db, profile)
            except (ProfileNotFoundError, ProfileValidationError) as exc:
                raise NotFound(f"profile '{profile}' is not loaded") from exc
            selected = next((item for item in summaries if item.slug == profile), None)
        elif summaries:
            scope = resolve_scope(db, summaries[0].slug)
            selected = summaries[0]
        else:
            scope = None
        threads = _studio_service(ctx, db).list_threads(scope) if scope is not None else []
    return _page(
        request,
        session,
        "studio.html",
        "studio",
        profiles=summaries,
        selected=selected,
        threads=threads,
    )


@router.post("/studio/threads", response_class=HTMLResponse)
async def studio_thread_create(request: Request) -> Response:
    """Create a Draft Room thread for one profile."""
    ctx = _require_studio(request)
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    slug = _form_value(form, "profile").strip()
    title = _form_value(form, "title").strip() or "New thread"
    with ctx.database.session() as db:
        try:
            scope = resolve_scope(db, slug)
            thread = _studio_service(ctx, db).create_thread(scope, title=title)
        except (ProfileNotFoundError, ProfileValidationError, StudioError) as exc:
            raise BadRequest(str(exc)) from exc
    return RedirectResponse(f"/studio/{thread.id}", status_code=303)


@router.get("/studio/{thread_id}", response_class=HTMLResponse)
def studio_thread_page(request: Request, thread_id: int, notice: str | None = None) -> HTMLResponse:
    """Show one thread and its messages."""
    ctx = _require_studio(request)
    session = _session(request)
    with ctx.database.session() as db:
        payload = _studio_thread_payload(ctx, db, thread_id, notice=notice)
    return _page(request, session, "studio_thread.html", "studio", **payload)


@router.post("/studio/{thread_id}/messages", response_class=HTMLResponse)
async def studio_message_create(request: Request, thread_id: int) -> Response:
    """Run one Editor turn and redirect back to the thread."""
    ctx = _require_studio(request)
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    text = _form_value(form, "text")
    role = _form_value(form, "role").strip() or "editor"
    platform = _form_value(form, "platform").strip() or None
    file = _form_value(form, "file").strip() or None
    with ctx.database.session() as db:
        scope = StudioThreadRepository(db).scope_for(thread_id)
        if scope is None:
            raise NotFound(f"studio thread {thread_id} does not exist")
        constraints = constraints_for(platform) if platform else None
        try:
            _studio_service(ctx, db, with_provider=True).send_message(
                scope,
                thread_id,
                text,
                role=role,
                platform=platform,
                constraints=constraints,
                file=file,
            )
        except (AgentRunError, LLMError, StudioError, ValueError) as exc:
            error = ctx.redactor.redact(str(exc))[:500]
            payload = _studio_thread_payload(ctx, db, thread_id, error=error, draft_text=text)
            return _page(
                request,
                session,
                "studio_thread.html",
                "studio",
                status_code=400,
                **payload,
            )
    return RedirectResponse(f"/studio/{thread_id}", status_code=303)


@router.post("/studio/{message_id}/draft", response_class=HTMLResponse)
async def studio_draft_create(request: Request, message_id: int) -> Response:
    """Run lint and Compliance on a draft artifact and create a content item."""
    ctx = _require_studio(request)
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    with ctx.database.session() as db:
        repository = StudioMessageRepository(db)
        located = repository.locate(message_id)
        if located is None:
            raise NotFound(f"studio message {message_id} does not exist")
        scope, thread_id = located
        message = repository.get(scope, message_id)
        platform = (message.artifact_json or {}).get("platform") if message else None
        constraints = constraints_for(platform) if platform else None
        max_text_bytes = 500 if platform == "threads" else None
        try:
            result = _studio_service(ctx, db, with_provider=True).create_draft(
                scope,
                message_id,
                constraints=constraints,
                max_text_bytes=max_text_bytes,
            )
        except (AgentRunError, LLMError, StudioError, ValueError) as exc:
            error = ctx.redactor.redact(str(exc))[:500]
            payload = _studio_thread_payload(ctx, db, thread_id, error=error)
            return _page(
                request,
                session,
                "studio_thread.html",
                "studio",
                status_code=400,
                **payload,
            )
    if result.blocked:
        return RedirectResponse(f"/studio/{thread_id}?notice=blocked", status_code=303)
    return RedirectResponse("/queue?ok=drafted", status_code=303)


@router.post("/studio/{message_id}/patch", response_class=HTMLResponse)
async def studio_patch_apply(request: Request, message_id: int) -> Response:
    """Apply a profile patch after re-checking its base hash."""
    ctx = _require_studio(request)
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    with ctx.database.session() as db:
        repository = StudioMessageRepository(db)
        located = repository.locate(message_id)
        if located is None:
            raise NotFound(f"studio message {message_id} does not exist")
        scope, thread_id = located
        try:
            _studio_service(ctx, db).apply_profile_patch(scope, message_id)
        except StudioError as exc:
            error = ctx.redactor.redact(str(exc))[:500]
            payload = _studio_thread_payload(ctx, db, thread_id, error=error)
            return _page(
                request,
                session,
                "studio_thread.html",
                "studio",
                status_code=400,
                **payload,
            )
    return RedirectResponse(f"/studio/{thread_id}?notice=applied", status_code=303)


@router.post("/studio/{thread_id}/archive", response_class=HTMLResponse)
async def studio_thread_archive(request: Request, thread_id: int) -> Response:
    """Archive a thread without deleting its messages."""
    ctx = _require_studio(request)
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    with ctx.database.session() as db:
        scope = StudioThreadRepository(db).scope_for(thread_id)
        if scope is None:
            raise NotFound(f"studio thread {thread_id} does not exist")
        summary = profile_summary(db, scope)
        _studio_service(ctx, db).archive_thread(scope, thread_id)
    return RedirectResponse(f"/studio?profile={summary.slug}", status_code=303)


@router.get("/cost", response_class=HTMLResponse)
def cost_page(request: Request) -> HTMLResponse:
    """Show LLM cost rollups for the instance and each profile."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        summary = cost_summary(db)
    return _page(request, session, "cost.html", "cost", summary=summary)


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    profile: str | None = None,
    week: str | None = None,
) -> HTMLResponse:
    """Show one Monday-based week of scheduled and published items."""
    session = _session(request)
    context = _context(request)
    today = datetime.now(UTC).date()
    try:
        week_start = _parse_week(week) if week else _monday_of(today)
    except ValueError:
        week_start = _monday_of(today)
    with context.database.session() as db:
        timezone = None
        quiet = None
        if profile is not None:
            summary = next((s for s in ProfileCatalog(db).list() if s.slug == profile), None)
            if summary is not None:
                timezone = summary.timezone
                try:
                    quiet = load_policy(context.layout.profiles / profile).quiet_hours
                except (ProfileValidationError, OSError):
                    quiet = None
        week_data = calendar_week(
            db,
            context.redactor,
            week_start=week_start,
            timezone=timezone,
            profile=profile,
            quiet_hours=quiet,
        )
    return _page(request, session, "calendar.html", "calendar", week=week_data)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, limit: int = 100) -> HTMLResponse:
    """Show the newest audit entries across profiles."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        entries = list_audit_views(db, context.redactor, limit=limit)
    return _page(request, session, "audit.html", "audit", entries=entries)


@router.get("/profiles/{slug}/edit", response_class=HTMLResponse)
def profile_edit_page(request: Request, slug: str) -> HTMLResponse:
    """Show the profile text files in a validated editor."""
    session = _session(request)
    context = _context(request)
    try:
        state = read_profile_files(context.layout, slug)
    except ProfileEditError as exc:
        return _page(
            request,
            session,
            "error.html",
            "profiles",
            status_code=404,
            code="profile_not_found",
            message=context.redactor.redact(str(exc)) or "profile not found",
        )
    return _page(
        request,
        session,
        "profile_edit.html",
        "profiles",
        state=state,
        files=PROFILE_FILES,
        error=None,
        notice=None,
    )


@router.post("/profiles/{slug}/edit", response_class=HTMLResponse)
async def profile_edit_save(request: Request, slug: str) -> Response:
    """Validate and save the four profile files, then reload the profile."""
    session = _session(request)
    form = await _form(request)
    _require_csrf(_auth(request), session, form)
    submitted = {name: _form_value(form, name) for name in PROFILE_FILES}
    context = _context(request)
    try:
        with context.database.session() as db:
            save_profile_files(db, context.layout, slug, submitted)
    except ProfileEditError as exc:
        state = ProfileEditState(slug=slug, files=submitted)
        return _page(
            request,
            session,
            "profile_edit.html",
            "profiles",
            status_code=400,
            state=state,
            files=PROFILE_FILES,
            error=context.redactor.redact(str(exc)) or "profile edit failed",
            notice=None,
        )
    return RedirectResponse("/profiles?ok=edited", status_code=303)


@router.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request) -> HTMLResponse:
    """List profile metadata and queue counts. No profile file bodies."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        profiles = profiles_payload(db)
    return _page(request, session, "profiles.html", "profiles", profiles=profiles)


def _notice(code: str | None) -> str | None:
    return {
        "approved": "Item approved.",
        "rejected": "Item rejected.",
        "replied": "Reply sent.",
        "ignored": "Item ignored.",
        "generated": "Report generated.",
        "drafted": "Draft created and queued for approval.",
        "blocked": "Draft created but blocked; see the reason in the queue.",
        "applied": "Profile patch applied and the profile reloaded.",
    }.get(code or "")
