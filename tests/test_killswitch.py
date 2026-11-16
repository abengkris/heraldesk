"""Global and per-profile off-switch tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.bootstrap import InitResult
from heraldesk.core.locks import (
    BlockReason,
    KillSwitchError,
    OffSwitchState,
    engage_global_kill_switch,
    evaluate_off_switches,
    is_global_kill_switch_enabled,
    release_global_kill_switch,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.profiles import (
    ProfileDefinition,
    ProfileStatus,
    register_definition,
    set_profile_kill_switch,
    set_profile_status,
)

pytestmark = pytest.mark.core

MakeDefinition = Callable[[str], ProfileDefinition]


def test_global_marker_roundtrip(initialized: InitResult) -> None:
    layout = initialized.layout
    assert is_global_kill_switch_enabled(layout) is False
    assert engage_global_kill_switch(layout) is True
    assert is_global_kill_switch_enabled(layout) is True
    assert engage_global_kill_switch(layout) is False
    assert release_global_kill_switch(layout) is True
    assert is_global_kill_switch_enabled(layout) is False
    assert release_global_kill_switch(layout) is False


def test_global_marker_rejects_a_directory(initialized: InitResult) -> None:
    initialized.layout.killswitch_path.mkdir()
    with pytest.raises(KillSwitchError, match="not a file"):
        engage_global_kill_switch(initialized.layout)


def test_precedence_global_then_profile_then_status() -> None:
    decision = evaluate_off_switches(
        OffSwitchState(
            global_kill_switch=True,
            profile_kill_switch=True,
            profile_status="paused",
        )
    )
    assert decision.reason is BlockReason.GLOBAL_KILL_SWITCH

    decision = evaluate_off_switches(
        OffSwitchState(
            global_kill_switch=False,
            profile_kill_switch=True,
            profile_status="paused",
        )
    )
    assert decision.reason is BlockReason.PROFILE_KILL_SWITCH

    decision = evaluate_off_switches(
        OffSwitchState(
            global_kill_switch=False,
            profile_kill_switch=False,
            profile_status="paused",
        )
    )
    assert decision.reason is BlockReason.PROFILE_PAUSED

    decision = evaluate_off_switches(
        OffSwitchState(
            global_kill_switch=False,
            profile_kill_switch=False,
            profile_status="draft",
        )
    )
    assert decision.reason is BlockReason.PROFILE_DRAFT

    decision = evaluate_off_switches(
        OffSwitchState(
            global_kill_switch=False,
            profile_kill_switch=False,
            profile_status="active",
        )
    )
    assert decision.allowed is True
    assert decision.reason is None


def test_unknown_status_fails_closed() -> None:
    decision = evaluate_off_switches(
        OffSwitchState(
            global_kill_switch=False,
            profile_kill_switch=False,
            profile_status="something-new",
        )
    )
    assert decision.allowed is False
    assert decision.reason is BlockReason.UNKNOWN_STATUS


def test_per_profile_kill_switch_is_persisted(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    summary, _ = register_definition(db_session, make_definition("alpha"))
    scope = ProfileScope(summary.id)
    updated, changed = set_profile_kill_switch(db_session, scope, True)
    assert changed is True
    assert updated.kill_switch is True

    unchanged, changed_again = set_profile_kill_switch(db_session, scope, True)
    assert changed_again is False
    assert unchanged.kill_switch is True


def test_pause_and_resume_change_status(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    summary, _ = register_definition(db_session, make_definition("alpha"))
    scope = ProfileScope(summary.id)

    paused, changed = set_profile_status(db_session, scope, ProfileStatus.PAUSED)
    assert changed is True
    assert paused.status == "paused"

    resumed, changed = set_profile_status(db_session, scope, ProfileStatus.ACTIVE)
    assert changed is True
    assert resumed.status == "active"
