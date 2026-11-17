"""The autonomy policy engine.

The engine is deterministic and side-effect free. It maps a profile's parsed policy and
a Compliance risk level to one of two outcomes:

* ``pending_approval``: create an approval request and wait for a human.
* ``auto_approve``: the item may move through ``approved`` to ``scheduled`` immediately.

M4 implemented ``draft_only``, ``require_approval`` and ``auto_publish``. M5 completes the
vocabulary: ``auto`` publishes as soon as the item is approved, and ``auto_after_delay``
schedules it after the rule's ``delay_minutes`` so the user still has a cancellation
window. A ``default_mode: auto_after_delay`` without a rule uses 30 minutes.

Compliance always gates every mode: an item that did not pass Compliance can never be
auto-approved, even under ``auto`` or ``auto_after_delay``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from heraldesk.domain.policy import (
    AutonomyPolicy,
    PolicyAction,
    PolicyMode,
    PolicyRisk,
    PolicyRule,
)

DEFAULT_AUTO_AFTER_DELAY_MINUTES: Final = 30


class AutonomyDecision(StrEnum):
    """What the pipeline must do with a draft that passed Compliance."""

    PENDING_APPROVAL = "pending_approval"
    AUTO_APPROVE = "auto_approve"


@dataclass(frozen=True, slots=True)
class AutonomyResult:
    """One engine decision plus the mode, delay and reason behind it."""

    mode: PolicyMode
    decision: AutonomyDecision
    reason: str
    delay_minutes: int = 0


def _risk_or_none(risk_level: str | None) -> PolicyRisk | None:
    if risk_level is None:
        return None
    try:
        return PolicyRisk(risk_level)
    except ValueError:
        return None


def _matching_rules(
    policy: AutonomyPolicy,
    *,
    action: PolicyAction,
    risk: PolicyRisk,
    intent: str | None,
) -> list[PolicyRule]:
    return [
        rule
        for rule in policy.rules
        if rule.action is action
        and rule.risk is risk
        and (not rule.intents or (intent is not None and intent in rule.intents))
    ]


def _winning_rule(
    policy: AutonomyPolicy,
    *,
    action: PolicyAction,
    risk: PolicyRisk,
    intent: str | None,
) -> PolicyRule | None:
    matching = _matching_rules(policy, action=action, risk=risk, intent=intent)
    for rule in matching:
        if rule.intents:
            return rule
    return matching[0] if matching else None


def resolve_mode(
    policy: AutonomyPolicy,
    *,
    action: PolicyAction,
    risk: PolicyRisk,
    intent: str | None = None,
) -> PolicyMode:
    """Return the most specific matching rule mode, or ``default_mode``.

    Rules with a non-empty ``intents`` list are more specific than rules without one.
    Among equal specificity the first rule in file order wins.
    """
    rule = _winning_rule(policy, action=action, risk=risk, intent=intent)
    if rule is not None:
        return rule.mode
    return policy.default_mode


def decide(
    policy: AutonomyPolicy,
    *,
    action: PolicyAction,
    risk_level: str | None,
    compliance_passed: bool,
    intent: str | None = None,
) -> AutonomyResult:
    """Apply the autonomy matrix to one draft.

    ``compliance_passed`` must be false for any item Compliance blocked. A draft that
    fails Compliance can never be auto-approved, even in ``auto`` modes.
    """
    risk = _risk_or_none(risk_level)
    if risk is None:
        return AutonomyResult(
            mode=policy.default_mode,
            decision=AutonomyDecision.PENDING_APPROVAL,
            reason="risk level is not an eligible autonomy risk; human approval required",
        )

    mode = resolve_mode(policy, action=action, risk=risk, intent=intent)
    rule = _winning_rule(policy, action=action, risk=risk, intent=intent)

    if mode is PolicyMode.AUTO:
        if compliance_passed:
            return AutonomyResult(
                mode=mode,
                decision=AutonomyDecision.AUTO_APPROVE,
                reason="auto: Compliance passed; publishing without human approval",
            )
        return AutonomyResult(
            mode=mode,
            decision=AutonomyDecision.PENDING_APPROVAL,
            reason="auto requires a passing Compliance check",
        )

    if mode is PolicyMode.AUTO_AFTER_DELAY:
        if not compliance_passed:
            return AutonomyResult(
                mode=mode,
                decision=AutonomyDecision.PENDING_APPROVAL,
                reason="auto_after_delay requires a passing Compliance check",
            )
        delay = DEFAULT_AUTO_AFTER_DELAY_MINUTES
        if rule is not None and rule.mode is PolicyMode.AUTO_AFTER_DELAY:
            delay = max(1, rule.delay_minutes)
        return AutonomyResult(
            mode=mode,
            decision=AutonomyDecision.AUTO_APPROVE,
            reason=f"auto_after_delay: publishing in {delay} minutes unless cancelled",
            delay_minutes=delay,
        )

    if mode is PolicyMode.AUTO_PUBLISH:
        if compliance_passed and risk is PolicyRisk.LOW:
            return AutonomyResult(
                mode=mode,
                decision=AutonomyDecision.AUTO_APPROVE,
                reason="auto_publish: low risk and Compliance passed",
            )
        return AutonomyResult(
            mode=mode,
            decision=AutonomyDecision.PENDING_APPROVAL,
            reason="auto_publish requires low risk and a passing Compliance check",
        )

    if mode is PolicyMode.REQUIRE_APPROVAL:
        return AutonomyResult(
            mode=mode,
            decision=AutonomyDecision.PENDING_APPROVAL,
            reason="require_approval: human approval required",
        )

    return AutonomyResult(
        mode=PolicyMode.DRAFT_ONLY,
        decision=AutonomyDecision.PENDING_APPROVAL,
        reason="draft_only: human approval required; the item never auto-schedules",
    )
