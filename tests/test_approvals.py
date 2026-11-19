"""Approval requests, decisions, edits, expiry, kill switches and redaction."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.locks import engage_global_kill_switch
from heraldesk.db.models import Approval, ContentItem
from heraldesk.db.repositories import (
    ApprovalRepository,
    ContentItemRepository,
    DuplicateRecord,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import (
    APPROVAL_TTL,
    ApprovalState,
    approve,
    as_utc,
    edit_item,
    expire_pending_approvals,
    list_queue,
    reject,
    request_approval,
)
from heraldesk.domain.errors import (
    ApprovalBlockedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalStateError,
    ContentItemNotFoundError,
)
from heraldesk.domain.profiles import (
    ProfileStatus,
    load_profile,
    register_definition,
    set_profile_kill_switch,
    set_profile_status,
    write_profile_skeleton,
)
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
SECRET = "sk-ABCDEFGHIJKLMNOP1234"


def make_item(
    session: Session,
    setup: ProfileSetup,
    *,
    state: str = "pending_approval",
    body: str = "A plain draft about dashboards.",
    risk_level: str | None = "low",
) -> ContentItem:
    return ContentItemRepository(session).add(
        setup.scope,
        platform="fake",
        body=body,
        state=state,
        risk_level=risk_level,
        risk_notes="Compliance notes" if risk_level else None,
    )


def make_pending(
    session: Session,
    setup: ProfileSetup,
    *,
    now: datetime = NOW,
    body: str = "A plain draft about dashboards.",
) -> tuple[ContentItem, Approval]:
    item = make_item(session, setup, body=body)
    approval = request_approval(session, setup.scope, item.id, now=now)
    return item, approval


def actions(session: Session, setup: ProfileSetup) -> list[str]:
    return [entry.action for entry in AuditService(session).history(setup.scope, limit=1000)]


def test_request_approval_is_idempotent_and_unique(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_item(db_session, profile_setup)
    first = request_approval(db_session, profile_setup.scope, item.id, now=NOW)
    second = request_approval(db_session, profile_setup.scope, item.id, now=NOW)

    assert first.id == second.id
    assert len(ApprovalRepository(db_session).list(profile_setup.scope)) == 1
    with pytest.raises(DuplicateRecord):
        ApprovalRepository(db_session).add(
            profile_setup.scope,
            content_item_id=item.id,
            expires_at=NOW + APPROVAL_TTL,
        )


def test_request_approval_requires_pending_approval(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_item(db_session, profile_setup, state="drafted")
    with pytest.raises(ApprovalStateError, match="not pending_approval"):
        request_approval(db_session, profile_setup.scope, item.id, now=NOW)


def test_approve_happy_path(profile_setup: ProfileSetup, db_session: Session) -> None:
    item, approval = make_pending(db_session, profile_setup)
    outcome = approve(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        now=NOW,
    )

    assert outcome.from_state == "pending_approval"
    assert outcome.to_state == "approved"
    assert ContentItemRepository(db_session).get(profile_setup.scope, item.id).state == "approved"
    stored = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored is not None
    assert stored.state == ApprovalState.APPROVED.value
    assert stored.decision == "approve"
    assert stored.decided_at is not None

    recorded = actions(db_session, profile_setup)
    assert "approval.requested" in recorded
    assert "approval.approved" in recorded
    assert "content.state_changed" in recorded


def test_approve_illegal_states(profile_setup: ProfileSetup, db_session: Session) -> None:
    drafted = make_item(db_session, profile_setup, state="drafted")
    with pytest.raises(ApprovalStateError, match="not pending_approval"):
        approve(db_session, profile_setup.layout, profile_setup.scope, drafted.id, now=NOW)

    pending = make_item(db_session, profile_setup)
    with pytest.raises(ApprovalNotFoundError):
        approve(db_session, profile_setup.layout, profile_setup.scope, pending.id, now=NOW)

    approved = make_item(db_session, profile_setup, state="approved")
    ApprovalRepository(db_session).add(
        profile_setup.scope,
        content_item_id=approved.id,
        expires_at=NOW + APPROVAL_TTL,
    )
    with pytest.raises(ApprovalStateError, match="not pending_approval"):
        approve(db_session, profile_setup.layout, profile_setup.scope, approved.id, now=NOW)


def test_approve_schedules_a_require_approval_item(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_item(db_session, profile_setup)
    ContentItemRepository(db_session).set_publish_policy(
        profile_setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
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
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "scheduled"
    assert as_utc(stored.scheduled_at) == NOW


def test_approve_honours_the_auto_after_delay_window(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_item(db_session, profile_setup)
    ContentItemRepository(db_session).set_publish_policy(
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
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert as_utc(stored.scheduled_at) == NOW + timedelta(minutes=30)


def test_approve_defers_an_auto_after_delay_item_past_quiet_hours(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    (profile_setup.definition.path / "policy.yaml").write_text(
        'quiet_hours: { start: "12:15", end: "13:00" }\n',
        encoding="utf-8",
    )
    item = make_item(db_session, profile_setup)
    ContentItemRepository(db_session).set_publish_policy(
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
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert as_utc(stored.scheduled_at) == datetime(2026, 11, 16, 13, 0, tzinfo=UTC)


def test_approve_keeps_draft_only_items_manual(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_item(db_session, profile_setup)
    ContentItemRepository(db_session).set_publish_policy(
        profile_setup.scope,
        item.id,
        mode="draft_only",
        delay_minutes=None,
    )
    request_approval(db_session, profile_setup.scope, item.id, now=NOW)

    outcome = approve(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        now=NOW,
    )

    assert outcome.to_state == "approved"
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "approved"


def test_reject_happy_path_redacts_the_reason(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, approval = make_pending(db_session, profile_setup)
    outcome = reject(
        db_session,
        profile_setup.scope,
        item.id,
        reason=f"off-brand and leaks {SECRET}",
        now=NOW,
    )

    assert outcome.to_state == "rejected"
    assert SECRET not in (outcome.reason or "")
    stored = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored is not None
    assert stored.state == ApprovalState.REJECTED.value
    assert stored.decision == "reject"
    assert SECRET not in (stored.reason or "")

    audits = AuditService(db_session).history(profile_setup.scope, limit=1000)
    assert "approval.rejected" in [entry.action for entry in audits]
    assert SECRET not in json.dumps([entry.payload for entry in audits])


def test_reject_illegal_state(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = make_item(db_session, profile_setup, state="approved")
    with pytest.raises(ApprovalStateError, match="not pending_approval"):
        reject(db_session, profile_setup.scope, item.id, now=NOW)


@pytest.mark.parametrize("state", ["blocked", "rejected", "needs_review"])
def test_edit_reopens_blocked_rejected_and_needs_review(
    state: str, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_item(db_session, profile_setup, state=state, body="old body")
    outcome = edit_item(
        db_session,
        profile_setup.scope,
        item.id,
        body="new body after human edit",
        now=NOW,
    )

    assert outcome.from_state == state
    assert outcome.to_state == "drafted"
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "drafted"
    assert stored.body == "new body after human edit"
    assert stored.risk_level is None
    assert "approval.edited" in actions(db_session, profile_setup)


def test_edit_approved_returns_to_pending_approval(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, old_approval = make_pending(db_session, profile_setup)
    approve(db_session, profile_setup.layout, profile_setup.scope, item.id, now=NOW)

    outcome = edit_item(
        db_session,
        profile_setup.scope,
        item.id,
        body="edited after approval",
        now=NOW + timedelta(hours=1),
    )

    assert outcome.from_state == "approved"
    assert outcome.to_state == "pending_approval"
    assert outcome.approval_id is not None
    assert outcome.approval_id != old_approval.id

    stored_item = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored_item is not None
    assert stored_item.state == "pending_approval"
    assert stored_item.risk_level is None
    stored_old = ApprovalRepository(db_session).get(profile_setup.scope, old_approval.id)
    assert stored_old is not None
    assert stored_old.state == ApprovalState.APPROVED.value
    pending = ApprovalRepository(db_session).pending_for_item(profile_setup.scope, item.id)
    assert pending is not None
    assert pending.state == "pending"


def test_edit_pending_approval_stays_pending_and_resets_expiry(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, approval = make_pending(
        db_session,
        profile_setup,
        now=NOW - timedelta(hours=71),
    )
    outcome = edit_item(
        db_session,
        profile_setup.scope,
        item.id,
        body="edited while pending",
        now=NOW,
    )

    assert outcome.from_state == "pending_approval"
    assert outcome.to_state == "pending_approval"
    assert outcome.approval_id == approval.id
    stored_item = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored_item is not None
    assert stored_item.state == "pending_approval"
    stored = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored is not None
    assert stored.state == "pending"
    assert stored.edited_body == "edited while pending"
    assert as_utc(stored.expires_at) > NOW + timedelta(hours=71)


@pytest.mark.parametrize("state", ["drafted", "reviewed", "scheduled", "published", "cancelled"])
def test_edit_is_illegal_outside_user_holdable_states(
    state: str, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_item(db_session, profile_setup, state=state)
    with pytest.raises(ApprovalStateError, match="user edits are only allowed"):
        edit_item(db_session, profile_setup.scope, item.id, body="nope", now=NOW)


def test_edit_redacts_secrets(profile_setup: ProfileSetup, db_session: Session) -> None:
    item, approval = make_pending(db_session, profile_setup)
    edit_item(
        db_session,
        profile_setup.scope,
        item.id,
        body=f"Contact {SECRET} for details.",
        now=NOW,
    )

    stored_item = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored_item is not None
    assert SECRET not in stored_item.body
    stored_approval = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored_approval is not None
    assert SECRET not in (stored_approval.edited_body or "")
    audits = AuditService(db_session).history(profile_setup.scope, limit=1000)
    assert SECRET not in json.dumps([entry.payload for entry in audits])


def test_expiry_after_72_hours_expires_and_cancels(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, approval = make_pending(
        db_session,
        profile_setup,
        now=NOW - timedelta(hours=73),
    )
    outcomes = expire_pending_approvals(db_session, profile_setup.scope, now=NOW)

    assert len(outcomes) == 1
    assert outcomes[0].cancelled is True
    stored_item = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored_item is not None
    assert stored_item.state == "cancelled"
    stored = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored is not None
    assert stored.state == ApprovalState.EXPIRED.value
    assert stored.decided_at is not None
    recorded = actions(db_session, profile_setup)
    assert "approval.expired" in recorded
    assert "content.state_changed" in recorded


def test_expiry_before_72_hours_does_nothing(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, approval = make_pending(
        db_session,
        profile_setup,
        now=NOW - timedelta(hours=71),
    )
    outcomes = expire_pending_approvals(db_session, profile_setup.scope, now=NOW)

    assert outcomes == []
    assert ContentItemRepository(db_session).get(profile_setup.scope, item.id).state == (
        "pending_approval"
    )
    stored = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored is not None
    assert stored.state == "pending"
    assert "approval.expired" not in actions(db_session, profile_setup)


def test_expired_approval_cannot_be_approved(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, approval = make_pending(
        db_session,
        profile_setup,
        now=NOW - timedelta(hours=73),
    )
    with pytest.raises(ApprovalExpiredError, match="expired"):
        approve(db_session, profile_setup.layout, profile_setup.scope, item.id, now=NOW)

    assert ContentItemRepository(db_session).get(profile_setup.scope, item.id).state == (
        "pending_approval"
    )
    stored = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored is not None
    assert stored.state == "pending"


def test_global_kill_switch_blocks_approve(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, approval = make_pending(db_session, profile_setup)
    engage_global_kill_switch(profile_setup.layout)

    with pytest.raises(ApprovalBlockedError, match="global_kill_switch"):
        approve(db_session, profile_setup.layout, profile_setup.scope, item.id, now=NOW)

    assert ContentItemRepository(db_session).get(profile_setup.scope, item.id).state == (
        "pending_approval"
    )
    stored = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert stored is not None
    assert stored.state == "pending"
    assert "approval.approved" not in actions(db_session, profile_setup)

    # Reject is a safe action and stays available while halted.
    reject(db_session, profile_setup.scope, item.id, now=NOW)
    assert ContentItemRepository(db_session).get(profile_setup.scope, item.id).state == "rejected"


def test_per_profile_kill_switch_blocks_approve(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, _ = make_pending(db_session, profile_setup)
    set_profile_kill_switch(db_session, profile_setup.scope, True)

    with pytest.raises(ApprovalBlockedError, match="profile_kill_switch"):
        approve(db_session, profile_setup.layout, profile_setup.scope, item.id, now=NOW)


def test_paused_profile_blocks_approve(profile_setup: ProfileSetup, db_session: Session) -> None:
    item, _ = make_pending(db_session, profile_setup)
    set_profile_status(db_session, profile_setup.scope, ProfileStatus.PAUSED)

    with pytest.raises(ApprovalBlockedError, match="profile_paused"):
        approve(db_session, profile_setup.layout, profile_setup.scope, item.id, now=NOW)


def test_approvals_are_scoped_to_the_profile(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    other_definition = load_profile(write_profile_skeleton(profile_setup.layout.profiles, "beta"))
    other_summary, _ = register_definition(db_session, other_definition)
    other_scope = ProfileScope(other_summary.id)
    item, approval = make_pending(db_session, profile_setup)

    with pytest.raises(ContentItemNotFoundError):
        approve(
            db_session,
            profile_setup.layout,
            other_scope,
            item.id,
            now=NOW,
        )
    assert ApprovalRepository(db_session).get(other_scope, approval.id) is None
    assert list_queue(db_session, other_scope) == []


def test_list_queue_exposes_the_open_approval(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item, approval = make_pending(db_session, profile_setup)
    entries = list_queue(db_session, profile_setup.scope)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.item_id == item.id
    assert entry.approval_id == approval.id
    assert entry.approval_state == "pending"
    assert entry.expires_at is not None


def test_every_approval_action_is_audited(profile_setup: ProfileSetup, db_session: Session) -> None:
    first, _ = make_pending(db_session, profile_setup)
    approve(db_session, profile_setup.layout, profile_setup.scope, first.id, now=NOW)

    second, _ = make_pending(db_session, profile_setup)
    reject(db_session, profile_setup.scope, second.id, now=NOW)

    make_pending(
        db_session,
        profile_setup,
        now=NOW - timedelta(hours=73),
    )
    expire_pending_approvals(db_session, profile_setup.scope, now=NOW)

    fourth = make_item(db_session, profile_setup, state="blocked")
    edit_item(db_session, profile_setup.scope, fourth.id, body="fixed body", now=NOW)

    recorded = set(actions(db_session, profile_setup))
    assert {
        "approval.requested",
        "approval.approved",
        "approval.rejected",
        "approval.expired",
        "approval.edited",
        "content.state_changed",
    } <= recorded
