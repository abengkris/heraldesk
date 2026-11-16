"""LiteLLM-backed provider.

LiteLLM is imported lazily so the CLI, the fake provider and the test suite never pay its
import cost, and so an installation without the optional stack still runs offline.

The provider talks to the network only when ``complete`` is called. Telemetry is disabled
explicitly to keep the "no phone-home" rule. API keys are read by LiteLLM from the process
environment, which the factory fills from the instance ``.env``.
"""

from __future__ import annotations

import importlib
from typing import Any

from heraldesk.llm.errors import LLMError
from heraldesk.llm.types import LLMRequest, LLMResult


class LiteLLMProvider:
    """Synchronous LiteLLM completion wrapper with timeout and usage extraction."""

    name = "litellm"

    def __init__(self, *, model: str, timeout_seconds: float = 60.0) -> None:
        if not model.strip():
            raise LLMError("llm.default_model is empty; set a LiteLLM model in heraldesk.yaml")
        self.default_model = model.strip()
        self._timeout_seconds = timeout_seconds
        self._module: Any = None

    def _litellm(self) -> Any:
        if self._module is None:
            try:
                module: Any = importlib.import_module("litellm")
            except ImportError as exc:  # pragma: no cover - dependency is installed
                raise LLMError(
                    "litellm is not installed; reinstall heraldesk to use a real provider"
                ) from exc
            # Heraldesk sends no telemetry. Disable LiteLLM's own reporting as well.
            module.telemetry = False
            module.suppress_debug_info = True
            self._module = module
        return self._module

    def complete(self, request: LLMRequest) -> LLMResult:
        """Call LiteLLM once and return text, tokens and cost."""
        module = self._litellm()
        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            "temperature": request.temperature,
            "timeout": self._timeout_seconds,
        }
        if request.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens

        try:
            response = module.completion(**kwargs)
        except Exception as exc:
            raise LLMError(f"LLM call failed ({type(exc).__name__})") from exc

        text = self._extract_text(response)
        tokens_in, tokens_out = self._extract_usage(response)
        return LLMResult(
            text=text,
            model=str(getattr(response, "model", None) or request.model),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=self._extract_cost(module, response),
        )

    @staticmethod
    def _extract_text(response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            raise LLMError("LLM provider returned no choices")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM provider returned an empty message")
        return content

    @staticmethod
    def _extract_usage(response: Any) -> tuple[int, int]:
        usage = getattr(response, "usage", None)
        tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
        return tokens_in, tokens_out

    @staticmethod
    def _extract_cost(module: Any, response: Any) -> float:
        try:
            return float(module.completion_cost(completion_response=response))
        except Exception:
            # Cost reporting must never fail a run; an unknown model simply records 0.
            return 0.0
