"""LLM layer error types."""

from __future__ import annotations

from datetime import datetime


class LLMError(Exception):
    """Raised when a provider call cannot complete.

    Messages are safe to show: callers still redact them before persistence.
    """


class LLMConfigError(LLMError):
    """Raised when the configured provider or model cannot be used."""


class DailyCostCapExceeded(LLMError):
    """Raised before a call that would exceed ``llm.max_daily_cost_usd``."""

    def __init__(
        self, *, spent_usd: float, pending_usd: float, cap_usd: float, window_start: datetime
    ) -> None:
        self.spent_usd = spent_usd
        self.pending_usd = pending_usd
        self.cap_usd = cap_usd
        self.window_start = window_start
        super().__init__(
            f"daily LLM cost cap reached: ${spent_usd + pending_usd:.4f} of "
            f"${cap_usd:.2f} used in the UTC day starting {window_start.isoformat()}"
        )
