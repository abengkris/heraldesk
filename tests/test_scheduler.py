"""Scheduler tests: due selection, off switches, approvals, delay, dry-run and retries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher
from heraldesk.core.locks import engage_global_kill_switch
from heraldesk.db.models import ContentItem
from heraldesk.db.repositories import (
    ApprovalRepository,
    ContentItemRepository,
    SocialAccountRepository,
)
from heraldesk.domain.approvals import approve, request_approval
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.profiles import (
    ProfileStatus,
    set_profile_kill_switch,
    set_profile_status,
)
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.scheduler.runner import run_scheduler
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
BODY = "A scheduled post about dashboards."


def cipher_for(setup: ProfileSetup) -> CredentialCipher:
    return CredentialCipher.from_key_file(setup.layout.key_path)


def make_scheduled(
    session: Session,
    setup: ProfileSetup,
    *,
    when: datetime = NOW,
    mode: str = "require_approval",
    platform: str = "fake",
    account_id: int | None = None,
    activate: bool = True,
) -> ContentItem:
    if activate:
        set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform=platform,
        body=BODY,
        account_id=account_id,
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
    stored = repository.get(setup.scope, item.id)
    assert stored is not None
    return stored


def run(
    session: Session,
    setup: ProfileSetup,
    adapter: FakeAdapter | None = None,
    *,
    now: datetime = NOW,
    dry_run: bool = False,
    backoff: tuple[int, ...] = (60, 300),
):
    adapter = adapter or FakeAdapter()
    return run_scheduler(
        session,
        setup.layout,
        now=now,
        dry_run=dry_run,
        adapter_factory=lambda _platform: adapter,
        cipher=cipher_for(setup),
        retry_backoff_seconds=backoff,
    )


def test_publishes_due_items_only(profile_setup: ProfileSetup, db_session: Session) -> None:
    due = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))
    future = make_scheduled(db_session, profile_setup, when=NOW + timedelta(hours=1))
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert [entry.item_id for entry in result.published] == [due.id]
    assert all(entry.item_id != future.id for entry in result.entries)
    published = ContentItemRepository(db_session).get(profile_setup.scope, due.id)
    future_item = ContentItemRepository(db_session).get(profile_setup.scope, future.id)
    assert published is not None and published.state == "published"
    assert future_item is not None and future_item.state == "scheduled"
    assert len(adapter.published) == 1


def test_skips_everything_under_the_global_kill_switch(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    make_scheduled(db_session, profile_setup)
    engage_global_kill_switch(profile_setup.layout)
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert result.published == ()
    assert result.skipped
    assert result.skipped[0].reason == "global_kill_switch"
    assert adapter.publish_calls == 0


def test_skips_a_profile_kill_switch(profile_setup: ProfileSetup, db_session: Session) -> None:
    make_scheduled(db_session, profile_setup)
    set_profile_kill_switch(db_session, profile_setup.scope, True)
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert result.published == ()
    assert result.skipped[0].reason == "profile_kill_switch"
    assert adapter.publish_calls == 0


def test_skips_a_paused_profile(profile_setup: ProfileSetup, db_session: Session) -> None:
    make_scheduled(db_session, profile_setup)
    set_profile_status(db_session, profile_setup.scope, ProfileStatus.PAUSED)
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert result.published == ()
    assert result.skipped[0].reason == "profile_paused"
    assert adapter.publish_calls == 0


def test_skips_a_draft_profile(profile_setup: ProfileSetup, db_session: Session) -> None:
    make_scheduled(db_session, profile_setup, activate=False)
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert result.published == ()
    assert result.skipped[0].reason == "profile_draft"
    assert adapter.publish_calls == 0


def test_skips_cancelled_items(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = ContentItemRepository(db_session).add(
        profile_setup.scope,
        platform="fake",
        body="Cancelled post.",
        state=ContentState.CANCELLED.value,
    )
    ContentItemRepository(db_session).set_scheduled_at(
        profile_setup.scope,
        item.id,
        scheduled_at=NOW - timedelta(minutes=1),
    )
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert result.due == 0
    assert adapter.publish_calls == 0


def test_skips_an_expired_approval(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = make_scheduled(db_session, profile_setup)
    approval = ApprovalRepository(db_session).add(
        profile_setup.scope,
        content_item_id=item.id,
        expires_at=NOW + timedelta(hours=1),
    )
    ApprovalRepository(db_session).update_decision(
        profile_setup.scope,
        approval.id,
        state="expired",
        decision=None,
        reason="expired",
        edited_body=None,
        decided_at=NOW,
        channel="cli",
    )
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert result.published == ()
    assert result.skipped
    assert "expired" in (result.skipped[0].reason or "")
    assert adapter.publish_calls == 0


def test_skips_a_draft_only_item_until_manual_publish(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    make_scheduled(db_session, profile_setup, mode="draft_only")
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter)

    assert result.published == ()
    assert result.skipped[0].reason == "publish mode draft_only is manual only"
    assert adapter.publish_calls == 0


def test_auto_after_delay_respects_the_window(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = ContentItemRepository(db_session).add(
        profile_setup.scope,
        platform="fake",
        body=BODY,
        state=ContentState.PENDING_APPROVAL.value,
        risk_level="low",
    )
    set_profile_status(db_session, profile_setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(db_session)
    repository.set_publish_policy(
        profile_setup.scope,
        item.id,
        mode="auto_after_delay",
        delay_minutes=30,
    )
    request_approval(db_session, profile_setup.scope, item.id, now=NOW)
    outcome = approve(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        now=NOW,
    )
    assert outcome.to_state == "scheduled"
    scheduled = repository.get(profile_setup.scope, item.id)
    assert scheduled is not None
    due_at = scheduled.scheduled_at
    assert due_at is not None
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    assert due_at == NOW + timedelta(minutes=30)

    adapter = FakeAdapter()
    early = run(db_session, profile_setup, adapter, now=NOW + timedelta(minutes=29))
    assert early.due == 0

    on_time = run(db_session, profile_setup, adapter, now=NOW + timedelta(minutes=30))
    assert [entry.item_id for entry in on_time.published] == [item.id]
    stored = repository.get(profile_setup.scope, item.id)
    assert stored is not None and stored.state == "published"


def test_dry_run_lists_without_writing(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))
    before = len(AuditService(db_session).history(profile_setup.scope, limit=1000))
    adapter = FakeAdapter()

    result = run(db_session, profile_setup, adapter, dry_run=True)

    assert result.dry_run is True
    assert result.planned
    assert result.planned[0].item_id == item.id
    assert adapter.publish_calls == 0
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "scheduled"
    assert stored.publish_attempts == 0
    assert len(AuditService(db_session).history(profile_setup.scope, limit=1000)) == before


def test_scheduler_run_is_audited(profile_setup: ProfileSetup, db_session: Session) -> None:
    make_scheduled(db_session, profile_setup)

    result = run(db_session, profile_setup)

    assert len(result.published) == 1
    entries = [
        entry
        for entry in AuditService(db_session).history(None, limit=100)
        if entry.action == "scheduler.run"
    ]
    assert entries
    payload = entries[-1].payload
    assert payload["published"] == 1
    assert payload["profiles_checked"] == 1


def test_second_run_does_not_publish_twice(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    make_scheduled(db_session, profile_setup)
    adapter = FakeAdapter()

    first = run(db_session, profile_setup, adapter)
    second = run(db_session, profile_setup, adapter)

    assert len(first.published) == 1
    assert second.due == 0
    assert adapter.publish_calls == 1
    assert len(adapter.published) == 1


def test_failure_is_retried_after_backoff(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = make_scheduled(db_session, profile_setup, when=NOW - timedelta(minutes=1))
    adapter = FakeAdapter(fail_times=1)

    first = run(db_session, profile_setup, adapter, backoff=(60, 300))
    assert len(first.failed) == 1
    failed = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert failed is not None
    assert failed.state == "failed"
    assert failed.publish_attempts == 1

    early = run(
        db_session,
        profile_setup,
        adapter,
        now=NOW + timedelta(seconds=30),
        backoff=(60, 300),
    )
    assert early.due == 0

    retried = run(
        db_session,
        profile_setup,
        adapter,
        now=NOW + timedelta(seconds=61),
        backoff=(60, 300),
    )
    assert [entry.item_id for entry in retried.published] == [item.id]
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "published"
    assert stored.publish_attempts == 2


def test_unsupported_platform_fails_with_a_documented_reason(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    cipher = cipher_for(profile_setup)
    account = SocialAccountRepository(db_session).add(
        profile_setup.scope,
        platform="linkedin",
        handle="@acme",
        credentials_encrypted=cipher.encrypt_json({"token": "linkedin-token-value"}),
    )
    make_scheduled(
        db_session,
        profile_setup,
        platform="linkedin",
        account_id=account.id,
    )

    result = run_scheduler(
        db_session,
        profile_setup.layout,
        now=NOW,
        cipher=cipher,
    )

    assert len(result.failed) == 1
    failed = result.failed[0]
    assert failed.reason is not None
    assert "no publisher adapter" in failed.reason
    assert "docs/platforms/README.md" in failed.reason
