"""Deterministic anti-slop lint: accept and reject cases."""

from __future__ import annotations

import pytest

from heraldesk.domain.policy import BannedLists
from heraldesk.quality import lint
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core


def profile_with(setup: ProfileSetup, banned: BannedLists):
    return setup.definition.model_copy(update={"banned": banned})


def test_clean_draft_is_accepted(profile_setup: ProfileSetup) -> None:
    result = lint(
        "We shipped a smaller dashboard. It loads in under a second on the same hardware.",
        profile_setup.definition,
    )
    assert result.ok
    assert result.issues == ()


def test_em_dash_is_rejected(profile_setup: ProfileSetup) -> None:
    result = lint("The dashboard is new \u2014 and faster.", profile_setup.definition)
    assert not result.ok
    assert any(issue.rule == "em_dash" for issue in result.issues)


def test_default_buzzword_is_rejected_case_insensitively(profile_setup: ProfileSetup) -> None:
    result = lint("We unlock SYNERGY for every team.", profile_setup.definition)
    assert not result.ok
    assert {issue.rule for issue in result.issues} == {"buzzword"}
    assert len(result.issues) >= 2


def test_profile_banned_word_is_rejected(profile_setup: ProfileSetup) -> None:
    profile = profile_with(profile_setup, BannedLists(banned_words=["hustle"]))
    result = lint("This hustle culture advice is bad.", profile)
    assert not result.ok
    assert any(issue.rule == "banned_word" for issue in result.issues)


def test_empty_buzzword_list_turns_the_default_rule_off(profile_setup: ProfileSetup) -> None:
    profile = profile_with(profile_setup, BannedLists(banned_buzzwords=[]))
    result = lint("We unlock synergy for every team.", profile)
    assert result.ok


def test_custom_buzzword_list_is_used(profile_setup: ProfileSetup) -> None:
    profile = profile_with(profile_setup, BannedLists(banned_buzzwords=["purple cow"]))
    assert not lint("Our purple cow strategy.", profile).ok
    assert lint("We unlock synergy for every team.", profile).ok


def test_em_dash_can_be_allowed_per_profile(profile_setup: ProfileSetup) -> None:
    profile = profile_with(profile_setup, BannedLists(allow_em_dash=True))
    assert lint("The dashboard is new \u2014 and faster.", profile).ok


def test_whole_words_only(profile_setup: ProfileSetup) -> None:
    profile = profile_with(profile_setup, BannedLists(banned_words=["unlock"]))
    assert lint("The door was unlocked.", profile).ok
    assert not lint("We unlock the door.", profile).ok
