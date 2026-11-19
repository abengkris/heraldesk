"""Quiet-hours and daily-cap enforcement at publish time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.policy import parse_policy_yaml
from heraldesk.domain.profiles import ProfileStatus, load_policy, set_profile_status
from heraldesk.domain.scheduling import (
    DAILY_POST_CAP_REASON,
    QUIET_HOURS_REASON,
    defer_past_quiet_hours,
    evaluate_publish_gate,
    is_quiet_hours,
    local_day_bounds,
    published_today,
)
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.publisher import PublishStatus, publish_item
from heraldesk.scheduler.runner import run_scheduler
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BODY = "A scheduled post about dashboards."


def write_policy(setup: ProfileSetup, text: str) -> None:
    """Replace the profile policy file; the scheduler reads it directly."""
    (setup.definition.path / "policy.yaml").write_text(text, encoding="utf-8")


def make_scheduled(
    session: Session,
    setup: ProfileSetup,
    *,
    when: datetime = NOW,
    mode: str = "require_approval",
) -> int:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body=BODY,
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(setup.scope, item.id, mode=mode, delay_minutes=None)
    transition(
        session,
        setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=when,
    )
    return item.id


def cipher_for(setup: ProfileSetup) -> CredentialCipher:
    return CredentialCipher.from_key_file(setup.layout.key_path)


def sweep(
    session: Session,
    setup: ProfileSetup,
    adapter: FakeAdapter,
    *,
    now: datetime = NOW,
    dry_run: bool = False,
):
    return run_scheduler(
        session,
        setup.layout,
        now=now,
        dry_run=dry_run,
        adapter_factory=lambda _platform: adapter,
        cipher=cipher_for(setup),
        retry_backoff_seconds=(60, 300),
    )


def test_quiet_window_crossing_midnight_is_half_open() -> None:
    quiet = parse_policy_yaml('quiet_hours: { start: "21:00", end: "07:00" }').quiet_hours
    assert quiet is not None
    assert is_quiet_hours(datetime(2026, 11, 17, 21, 0), quiet)
    assert is_quiet_hours(datetime(2026, 11, 17, 23, 30), quiet)
    assert is_quiet_hours(datetime(2026, 11, 18, 6, 59), quiet)
    assert not is_quiet_hours(datetime(2026, 11, 18, 7, 0), quiet)
    assert not is_quiet_hours(datetime(2026, 11, 17, 12, 0), quiet)


def test_quiet_window_inside_one_day() -> None:
    quiet = parse_policy_yaml('quiet_hours: { start: "01:00", end: "05:00" }').quiet_hours
    assert quiet is not None
    assert is_quiet_hours(datetime(2026, 11, 17, 1, 0), quiet)
    assert is_quiet_hours(datetime(2026, 11, 17, 4, 59), quiet)
    assert not is_quiet_hours(datetime(2026, 11, 17, 5, 0), quiet)
    assert not is_quiet_hours(datetime(2026, 11, 17, 0, 59), quiet)


def test_deferral_uses_the_profile_timezone() -> None:
    quiet = parse_policy_yaml('quiet_hours: { start: "21:00", end: "07:00" }').quiet_hours
    assert quiet is not None

    # 23:30 UTC is 06:30 the next day in Jakarta, so the window ends at 07:00 local.
    early = defer_past_quiet_hours(
        datetime(2026, 11, 17, 23, 30, tzinfo=UTC), "Asia/Jakarta", quiet
    )
    assert early == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)

    # 15:00 UTC is 22:00 the same day in Jakarta; the window still ends the next morning.
    evening = defer_past_quiet_hours(
        datetime(2026, 11, 17, 15, 0, tzinfo=UTC), "Asia/Jakarta", quiet
    )
    assert evening == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)

    # 12:00 UTC is 19:00 local, outside the window.
    daytime = defer_past_quiet_hours(
        datetime(2026, 11, 17, 12, 0, tzinfo=UTC), "Asia/Jakarta", quiet
    )
    assert daytime == datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def test_deferral_waits_for_the_second_occurrence_on_fall_back() -> None:
    quiet = parse_policy_yaml('quiet_hours: { start: "23:00", end: "01:30" }').quiet_hours
    assert quiet is not None
    # 05:20 UTC is the first 01:20 local. The repeated 01:00-01:30 hour comes an hour
    # later, so resuming at the first 01:30 would land inside the window again.
    deferred = defer_past_quiet_hours(
        datetime(2026, 11, 1, 5, 20, tzinfo=UTC), "America/New_York", quiet
    )
    assert deferred == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_local_day_bounds_follow_daylight_saving() -> None:
    start, end = local_day_bounds(datetime(2026, 3, 8, 12, 0, tzinfo=UTC), "America/New_York")
    assert end - start == timedelta(hours=23)


def test_published_today_counts_the_local_calendar_day(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    repository = ContentItemRepository(db_session)
    late = repository.add(
        profile_setup.scope, platform="fake", body="late", state=ContentState.PUBLISHED.value
    )
    repository.mark_published(
        profile_setup.scope,
        late.id,
        external_id="late",
        published_at=datetime(2026, 11, 17, 16, 30, tzinfo=UTC),  # 23:30 Jakarta
    )
    next_day = repository.add(
        profile_setup.scope, platform="fake", body="next", state=ContentState.PUBLISHED.value
    )
    repository.mark_published(
        profile_setup.scope,
        next_day.id,
        external_id="next",
        published_at=datetime(2026, 11, 17, 17, 30, tzinfo=UTC),  # 00:30 Jakarta, Nov 18
    )

    now = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)
    assert published_today(db_session, profile_setup.scope, timezone="Asia/Jakarta", now=now) == 1


def test_gate_blocks_inside_quiet_hours(profile_setup: ProfileSetup, db_session: Session) -> None:
    policy = parse_policy_yaml('quiet_hours: { start: "21:00", end: "07:00" }\n')
    gate = evaluate_publish_gate(
        db_session,
        profile_setup.scope,
        timezone="Asia/Jakarta",
        policy=policy,
        now=datetime(2026, 11, 17, 15, 0, tzinfo=UTC),  # 22:00 local
    )
    assert gate.allowed is False
    assert gate.reason == QUIET_HOURS_REASON


def test_gate_blocks_at_the_daily_cap(profile_setup: ProfileSetup, db_session: Session) -> None:
    repository = ContentItemRepository(db_session)
    published = repository.add(
        profile_setup.scope, platform="fake", body="today", state=ContentState.PUBLISHED.value
    )
    repository.mark_published(
        profile_setup.scope,
        published.id,
        external_id="today",
        published_at=NOW - timedelta(hours=1),
    )
    policy = parse_policy_yaml("max_posts_per_day: 1\n")

    gate = evaluate_publish_gate(
        db_session,
        profile_setup.scope,
        timezone="UTC",
        policy=policy,
        now=NOW,
    )

    assert gate.allowed is False
    assert gate.reason == DAILY_POST_CAP_REASON


def test_zero_cap_disables_automatic_publishing(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    gate = evaluate_publish_gate(
        db_session,
        profile_setup.scope,
        timezone="UTC",
        policy=parse_policy_yaml("max_posts_per_day: 0\n"),
        now=NOW,
    )
    assert gate.allowed is False
    assert gate.reason == DAILY_POST_CAP_REASON


def test_scheduler_skips_quiet_hours_then_publishes_at_the_end(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    write_policy(
        profile_setup,
        'quiet_hours: { start: "11:00", end: "13:00" }\nmax_posts_per_day: 10\n',
    )
    item_id = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))
    adapter = FakeAdapter()

    blocked = sweep(db_session, profile_setup, adapter, now=NOW)

    assert [entry.reason for entry in blocked.skipped] == [QUIET_HOURS_REASON]
    assert adapter.published == []
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert stored is not None and stored.state == ContentState.SCHEDULED.value

    allowed = sweep(
        db_session, profile_setup, adapter, now=datetime(2026, 11, 17, 13, 0, tzinfo=UTC)
    )

    assert [entry.item_id for entry in allowed.published] == [item_id]
    assert len(adapter.published) == 1


def test_scheduler_enforces_the_daily_cap(profile_setup: ProfileSetup, db_session: Session) -> None:
    write_policy(profile_setup, "max_posts_per_day: 1\n")
    first = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=2))
    second = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))
    adapter = FakeAdapter()

    result = sweep(db_session, profile_setup, adapter, now=NOW)

    assert [entry.item_id for entry in result.published] == [first]
    assert [entry.item_id for entry in result.skipped] == [second]
    assert result.skipped[0].reason == DAILY_POST_CAP_REASON
    assert len(adapter.published) == 1


def test_dry_run_reports_policy_skips_without_writing(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    write_policy(profile_setup, 'quiet_hours: { start: "11:00", end: "13:00" }\n')
    item_id = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))
    adapter = FakeAdapter()

    result = sweep(db_session, profile_setup, adapter, now=NOW, dry_run=True)

    assert [entry.item_id for entry in result.skipped] == [item_id]
    assert result.skipped[0].reason == QUIET_HOURS_REASON
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert stored is not None and stored.state == ContentState.SCHEDULED.value


def test_manual_publish_overrides_the_gate_and_is_audited(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    write_policy(profile_setup, 'quiet_hours: { start: "11:00", end: "13:00" }\n')
    item_id = make_scheduled(db_session, profile_setup, when=NOW)
    adapter = FakeAdapter()

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item_id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW,
        trigger="manual",
        actor="user",
    )

    assert outcome.status is PublishStatus.PUBLISHED
    assert len(adapter.published) == 1
    actions = {
        entry.action for entry in AuditService(db_session).history(profile_setup.scope, limit=100)
    }
    assert "publish.policy_override" in actions


def test_malformed_policy_fails_closed_for_automatic_publishing(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    write_policy(profile_setup, "default_mode: not-a-real-mode\n")
    item_id = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))
    adapter = FakeAdapter()

    result = sweep(db_session, profile_setup, adapter, now=NOW)

    assert [entry.reason for entry in result.skipped] == ["policy_unavailable"]
    assert adapter.published == []
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert stored is not None and stored.state == ContentState.SCHEDULED.value


def test_profile_isolation_applies_the_gate_per_profile(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    """The policy file is read for the owning profile, never a sibling."""
    write_policy(profile_setup, "max_posts_per_day: 10\n")
    item_id = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))

    assert load_policy(profile_setup.definition.path).max_posts_per_day == 10
    result = sweep(db_session, profile_setup, FakeAdapter(), now=NOW)
    assert [entry.item_id for entry in result.published] == [item_id]
