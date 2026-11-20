"""Inbox two-pane and Dashboard "Needs attention" tests, plus profile-scoped navigation."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import ContentItemRepository, TelegramMessageRepository
from heraldesk.domain.content import ContentState
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.telegram.fake import FakeTelegramAdapter
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
CHAT_ID = 123456789
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')
HX = {"HX-Request": "true"}


def write_config(initialized: InitResult, *, telegram: bool = False) -> None:
    lines = ["version: 1"]
    if telegram:
        lines.extend(
            ["telegram:", "  enabled: true", "  mode: polling", f"  allowed_chat_ids: [{CHAT_ID}]"]
        )
    initialized.layout.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def config_for(initialized: InitResult, *, telegram: bool = False) -> Config:
    write_config(initialized, telegram=telegram)
    return load_config(initialized.layout.home)


def make_client(initialized: InitResult, *, telegram: bool = False) -> TestClient:
    return TestClient(
        create_app(
            AppOptions(
                layout=initialized.layout,
                config=config_for(initialized, telegram=telegram),
                enable_telegram=telegram,
                telegram_adapter=FakeTelegramAdapter() if telegram else None,
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


def add_inbox(session: Session, setup: ProfileSetup, text: str = "hello") -> int:
    message = TelegramMessageRepository(session).add(
        setup.scope,
        chat_id=123456789,
        sender_id=555666777,
        text=text,
        command=None,
        content_item_id=None,
        response=None,
    )
    session.commit()
    return message.id


def test_nav_links_carry_the_profile_parameter(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        all_profiles = client.get("/")
        assert 'href="/queue"' in all_profiles.text
        assert 'href="/queue?profile=' not in all_profiles.text

        scoped = client.get("/?profile=acme")
        assert 'href="/queue?profile=acme"' in scoped.text
        assert 'href="/inbox?profile=acme"' in scoped.text


def test_inbox_panel_returns_a_fragment_for_htmx(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id = add_inbox(db_session, profile_setup, "needs a reply")
    with make_client(initialized) as client:
        login(client)
        fragment = client.get(f"/inbox/{item_id}/panel", headers=HX)
        assert fragment.status_code == 200
        assert "needs a reply" in fragment.text
        assert "<html" not in fragment.text

        full = client.get(f"/inbox/{item_id}/panel", follow_redirects=False)
        assert full.status_code == 303


def test_inbox_direct_visit_renders_two_panes(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id = add_inbox(db_session, profile_setup, "full message text")
    with make_client(initialized) as client:
        login(client)
        page = client.get(f"/inbox/{item_id}")
        assert page.status_code == 200
        assert "full message text" in page.text
        assert "Message #" in page.text
        assert "queue-layout has-detail" in page.text


def test_inbox_reply_via_htmx_updates_badge_and_toast(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    first = add_inbox(db_session, profile_setup, "one")
    _second = add_inbox(db_session, profile_setup, "two")
    set_profile_status(db_session, profile_setup.scope, ProfileStatus.ACTIVE)
    db_session.commit()
    with make_client(initialized, telegram=True) as client:
        login(client)
        csrf = _CSRF.search(client.get("/inbox").text)
        assert csrf is not None
        response = client.post(
            f"/inbox/{first}/reply",
            data={"csrf": csrf.group(1), "text": "done"},
            headers=HX,
        )
        assert response.status_code == 200
        assert 'id="inbox-badge" hx-swap-oob' in response.text
        assert "Reply sent." in response.text


def test_dashboard_empty_state(initialized: InitResult, profile_setup: ProfileSetup) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/")
        assert "Nothing needs your review." in page.text


def test_dashboard_lists_attention_items(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    repo = ContentItemRepository(db_session)
    repo.add(
        profile_setup.scope, platform="fake", body="blocked draft", state=ContentState.BLOCKED.value
    )
    repo.add(
        profile_setup.scope, platform="fake", body="failed draft", state=ContentState.FAILED.value
    )
    repo.add(
        profile_setup.scope,
        platform="fake",
        body="pending draft",
        state=ContentState.PENDING_APPROVAL.value,
    )
    scheduled = repo.add(
        profile_setup.scope,
        platform="fake",
        body="scheduled draft",
        state=ContentState.SCHEDULED.value,
    )
    repo.set_scheduled_at(
        profile_setup.scope, scheduled.id, scheduled_at=datetime.now(UTC) + timedelta(hours=3)
    )
    db_session.commit()

    with make_client(initialized) as client:
        login(client)
        page = client.get("/")
        assert "Blocked" in page.text
        assert "Failed publishes" in page.text
        assert "Pending approval" in page.text
        assert "Next scheduled" in page.text


def test_post_login_redirect_goes_to_queue_when_pending(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    ContentItemRepository(db_session).add(
        profile_setup.scope,
        platform="fake",
        body="pending",
        state=ContentState.PENDING_APPROVAL.value,
    )
    db_session.commit()

    with make_client(initialized) as client:
        page = client.get("/login")
        match = _CSRF.search(page.text)
        response = client.post(
            "/login", data={"token": ADMIN_TOKEN, "csrf": match.group(1)}, follow_redirects=False
        )
        assert response.headers["location"] == "/queue"


def test_post_login_redirect_goes_to_dashboard_when_clear(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        page = client.get("/login")
        match = _CSRF.search(page.text)
        response = client.post(
            "/login", data={"token": ADMIN_TOKEN, "csrf": match.group(1)}, follow_redirects=False
        )
        assert response.headers["location"] == "/"
