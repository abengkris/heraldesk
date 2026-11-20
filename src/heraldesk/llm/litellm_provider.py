"""LiteLLM-backed provider.

LiteLLM is imported lazily so the CLI, the fake provider and the test suite never pay its
import cost, and so an installation without the optional stack still runs offline.

The provider talks to the network only when ``complete`` is called. Telemetry is disabled
explicitly to keep the "no phone-home" rule. API keys are read by LiteLLM from the process
environment, which the factory fills from the instance ``.env``.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from typing import Any

from heraldesk.llm.errors import LLMError
from heraldesk.llm.types import LLMRequest, LLMResult


class LiteLLMProvider:
    """Synchronous LiteLLM completion wrapper with timeout and usage extraction."""

    name = "litellm"

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float = 60.0,
        min_interval_seconds: float = 0.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not model.strip():
            raise LLMError("llm.default_model is empty; set a LiteLLM model in heraldesk.yaml")
        if min_interval_seconds < 0:
            raise LLMError("llm.min_interval_seconds must not be negative")
        self.default_model = model.strip()
        self._timeout_seconds = timeout_seconds
        self._min_interval_seconds = min_interval_seconds
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._next_call_at = 0.0
        self._module: Any = None

    def _pace(self) -> None:
        """Wait until the configured minimum spacing has elapsed."""
        if self._min_interval_seconds <= 0:
            return
        now = self._clock()
        delay = self._next_call_at - now
        if delay > 0:
            self._sleep(delay)
            now = self._clock()
        self._next_call_at = now + self._min_interval_seconds

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
            # Reasoning models often accept only their default temperature. Let LiteLLM
            # drop parameters the selected model does not support instead of failing the run.
            module.drop_params = True
            self._module = module
        return self._module

    def complete(self, request: LLMRequest) -> LLMResult:
        """Call LiteLLM once and return text, tokens and cost."""
        self._pace()
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
            raise LLMError(self._describe_failure(exc)) from exc

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
    def _describe_failure(exc: Exception) -> str:
        """Return an actionable, secret-free description of a provider failure."""
        status = getattr(exc, "status_code", None)
        hints = {
            401: "authentication failed (HTTP 401); check the API key",
            402: "insufficient provider balance or credits (HTTP 402)",
            403: "provider denied access (HTTP 403); check model access",
            404: "model not found (HTTP 404); check llm.default_model",
            429: "provider rate limit or quota exhausted (HTTP 429)",
        }
        if status in hints:
            detail = hints[status]
        elif isinstance(status, int) and status >= 500:
            detail = f"provider server error (HTTP {status})"
        elif isinstance(status, int):
            detail = f"provider rejected the request (HTTP {status})"
        else:
            detail = type(exc).__name__
        return f"LLM call failed: {detail}"

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
