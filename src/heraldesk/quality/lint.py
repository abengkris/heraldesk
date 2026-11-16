"""Anti-slop lint: deterministic, configurable and free.

Rules in M3:
- reject an em dash (U+2014) unless ``allow_em_dash`` is true;
- reject buzzwords from the built-in list, overridable per profile;
- reject words from the profile's ``banned.yaml`` ``banned_words`` list.

Matching is case-insensitive and operates on whole words or phrases, so ``unlock`` does
not match ``unlocked`` and a banned two-word phrase still matches across normal spaces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from heraldesk.domain.profiles import ProfileDefinition

DEFAULT_BUZZWORDS: Final[tuple[str, ...]] = (
    "game-changer",
    "game changer",
    "synergy",
    "synergies",
    "leverage",
    "seamless",
    "cutting-edge",
    "revolutionary",
    "world-class",
    "best-in-class",
    "next-generation",
    "elevate",
    "empower",
    "unleash",
    "delve",
    "robust",
    "unlock",
    "unlocking",
    "in today's fast-paced world",
    "look no further",
    "it's not just",
)


@dataclass(frozen=True, slots=True)
class LintIssue:
    """One deterministic lint failure."""

    rule: str
    message: str
    match: str


@dataclass(frozen=True, slots=True)
class LintResult:
    """The outcome of linting one draft."""

    issues: tuple[LintIssue, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Return ``True`` when the draft passed every rule."""
        return not self.issues

    @property
    def summary(self) -> str:
        """Return a compact human-readable reason for retry prompts and audit logs."""
        return "; ".join(issue.message for issue in self.issues)


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", re.IGNORECASE)


def _find_terms(text: str, terms: tuple[str, ...], *, rule: str, label: str) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for term in terms:
        if not term.strip():
            continue
        match = _pattern(term).search(text)
        if match is not None:
            issues.append(
                LintIssue(
                    rule=rule,
                    message=f"{label}: {match.group(0)}",
                    match=match.group(0),
                )
            )
    return issues


def lint(text: str, profile: ProfileDefinition) -> LintResult:
    """Lint one draft against the profile's deterministic rules."""
    issues: list[LintIssue] = []

    if not profile.banned.allow_em_dash and "\u2014" in text:
        issues.append(
            LintIssue(
                rule="em_dash",
                message="em dash is not allowed; use a full stop or a comma",
                match="\u2014",
            )
        )

    configured = profile.banned.banned_buzzwords
    buzzwords = DEFAULT_BUZZWORDS if configured is None else tuple(configured)
    issues.extend(_find_terms(text, buzzwords, rule="buzzword", label="banned buzzword"))

    banned_words = tuple(profile.banned.banned_words)
    issues.extend(_find_terms(text, banned_words, rule="banned_word", label="banned word"))

    return LintResult(issues=tuple(issues))
