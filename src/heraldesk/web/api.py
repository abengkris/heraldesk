"""Authenticated JSON API: ``/api/v1``.

Every route delegates to an existing domain service and requires
``Authorization: Bearer <admin token>``. Errors use one shape: ``{"error", "code"}``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from heraldesk.analyst import ReportError
from heraldesk.community import InboxError, InboxItemNotFoundError, InboxStateError
from heraldesk.db.engine import inspect_database
from heraldesk.domain.errors import ApprovalError, ContentError
from heraldesk.platforms.base import PublishError
from heraldesk.web.auth import AdminAuth, Unauthorized
from heraldesk.web.context import WebContext
from heraldesk.web.errors import BadRequest, Conflict, NotFound, WebError
from heraldesk.web.service import (
    approve_by_id,
    build_status,
    ignore_inbox,
    list_inbox_views,
    list_queue_views,
    publish_content,
    reject_by_id,
    reply_inbox,
    report_for,
    report_summary,
)

router = APIRouter(prefix="/api/v1")


def require_api_auth(request: Request) -> None:
    """Require a Bearer token. Cookies are deliberately not accepted for the API."""
    auth = _auth(request)
    if not auth.verify_bearer(request.headers.get("authorization")):
        raise Unauthorized("a valid Bearer admin token is required")


def _auth(request: Request) -> AdminAuth:
    auth: AdminAuth = request.app.state.auth
    return auth


def _context(request: Request) -> WebContext:
    context: WebContext = request.app.state.context
    return context


def _redact(context: WebContext, message: str) -> str:
    return context.redactor.redact(message)


def _domain_error(context: WebContext, exc: Exception) -> WebError:
    if isinstance(exc, WebError):
        return exc
    message = _redact(context, str(exc)) or "request failed"
    if isinstance(exc, PublishError):
        return WebError(502, "publish_failed", message)
    if isinstance(exc, InboxItemNotFoundError):
        return NotFound(message)
    if isinstance(exc, InboxStateError):
        return Conflict(message, "invalid_state")
    return BadRequest(message, "request_failed")


class ApproveBody(BaseModel):
    comment: str | None = Field(default=None, max_length=500)


class RejectBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class IgnoreBody(BaseModel):
    reason: Literal["spam", "off-topic", "other"] = "other"


dependencies = [Depends(require_api_auth)]


@router.get("/status", dependencies=dependencies)
def api_status(request: Request) -> dict[str, Any]:
    """Health, version and queue counts. No secrets."""
    context = _context(request)
    reachable = context.database.is_healthy()
    revision = inspect_database(context.layout.db_path).revision if reachable else None
    with context.database.session() as session:
        return build_status(
            session,
            db_ok=reachable and revision is not None,
            revision=revision,
            telegram_active=context.telegram_active,
            telegram_configured=context.config.telegram.enabled,
            telegram_mode=context.config.telegram.mode,
            allowed_chats=len(context.allowed_chat_ids),
        )


@router.get("/queue", dependencies=dependencies)
def api_queue(
    request: Request,
    status: Annotated[str, Query()] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """List approval-backed queue entries with redacted previews."""
    context = _context(request)
    with context.database.session() as session:
        views = list_queue_views(session, context.redactor, status=status, limit=limit)
        return {"items": [view.as_dict() for view in views], "count": len(views)}


@router.post("/approvals/{approval_id}/approve", dependencies=dependencies)
def api_approve(
    request: Request,
    approval_id: int,
    body: ApproveBody | None = None,
) -> dict[str, Any]:
    """Approve one pending content item by approval id."""
    context = _context(request)
    with context.database.session() as session:
        try:
            outcome = approve_by_id(
                session,
                context.layout,
                approval_id,
                comment=None if body is None else body.comment,
                actor="user",
            )
        except (ApprovalError, ContentError) as exc:
            raise _domain_error(context, exc) from exc
        return {
            "approval_id": outcome.approval_id,
            "content_item_id": outcome.content_item_id,
            "status": "approved",
            "to_state": outcome.to_state,
        }


@router.post("/approvals/{approval_id}/reject", dependencies=dependencies)
def api_reject(
    request: Request,
    approval_id: int,
    body: RejectBody,
) -> dict[str, Any]:
    """Reject one pending content item by approval id."""
    context = _context(request)
    with context.database.session() as session:
        try:
            outcome = reject_by_id(
                session,
                approval_id,
                reason=body.reason,
                actor="user",
            )
        except (ApprovalError, ContentError) as exc:
            raise _domain_error(context, exc) from exc
        return {
            "approval_id": outcome.approval_id,
            "content_item_id": outcome.content_item_id,
            "status": "rejected",
            "to_state": outcome.to_state,
        }


@router.get("/inbox", dependencies=dependencies)
def api_inbox(
    request: Request,
    status: Annotated[str, Query()] = "pending",
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> dict[str, Any]:
    """List inbox items with masked chat ids and redacted previews."""
    context = _context(request)
    with context.database.session() as session:
        try:
            views = list_inbox_views(session, context.redactor, status=status, limit=limit)
        except (InboxError, WebError) as exc:
            raise _domain_error(context, exc) from exc
        return {"items": [view.as_dict() for view in views], "count": len(views)}


@router.post("/inbox/{item_id}/reply", dependencies=dependencies)
def api_reply(request: Request, item_id: int, body: ReplyBody) -> dict[str, Any]:
    """Send one manual reply through the existing Telegram adapter."""
    context = _context(request)
    with context.database.session() as session:
        try:
            view = reply_inbox(session, context, item_id, body.text)
        except (InboxError, WebError) as exc:
            raise _domain_error(context, exc) from exc
        return {"item": view.as_dict()}


@router.post("/inbox/{item_id}/ignore", dependencies=dependencies)
def api_ignore(request: Request, item_id: int, body: IgnoreBody) -> dict[str, Any]:
    """Ignore one inbox item with a bounded reason."""
    context = _context(request)
    with context.database.session() as session:
        try:
            view = ignore_inbox(session, context, item_id, body.reason)
        except (InboxError, WebError) as exc:
            raise _domain_error(context, exc) from exc
        return {"item": view.as_dict()}


@router.get("/reports/weekly", dependencies=dependencies)
def api_weekly_report(
    request: Request,
    week: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Build the weekly report in memory and return markdown plus summary counts."""
    context = _context(request)
    with context.database.session() as session:
        try:
            data, markdown = report_for(session, "weekly", week)
        except (ReportError, WebError) as exc:
            raise _domain_error(context, exc) from exc
        return {"summary": report_summary(data), "markdown": markdown}


@router.get("/reports/monthly", dependencies=dependencies)
def api_monthly_report(
    request: Request,
    month: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Build the monthly report in memory and return markdown plus summary counts."""
    context = _context(request)
    with context.database.session() as session:
        try:
            data, markdown = report_for(session, "monthly", month)
        except (ReportError, WebError) as exc:
            raise _domain_error(context, exc) from exc
        return {"summary": report_summary(data), "markdown": markdown}


@router.post("/publish/{content_item_id}", dependencies=dependencies)
def api_publish(request: Request, content_item_id: int) -> dict[str, Any]:
    """Publish one content item through the existing publisher."""
    context = _context(request)
    with context.database.session() as session:
        try:
            outcome = publish_content(session, context, content_item_id, actor="user")
        except WebError:
            raise
        except (ContentError, PublishError) as exc:
            raise _domain_error(context, exc) from exc
        if outcome.status.value == "failed":
            raise WebError(502, "publish_failed", outcome.error or "publish failed")
        if outcome.status.value == "skipped":
            raise Conflict(outcome.reason or "item is not publishable")
        return {
            "content_item_id": outcome.item_id,
            "status": outcome.status.value,
            "external_id": outcome.external_id,
            "attempts": outcome.attempts,
        }
