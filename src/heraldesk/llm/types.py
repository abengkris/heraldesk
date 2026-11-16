"""Shared LLM request, result and provider types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One chat message in an LLM request."""

    role: MessageRole
    content: str


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """A single completion request.

    ``agent`` names the agent role so the fake provider can pick a fixture and traces can
    be attributed without parsing the prompt.
    """

    messages: tuple[ChatMessage, ...]
    model: str
    agent: str = ""
    temperature: float = 0.0
    json_mode: bool = True
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class LLMResult:
    """A provider response with usage information."""

    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


class LLMProvider(Protocol):
    """Minimal synchronous provider contract used by the agent runtime."""

    name: str
    default_model: str

    def complete(self, request: LLMRequest) -> LLMResult:
        """Return one completion or raise :class:`heraldesk.llm.errors.LLMError`."""
        ...
