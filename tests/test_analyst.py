"""Tests for deterministic weekly and monthly reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from heraldesk.analyst import (
    ReportError,
    build_report,
    monthly_period,
    render_markdown,
    weekly_period,
    write_report,
)
from heraldesk.core.audit import AuditService
from heraldesk.db.repositories import (
    AgentRunRepository,
    ContentItemRepository,
    InboxRepository,
    TelegramMessageRepository,
)
from heraldesk.db.scope import ProfileScope

pytestmark = pytest.mark.core

TOKEN = "sk-1234567890abcdefghijklmnop"


def add_content(
    session: Session,
    scope: ProfileScope,
    *,
    body: str,
    state: str,
    created_at: datetime,
) -> int:
    item = ContentItemRepository(session).add(
        scope,
        platform="fake",
        body=body,
        state=state,
        created_at=created_at,
    )
    return item.id


def add_inbox(
    session: Session,
    scope: ProfileScope | None,
    *,
    created_at: datetime,
) -> int:
    message = TelegramMessageRepository(session).add(
        scope,
        chat_id=123456789,
        sender_id=555666777,
        text="inbox message",
        command=None,
        content_item_id=None,
        response=None,
        created_at=created_at,
    )
    return message.id


def test_weekly_and_monthly_period_boundaries() -> None:
    week = weekly_period("2026-W46")
    assert week.start == datetime(2026, 11, 9, tzinfo=UTC)
    assert week.end == datetime(2026, 11, 16, tzinfo=UTC)
    assert week.filename == "weekly-2026-W46.md"

    month = monthly_period("2026-12")
    assert month.start == datetime(2026, 12, 1, tzinfo=UTC)
    assert month.end == datetime(2027, 1, 1, tzinfo=UTC)
    assert month.filename == "monthly-2026-12.md"


@pytest.mark.parametrize("value", ["2026-W99", "2026-46", "not-a-week"])
def test_invalid_week_is_rejected(value: str) -> None:
    with pytest.raises(ReportError):
        weekly_period(value)


@pytest.mark.parametrize("value", ["2026-13", "2026-00", "2026/11", "nope"])
def test_invalid_month_is_rejected(value: str) -> None:
    with pytest.raises(ReportError):
        monthly_period(value)


def test_report_uses_created_at_cohort(
    db_session: Session,
    profile_setup,
) -> None:
    period = weekly_period("2026-W46")
    scope = profile_setup.scope
    inside = period.start + timedelta(days=2)
    outside = period.end + timedelta(days=1)

    add_content(db_session, scope, body="published", state="published", created_at=inside)
    add_content(
        db_session,
        scope,
        body="approved",
        state="approved",
        created_at=inside + timedelta(hours=1),
    )
    failed = add_content(
        db_session,
        scope,
        body="failed",
        state="failed",
        created_at=inside + timedelta(hours=2),
    )
    ContentItemRepository(db_session).record_publish_attempt(
        scope,
        failed,
        attempts=3,
        error="provider timeout",
    )
    add_content(db_session, scope, body="outside", state="published", created_at=outside)
    AgentRunRepository(db_session).add(
        scope,
        agent="writer",
        status="succeeded",
        model="fake/model",
        input={},
        started_at=inside,
        cost_usd=0.25,
    )
    AgentRunRepository(db_session).add(
        scope,
        agent="writer",
        status="succeeded",
        model="fake/model",
        input={},
        started_at=outside,
        cost_usd=9.0,
    )

    pending = add_inbox(db_session, scope, created_at=inside)
    handled = add_inbox(db_session, scope, created_at=inside)
    ignored = add_inbox(db_session, None, created_at=inside)
    add_inbox(db_session, scope, created_at=outside)
    InboxRepository(db_session).mark_handled(
        handled,
        response="reply",
        triaged_at=inside,
    )
    InboxRepository(db_session).mark_ignored(
        ignored,
        reason="spam",
        triaged_at=inside,
    )

    data = build_report(db_session, period)

    assert (data.created, data.approved, data.published, data.failed) == (3, 2, 1, 1)
    assert data.approval_rate == pytest.approx(2 / 3)
    assert data.total_cost_usd == pytest.approx(0.25)
    assert (data.inbox_pending, data.inbox_handled, data.inbox_ignored) == (1, 1, 1)
    assert len(data.failures) == 1
    assert data.failures[0].item_id == failed
    assert len(data.top_posts) == 1
    assert pending not in [row.item_id for row in data.failures]


def test_report_limits_failures_and_top_posts(db_session: Session, profile_setup) -> None:
    period = weekly_period("2026-W46")
    scope = profile_setup.scope
    failures = []
    posts = []
    for index in range(12):
        item_id = add_content(
            db_session,
            scope,
            body=f"failed {index}",
            state="failed",
            created_at=period.start + timedelta(hours=index),
        )
        failures.append(item_id)
    for index in range(6):
        item_id = add_content(
            db_session,
            scope,
            body=f"post {index}",
            state="published",
            created_at=period.start + timedelta(hours=20 + index),
        )
        posts.append(item_id)

    data = build_report(db_session, period)

    assert len(data.failures) == 10
    assert data.failures[0].item_id == failures[-1]
    assert len(data.top_posts) == 5
    assert data.top_posts[0].item_id == posts[-1]


def test_empty_period_still_writes_zero_report(
    db_session: Session,
    initialized,
) -> None:
    period = weekly_period("2026-W46")

    path = write_report(db_session, initialized.layout, period)

    text = path.read_text(encoding="utf-8")
    assert path == initialized.layout.reports / "weekly-2026-W46.md"
    assert "# Weekly report: 2026-W46" in text
    assert "Total content created: 0" in text
    assert "Approved: 0" in text
    assert "Approval rate: 0.0%" in text
    assert "Total cost: $0.000000" in text
    assert "| pending | 0 |" in text
    actions = [entry.action for entry in AuditService(db_session).history(None)]
    assert "report.generated" in actions


def test_same_input_produces_an_identical_file(
    db_session: Session,
    initialized,
) -> None:
    period = weekly_period("2026-W46")

    first = write_report(db_session, initialized.layout, period).read_bytes()
    second = write_report(db_session, initialized.layout, period).read_bytes()

    assert first == second


def test_report_redacts_publish_errors(db_session: Session, profile_setup) -> None:
    period = weekly_period("2026-W46")
    item_id = add_content(
        db_session,
        profile_setup.scope,
        body="failed",
        state="failed",
        created_at=period.start + timedelta(days=1),
    )
    ContentItemRepository(db_session).record_publish_attempt(
        profile_setup.scope,
        item_id,
        attempts=1,
        error=f"provider rejected {TOKEN}",
    )

    text = render_markdown(build_report(db_session, period))

    assert TOKEN not in text
    assert "[REDACTED]" in text
