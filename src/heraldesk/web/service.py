"""Shared read models and actions for the REST API and the Web UI.

Everything here delegates to existing domain services. No state machine, approval,
publishing or report logic is duplicated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from heraldesk import __version__, paths
from heraldesk.analyst import (
    ReportData,
    build_report,
    monthly_period,
    render_markdown,
    weekly_period,
    write_report,
)
from heraldesk.community import (
    IgnoreReason,
    InboxItemView,
    InboxStatus,
    ignore_item,
    list_items,
    reply_item,
)
from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import (
    ApprovalLocator,
    ApprovalRepository,
    AuditRepository,
    ContentItemRepository,
    InboxRepository,
    ProfileCatalog,
    ProfileRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import (
    ApprovalOutcome,
    approve,
    as_utc,
    list_queue,
    reject,
    scope_for_content_item,
)
from heraldesk.platforms import publish_adapter_for
from heraldesk.platforms.base import PlatformAdapter, PublishError
from heraldesk.publisher import PublishOutcome, publish_item
from heraldesk.telegram.formatting import mask_chat_id, mask_sender_id, preview
from heraldesk.telegram.runtime import notifier_for
from heraldesk.web.context import AdapterFactory, WebContext
from heraldesk.web.errors import BadRequest, NotFound, WebError

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
QUEUE_FILTERS = ("pending", "approved", "rejected", "expired", "all")
INBOX_STATUSES = ("pending", "handled", "ignored")
REPORT_FILENAME = re.compile(r"^(?:weekly-\d{4}-W\d{2}|monthly-\d{4}-\d{2})\.md$")
PREVIEW_LIMIT = 180


@dataclass(frozen=True, slots=True)
class QueueView:
    """One queue entry safe for API/HTML output."""

    approval_id: int
    content_item_id: int
    profile_slug: str
    platform: str
    state: str
    approval_state: str
    risk_level: str | None
    preview: str
    reason: str | None
    expires_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "content_item_id": self.content_item_id,
            "profile": self.profile_slug,
            "platform": self.platform,
            "state": self.state,
            "approval_state": self.approval_state,
            "risk_level": self.risk_level,
            "preview": self.preview,
            "reason": self.reason,
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class QueueDetailView:
    """Full redacted content for one approval, used before a human decides."""

    approval_id: int
    content_item_id: int
    profile_slug: str
    platform: str
    state: str
    approval_state: str
    risk_level: str | None
    body: str
    reason: str | None
    expires_at: datetime | None
    created_at: datetime | None
    scheduled_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "content_item_id": self.content_item_id,
            "profile": self.profile_slug,
            "platform": self.platform,
            "state": self.state,
            "approval_state": self.approval_state,
            "risk_level": self.risk_level,
            "body": self.body,
            "reason": self.reason,
            "expires_at": None if self.expires_at is None else self.expires_at.isoformat(),
            "created_at": None if self.created_at is None else self.created_at.isoformat(),
            "scheduled_at": (None if self.scheduled_at is None else self.scheduled_at.isoformat()),
        }


@dataclass(frozen=True, slots=True)
class InboxView:
    """One inbox item safe for API/HTML output."""

    id: int
    profile_slug: str | None
    chat: str
    sender: str
    preview: str
    command: str | None
    content_item_id: int | None
    status: str
    ignore_reason: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile_slug,
            "chat": self.chat,
            "sender": self.sender,
            "preview": self.preview,
            "command": self.command,
            "content_item_id": self.content_item_id,
            "status": self.status,
            "ignore_reason": self.ignore_reason,
            "created_at": self.created_at.isoformat(),
        }


def _queue_counts(raw: dict[str, int]) -> dict[str, int]:
    counts = {state: raw.get(state, 0) for state in QUEUE_COUNT_STATES}
    counts["total"] = sum(counts.values())
    return counts


def build_status(
    session: Session,
    *,
    db_ok: bool,
    revision: str | None,
    telegram_active: bool,
    telegram_configured: bool,
    telegram_mode: str,
    allowed_chats: int,
) -> dict[str, Any]:
    """Return the shared status payload used by ``/status`` and ``/api/v1/status``."""
    totals = {state: 0 for state in QUEUE_COUNT_STATES}
    profiles: list[dict[str, Any]] = []
    last_run: dict[str, Any] | None = None
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
            "created_at": as_utc(latest.created_at).isoformat(),
            "payload": latest.payload,
        }
    return {
        "status": "ok" if db_ok else "error",
        "version": __version__,
        "db": {"ok": db_ok, "revision": revision},
        "profiles": profiles,
        "queue": _queue_counts(totals),
        "scheduler": {
            "last_run_at": None if last_run is None else last_run["created_at"],
            "last_run": last_run,
        },
        "telegram": {
            "enabled": telegram_active,
            "configured": telegram_configured,
            "mode": telegram_mode,
            "allowed_chats": allowed_chats,
        },
    }


def list_queue_views(
    session: Session,
    redactor: SecretRegistry,
    *,
    status: str = "pending",
    limit: int = 50,
) -> list[QueueView]:
    """Return approval-backed queue entries across profiles, newest first."""
    selected = status.strip().lower()
    if selected not in QUEUE_FILTERS:
        raise BadRequest(f"status must be one of: {', '.join(QUEUE_FILTERS)}", "invalid_status")
    if limit < 1 or limit > 200:
        raise BadRequest("limit must be between 1 and 200", "invalid_limit")

    views: list[QueueView] = []
    for summary in ProfileCatalog(session).list():
        entries = list_queue(session, ProfileScope(summary.id), limit=200)
        for entry in entries:
            if entry.approval_id is None or entry.approval_state is None:
                continue
            if selected != "all" and entry.approval_state != selected:
                continue
            views.append(
                QueueView(
                    approval_id=entry.approval_id,
                    content_item_id=entry.item_id,
                    profile_slug=summary.slug,
                    platform=entry.platform,
                    state=entry.item_state,
                    approval_state=entry.approval_state,
                    risk_level=entry.risk_level,
                    preview=preview(redactor.redact(entry.body), limit=PREVIEW_LIMIT),
                    reason=None if entry.reason is None else redactor.redact(entry.reason),
                    expires_at=entry.expires_at,
                )
            )
    views.sort(key=lambda view: view.approval_id, reverse=True)
    return views[:limit]


def list_inbox_views(
    session: Session,
    redactor: SecretRegistry,
    *,
    status: str = "pending",
    limit: int = 20,
) -> list[InboxView]:
    """Return inbox items through the existing triage service (which audits the read)."""
    selected = status.strip().lower()
    if selected not in INBOX_STATUSES:
        raise BadRequest(f"status must be one of: {', '.join(INBOX_STATUSES)}", "invalid_status")
    if limit < 1 or limit > 200:
        raise BadRequest("limit must be between 1 and 200", "invalid_limit")
    items = list_items(session, status=InboxStatus(selected), limit=limit)
    return [_inbox_view(item, redactor) for item in items]


def _inbox_view(item: InboxItemView, redactor: SecretRegistry) -> InboxView:
    return InboxView(
        id=item.id,
        profile_slug=item.profile_slug,
        chat=mask_chat_id(item.chat_id),
        sender=mask_sender_id(item.sender_id),
        preview=preview(redactor.redact(item.text), limit=PREVIEW_LIMIT),
        command=item.command,
        content_item_id=item.content_item_id,
        status=item.status,
        ignore_reason=item.ignore_reason,
        created_at=item.created_at,
    )


def _approval_target(session: Session, approval_id: int) -> tuple[ProfileScope, int]:
    resolved = ApprovalLocator(session).resolve(approval_id)
    if resolved is None:
        raise NotFound(f"approval {approval_id} does not exist")
    return resolved


def approve_by_id(
    session: Session,
    layout: paths.Layout,
    approval_id: int,
    *,
    comment: str | None = None,
    actor: str = "user",
) -> ApprovalOutcome:
    """Approve the content item behind an approval id."""
    scope, content_item_id = _approval_target(session, approval_id)
    return approve(
        session,
        layout,
        scope,
        content_item_id,
        actor=actor,
        channel="web",
        comment=comment,
    )


def reject_by_id(
    session: Session,
    approval_id: int,
    *,
    reason: str,
    actor: str = "user",
) -> ApprovalOutcome:
    """Reject the content item behind an approval id."""
    if not reason.strip():
        raise BadRequest("reason must not be empty", "reason_required")
    scope, content_item_id = _approval_target(session, approval_id)
    return reject(
        session,
        scope,
        content_item_id,
        reason=reason,
        actor=actor,
        channel="web",
    )


def reply_inbox(
    session: Session,
    context: WebContext,
    item_id: int,
    text: str,
) -> InboxView:
    """Send one manual inbox reply through the existing community service."""
    item = reply_item(
        session,
        context.layout,
        item_id,
        text,
        adapter=context.adapter,
        redactor=context.redactor,
    )
    return _inbox_view(item, context.redactor)


def ignore_inbox(
    session: Session,
    context: WebContext,
    item_id: int,
    reason: str,
) -> InboxView:
    """Ignore one inbox item with a bounded reason."""
    try:
        selected = IgnoreReason(reason)
    except ValueError as exc:
        raise BadRequest("reason must be spam, off-topic or other", "invalid_reason") from exc
    item = ignore_item(session, item_id, reason=selected)
    return _inbox_view(item, context.redactor)


def report_for(session: Session, kind: str, value: str | None) -> tuple[ReportData, str]:
    """Build a weekly or monthly report in memory without writing a file."""
    if kind == "weekly":
        period = weekly_period(value)
    elif kind == "monthly":
        period = monthly_period(value)
    else:  # pragma: no cover - callers pass a literal
        raise BadRequest("unknown report kind", "invalid_report_kind")
    data = build_report(session, period)
    return data, render_markdown(data)


def report_summary(data: ReportData) -> dict[str, Any]:
    """Return the numeric report summary used by the API and UI."""
    return {
        "period": data.period.label,
        "period_type": data.period.kind,
        "created": data.created,
        "approved": data.approved,
        "published": data.published,
        "failed": data.failed,
        "approval_rate": data.approval_rate,
        "total_cost_usd": data.total_cost_usd,
        "inbox_pending": data.inbox_pending,
        "inbox_handled": data.inbox_handled,
        "inbox_ignored": data.inbox_ignored,
    }


def generate_report(
    session: Session,
    layout: paths.Layout,
    kind: str,
    value: str | None,
) -> Path:
    """Write one report through the existing audited analyst service."""
    if kind == "weekly":
        period = weekly_period(value)
    elif kind == "monthly":
        period = monthly_period(value)
    else:  # pragma: no cover - callers pass a literal
        raise BadRequest("unknown report kind", "invalid_report_kind")
    return write_report(session, layout, period)


def list_report_files(layout: paths.Layout) -> list[dict[str, Any]]:
    """Return generated report files newest first, without reading their contents."""
    if not layout.reports.is_dir():
        return []
    files: list[dict[str, Any]] = []
    candidates = [
        path
        for path in layout.reports.iterdir()
        if path.is_file() and REPORT_FILENAME.fullmatch(path.name)
    ]
    for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
        stat = path.stat()
        files.append(
            {
                "filename": path.name,
                "kind": "weekly" if path.name.startswith("weekly-") else "monthly",
                "label": path.stem.split("-", 1)[1],
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            }
        )
    return files


def report_file(layout: paths.Layout, filename: str) -> Path:
    """Resolve a generated report filename safely, rejecting traversal and unknown names."""
    if not REPORT_FILENAME.fullmatch(filename):
        raise NotFound("report does not exist")
    path = layout.reports / filename
    try:
        resolved = path.resolve()
        reports = layout.reports.resolve()
    except OSError as exc:
        raise NotFound("report does not exist") from exc
    if resolved.parent != reports or not resolved.is_file():
        raise NotFound("report does not exist")
    return resolved


def read_report(layout: paths.Layout, filename: str) -> str:
    """Read one generated report, decoding errors as not found."""
    path = report_file(layout, filename)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NotFound("report cannot be read") from exc


def publish_content(
    session: Session,
    context: WebContext,
    content_item_id: int,
    *,
    actor: str = "web",
) -> PublishOutcome:
    """Publish one content item using the existing publisher and adapter routing."""
    scope = scope_for_content_item(session, content_item_id)
    if scope is None:
        raise NotFound(f"content item {content_item_id} does not exist")

    factory: AdapterFactory = context.adapter_factory or publish_adapter_for
    try:
        cipher = CredentialCipher.from_key_file(context.layout.key_path)
    except Exception as exc:
        raise WebError(
            500,
            "crypto_error",
            "publishing is not available; secret.key cannot be read",
        ) from exc

    item = ContentItemRepository(session).get(scope, content_item_id)
    if item is None:
        raise NotFound(f"content item {content_item_id} does not exist")
    adapter: PlatformAdapter | None
    try:
        adapter = factory(item.platform)
    except PublishError:
        adapter = None
    return publish_item(
        session,
        context.layout,
        scope,
        content_item_id,
        adapter=adapter,
        cipher=cipher,
        actor=actor,
        trigger="manual",
        notifier=notifier_for(session, context.layout),
    )


def inbox_counts(session: Session) -> dict[str, int]:
    """Return instance-wide inbox counts for the dashboard."""
    return InboxRepository(session).count_by_status()


def profiles_payload(session: Session) -> list[dict[str, Any]]:
    """Return profile metadata and queue counts for the profiles page."""
    rows: list[dict[str, Any]] = []
    repository = ContentItemRepository(session)
    for summary in ProfileCatalog(session).list():
        rows.append(
            {
                "slug": summary.slug,
                "name": summary.name,
                "status": summary.status,
                "kill_switch": summary.kill_switch,
                "queue": _queue_counts(repository.state_counts(ProfileScope(summary.id))),
            }
        )
    return rows


def dashboard_counts(session: Session) -> dict[str, Any]:
    """Return aggregate queue and inbox counts for the dashboard."""
    totals = {state: 0 for state in QUEUE_COUNT_STATES}
    repository = ContentItemRepository(session)
    for summary in ProfileCatalog(session).list():
        counts = repository.state_counts(ProfileScope(summary.id))
        for state in QUEUE_COUNT_STATES:
            totals[state] += counts.get(state, 0)
    return {
        "queue": _queue_counts(totals),
        "inbox": inbox_counts(session),
        "profiles": len(ProfileCatalog(session).list()),
    }


def latest_report_file(layout: paths.Layout) -> dict[str, Any] | None:
    """Return the newest generated report metadata, if any."""
    files = list_report_files(layout)
    return files[0] if files else None


def audit_login(session: Session, action: str, *, detail: str | None = None) -> None:
    """Record an authentication event without a token or full IP."""
    payload: dict[str, Any] = {}
    if detail is not None:
        payload["detail"] = detail
    AuditService(session).record(
        None,
        actor="user",
        action=action,
        entity="auth",
        payload=payload,
    )


__all__ = [
    "INBOX_STATUSES",
    "QUEUE_FILTERS",
    "InboxView",
    "QueueView",
    "approve_by_id",
    "audit_login",
    "build_status",
    "dashboard_counts",
    "generate_report",
    "ignore_inbox",
    "inbox_counts",
    "latest_report_file",
    "list_inbox_views",
    "list_queue_views",
    "list_report_files",
    "profiles_payload",
    "publish_content",
    "read_report",
    "reject_by_id",
    "reply_inbox",
    "report_file",
    "report_for",
    "report_summary",
]


def get_queue_detail(
    session: Session,
    redactor: SecretRegistry,
    approval_id: int,
) -> QueueDetailView:
    """Return the full redacted draft and approval state for one approval id."""
    resolved = ApprovalLocator(session).resolve(approval_id)
    if resolved is None:
        raise NotFound("approval not found")
    scope, content_item_id = resolved
    item = ContentItemRepository(session).get(scope, content_item_id)
    if item is None:
        raise NotFound("content item not found")
    approval = ApprovalRepository(session).get(scope, approval_id)
    profile = ProfileRepository(session).require(scope)
    reason = None
    if approval is not None and approval.reason:
        reason = approval.reason
    elif item.last_error:
        reason = item.last_error
    elif item.risk_notes:
        reason = item.risk_notes
    return QueueDetailView(
        approval_id=approval_id,
        content_item_id=content_item_id,
        profile_slug=profile.slug,
        platform=item.platform,
        state=item.state,
        approval_state=approval.state if approval is not None else "unknown",
        risk_level=item.risk_level,
        body=redactor.redact(item.body),
        reason=None if reason is None else redactor.redact(reason),
        expires_at=as_utc(approval.expires_at) if approval is not None else None,
        created_at=as_utc(item.created_at),
        scheduled_at=None if item.scheduled_at is None else as_utc(item.scheduled_at),
    )
