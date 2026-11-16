"""Structured input and output schemas for agents.

M2 defined the Writer contract. M3 adds the Strategist, Researcher and Compliance
contracts used by the ``daily_content`` pipeline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WriterInput(BaseModel):
    """One content idea handed to the Writer."""

    model_config = ConfigDict(extra="forbid")

    idea: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    angle: str | None = None
    evidence: list[str] = Field(default_factory=list)
    feedback: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    """A platform-ready draft with optional variants and hashtags."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)


class StrategistInput(BaseModel):
    """What the Strategist needs to choose today's slots."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(min_length=1)
    platforms: list[str] = Field(min_length=1)
    slots: int = Field(default=1, ge=1, le=10)


class ContentSlot(BaseModel):
    """One planned post: which platform, which pillar and which goal."""

    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1)
    pillar: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    angle: str = ""


class ContentPlan(BaseModel):
    """The Strategist's plan for one day."""

    model_config = ConfigDict(extra="forbid")

    slots: list[ContentSlot] = Field(min_length=1)
    pillars: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)


class ResearcherInput(BaseModel):
    """Research task for one slot, including untrusted search evidence."""

    model_config = ConfigDict(extra="forbid")

    date: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    pillar: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    angle: str = ""
    evidence: list[str] = Field(default_factory=list)


class Idea(BaseModel):
    """One researched content idea."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    angle: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class ResearchOutput(BaseModel):
    """Ideas returned by the Researcher."""

    model_config = ConfigDict(extra="forbid")

    ideas: list[Idea] = Field(min_length=1)


class ComplianceInput(BaseModel):
    """A draft handed to Compliance for a risk assessment."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    platform: str = Field(min_length=1)


class RiskAssessment(BaseModel):
    """Compliance's structured verdict."""

    model_config = ConfigDict(extra="forbid")

    risk_level: Literal["low", "medium", "high", "blocked"]
    reasons: list[str] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list)
