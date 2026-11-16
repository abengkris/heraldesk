"""Pydantic schemas for ``policy.yaml`` and ``banned.yaml``.

The autonomy policy is parsed in M1; the engine that applies it arrives in M4. Rule
matching is documented here for users and will be implemented when the engine lands:
most specific (action + risk + intent) first, then first match in file order, then
``default_mode``.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from heraldesk.domain.errors import ProfileValidationError

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class PolicyMode(StrEnum):
    """How much freedom a profile gives an action."""

    DRAFT_ONLY = "draft_only"
    REQUIRE_APPROVAL = "require_approval"
    AUTO = "auto"
    AUTO_AFTER_DELAY = "auto_after_delay"


class PolicyAction(StrEnum):
    """Actions covered by policy rules."""

    POST = "post"
    REPLY = "reply"


class PolicyRisk(StrEnum):
    """Risk levels a rule can match. ``blocked`` never reaches policy."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QuietHours(_StrictModel):
    """A local-time window when nothing may be published."""

    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, value: str) -> str:
        if not _TIME_PATTERN.fullmatch(value):
            raise ValueError("must be a 24-hour time such as 21:00")
        return value

    @model_validator(mode="after")
    def _not_empty_window(self) -> Self:
        if self.start == self.end:
            raise ValueError("quiet_hours start and end must differ")
        return self


class PolicyRule(_StrictModel):
    """One autonomy rule. ``delay_minutes`` is required for ``auto_after_delay``."""

    action: PolicyAction
    risk: PolicyRisk
    mode: PolicyMode
    delay_minutes: int = Field(default=0, ge=0)
    intents: list[str] = Field(default_factory=list)

    @field_validator("intents")
    @classmethod
    def _clean_intents(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _delay_is_required(self) -> Self:
        if self.mode is PolicyMode.AUTO_AFTER_DELAY and self.delay_minutes <= 0:
            raise ValueError("mode auto_after_delay requires delay_minutes greater than 0")
        return self


class AutonomyPolicy(_StrictModel):
    """Parsed ``policy.yaml``. Safe by default: ``draft_only`` with no rules."""

    default_mode: PolicyMode = PolicyMode.DRAFT_ONLY
    rules: list[PolicyRule] = Field(default_factory=list)
    quiet_hours: QuietHours | None = None
    max_posts_per_day: int = Field(default=3, ge=0)
    disclose_ai: bool = False


class BannedLists(_StrictModel):
    """Parsed ``banned.yaml``."""

    banned_words: list[str] = Field(default_factory=list)
    banned_topics: list[str] = Field(default_factory=list)
    required_disclaimers: list[str] = Field(default_factory=list)

    @field_validator("banned_words", "banned_topics", "required_disclaimers")
    @classmethod
    def _clean_entries(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))


def _load_mapping(text: str, *, source: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ProfileValidationError(f"{source}: invalid YAML{location}: {problem}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ProfileValidationError(f"{source}: expected a YAML mapping at the top level")
    return loaded


def parse_policy_yaml(text: str, *, source: str = "policy.yaml") -> AutonomyPolicy:
    """Parse and validate ``policy.yaml`` text.

    Raises :class:`ProfileValidationError` with the source path and every Pydantic error.
    """
    data = _load_mapping(text, source=source)
    try:
        return AutonomyPolicy.model_validate(data)
    except ValidationError as exc:
        raise ProfileValidationError(f"{source}: invalid autonomy policy:\n{exc}") from exc


def parse_banned_yaml(text: str, *, source: str = "banned.yaml") -> BannedLists:
    """Parse and validate ``banned.yaml`` text."""
    data = _load_mapping(text, source=source)
    try:
        return BannedLists.model_validate(data)
    except ValidationError as exc:
        raise ProfileValidationError(f"{source}: invalid banned lists:\n{exc}") from exc
