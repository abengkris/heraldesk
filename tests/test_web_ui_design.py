"""Web UI design contract tests: layout, kill switch, no external URLs, progressive enhancement."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.platforms.fake import FakeAdapter

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def write_config(initialized: InitResult) -> None:
    initialized.layout.config_path.write_text("version: 1\n", encoding="utf-8")


def config_for(initialized: InitResult) -> Config:
    write_config(initialized)
    return load_config(initialized.layout.home)


def make_client(initialized: InitResult) -> TestClient:
    return TestClient(
        create_app(
            AppOptions(
                layout=initialized.layout,
                config=config_for(initialized),
                enable_telegram=False,
                admin_token=ADMIN_TOKEN,
                adapter_factory=lambda platform: FakeAdapter(),
                scheduler_interval_seconds=3600.0,
            )
        )
    )


def login(client: TestClient) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    match = _CSRF.search(page.text)
    assert match is not None
    response = client.post(
        "/login", data={"token": ADMIN_TOKEN, "csrf": match.group(1)}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


def test_kill_switch_is_a_two_step_control_on_authenticated_pages(
    initialized: InitResult,
) -> None:
    with make_client(initialized) as client:
        login(client)
        dashboard = client.get("/")
        assert "Kill switch: off" in dashboard.text
        assert "Confirm engage" in dashboard.text
        assert "killswitch-banner" not in dashboard.text

        csrf = _CSRF.search(dashboard.text)
        assert csrf is not None
        engaged = client.post(
            "/killswitch", data={"csrf": csrf.group(1), "action": "engage"}, follow_redirects=False
        )
        assert engaged.status_code == 303

        after = client.get("/")
        assert "Disengage kill switch" in after.text
        assert "killswitch-banner" in after.text


def test_no_mode_indicator_or_switcher_placeholder(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        dashboard = client.get("/")
        assert "mode-indicator" not in dashboard.text
        assert "profile-switcher" not in dashboard.text


def test_skip_link_and_mobile_nav_are_present(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        dashboard = client.get("/")
        assert 'href="#main"' in dashboard.text
        assert "Skip to content" in dashboard.text
        assert 'class="mobile-nav"' in dashboard.text
        assert ">More</summary>" in dashboard.text


def test_csrf_header_only_on_authenticated_pages(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login_page = client.get("/login")
        assert "hx-headers" not in login_page.text

        login(client)
        dashboard = client.get("/")
        assert "hx-headers" in dashboard.text
        assert "X-CSRFToken" in dashboard.text


def test_no_external_urls_in_html_or_static_assets(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        for page in ("/", "/queue", "/inbox"):
            response = client.get(page)
            assert "http://" not in response.text
            assert "https://" not in response.text

    import heraldesk.web

    static_dir = Path(heraldesk.web.__file__).parent / "static"
    for name in ("app.css", "app.js"):
        text = (static_dir / name).read_text(encoding="utf-8")
        assert "http://" not in text
        assert "https://" not in text
