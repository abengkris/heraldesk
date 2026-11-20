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
        "docs/draft-room.md",
        "docs/open-questions.md",
        "docs/platforms/README.md",
        "docs/platforms/nostr.md",
        "docs/platforms/threads.md",
        "docs/providers.md",
        "docs/releasing.md",
    ):
        path = ROOT / name
        assert path.is_file(), name
        assert path.stat().st_size > 100, name


def test_readme_links_the_provider_guide() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/providers.md" in text


def test_draft_room_spec_keeps_the_approval_boundary() -> None:
    text = (ROOT / "docs/draft-room.md").read_text(encoding="utf-8")
    assert "cannot approve" in text
    assert "studio.enabled" in text
    assert "pending_approval" in text
    assert "AC-7" in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/draft-room.md" in readme


def test_provider_guide_covers_custom_endpoints() -> None:
    text = (ROOT / "docs/providers.md").read_text(encoding="utf-8")
    for phrase in (
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "OpenCode Console",
        "max_daily_cost_usd",
    ):
        assert phrase in text


def test_readme_release_links_match_the_package_version() -> None:
    from heraldesk import __version__

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"version-{__version__}-blue.svg" in text
    assert f"releases/tag/v{__version__}" in text
    wheel = f"releases/download/v{__version__}/heraldesk-{__version__}-py3-none-any.whl"
    assert wheel in text
