"""Server-rendered Web UI: login, dashboard, queue, inbox, reports and profiles.

Forms are normal POST/redirect forms. HTMX progressively enhances queue and inbox rows
by replacing the affected row; without JavaScript the same forms still work.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from heraldesk import __version__
from heraldesk.analyst import ReportError, monthly_period, weekly_period
from heraldesk.community import InboxError
from heraldesk.domain.errors import ApprovalError, ContentError
from heraldesk.web.auth import (
    LOGIN_CSRF_COOKIE,
    SESSION_COOKIE,
    AdminAuth,
    Forbidden,
    UISession,
    Unauthorized,
)
from heraldesk.web.context import WebContext
from heraldesk.web.errors import BadRequest, NotFound, WebError
from heraldesk.web.rendering import render_markdown
from heraldesk.web.service import (
    approve_by_id,
    audit_login,
    dashboard_counts,
    generate_report,
    get_queue_detail,
    ignore_inbox,
    latest_report_file,
    list_inbox_views,
    list_queue_views,
    list_report_files,
    profiles_payload,
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
    return _render(
        request,
        name,
        {
            "session": session,
            "csrf": session.csrf,
            "active": active,
            "version": __version__,
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
    response: Response = RedirectResponse("/", status_code=303)
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


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """Show queue counts, inbox counts, profiles and the newest report."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        counts = dashboard_counts(db)
    return _page(
        request,
        session,
        "dashboard.html",
        "dashboard",
        counts=counts,
        last_report=latest_report_file(context.layout),
    )


@router.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, ok: str | None = None) -> HTMLResponse:
    """List approval-backed queue entries with redacted previews."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        entries = list_queue_views(db, context.redactor, status="pending")
    return _page(
        request,
        session,
        "queue.html",
        "queue",
        entries=entries,
        notice=_notice(ok),
        error=None,
    )


@router.get("/queue/{approval_id}", response_class=HTMLResponse)
def queue_detail_page(request: Request, approval_id: int) -> HTMLResponse:
    """Show the full redacted draft before a human approves or rejects it."""
    session = _session(request)
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
    return _page(request, session, "queue_detail.html", "queue", detail=detail)


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
        return Response("")
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
        return Response("")
    return RedirectResponse("/queue?ok=rejected", status_code=303)


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
            {"error": message, "colspan": 7},
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
def inbox_page(request: Request, ok: str | None = None) -> HTMLResponse:
    """List pending inbox items with masked ids."""
    session = _session(request)
    context = _context(request)
    with context.database.session() as db:
        items = list_inbox_views(db, context.redactor, status="pending", limit=50)
    return _page(
        request,
        session,
        "inbox.html",
        "inbox",
        items=items,
        notice=_notice(ok),
        error=None,
    )


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
        return Response("")
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
        return Response("")
    return RedirectResponse("/inbox?ok=ignored", status_code=303)


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
            {"error": message, "colspan": 5},
            status_code=200,
        )
    with context.database.session() as db:
        items = list_inbox_views(db, context.redactor, status="pending", limit=50)
    return _page(
        request,
        session,
        "inbox.html",
        "inbox",
        items=items,
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
    }.get(code or "")
