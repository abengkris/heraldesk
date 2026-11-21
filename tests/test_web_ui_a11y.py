"""Contrast, security headers and rendered-HTML accessibility checks."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import AgentRunRepository, ContentItemRepository
from heraldesk.domain.approvals import request_approval
from heraldesk.domain.content import ContentState
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')
CSS = Path(__file__).resolve().parents[1] / "src" / "heraldesk" / "web" / "static" / "app.css"


def write_config(initialized: InitResult, *, studio: bool = False) -> None:
    lines = ["version: 1"]
    if studio:
        lines.extend(["studio:", "  enabled: true", "  max_messages: 40", "  max_cost_usd: 0.25"])
    initialized.layout.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def config_for(initialized: InitResult) -> Config:
    return load_config(initialized.layout.home)


def make_client(initialized: InitResult) -> TestClient:
    return TestClient(
        create_app(
            AppOptions(
                layout=initialized.layout,
                config=config_for(initialized),
                enable_telegram=False,
                admin_token=ADMIN_TOKEN,
                adapter_factory=lambda platform: None,
                scheduler_interval_seconds=3600.0,
            )
        )
    )


def login(client: TestClient) -> None:
    page = client.get("/login")
    match = _CSRF.search(page.text)
    assert match is not None
    response = client.post(
        "/login", data={"token": ADMIN_TOKEN, "csrf": match.group(1)}, follow_redirects=False
    )
    assert response.status_code == 303


# --- Contrast -----------------------------------------------------------------


def _block(css: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", css, re.S)
    assert match is not None, selector
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", match.group(1)))


def _luminance(value: str) -> float:
    value = value.lstrip("#")
    channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]

    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * linear(channels[0]) + 0.7152 * linear(channels[1]) + 0.0722 * linear(channels[2])
    )


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _theme_tokens() -> list[dict[str, str]]:
    css = CSS.read_text(encoding="utf-8")
    return [_block(css, ":root"), _block(css, ':root[data-theme="dark"]')]


@pytest.mark.parametrize("tokens", _theme_tokens())
def test_text_pairs_meet_wcag_aa(tokens: dict[str, str]) -> None:
    pairs = [
        ("text", "bg"),
        ("text", "surface"),
        ("muted", "surface"),
        ("muted", "bg"),
        ("accent", "surface"),
        ("accent-contrast", "accent"),
    ]
    failures = [
        f"{foreground}/{background}={_contrast(tokens[foreground], tokens[background]):.2f}"
        for foreground, background in pairs
        if _contrast(tokens[foreground], tokens[background]) < 4.5
    ]
    assert not failures, failures


@pytest.mark.parametrize("tokens", _theme_tokens())
def test_status_badges_meet_wcag_aa(tokens: dict[str, str]) -> None:
    failures = []
    for status in ("ok", "warn", "danger", "info", "muted"):
        ratio = _contrast(tokens[f"status-{status}"], tokens[f"status-{status}-bg"])
        if ratio < 4.5:
            failures.append(f"{status}={ratio:.2f}")
    assert not failures, failures


@pytest.mark.parametrize("tokens", _theme_tokens())
def test_ui_borders_and_focus_meet_three_to_one(tokens: dict[str, str]) -> None:
    failures = []
    for name in ("border", "border-strong", "focus"):
        ratio = _contrast(tokens[name], tokens["surface"])
        if ratio < 3.0:
            failures.append(f"{name}={ratio:.2f}")
    assert not failures, failures


# --- Security headers ----------------------------------------------------------


def test_html_responses_send_csp_and_frame_headers(initialized: InitResult) -> None:
    write_config(initialized)
    with make_client(initialized) as client:
        login(client)
        for path in ("/", "/queue", "/audit", "/reports", "/profiles"):
            response = client.get(path)
            csp = response.headers["content-security-policy"]
            assert "default-src 'self'" in csp
            assert "script-src 'self'" in csp
            assert "style-src 'self'" in csp
            assert "unsafe-inline" not in csp
            assert "unsafe-eval" not in csp
            assert "frame-ancestors 'none'" in csp
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"


# --- Rendered HTML -------------------------------------------------------------


class _PageAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.h1 = 0
        self.style_attrs: list[str] = []
        self.handlers: list[str] = []
        self.inline_scripts = 0
        self.labels: set[str] = set()
        self.controls: list[tuple[str, str, bool]] = []
        self.forms: list[dict[str, object]] = []
        self.form_stack: list[dict[str, object]] = []
        self.in_label = 0
        self.has_nav = False
        self.has_main = False
        self.skip_link = False
        self.urls: list[str] = []
        self._in_script_without_src = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {key: value or "" for key, value in attrs}
        if tag == "h1":
            self.h1 += 1
        if "style" in data:
            self.style_attrs.append(tag)
        for key in data:
            if key.startswith("on"):
                self.handlers.append(f"{tag}[{key}]")
        if tag == "script":
            if "src" in data:
                self._in_script_without_src = False
            else:
                self.inline_scripts += 1
                self._in_script_without_src = True
        if tag == "nav":
            self.has_nav = True
        if tag == "main":
            self.has_main = True
        if tag == "a" and data.get("href") == "#main":
            self.skip_link = True
        if tag == "label":
            self.in_label += 1
            if data.get("for"):
                self.labels.add(data["for"])
        if tag in {"input", "select", "textarea"}:
            input_type = data.get("type", "text").lower()
            if input_type not in {"hidden", "submit", "button", "reset", "checkbox", "radio"}:
                self.controls.append((tag, data.get("id", ""), bool(self.in_label)))
            if input_type == "checkbox" and self.form_stack and data.get("id"):
                self.labels.add(data["id"])
        if tag == "form":
            form = {
                "method": data.get("method", "get").lower(),
                "csrf": False,
            }
            self.forms.append(form)
            self.form_stack.append(form)
        if tag == "input" and data.get("name") == "csrf" and self.form_stack:
            self.form_stack[-1]["csrf"] = True
        for key in ("href", "src", "action"):
            value = data.get(key, "")
            if value.startswith(("http://", "https://", "//")):
                self.urls.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self.in_label:
            self.in_label -= 1
        if tag == "form" and self.form_stack:
            self.form_stack.pop()
        if tag == "script":
            self._in_script_without_src = False


def _seed(initialized: InitResult, setup: ProfileSetup, session: Session) -> int:
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body="A pending draft about dashboards.",
        state=ContentState.PENDING_APPROVAL.value,
        risk_level="low",
    )
    request_approval(session, setup.scope, item.id, actor="user")
    scheduled = repository.add(
        setup.scope,
        platform="fake",
        body="A scheduled draft.",
        state=ContentState.SCHEDULED.value,
    )
    repository.set_scheduled_at(
        setup.scope, scheduled.id, scheduled_at=datetime.now(UTC) + timedelta(days=1)
    )
    AgentRunRepository(session).add(
        setup.scope,
        agent="writer",
        status="succeeded",
        model="fake",
        input={},
        started_at=datetime.now(UTC),
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.5,
    )
    initialized.layout.reports.mkdir(parents=True, exist_ok=True)
    (initialized.layout.reports / "weekly-2026-W48.md").write_text(
        "# Weekly\n\nA report.\n", encoding="utf-8"
    )
    session.commit()
    return item.id


def test_authenticated_pages_have_no_html_lint_errors(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    write_config(initialized, studio=True)
    item_id = _seed(initialized, profile_setup, db_session)
    with make_client(initialized) as client:
        login(client)
        studio_page = client.get("/studio")
        match = _CSRF.search(studio_page.text)
        assert match is not None
        created = client.post(
            "/studio/threads",
            data={"csrf": match.group(1), "profile": "acme", "title": "Lint thread"},
            follow_redirects=False,
        )
        assert created.status_code == 303
        thread_url = created.headers["location"]
        pages = [
            "/",
            "/queue",
            f"/queue/{item_id}",
            "/inbox",
            "/calendar",
            "/cost",
            "/audit",
            "/reports",
            "/profiles",
            "/profiles/acme/edit",
            "/studio",
            thread_url,
        ]
        for path in pages:
            response = client.get(path)
            assert response.status_code == 200, path
            text = response.text
            assert "http://" not in text, path
            assert "https://" not in text, path
            audit = _PageAudit()
            audit.feed(text)
            assert audit.h1 == 1, (path, audit.h1)
            assert audit.has_nav and audit.has_main, path
            assert audit.skip_link, path
            assert not audit.style_attrs, (path, audit.style_attrs)
            assert not audit.handlers, (path, audit.handlers)
            assert audit.inline_scripts == 0, path
            assert not audit.urls, (path, audit.urls)
            for tag, control_id, wrapped in audit.controls:
                assert control_id in audit.labels or wrapped, (path, tag, control_id)
            for form in audit.forms:
                if form["method"] == "post":
                    assert form["csrf"], path


def test_one_status_mapping_covers_every_state_and_risk() -> None:
    from heraldesk.domain.content import ContentState
    from heraldesk.web.service import RISK_SEMANTIC, STATE_SEMANTIC, status_semantic
    from heraldesk.web.ui import templates

    assert set(RISK_SEMANTIC) == {"low", "medium", "high", "blocked"}
    assert {state.value for state in ContentState} <= set(STATE_SEMANTIC)
    semantics = {"ok", "warn", "danger", "info", "muted"}
    assert set(RISK_SEMANTIC.values()) <= semantics
    assert set(STATE_SEMANTIC.values()) <= semantics

    macro = templates.env.get_template("_components.html").module
    for value in ("pending_approval", "blocked", "draft_only", "published"):
        html = macro.status_badge("state", value)
        assert value.replace("_", " ") in html
        assert "badge" in html and "status-" in html
    for risk in ("low", "medium", "high", "blocked"):
        html = macro.status_badge("risk", risk)
        assert risk in html and status_semantic("risk", risk) in html
