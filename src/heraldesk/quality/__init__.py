"""Deterministic content quality checks.

The lint runner is intentionally separate from the LLM Compliance agent: it runs first,
costs nothing and produces stable, explainable failures.
"""

from __future__ import annotations

from heraldesk.quality.lint import (
    DEFAULT_BUZZWORDS,
    LintIssue,
    LintResult,
    lint,
)

__all__ = ["DEFAULT_BUZZWORDS", "LintIssue", "LintResult", "lint"]
