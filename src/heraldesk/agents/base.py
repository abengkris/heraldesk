"""Agent base class.

An agent declares a role, the tools it is allowed to use, its input schema and its output
schema. It never calls an LLM directly: :class:`heraldesk.agents.runner.AgentRunner` owns
the provider call, validation, retries and cost accounting.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel

from heraldesk.db.scope import ProfileScope
from heraldesk.domain.profiles import ProfileDefinition
from heraldesk.platforms.base import PlatformConstraints


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Everything an agent may see about one profile and one platform."""

    scope: ProfileScope
    profile: ProfileDefinition
    platform: str | None = None
    constraints: PlatformConstraints | None = None
    memories: tuple[str, ...] = ()


class Agent(ABC):
    """Base class for every Heraldesk agent."""

    role: ClassVar[str]
    tools: ClassVar[tuple[str, ...]] = ()
    input_schema: ClassVar[type[BaseModel]]
    output_schema: ClassVar[type[BaseModel]]

    @abstractmethod
    def build_user_prompt(self, payload: BaseModel) -> str:
        """Render the validated task input as the user message."""

    def validate_output(self, output: BaseModel, context: AgentContext) -> None:  # noqa: B027
        """Validate role-specific rules after Pydantic parsing.

        The default accepts every schema-valid output. Agents override this and raise
        :class:`heraldesk.agents.errors.AgentOutputError` to trigger a retry.
        """

    def _payload_json(self, payload: BaseModel) -> str:
        return json.dumps(payload.model_dump(mode="json"), indent=2, ensure_ascii=False)
