"""Documentation integrity checks."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.core

ROOT = Path(__file__).resolve().parents[1]


def test_security_policy_is_not_a_readme_copy() -> None:
    text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert text.startswith("# Security Policy")
    assert "Reporting a vulnerability" in text
    assert "# Heraldesk\n" not in text


def test_readme_keeps_its_heading() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert text.startswith("# Heraldesk")
    assert "## Requirements" in text


def test_key_documents_are_present_and_non_empty() -> None:
    for name in (
        "README.md",
        "AGENTS.md",
        "SECURITY.md",
        "ROADMAP.md",
        "CHANGELOG.md",
        "docs/architecture.md",
        "docs/decisions.md",
        "docs/open-questions.md",
        "docs/platforms/README.md",
        "docs/platforms/nostr.md",
        "docs/platforms/threads.md",
        "docs/releasing.md",
    ):
        path = ROOT / name
        assert path.is_file(), name
        assert path.stat().st_size > 100, name
