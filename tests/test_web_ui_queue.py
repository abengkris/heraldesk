"""Queue master-detail UI tests: two-pane rendering, fragments, CSRF, blocked items, htmx."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.domain.approvals import approve, request_approval
from heraldesk.domain.content import ContentState
from heraldesk.domain.errors import ApprovalError
from heraldesk.platforms.fake import FakeAdapter
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')
HX = {"HX-Request": "true"}


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
    match = _CSRF.search(page.text)
    assert match is not None
    response = client.post(
        "/login", data={"token": ADMIN_TOKEN, "csrf": match.group(1)}, follow_redirects=False
    )
    assert response.status_code == 303, response.text


def pending_item(session: Session, setup: ProfileSetup) -> tuple[int, int]:
    item = ContentItemRepository(session).add(
        setup.scope,
        platform="fake",
        body="Queue draft body.",
        state=ContentState.PENDING_APPROVAL.value,
    )
    approval = request_approval(session, setup.scope, item.id, actor="user")
    session.commit()
    return item.id, approval.id


def test_direct_visit_renders_the_two_panes(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    _item_id, approval_id = pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        page = client.get(f"/queue/{approval_id}")
        assert page.status_code == 200
        assert "Queue draft body." in page.text
        assert "Approve" in page.text
        assert "queue-layout has-detail" in page.text


def test_panel_returns_a_fragment_for_htmx(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    _item_id, approval_id = pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        fragment = client.get(f"/queue/{approval_id}/panel", headers=HX)
        assert fragment.status_code == 200
        assert "Queue draft body." in fragment.text
        assert "<html" not in fragment.text

        full = client.get(f"/queue/{approval_id}/panel", follow_redirects=False)
        assert full.status_code == 303
        assert full.headers["location"].endswith(f"/queue/{approval_id}")


def test_approve_via_htmx_updates_badge_and_toast(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    _a, approval_a = pending_item(db_session, profile_setup)
    _b, _approval_b = pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        csrf = _CSRF.search(client.get("/queue").text)
        assert csrf is not None
        response = client.post(
            f"/queue/{approval_a}/approve",
            data={"csrf": csrf.group(1)},
            headers=HX,
        )
        assert response.status_code == 200
        assert 'id="queue-badge" hx-swap-oob' in response.text
        assert "Item approved." in response.text


def test_blocked_item_cannot_be_approved_server_side(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = ContentItemRepository(db_session).add(
        profile_setup.scope, platform="fake", body="blocked", state=ContentState.BLOCKED.value
    )
    db_session.commit()

    with pytest.raises(ApprovalError):
        approve(db_session, profile_setup.layout, profile_setup.scope, item.id, actor="user")


def test_blocked_item_detail_disables_approve(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    from types import SimpleNamespace

    from heraldesk.web.ui import templates

    detail = SimpleNamespace(
        approval_id=1,
        content_item_id=1,
        profile_slug="acme",
        platform="fake",
        state="blocked",
        approval_state="unknown",
        risk_level="high",
        body="blocked draft",
        reason=None,
        expires_at=None,
        created_at=None,
        scheduled_at=None,
        account_handle=None,
        max_characters=5000,
        compliance_notes="blocked: reason",
        lint_findings=[],
        history=[],
    )
    html = templates.env.get_template("partials/queue_detail.html").render(
        detail=detail, csrf="csrf"
    )
    assert "Edit and re-run compliance first" in html
    assert "disabled" in html


def test_write_forms_all_carry_csrf(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    _item_id, approval_id = pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        page = client.get(f"/queue/{approval_id}")
        assert page.text.count('name="csrf"') >= 3
        assert f'action="/queue/{approval_id}/approve"' in page.text
        assert f'action="/queue/{approval_id}/reject"' in page.text
        assert f'action="/queue/{approval_id}/edit"' in page.text


def test_expired_session_htmx_redirects_to_login(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        response = client.get("/queue/1/panel", headers=HX, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers.get("hx-redirect") == "/login"
