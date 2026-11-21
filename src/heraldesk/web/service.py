"""Shared read models and actions for the REST API and the Web UI.

Everything here delegates to existing domain services. No state machine, approval,
publishing or report logic is duplicated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
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
from heraldesk.db.models import AgentRun, AuditLog
from heraldesk.db.repositories import (
    AgentRunRepository,
    ApprovalLocator,
    ApprovalRepository,
    AuditCatalog,
    AuditRepository,
    ContentItemRepository,
    GlobalCostRepository,
    InboxRepository,
    ProfileCatalog,
    ProfileRepository,
    SocialAccountRepository,
    StudioMessageRepository,
    StudioThreadRepository,
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
from heraldesk.domain.autonomy import loosens_autonomy
from heraldesk.domain.errors import ProfileValidationError
from heraldesk.domain.policy import AutonomyPolicy
from heraldesk.domain.profiles import load_policy, load_profile
from heraldesk.platforms import publish_adapter_for
from heraldesk.platforms.base import PlatformAdapter, PublishError, constraints_for
from heraldesk.publisher import PublishOutcome, publish_item
from heraldesk.telegram.formatting import mask_chat_id, mask_sender_id, preview
from heraldesk.telegram.runtime import notifier_for
from heraldesk.web.context import AdapterFactory, WebContext
from heraldesk.web.errors import BadRequest, NotFound, WebError

STATE_SEMANTIC = {
    "idea": "info",
    "drafted": "muted",
    "reviewed": "info",
    "pending_approval": "warn",
    "approved": "ok",
    "scheduled": "info",
    "published": "ok",
    "rejected": "danger",
    "failed": "danger",
    "blocked": "danger",
    "cancelled": "muted",
    "needs_review": "warn",
    "pending": "warn",
    "handled": "ok",
    "ignored": "muted",
}

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
    # UI-only fields (not serialized by as_dict, so the JSON API is unchanged).
    scheduled_at: datetime | None = None
    created_at: datetime | None = None
    account_handle: str | None = None

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
class CalendarView:
    """One scheduled or published item in the calendar window."""

    content_item_id: int
    profile_slug: str
    platform: str
    state: str
    when: datetime
    preview: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "content_item_id": self.content_item_id,
            "profile": self.profile_slug,
            "platform": self.platform,
            "state": self.state,
            "when": self.when.isoformat(),
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class AuditView:
    """One audit entry safe for the UI and API."""

    id: int
    profile_slug: str | None
    actor: str
    action: str
    entity: str
    entity_id: str | None
    payload: str
    created_at: datetime
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile_slug,
            "actor": self.actor,
            "action": self.action,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
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
    # UI-only fields (not serialized by as_dict, so the JSON API is unchanged).
    account_handle: str | None = None
    max_characters: int | None = None
    compliance_notes: str | None = None
    lint_findings: list[str] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)

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
    offset: int = 0,
    profile: str | None = None,
) -> list[QueueView]:
    """Return approval-backed queue entries across profiles, newest first."""
    selected = status.strip().lower()
    if selected not in QUEUE_FILTERS:
        raise BadRequest(f"status must be one of: {', '.join(QUEUE_FILTERS)}", "invalid_status")
    if limit < 1 or limit > 200:
        raise BadRequest("limit must be between 1 and 200", "invalid_limit")
    if offset < 0:
        raise BadRequest("offset must not be negative", "invalid_offset")
    summaries = ProfileCatalog(session).list()
    if profile is not None:
        summaries = [summary for summary in summaries if summary.slug == profile]
        if not summaries:
            raise NotFound(f"profile '{profile}' is not registered")

    views: list[QueueView] = []
    items_repo = ContentItemRepository(session)
    for summary in summaries:
        scope = ProfileScope(summary.id)
        accounts = {a.id: a.handle for a in SocialAccountRepository(session).list(scope)}
        entries = list_queue(session, scope, limit=200)
        for entry in entries:
            if entry.approval_id is None or entry.approval_state is None:
                continue
            if selected != "all" and entry.approval_state != selected:
                continue
            item = items_repo.get(scope, entry.item_id)
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
                    scheduled_at=(
                        None
                        if item is None or item.scheduled_at is None
                        else as_utc(item.scheduled_at)
                    ),
                    created_at=None if item is None else as_utc(item.created_at),
                    account_handle=(
                        None
                        if item is None or item.account_id is None
                        else accounts.get(item.account_id)
                    ),
                )
            )
    views.sort(key=lambda view: view.approval_id, reverse=True)
    return views[offset : offset + limit]


def list_inbox_views(
    session: Session,
    redactor: SecretRegistry,
    *,
    status: str = "pending",
    limit: int = 20,
    offset: int = 0,
) -> list[InboxView]:
    """Return inbox items through the existing triage service (which audits the read)."""
    selected = status.strip().lower()
    if selected not in INBOX_STATUSES:
        raise BadRequest(f"status must be one of: {', '.join(INBOX_STATUSES)}", "invalid_status")
    if limit < 1 or limit > 200:
        raise BadRequest("limit must be between 1 and 200", "invalid_limit")
    if offset < 0:
        raise BadRequest("offset must not be negative", "invalid_offset")
    items = list_items(
        session,
        status=InboxStatus(selected),
        limit=limit,
        offset=offset,
    )
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


def get_inbox_detail(session: Session, redactor: SecretRegistry, item_id: int) -> dict[str, Any]:
    """Return one inbox item with its full redacted text, for the review panel."""
    message = InboxRepository(session).get(item_id)
    if message is None:
        raise NotFound("inbox item not found")
    slugs = {profile.id: profile.slug for profile in ProfileCatalog(session).list()}
    return {
        "id": message.id,
        "profile_slug": slugs.get(message.profile_id) if message.profile_id is not None else None,
        "chat": mask_chat_id(message.chat_id),
        "sender": mask_sender_id(message.sender_id),
        "text": redactor.redact(message.text),
        "command": message.command,
        "status": message.status,
        "ignore_reason": message.ignore_reason,
        "created_at": as_utc(message.created_at),
    }


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
        modified = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        files.append(
            {
                "filename": path.name,
                "kind": "weekly" if path.name.startswith("weekly-") else "monthly",
                "label": path.stem.split("-", 1)[1],
                "size": stat.st_size,
                "modified": modified.isoformat(),
                "modified_label": modified.strftime("%Y-%m-%d %H:%M"),
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


COST_WINDOW_DAYS = (7, 14, 30)


@dataclass(frozen=True, slots=True)
class CostDayRow:
    """One profile's spend on one UTC day."""

    day: str
    profile_slug: str
    runs: int
    tokens_in: int
    tokens_out: int
    cost_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "profile": self.profile_slug,
            "runs": self.runs,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
        }


def cost_breakdown(
    session: Session,
    *,
    days: int = 14,
    profile: str | None = None,
    now: datetime | None = None,
    cap_usd: float | None = None,
) -> dict[str, Any]:
    """Return per-day, per-profile cost rows for a rolling UTC window.

    The window includes today and ``days - 1`` earlier UTC days. ``days`` must be one of
    :data:`COST_WINDOW_DAYS`; anything else falls back to 14.
    """
    window = days if days in COST_WINDOW_DAYS else 14
    current = as_utc(datetime.now(UTC) if now is None else now)
    today = datetime.combine(current.date(), datetime.min.time(), tzinfo=UTC)
    summaries = ProfileCatalog(session).list()
    if profile is not None:
        summaries = [summary for summary in summaries if summary.slug == profile]

    repository = AgentRunRepository(session)
    rows: list[CostDayRow] = []
    total_in = total_out = total_runs = 0
    total_cost = 0.0
    for offset in range(window):
        day_start = today - timedelta(days=offset)
        day_end = day_start + timedelta(days=1)
        for summary in summaries:
            totals = repository.totals_since(ProfileScope(summary.id), day_start, day_end)
            if totals.runs == 0:
                continue
            rows.append(
                CostDayRow(
                    day=day_start.date().isoformat(),
                    profile_slug=summary.slug,
                    runs=totals.runs,
                    tokens_in=totals.tokens_in,
                    tokens_out=totals.tokens_out,
                    cost_usd=totals.cost_usd,
                )
            )
            total_in += totals.tokens_in
            total_out += totals.tokens_out
            total_runs += totals.runs
            total_cost += totals.cost_usd

    cap: dict[str, Any] | None = None
    if cap_usd is not None and cap_usd > 0:
        spent = GlobalCostRepository(session).totals_since(today).cost_usd
        percent = spent / cap_usd * 100.0
        if percent > 100.0:
            state = "danger"
        elif percent >= 80.0:
            state = "warn"
        else:
            state = "ok"
        cap = {
            "spent": spent,
            "cap": cap_usd,
            "percent": round(percent, 1),
            "state": state,
        }

    return {
        "days": window,
        "timezone": "UTC",
        "as_of": current.isoformat(),
        "window_start": (today - timedelta(days=window - 1)).date().isoformat(),
        "today": today.date().isoformat(),
        "rows": [row.as_dict() for row in rows],
        "max_cost": max((row.cost_usd for row in rows), default=0.0) or 1.0,
        "total": {
            "runs": total_runs,
            "tokens_in": total_in,
            "tokens_out": total_out,
            "cost_usd": total_cost,
        },
        "cap": cap,
    }


def publish_content(
    session: Session,
    context: WebContext,
    content_item_id: int,
    *,
    actor: str = "user",
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


def profiles_overview(session: Session, profiles_root: Path) -> list[dict[str, Any]]:
    """Return profile metadata, queue counts and a read-only autonomy summary.

    A policy that cannot be loaded is reported as ``unknown`` instead of failing the page.
    """
    rows: list[dict[str, Any]] = []
    repository = ContentItemRepository(session)
    for summary in ProfileCatalog(session).list():
        try:
            policy: AutonomyPolicy | None = load_policy(profiles_root / summary.slug)
        except (ProfileValidationError, OSError):
            policy = None
        rows.append(
            {
                "slug": summary.slug,
                "name": summary.name,
                "type": summary.profile_type,
                "status": summary.status,
                "kill_switch": summary.kill_switch,
                "autonomy": _autonomy_summary(policy),
                "queue": _queue_counts(repository.state_counts(ProfileScope(summary.id))),
            }
        )
    return rows


def _autonomy_summary(policy: AutonomyPolicy | None) -> str:
    if policy is None:
        return "unknown"
    mode = policy.default_mode.value
    if policy.rules:
        return f"{mode} ({len(policy.rules)} rules)"
    return mode


def studio_thread_overview(session: Session, *, profile: str | None = None) -> list[dict[str, Any]]:
    """Return open and archived threads grouped by profile, with artifact counts."""
    groups: list[dict[str, Any]] = []
    thread_repository = StudioThreadRepository(session)
    message_repository = StudioMessageRepository(session)
    for summary in ProfileCatalog(session).list():
        if profile is not None and summary.slug != profile:
            continue
        scope = ProfileScope(summary.id)
        threads = thread_repository.list_threads(scope, limit=100)
        rows: list[dict[str, Any]] = []
        for thread in threads:
            messages = message_repository.list_messages(scope, thread.id, limit=200)
            rows.append(
                {
                    "thread": thread,
                    "message_count": len(messages),
                    "artifact_count": sum(1 for message in messages if message.artifact_type),
                }
            )
        groups.append(
            {
                "profile": summary.slug,
                "open": [row for row in rows if row["thread"].status == "open"],
                "archived": [row for row in rows if row["thread"].status == "archived"],
            }
        )
    return groups


def studio_thread_status(
    session: Session,
    scope: ProfileScope,
    thread_id: int,
    redactor: SecretRegistry,
) -> dict[str, Any]:
    """Return the latest agent-step state for a thread, if one exists."""
    messages = StudioMessageRepository(session).list_messages(scope, thread_id, limit=200)
    run_ids = [message.agent_run_id for message in messages if message.agent_run_id is not None]
    if not run_ids:
        return {"state": "idle", "error": None}
    run = session.get(AgentRun, run_ids[-1])
    if run is None:
        return {"state": "idle", "error": None}
    if run.status == "running":
        return {"state": "running", "error": None}
    if run.status == "failed":
        return {
            "state": "failed",
            "error": redactor.redact(run.error or "")[:500] or "the agent step failed",
        }
    return {"state": "finished", "error": None}


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
    "loosens_autonomy",
    "profiles_overview",
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
    accounts = {a.id: a.handle for a in SocialAccountRepository(session).list(scope)}
    reason = None
    if approval is not None and approval.reason:
        reason = approval.reason
    elif item.last_error:
        reason = item.last_error
    elif item.risk_notes:
        reason = item.risk_notes
    constraints = constraints_for(item.platform)
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
        account_handle=accounts.get(item.account_id) if item.account_id else None,
        max_characters=constraints.max_characters if constraints is not None else None,
        compliance_notes=None if item.risk_notes is None else redactor.redact(item.risk_notes),
        lint_findings=content_lint_findings(session, content_item_id),
        history=content_state_history(session, content_item_id),
    )


def content_state_history(session: Session, item_id: int) -> list[dict[str, str]]:
    """Return the state-transition history for one content item, oldest first.

    Read-only: it only reads the append-only audit log. Lint findings are excluded here.
    """
    rows = (
        session.execute(
            select(AuditLog)
            .where(AuditLog.entity == "content_item", AuditLog.entity_id == str(item_id))
            .order_by(AuditLog.id.asc())
        )
        .scalars()
        .all()
    )
    history: list[dict[str, str]] = []
    for row in rows:
        payload = row.payload or {}
        if row.action == "content.state_changed":
            history.append(
                {
                    "action": row.action,
                    "from_state": str(payload.get("from_state", "")),
                    "to_state": str(payload.get("to_state", "")),
                    "at": as_utc(row.created_at).isoformat(timespec="seconds"),
                }
            )
        else:
            history.append(
                {
                    "action": row.action,
                    "from_state": "",
                    "to_state": "",
                    "at": as_utc(row.created_at).isoformat(timespec="seconds"),
                }
            )
    return history


def content_lint_findings(session: Session, item_id: int) -> list[str]:
    """Return lint failure summaries recorded for one content item, if any."""
    rows = (
        session.execute(
            select(AuditLog).where(
                AuditLog.entity == "content_item",
                AuditLog.entity_id == str(item_id),
                AuditLog.action == "content.lint_failed",
            )
        )
        .scalars()
        .all()
    )
    findings: list[str] = []
    for row in rows:
        payload = row.payload or {}
        rules = payload.get("rules")
        if isinstance(rules, list):
            findings.extend(str(rule) for rule in rules)
        elif isinstance(rules, str):
            findings.append(rules)
    return findings


def profile_mode(layout: paths.Layout, slug: str | None) -> str | None:
    """Return the autonomy default mode label for one profile, or None."""
    if not slug:
        return None
    try:
        definition = load_profile(layout.profiles / slug)
    except (OSError, ValueError):
        return None
    return definition.policy.default_mode.value


def dashboard_needs_attention(
    session: Session,
    redactor: SecretRegistry,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Read-only summary of what needs a human's review.

    Returns only existing domain data, no new metrics. Every entry links back to its
    item or list; nothing here is a chart or a vanity number.
    """
    summaries = ProfileCatalog(session).list()
    if profile is not None:
        summaries = [summary for summary in summaries if summary.slug == profile]
    items = ContentItemRepository(session)
    now = datetime.now(UTC)
    blocked: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    pending = 0
    oldest_pending: datetime | None = None
    next_scheduled: dict[str, Any] | None = None

    for summary in summaries:
        scope = ProfileScope(summary.id)
        for item in items.list(scope, states=("blocked", "failed"), limit=200):
            row = {
                "content_item_id": item.id,
                "platform": item.platform,
                "profile_slug": summary.slug,
                "preview": preview(redactor.redact(item.body), limit=120),
            }
            if item.state == "blocked":
                blocked.append(row)
            elif item.state == "failed":
                failed.append(row)
        for item in items.list(scope, states=("pending_approval",), limit=200):
            pending += 1
            created = as_utc(item.created_at)
            if oldest_pending is None or created < oldest_pending:
                oldest_pending = created
        for item in items.list(scope, states=("scheduled",), limit=200):
            if item.scheduled_at is None:
                continue
            when = as_utc(item.scheduled_at)
            if when < now:
                continue
            if next_scheduled is None or when < as_utc(next_scheduled["when"]):
                next_scheduled = {
                    "content_item_id": item.id,
                    "platform": item.platform,
                    "profile_slug": summary.slug,
                    "when": when,
                }

    oldest_age: str | None = None
    if oldest_pending is not None:
        delta = now - oldest_pending
        minutes = int(delta.total_seconds() // 60)
        oldest_age = f"{minutes}m" if minutes < 120 else f"{minutes // 60}h"

    return {
        "blocked": blocked,
        "failed": failed,
        "pending_approval": pending,
        "oldest_pending_age": oldest_age,
        "inbox_pending": inbox_counts(session).get("pending", 0),
        "next_scheduled": next_scheduled,
    }


AUDIT_PAYLOAD_LIMIT = 4000
AUDIT_PAGE_SIZE = 50
AUDIT_MAX_OFFSET = 5000


@dataclass(frozen=True, slots=True)
class AuditPage:
    """One page of audit entries with stable offset pagination."""

    entries: list[AuditView]
    offset: int
    page_size: int
    has_next: bool
    has_prev: bool


def list_audit_views(
    session: Session,
    redactor: SecretRegistry,
    *,
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
) -> list[AuditView]:
    """Return newest audit entries with redacted, truncated payloads."""
    if limit < 1 or limit > 500:
        raise BadRequest("limit must be between 1 and 500", "invalid_limit")
    if offset < 0:
        raise BadRequest("offset must not be negative", "invalid_offset")
    return _audit_views(
        AuditCatalog(session).recent(limit=limit, offset=offset, action=action),
        session,
        redactor,
    )


def list_audit_page(
    session: Session,
    redactor: SecretRegistry,
    *,
    actor: str | None = None,
    action: str | None = None,
    entity: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    offset: int = 0,
    page_size: int = AUDIT_PAGE_SIZE,
) -> AuditPage:
    """Return one filtered audit page, newest first, with stable offset paging."""
    size = min(max(page_size, 1), AUDIT_PAGE_SIZE)
    start = min(max(offset, 0), AUDIT_MAX_OFFSET)
    action_value = action.strip() if action else None
    action_prefix = action_value is not None and action_value.endswith("*")
    if action_prefix and action_value is not None:
        action_value = action_value[:-1]
    rows = AuditCatalog(session).recent(
        limit=size + 1,
        offset=start,
        action=action_value or None,
        action_prefix=action_prefix,
        actor=actor or None,
        entity=entity or None,
        since=since,
        until=until,
    )
    has_next = len(rows) > size
    entries = _audit_views(
        rows[:size], session, redactor, pretty=True, payload_limit=AUDIT_PAYLOAD_LIMIT
    )
    return AuditPage(
        entries=entries,
        offset=start,
        page_size=size,
        has_next=has_next,
        has_prev=start > 0,
    )


def _audit_views(
    rows: list[AuditLog],
    session: Session,
    redactor: SecretRegistry,
    *,
    pretty: bool = False,
    payload_limit: int = 500,
) -> list[AuditView]:
    slugs = {profile.id: profile.slug for profile in ProfileCatalog(session).list()}
    views: list[AuditView] = []
    for entry in rows:
        payload = json.dumps(
            entry.payload,
            ensure_ascii=True,
            sort_keys=True,
            indent=2 if pretty else None,
        )
        redacted = redactor.redact(payload)
        truncated = len(redacted) > payload_limit
        views.append(
            AuditView(
                id=entry.id,
                profile_slug=None if entry.profile_id is None else slugs.get(entry.profile_id),
                actor=entry.actor,
                action=entry.action,
                entity=entry.entity,
                entity_id=entry.entity_id,
                payload=redacted[:payload_limit],
                created_at=as_utc(entry.created_at),
                truncated=truncated,
            )
        )
    return views


@dataclass(frozen=True, slots=True)
class ProfileCostView:
    """Cost rollup for one profile."""

    profile_slug: str
    runs_today: int
    cost_today: float
    cost_7d: float
    cost_30d: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile_slug,
            "runs_today": self.runs_today,
            "cost_today": self.cost_today,
            "cost_7d": self.cost_7d,
            "cost_30d": self.cost_30d,
        }


def cost_summary(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """Return instance and per-profile LLM cost rollups. Read-only."""
    current = as_utc(datetime.now(UTC) if now is None else now)
    day_start = datetime(current.year, current.month, current.day, tzinfo=UTC)
    week_start = day_start - timedelta(days=7)
    month_start = day_start - timedelta(days=30)
    global_repository = GlobalCostRepository(session)
    totals = {
        "today": global_repository.totals_since(day_start),
        "last_7_days": global_repository.totals_since(week_start),
        "last_30_days": global_repository.totals_since(month_start),
    }
    profiles: list[ProfileCostView] = []
    for summary in ProfileCatalog(session).list():
        repository = AgentRunRepository(session)
        scope = ProfileScope(summary.id)
        today = repository.totals_since(scope, day_start)
        week = repository.totals_since(scope, week_start)
        month = repository.totals_since(scope, month_start)
        profiles.append(
            ProfileCostView(
                profile_slug=summary.slug,
                runs_today=today.runs,
                cost_today=today.cost_usd,
                cost_7d=week.cost_usd,
                cost_30d=month.cost_usd,
            )
        )
    return {
        "as_of": current.isoformat(),
        "totals": {
            name: {
                "runs": value.runs,
                "tokens_in": value.tokens_in,
                "tokens_out": value.tokens_out,
                "cost_usd": value.cost_usd,
            }
            for name, value in totals.items()
        },
        "profiles": [view.as_dict() for view in profiles],
    }


def list_calendar_views(
    session: Session,
    redactor: SecretRegistry,
    *,
    days: int = 30,
    now: datetime | None = None,
) -> list[CalendarView]:
    """Return scheduled/published items from 7 days ago through *days* ahead."""
    if days < 1 or days > 365:
        raise BadRequest("days must be between 1 and 365", "invalid_days")
    current = as_utc(datetime.now(UTC) if now is None else now)
    start = current - timedelta(days=7)
    end = current + timedelta(days=days)
    views: list[CalendarView] = []
    for summary in ProfileCatalog(session).list():
        items = ContentItemRepository(session).list(
            ProfileScope(summary.id),
            states=("scheduled", "published"),
            limit=500,
        )
        for item in items:
            station = item.published_at if item.state == "published" else item.scheduled_at
            if station is None:
                continue
            when = as_utc(station)
            if when < start or when > end:
                continue
            views.append(
                CalendarView(
                    content_item_id=item.id,
                    profile_slug=summary.slug,
                    platform=item.platform,
                    state=item.state,
                    when=when,
                    preview=preview(redactor.redact(item.body), limit=120),
                )
            )
    views.sort(key=lambda view: (view.when, view.content_item_id))
    return views


def _clock_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def calendar_week(
    session: Session,
    redactor: SecretRegistry,
    *,
    week_start: date,
    timezone: str | None = None,
    profile: str | None = None,
    quiet_hours: Any = None,
) -> dict[str, Any]:
    """Build one Monday-based calendar week with items placed in local time.

    ``timezone`` is the selected profile's timezone; when ``None`` (All profiles), each
    item is shown in its own profile's timezone and quiet hours are not shaded.
    """
    tz: ZoneInfo
    if timezone:
        try:
            tz = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")
    else:
        tz = ZoneInfo("UTC")

    week_start_local = datetime.combine(week_start, datetime.min.time(), tzinfo=tz)
    week_start_utc = week_start_local.astimezone(UTC)
    week_end_utc = week_start_utc + timedelta(days=7)

    summaries = ProfileCatalog(session).list()
    if profile is not None:
        summaries = [summary for summary in summaries if summary.slug == profile]
    tz_by_slug = {summary.slug: summary.timezone for summary in summaries}

    items_by_cell: dict[str, list[dict[str, Any]]] = {}
    items_by_day: list[list[dict[str, Any]]] = [[] for _ in range(7)]
    for summary in summaries:
        scope = ProfileScope(summary.id)
        for item in ContentItemRepository(session).list(
            scope, states=("scheduled", "published"), limit=500
        ):
            station = item.published_at if item.state == "published" else item.scheduled_at
            if station is None:
                continue
            when_utc = as_utc(station)
            if not (week_start_utc <= when_utc < week_end_utc):
                continue
            item_tz = timezone or tz_by_slug.get(summary.slug) or "UTC"
            try:
                local = when_utc.astimezone(ZoneInfo(item_tz))
            except ZoneInfoNotFoundError:
                local = when_utc.astimezone(UTC)
            day = local.weekday() if timezone else when_utc.weekday()
            entry = {
                "content_item_id": item.id,
                "platform": item.platform,
                "state": item.state,
                "profile_slug": summary.slug,
                "preview": preview(redactor.redact(item.body), limit=100),
                "time": local.strftime("%H:%M"),
                "tz": item_tz,
                "day": day,
                "hour": local.hour,
                "status_class": "status-" + STATE_SEMANTIC.get(item.state, "muted"),
            }
            items_by_cell.setdefault(f"{day}-{local.hour}", []).append(entry)
            if 0 <= day < 7:
                items_by_day[day].append(entry)

    quiet_cells: list[str] = []
    quiet_label: str | None = None
    if quiet_hours is not None and timezone:
        try:
            start_min = _clock_minutes(quiet_hours.start)
            end_min = _clock_minutes(quiet_hours.end)
            quiet_label = f"{quiet_hours.start}-{quiet_hours.end}"
            for day in range(7):
                for hour in range(24):
                    cell = hour * 60
                    in_quiet = (
                        start_min <= cell < end_min
                        if start_min < end_min
                        else cell >= start_min or cell < end_min
                    )
                    if in_quiet:
                        quiet_cells.append(f"{day}-{hour}")
        except (AttributeError, ValueError):
            quiet_label = None

    day_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    days = [
        {
            "name": day_names[i],
            "iso": (week_start + timedelta(days=i)).isoformat(),
            "num": (week_start + timedelta(days=i)).day,
        }
        for i in range(7)
    ]

    return {
        "week_start": week_start.isoformat(),
        "prev_week": (week_start - timedelta(days=7)).isoformat(),
        "next_week": (week_start + timedelta(days=7)).isoformat(),
        "days": days,
        "items_by_cell": items_by_cell,
        "items_by_day": items_by_day,
        "quiet_cells": quiet_cells,
        "quiet_label": quiet_label,
        "timezone": timezone,
    }


@dataclass(frozen=True, slots=True)
class AccountView:
    """One social account without credentials."""

    id: int
    profile_slug: str
    platform: str
    handle: str
    label: str | None
    status: str
    last_checked_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile_slug,
            "platform": self.platform,
            "handle": self.handle,
            "label": self.label,
            "status": self.status,
            "last_checked_at": (
                None if self.last_checked_at is None else self.last_checked_at.isoformat()
            ),
        }


def list_account_views(
    session: Session,
    profile: str,
) -> list[AccountView]:
    """Return social-account metadata for one profile. Credentials never leave storage."""
    profile_id = ProfileCatalog(session).resolve(profile)
    if profile_id is None:
        raise NotFound(f"profile '{profile}' is not registered")
    accounts = SocialAccountRepository(session).list(ProfileScope(profile_id))
    return [
        AccountView(
            id=account.id,
            profile_slug=profile,
            platform=account.platform,
            handle=account.handle,
            label=account.label,
            status=account.status,
            last_checked_at=(
                None if account.last_checked_at is None else as_utc(account.last_checked_at)
            ),
        )
        for account in accounts
    ]
