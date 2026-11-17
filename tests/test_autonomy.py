"""The M4 autonomy policy matrix and rule matching."""

from __future__ import annotations

import pytest

from heraldesk.domain.autonomy import (
    AutonomyDecision,
    decide,
    resolve_mode,
)
from heraldesk.domain.policy import (
    AutonomyPolicy,
    PolicyAction,
    PolicyMode,
    PolicyRisk,
    parse_policy_yaml,
)

pytestmark = pytest.mark.core


def policy(mode: PolicyMode, rules: str = "") -> AutonomyPolicy:
    text = f"default_mode: {mode.value}\n"
    if rules:
        text = f"{text}rules:\n{rules}"
    return parse_policy_yaml(text)


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
@pytest.mark.parametrize("passed", [True, False])
def test_draft_only_always_waits_for_a_human(risk: str, passed: bool) -> None:
    result = decide(
        policy(PolicyMode.DRAFT_ONLY),
        action=PolicyAction.POST,
        risk_level=risk,
        compliance_passed=passed,
    )
    assert result.mode is PolicyMode.DRAFT_ONLY
    assert result.decision is AutonomyDecision.PENDING_APPROVAL


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
@pytest.mark.parametrize("passed", [True, False])
def test_require_approval_always_waits_for_a_human(risk: str, passed: bool) -> None:
    result = decide(
        policy(PolicyMode.REQUIRE_APPROVAL),
        action=PolicyAction.POST,
        risk_level=risk,
        compliance_passed=passed,
    )
    assert result.mode is PolicyMode.REQUIRE_APPROVAL
    assert result.decision is AutonomyDecision.PENDING_APPROVAL


def test_auto_publish_low_risk_and_passing_compliance_is_allowed() -> None:
    result = decide(
        policy(PolicyMode.AUTO_PUBLISH),
        action=PolicyAction.POST,
        risk_level="low",
        compliance_passed=True,
    )
    assert result.mode is PolicyMode.AUTO_PUBLISH
    assert result.decision is AutonomyDecision.AUTO_APPROVE


@pytest.mark.parametrize(
    ("risk", "passed"),
    [
        ("high", True),
        ("medium", True),
        ("low", False),
        (None, True),
        ("blocked", True),
    ],
)
def test_auto_publish_anything_else_waits_for_a_human(risk: str | None, passed: bool) -> None:
    result = decide(
        policy(PolicyMode.AUTO_PUBLISH),
        action=PolicyAction.POST,
        risk_level=risk,
        compliance_passed=passed,
    )
    assert result.mode is PolicyMode.AUTO_PUBLISH
    assert result.decision is AutonomyDecision.PENDING_APPROVAL


def test_m5_modes_are_deferred_to_require_approval() -> None:
    for mode in (PolicyMode.AUTO, PolicyMode.AUTO_AFTER_DELAY):
        result = decide(
            policy(mode),
            action=PolicyAction.POST,
            risk_level="low",
            compliance_passed=True,
        )
        assert result.mode is mode
        assert result.decision is AutonomyDecision.PENDING_APPROVAL
        assert "M5" in result.reason


def test_rule_overrides_default_mode_by_risk() -> None:
    parsed = policy(
        PolicyMode.REQUIRE_APPROVAL,
        "  - { action: post, risk: low, mode: auto_publish }\n",
    )
    low = decide(
        parsed,
        action=PolicyAction.POST,
        risk_level="low",
        compliance_passed=True,
    )
    high = decide(
        parsed,
        action=PolicyAction.POST,
        risk_level="high",
        compliance_passed=True,
    )
    assert low.decision is AutonomyDecision.AUTO_APPROVE
    assert high.decision is AutonomyDecision.PENDING_APPROVAL


def test_intent_specific_rule_wins_over_file_order() -> None:
    parsed = policy(
        PolicyMode.DRAFT_ONLY,
        "  - { action: reply, risk: low, mode: require_approval }\n"
        "  - { action: reply, risk: low, intents: [praise], mode: auto_publish }\n",
    )
    assert (
        resolve_mode(
            parsed,
            action=PolicyAction.REPLY,
            risk=PolicyRisk.LOW,
            intent="praise",
        )
        is PolicyMode.AUTO_PUBLISH
    )
    assert (
        resolve_mode(
            parsed,
            action=PolicyAction.REPLY,
            risk=PolicyRisk.LOW,
            intent="question",
        )
        is PolicyMode.REQUIRE_APPROVAL
    )
    assert (
        resolve_mode(
            parsed,
            action=PolicyAction.POST,
            risk=PolicyRisk.LOW,
        )
        is PolicyMode.DRAFT_ONLY
    )
