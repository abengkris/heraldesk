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
from typing import Any, Final

from heraldesk.domain.errors import ProfileValidationError
from heraldesk.domain.policy import (
    AutonomyPolicy,
    BannedLists,
    PolicyAction,
    PolicyMode,
    PolicyRisk,
    PolicyRule,
    parse_banned_yaml,
    parse_policy_yaml,
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


_MODE_RANK = {
    PolicyMode.DRAFT_ONLY: 0,
    PolicyMode.REQUIRE_APPROVAL: 1,
    PolicyMode.AUTO_PUBLISH: 2,
    PolicyMode.AUTO: 3,
    PolicyMode.AUTO_AFTER_DELAY: 4,
}
_RISK_RANK = {PolicyRisk.LOW: 0, PolicyRisk.MEDIUM: 1, PolicyRisk.HIGH: 2}
_AUTO_MODES = (PolicyMode.AUTO, PolicyMode.AUTO_AFTER_DELAY)


def loosens_autonomy(filename: str, old_text: str, new_text: str) -> bool:
    """Return ``True`` when a one-file edit gives the agents more freedom.

    Pure and conservative: unparsable input never reports a loosening (the save path
    rejects invalid files separately).
    """
    if old_text == new_text:
        return False
    try:
        if filename == "policy.yaml":
            return _policy_loosens(
                parse_policy_yaml(old_text, source="policy.yaml"),
                parse_policy_yaml(new_text, source="policy.yaml"),
            )
        if filename == "banned.yaml":
            return _banned_loosens(
                parse_banned_yaml(old_text, source="banned.yaml"),
                parse_banned_yaml(new_text, source="banned.yaml"),
            )
    except (ProfileValidationError, ValueError):
        return False
    return False


def _policy_loosens(old: AutonomyPolicy, new: AutonomyPolicy) -> bool:
    if new.default_mode is not PolicyMode.DRAFT_ONLY and (
        _MODE_RANK[new.default_mode] > _MODE_RANK[old.default_mode]
    ):
        return True
    if new.max_posts_per_day > old.max_posts_per_day:
        return True
    if old.disclose_ai and not new.disclose_ai:
        return True
    if _quiet_loosens(old, new):
        return True
    return _rules_loosen(old, new)


def _quiet_loosens(old: AutonomyPolicy, new: AutonomyPolicy) -> bool:
    if old.quiet_hours is None:
        return False
    if new.quiet_hours is None:
        return True
    return _quiet_minutes(new) < _quiet_minutes(old)


def _quiet_minutes(policy: AutonomyPolicy) -> int:
    assert policy.quiet_hours is not None
    start = _clock_minutes(policy.quiet_hours.start)
    end = _clock_minutes(policy.quiet_hours.end)
    return end - start if start < end else 1440 - start + end


def _clock_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _rules_loosen(old: AutonomyPolicy, new: AutonomyPolicy) -> bool:
    old_by_key: dict[tuple[str, tuple[str, ...]], list[Any]] = {}
    for rule in old.rules:
        old_by_key.setdefault((rule.action.value, tuple(sorted(rule.intents))), []).append(rule)
    for rule in new.rules:
        key = (rule.action.value, tuple(sorted(rule.intents)))
        candidates = old_by_key.get(key)
        if not candidates:
            if rule.mode in _AUTO_MODES:
                return True
            continue
        previous = candidates.pop(0)
        if previous.mode not in _AUTO_MODES and rule.mode in _AUTO_MODES:
            return True
        if rule.delay_minutes < previous.delay_minutes:
            return True
        if (
            previous.mode in _AUTO_MODES
            and rule.mode in _AUTO_MODES
            and _RISK_RANK[rule.risk] > _RISK_RANK[previous.risk]
        ):
            return True
    return False


def _banned_loosens(old: BannedLists, new: BannedLists) -> bool:
    return bool(set(old.banned_words) - set(new.banned_words)) or bool(
        set(old.banned_topics) - set(new.banned_topics)
    )
