"""Pydantic parsing tests for policy.yaml and banned.yaml."""

from __future__ import annotations

import pytest

from heraldesk.domain.errors import ProfileValidationError
from heraldesk.domain.policy import (
    NotifyMode,
    PolicyAction,
    PolicyMode,
    PolicyRisk,
    parse_banned_yaml,
    parse_policy_yaml,
)

pytestmark = pytest.mark.core

FULL_POLICY = """\
default_mode: draft_only
rules:
  - { action: post, risk: low, mode: auto_after_delay, delay_minutes: 30 }
  - { action: post, risk: medium, mode: require_approval }
  - { action: post, risk: high, mode: require_approval }
  - { action: reply, risk: low, intents: [praise], mode: auto }
  - { action: reply, risk: medium, mode: require_approval }
quiet_hours: { start: "21:00", end: "07:00" }
max_posts_per_day: 3
disclose_ai: true
"""


def test_empty_policy_is_safe_by_default() -> None:
    policy = parse_policy_yaml("")
    assert policy.default_mode is PolicyMode.DRAFT_ONLY
    assert policy.rules == []
    assert policy.quiet_hours is None
    assert policy.max_posts_per_day == 3
    assert policy.disclose_ai is False
    assert policy.notify is NotifyMode.ALL


def test_full_policy_parses() -> None:
    policy = parse_policy_yaml(FULL_POLICY, source="acme/policy.yaml")
    assert policy.default_mode is PolicyMode.DRAFT_ONLY
    assert len(policy.rules) == 5
    assert PolicyMode("auto_publish") is PolicyMode.AUTO_PUBLISH
    assert parse_policy_yaml("default_mode: auto_publish\n").default_mode is PolicyMode.AUTO_PUBLISH
    first = policy.rules[0]
    assert first.action is PolicyAction.POST
    assert first.risk is PolicyRisk.LOW
    assert first.mode is PolicyMode.AUTO_AFTER_DELAY
    assert first.delay_minutes == 30
    assert policy.rules[3].intents == ["praise"]
    assert policy.quiet_hours is not None
    assert policy.quiet_hours.start == "21:00"
    assert policy.quiet_hours.end == "07:00"
    assert policy.disclose_ai is True


def test_notify_mode_parses_and_rejects_unknown_values() -> None:
    assert parse_policy_yaml("notify: off\n").notify is NotifyMode.OFF
    assert parse_policy_yaml("notify: approvals_only\n").notify is NotifyMode.APPROVALS_ONLY
    with pytest.raises(ProfileValidationError, match=r"notify"):
        parse_policy_yaml("notify: sometimes\n")


def test_unknown_policy_field_is_rejected() -> None:
    with pytest.raises(ProfileValidationError, match=r"policy\.yaml"):
        parse_policy_yaml("default_mode: draft_only\nauto_post: true\n")


def test_invalid_mode_is_rejected_with_the_field() -> None:
    text = "rules:\n  - { action: post, risk: low, mode: yolo }\n"
    with pytest.raises(ProfileValidationError) as error:
        parse_policy_yaml(text, source="acme/policy.yaml")
    message = str(error.value)
    assert "acme/policy.yaml" in message
    assert "rules.0.mode" in message


def test_invalid_risk_is_rejected() -> None:
    text = "rules:\n  - { action: post, risk: blocked, mode: require_approval }\n"
    with pytest.raises(ProfileValidationError, match=r"rules\.0\.risk"):
        parse_policy_yaml(text)


def test_auto_after_delay_requires_a_delay() -> None:
    text = "rules:\n  - { action: post, risk: low, mode: auto_after_delay }\n"
    with pytest.raises(ProfileValidationError, match="delay_minutes"):
        parse_policy_yaml(text)


def test_quiet_hours_must_be_valid_times() -> None:
    text = 'quiet_hours: { start: "25:00", end: "07:00" }\n'
    with pytest.raises(ProfileValidationError, match="24-hour time"):
        parse_policy_yaml(text)


def test_quiet_hours_start_and_end_must_differ() -> None:
    text = 'quiet_hours: { start: "07:00", end: "07:00" }\n'
    with pytest.raises(ProfileValidationError, match="must differ"):
        parse_policy_yaml(text)


def test_max_posts_per_day_must_not_be_negative() -> None:
    with pytest.raises(ProfileValidationError, match="max_posts_per_day"):
        parse_policy_yaml("max_posts_per_day: -1\n")


def test_malformed_yaml_reports_a_location() -> None:
    with pytest.raises(ProfileValidationError) as error:
        parse_policy_yaml("rules: [\n", source="acme/policy.yaml")
    assert "acme/policy.yaml" in str(error.value)
    assert "line" in str(error.value)


def test_top_level_must_be_a_mapping() -> None:
    with pytest.raises(ProfileValidationError, match="top level"):
        parse_policy_yaml("- just\n- a list\n")


def test_banned_lists_parse_and_deduplicate() -> None:
    banned = parse_banned_yaml(
        """\
banned_words: ["synergy", " synergy ", ""]
banned_topics: [politics]
required_disclaimers: ["AI-assisted"]
"""
    )
    assert banned.banned_words == ["synergy"]
    assert banned.banned_topics == ["politics"]
    assert banned.required_disclaimers == ["AI-assisted"]


def test_banned_unknown_field_is_rejected() -> None:
    with pytest.raises(ProfileValidationError, match=r"banned\.yaml"):
        parse_banned_yaml("banned_word: [typo]\n")
