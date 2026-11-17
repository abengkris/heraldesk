"""Deterministic weekly and monthly reports for the M8 CLI.

No LLM is involved. Every number comes from the existing database, and the same database
state always renders the same Markdown file. Reports are instance-wide because the CLI
has no profile argument; content metrics use the ``created_at`` cohort inside the period
and inbox metrics use the inbox row ``created_at``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final, Literal

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import ContentItem
from heraldesk.db.repositories import (
    AgentRunRepository,
    ContentItemRepository,
    InboxRepository,
    ProfileCatalog,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.errors import DomainError
from heraldesk.telegram.formatting import preview

WEEK_PATTERN: Final = re.compile(r"^(\d{4})-W(\d{2})$")
MONTH_PATTERN: Final = re.compile(r"^(\d{4})-(\d{2})$")
APPROVED_STATES: Final = frozenset({"approved", "scheduled", "published"})
MAX_FAILURES: Final = 10
MAX_TOP_POSTS: Final = 5


class ReportError(DomainError):
    """A report period is malformed or cannot be generated."""


@dataclass(frozen=True, slots=True)
class ReportPeriod:
    """One resolved report window with half-open UTC boundaries."""

    kind: Literal["weekly", "monthly"]
    label: str
    start: datetime
    end: datetime
    filename: str


@dataclass(frozen=True, slots=True)
class FailureRow:
    """One failed publish included in the report."""

    item_id: int
    platform: str
    attempts: int
    created_at: datetime
    error: str | None


@dataclass(frozen=True, slots=True)
class PostRow:
    """One published post included in the top-five table."""

    item_id: int
    platform: str
    created_at: datetime
    body: str


@dataclass(frozen=True, slots=True)
class ReportData:
    """Everything one report renders, computed from the database only."""

    period: ReportPeriod
    created: int
    approved: int
    published: int
    failed: int
    approval_rate: float
    total_cost_usd: float
    inbox_pending: int
    inbox_handled: int
    inbox_ignored: int
    failures: tuple[FailureRow, ...]
    top_posts: tuple[PostRow, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _start_of_day(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def weekly_period(
    week: str | None = None,
    *,
    now: datetime | None = None,
) -> ReportPeriod:
    """Resolve ``YYYY-Www`` (or the current UTC ISO week) to a report period."""
    current = datetime.now(UTC) if now is None else _as_utc(now)
    if week is None:
        iso = current.isocalendar()
        year, week_number = iso.year, iso.week
    else:
        match = WEEK_PATTERN.fullmatch(week.strip())
        if match is None:
            raise ReportError("week must use the YYYY-Www format, for example 2026-W46")
        year, week_number = int(match.group(1)), int(match.group(2))
    try:
        monday = date.fromisocalendar(year, week_number, 1)
    except ValueError as exc:
        raise ReportError(f"week {year:04d}-W{week_number:02d} does not exist") from exc
    label = f"{year:04d}-W{week_number:02d}"
    start = _start_of_day(monday)
    return ReportPeriod(
        kind="weekly",
        label=label,
        start=start,
        end=start + timedelta(days=7),
        filename=f"weekly-{label}.md",
    )


def monthly_period(
    month: str | None = None,
    *,
    now: datetime | None = None,
) -> ReportPeriod:
    """Resolve ``YYYY-MM`` (or the current UTC month) to a report period."""
    current = datetime.now(UTC) if now is None else _as_utc(now)
    if month is None:
        year, month_number = current.year, current.month
    else:
        match = MONTH_PATTERN.fullmatch(month.strip())
        if match is None:
            raise ReportError("month must use the YYYY-MM format, for example 2026-11")
        year, month_number = int(match.group(1)), int(match.group(2))
        if month_number < 1 or month_number > 12:
            raise ReportError(f"month {year:04d}-{month_number:02d} does not exist")
    label = f"{year:04d}-{month_number:02d}"
    start = datetime(year, month_number, 1, tzinfo=UTC)
    if month_number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month_number + 1, 1, tzinfo=UTC)
    return ReportPeriod(
        kind="monthly",
        label=label,
        start=start,
        end=end,
        filename=f"monthly-{label}.md",
    )


def build_report(session: Session, period: ReportPeriod) -> ReportData:
    """Aggregate one instance-wide period from content, run and inbox tables."""
    items: list[ContentItem] = []
    total_cost = 0.0
    for profile in ProfileCatalog(session).list():
        scope = ProfileScope(profile.id)
        items.extend(
            ContentItemRepository(session).list_between(
                scope,
                start=period.start,
                end=period.end,
            )
        )
        total_cost += (
            AgentRunRepository(session)
            .totals_since(
                scope,
                period.start,
                period.end,
            )
            .cost_usd
        )

    ordered = sorted(
        items,
        key=lambda item: (_as_utc(item.created_at), item.id),
        reverse=True,
    )
    created = len(ordered)
    approved = sum(1 for item in ordered if item.state in APPROVED_STATES)
    published = sum(1 for item in ordered if item.state == "published")
    failed = sum(1 for item in ordered if item.state == "failed")
    failed_items = [item for item in ordered if item.state == "failed"]
    published_items = [item for item in ordered if item.state == "published"]
    failures = tuple(
        FailureRow(
            item_id=item.id,
            platform=item.platform,
            attempts=item.publish_attempts,
            created_at=_as_utc(item.created_at),
            error=item.last_error,
        )
        for item in failed_items[:MAX_FAILURES]
    )
    top_posts = tuple(
        PostRow(
            item_id=item.id,
            platform=item.platform,
            created_at=_as_utc(item.created_at),
            body=item.body,
        )
        for item in published_items[:MAX_TOP_POSTS]
    )
    inbox = InboxRepository(session).status_counts(since=period.start, until=period.end)
    return ReportData(
        period=period,
        created=created,
        approved=approved,
        published=published,
        failed=failed,
        approval_rate=(approved / created) if created else 0.0,
        total_cost_usd=total_cost,
        inbox_pending=inbox["pending"],
        inbox_handled=inbox["handled"],
        inbox_ignored=inbox["ignored"],
        failures=failures,
        top_posts=top_posts,
    )


def _period_label(period: ReportPeriod) -> str:
    last_day = (period.end - timedelta(days=1)).date().isoformat()
    return f"{period.start.date().isoformat()} to {last_day} (UTC)"


def _cell(value: str | None, redactor: SecretRegistry, *, limit: int) -> str:
    if value is None or not value.strip():
        return "-"
    redacted = redactor.redact(value)
    collapsed = " ".join(redacted.split()).replace("|", "\\|")
    return preview(collapsed, limit=limit)


def render_markdown(data: ReportData, *, redactor: SecretRegistry | None = None) -> str:
    """Render one report as stable Markdown without a generation timestamp."""
    redaction = redactor or SecretRegistry.from_environment()
    title = "Weekly" if data.period.kind == "weekly" else "Monthly"
    lines = [
        f"# {title} report: {data.period.label}",
        "",
        f"- Period: {_period_label(data.period)}",
        f"- Total content created: {data.created}",
        f"- Approved: {data.approved}",
        f"- Published: {data.published}",
        f"- Failed: {data.failed}",
        f"- Approval rate: {data.approval_rate * 100:.1f}%",
        f"- Total cost: ${data.total_cost_usd:.6f}",
        "",
        "## Inbox",
        "",
        "| Status | Count |",
        "| --- | ---: |",
        f"| pending | {data.inbox_pending} |",
        f"| handled | {data.inbox_handled} |",
        f"| ignored | {data.inbox_ignored} |",
        "",
        "## Publish failures",
        "",
    ]
    if data.failures:
        lines.extend(
            [
                "| ID | Platform | Attempts | Created | Error |",
                "| ---: | --- | ---: | --- | --- |",
            ]
        )
        for failure in data.failures:
            lines.append(
                f"| {failure.item_id} | {_cell(failure.platform, redaction, limit=40)} | "
                f"{failure.attempts} | {failure.created_at.date().isoformat()} | "
                f"{_cell(failure.error, redaction, limit=160)} |"
            )
    else:
        lines.append("_No publish failures in this period._")
    lines.extend(
        [
            "",
            "## Top published posts",
            "",
        ]
    )
    if data.top_posts:
        lines.extend(
            [
                "| ID | Platform | Created | Preview |",
                "| ---: | --- | --- | --- |",
            ]
        )
        for post in data.top_posts:
            lines.append(
                f"| {post.item_id} | {_cell(post.platform, redaction, limit=40)} | "
                f"{post.created_at.date().isoformat()} | "
                f"{_cell(post.body, redaction, limit=120)} |"
            )
    else:
        lines.append("_No published posts in this period._")
    lines.append("")
    return "\n".join(lines)


def write_report(session: Session, layout: paths.Layout, period: ReportPeriod) -> Path:
    """Build, render and overwrite one report file, then audit the generation."""
    data = build_report(session, period)
    markdown = render_markdown(data)
    try:
        layout.reports.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = layout.reports / period.filename
        path.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"cannot write report in {layout.reports}: {exc}") from exc

    AuditService(session).record(
        None,
        actor="user",
        action="report.generated",
        entity="report",
        entity_id=period.label,
        payload={
            "period": period.label,
            "period_type": period.kind,
            "path": str(path),
            "created": data.created,
            "approved": data.approved,
            "published": data.published,
            "failed": data.failed,
            "inbox_pending": data.inbox_pending,
            "inbox_handled": data.inbox_handled,
            "inbox_ignored": data.inbox_ignored,
        },
    )
    return path
