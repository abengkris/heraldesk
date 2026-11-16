"""Structured input and output schemas for agents.

Only the Writer is real in M2. The other agents get their schemas when their milestone
starts, so nothing here guesses at future behaviour.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WriterInput(BaseModel):
    """One content idea handed to the Writer."""

    model_config = ConfigDict(extra="forbid")

    idea: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    angle: str | None = None
    evidence: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    """A platform-ready draft with optional variants and hashtags."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
