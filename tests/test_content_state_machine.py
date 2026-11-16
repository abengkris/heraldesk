"""Every legal and illegal content state transition."""

from __future__ import annotations

import pytest

from heraldesk.core.audit import AuditService
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.content import (
    LEGAL_TRANSITIONS,
    ContentState,
    can_transition,
    transition,
)
from heraldesk.domain.errors import ContentItemNotFoundError, IllegalContentTransition
from heraldesk.domain.profiles import load_profile, register_definition, write_profile_skeleton
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core


def make_item(db_session, setup: ProfileSetup, state: ContentState):
    return ContentItemRepository(db_session).add(
        setup.scope,
        platform="fake",
        body=f"item in {state.value}",
        state=state.value,
    )


def test_every_legal_transition_succeeds_and_is_audited(
    profile_setup: ProfileSetup, db_session
) -> None:
    repository = ContentItemRepository(db_session)
    for source, targets in LEGAL_TRANSITIONS.items():
        for target in sorted(targets, key=lambda state: state.value):
            item = make_item(db_session, profile_setup, source)
            updated = transition(db_session, profile_setup.scope, item.id, target)

            assert updated.state == target.value
            assert repository.get(profile_setup.scope, item.id).state == target.value
            actions = [
                entry.action
                for entry in AuditService(db_session).history(profile_setup.scope, limit=1000)
            ]
            assert "content.state_changed" in actions


def test_every_illegal_transition_raises_and_leaves_the_state_alone(
    profile_setup: ProfileSetup, db_session
) -> None:
    repository = ContentItemRepository(db_session)
    for source in ContentState:
        allowed = LEGAL_TRANSITIONS[source]
        forbidden = [
            target for target in ContentState if target not in allowed and target is not source
        ]
        for target in forbidden:
            item = make_item(db_session, profile_setup, source)
            with pytest.raises(IllegalContentTransition):
                transition(db_session, profile_setup.scope, item.id, target)
            assert repository.get(profile_setup.scope, item.id).state == source.value


def test_terminal_states_cannot_move_in_m3(profile_setup: ProfileSetup, db_session) -> None:
    for state in (
        ContentState.BLOCKED,
        ContentState.REJECTED,
        ContentState.PUBLISHED,
        ContentState.CANCELLED,
    ):
        item = make_item(db_session, profile_setup, state)
        assert not can_transition(state, ContentState.DRAFTED)
        with pytest.raises(IllegalContentTransition):
            transition(db_session, profile_setup.scope, item.id, ContentState.DRAFTED)


def test_edit_paths_stay_closed_until_m4(profile_setup: ProfileSetup, db_session) -> None:
    for state in (ContentState.BLOCKED, ContentState.REJECTED):
        item = make_item(db_session, profile_setup, state)
        with pytest.raises(IllegalContentTransition):
            transition(db_session, profile_setup.scope, item.id, ContentState.DRAFTED)


def test_cancelled_edges_from_failed_and_needs_review(
    profile_setup: ProfileSetup, db_session
) -> None:
    for state in (ContentState.FAILED, ContentState.NEEDS_REVIEW):
        assert can_transition(state, ContentState.CANCELLED)
        item = make_item(db_session, profile_setup, state)
        updated = transition(db_session, profile_setup.scope, item.id, ContentState.CANCELLED)
        assert updated.state == "cancelled"


def test_transition_is_profile_scoped(profile_setup: ProfileSetup, db_session) -> None:
    other = load_profile(write_profile_skeleton(profile_setup.layout.profiles, "beta"))
    summary, _ = register_definition(db_session, other)
    other_scope = ProfileScope(summary.id)

    item = make_item(db_session, profile_setup, ContentState.DRAFTED)
    with pytest.raises(ContentItemNotFoundError):
        transition(db_session, other_scope, item.id, ContentState.REVIEWED)
