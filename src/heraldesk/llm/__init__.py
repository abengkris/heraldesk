"""Heraldesk LLM layer: providers, cost accounting and structured outputs."""

from __future__ import annotations

from heraldesk.llm.budget import CostTracker, CostWindow, profile_day_window, utc_day_window
from heraldesk.llm.errors import DailyCostCapExceeded, LLMConfigError, LLMError
from heraldesk.llm.factory import build_llm, export_dotenv_keys, resolve_model
from heraldesk.llm.fake import FakeLLM
from heraldesk.llm.types import ChatMessage, LLMProvider, LLMRequest, LLMResult

__all__ = [
    "ChatMessage",
    "CostTracker",
    "CostWindow",
    "DailyCostCapExceeded",
    "FakeLLM",
    "LLMConfigError",
    "LLMError",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "build_llm",
    "export_dotenv_keys",
    "profile_day_window",
    "resolve_model",
    "utc_day_window",
]
