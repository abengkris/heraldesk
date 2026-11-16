"""Agent runtime error types."""

from __future__ import annotations


class AgentError(Exception):
    """Base class for agent configuration and execution failures."""


class AgentConfigError(AgentError):
    """Raised when an agent definition, prompt or schema is unusable."""


class AgentOutputError(AgentError):
    """Raised when a model reply cannot be parsed or validated against the schema."""


class AgentRunError(AgentError):
    """Raised when one agent run fails. The failed run is already recorded."""

    def __init__(self, message: str, *, run_id: int | None = None) -> None:
        self.run_id = run_id
        super().__init__(message)
