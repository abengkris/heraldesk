"""Keep the GitHub issue and pull-request templates present and well formed."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.core

ROOT = Path(__file__).resolve().parents[1]
ISSUE_DIR = ROOT / ".github" / "ISSUE_TEMPLATE"
PR_TEMPLATE = ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"


def test_issue_forms_are_valid_yaml_with_unique_ids() -> None:
    names = {"bug_report.yml", "feature_request.yml"}
    assert names <= {path.name for path in ISSUE_DIR.glob("*.yml")}
    for name in names:
        data = yaml.safe_load((ISSUE_DIR / name).read_text(encoding="utf-8"))
        assert data["name"]
        assert data["description"]
        body = data["body"]
        assert body
        assert any(block["type"] == "textarea" for block in body)
        ids = [block["id"] for block in body if "id" in block]
        assert len(ids) == len(set(ids)), name


def test_bug_report_tells_reporters_not_to_paste_secrets() -> None:
    text = (ISSUE_DIR / "bug_report.yml").read_text(encoding="utf-8").lower()
    assert "do not paste credentials" in text
    assert "security.md" in text
    assert "heraldesk doctor" in text


def test_pull_request_template_has_test_and_safety_checklists() -> None:
    text = PR_TEMPLATE.read_text(encoding="utf-8").lower()
    assert "## checklist" in text
    assert "uv run pytest" in text
    assert "uv run mypy src/heraldesk" in text
    assert "no credentials" in text
    assert "conventional commits" in text
    assert "javascript disabled" in text or "js disabled" in text
