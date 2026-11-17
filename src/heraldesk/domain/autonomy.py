"""The M4 autonomy policy engine.

The engine is deterministic and side-effect free. It maps a profile's parsed policy and
a Compliance risk level to one of two M4 outcomes:

* ``pending_approval``: create an approval request and wait for a human.
* ``auto_approve``: move the item through ``approved`` to ``scheduled`` immediately.

Only ``draft_only``, ``require_approval`` and ``auto_publish`` are implemented. The
M5 scheduler modes ``auto`` and ``auto_after_delay`` are deliberately downgraded to
``require_approval`` so no draft can be auto-approved before the scheduler exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from heraldesk.domain.policy import AutonomyPolicy, PolicyAction, PolicyMode, PolicyRisk


class AutonomyDecision(StrEnum):
    """What the pipeline must do with a draft that passed Compliance."""

    PENDING_APPROVAL = "pending_approval"
    AUTO_APPROVE = "auto_approve"


@dataclass(frozen=True, slots=True)
class AutonomyResult:
    """One engine decision plus the mode and reason behind it."""

    mode: PolicyMode
    decision: AutonomyDecision
    reason: str


_DEFERRED_MODES: Final = frozenset({PolicyMode.AUTO, PolicyMode.AUTO_AFTER_DELAY})


def _risk_or_none(risk_level: str | None) -> PolicyRisk | None:
    if risk_level is None:
        return None
    try:
        return PolicyRisk(risk_level)
    except ValueError:
        return None


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
    matching = [
        rule
        for rule in policy.rules
        if rule.action is action
        and rule.risk is risk
        and (not rule.intents or (intent is not None and intent in rule.intents))
    ]
    for rule in matching:
        if rule.intents:
            return rule.mode
    if matching:
        return matching[0].mode
    return policy.default_mode


def decide(
    policy: AutonomyPolicy,
    *,
    action: PolicyAction,
    risk_level: str | None,
    compliance_passed: bool,
    intent: str | None = None,
) -> AutonomyResult:
    """Apply the M4 autonomy matrix to one draft.

    ``compliance_passed`` must be false for any item Compliance blocked. A draft that
    fails Compliance can never be auto-approved, even in ``auto_publish`` mode.
    """
    risk = _risk_or_none(risk_level)
    if risk is None:
        return AutonomyResult(
            mode=policy.default_mode,
            decision=AutonomyDecision.PENDING_APPROVAL,
            reason="risk level is not an eligible autonomy risk; human approval required",
        )

    mode = resolve_mode(policy, action=action, risk=risk, intent=intent)

    if mode in _DEFERRED_MODES:
        return AutonomyResult(
            mode=mode,
            decision=AutonomyDecision.PENDING_APPROVAL,
            reason=f"mode {mode.value} is reserved for M5; treated as require_approval",
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
