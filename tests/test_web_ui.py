"""M11 Web UI tests: login, CSRF, pages, triage, reports and no secret leakage."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import (
    AgentRunRepository,
    ContentItemRepository,
    TelegramMessageRepository,
)
from heraldesk.domain.approvals import request_approval
from heraldesk.domain.content import ContentState
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.telegram.fake import FakeTelegramAdapter
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
ADMIN_TOKEN = "test-admin-token"
CHAT_ID = 123456789

_CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def write_config(initialized: InitResult, *, telegram: bool = False) -> None:
    lines = ["version: 1"]
    if telegram:
        lines.extend(
            [
                "telegram:",
                "  enabled: true",
                "  mode: polling",
                f"  allowed_chat_ids: [{CHAT_ID}]",
            ]
        )
    initialized.layout.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def config_for(initialized: InitResult, *, telegram: bool = False) -> Config:
    write_config(initialized, telegram=telegram)
    return load_config(initialized.layout.home)


def make_client(
    initialized: InitResult,
    *,
    telegram: bool = False,
    adapter: FakeTelegramAdapter | None = None,
    cookie_secure: bool = False,
) -> TestClient:
    return TestClient(
        create_app(
            AppOptions(
                layout=initialized.layout,
                config=config_for(initialized, telegram=telegram),
                enable_telegram=telegram,
                telegram_adapter=adapter,
                admin_token=ADMIN_TOKEN,
                cookie_secure=cookie_secure,
                adapter_factory=lambda platform: None,
                scheduler_interval_seconds=3600.0,
            )
        )
    )


def csrf_from(html: str) -> str:
    match = _CSRF.search(html)
    assert match is not None
    return match.group(1)


def login(client: TestClient, token: str = ADMIN_TOKEN) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    token_value = csrf_from(page.text)
    response = client.post(
        "/login",
        data={"token": token, "csrf": token_value},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


def pending_item(session: Session, setup: ProfileSetup) -> tuple[int, int]:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body="Pending body preview sk-ABCDEFGHIJKLMNOP1234",
        state=ContentState.PENDING_APPROVAL.value,
        risk_level="low",
    )
    repository.set_publish_policy(
        setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    approval = request_approval(session, setup.scope, item.id, now=NOW)
    session.commit()
    return item.id, approval.id


def test_health_and_static_are_public_but_pages_redirect(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/static/app.css").status_code == 200
        assert client.get("/static/htmx.min.js").status_code == 200
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login"
        assert client.get("/queue", follow_redirects=False).status_code == 303


def test_login_sets_http_only_lax_cookie_and_logout(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        page = client.get("/login")
        assert "token=" not in page.text
        login_response = client.post(
            "/login",
            data={"token": ADMIN_TOKEN, "csrf": csrf_from(page.text)},
            follow_redirects=False,
        )
        assert login_response.status_code == 303
        cookie = login_response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "secure" not in cookie

        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert ADMIN_TOKEN not in dashboard.text
        assert "Dashboard" in dashboard.text

        csrf = csrf_from(dashboard.text)
        logout = client.post("/logout", data={"csrf": csrf}, follow_redirects=False)
        assert logout.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


def test_secure_cookie_when_proxy_mode(initialized: InitResult) -> None:
    with make_client(initialized, cookie_secure=True) as client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"token": ADMIN_TOKEN, "csrf": csrf_from(page.text)},
            follow_redirects=False,
        )
        assert "secure" in response.headers["set-cookie"].lower()


def test_login_rejects_bad_token_and_rate_limits(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        for _ in range(5):
            page = client.get("/login")
            response = client.post(
                "/login",
                data={"token": "wrong", "csrf": csrf_from(page.text)},
                follow_redirects=False,
            )
            assert response.status_code == 401
        page = client.get("/login")
        blocked = client.post(
            "/login",
            data={"token": ADMIN_TOKEN, "csrf": csrf_from(page.text)},
            follow_redirects=False,
        )
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers


def test_queue_detail_page_shows_the_full_draft(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    long_body = f"Full draft text. {'detail ' * 30}END-MARKER sk-ABCDEFGHIJKLMNOP1234"
    item = ContentItemRepository(db_session).add(
        profile_setup.scope,
        platform="fake",
        body=long_body,
        state=ContentState.PENDING_APPROVAL.value,
        risk_level="low",
    )
    approval = request_approval(db_session, profile_setup.scope, item.id, now=NOW)
    db_session.commit()

    with make_client(initialized) as client:
        login(client)
        page = client.get(f"/queue/{approval.id}")

    assert page.status_code == 200
    assert "END-MARKER" in page.text
    assert "Full draft text." in page.text
    assert "sk-ABCDEFGHIJKLMNOP1234" not in page.text
    assert "Approve" in page.text
    assert "Reject" in page.text


def test_queue_page_approves_and_rejects_with_csrf(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id, approval_id = pending_item(db_session, profile_setup)
    second_item, second_approval = pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        page = client.get("/queue")
        assert page.status_code == 200
        assert "sk-ABCDEFGHIJKLMNOP1234" not in page.text
        csrf = csrf_from(page.text)

        rejected = client.post(
            f"/queue/{second_approval}/reject",
            data={"csrf": csrf, "reason": "off brand"},
            follow_redirects=False,
        )
        assert rejected.status_code == 303

        approved = client.post(
            f"/queue/{approval_id}/approve",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        assert approved.status_code == 303

        db_session.expire_all()
        first = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
        second = ContentItemRepository(db_session).get(profile_setup.scope, second_item)
        assert first is not None and first.state == "scheduled"
        assert second is not None and second.state == "rejected"


def test_queue_rejects_missing_csrf(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    _, approval_id = pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        response = client.post(
            f"/queue/{approval_id}/approve",
            data={},
            follow_redirects=False,
        )
        assert response.status_code == 403
        assert "CSRF" in response.text


def test_inbox_reply_and_ignore(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    adapter = FakeTelegramAdapter()
    set_profile_status(db_session, profile_setup.scope, ProfileStatus.ACTIVE)
    first = TelegramMessageRepository(db_session).add(
        profile_setup.scope,
        chat_id=CHAT_ID,
        sender_id=555,
        text="Please reply",
        command=None,
        content_item_id=None,
        response=None,
        created_at=NOW,
    )
    second = TelegramMessageRepository(db_session).add(
        profile_setup.scope,
        chat_id=CHAT_ID,
        sender_id=556,
        text="Ignore me",
        command=None,
        content_item_id=None,
        response=None,
        created_at=NOW,
    )
    db_session.commit()

    with make_client(initialized, telegram=True, adapter=adapter) as client:
        login(client)
        page = client.get("/inbox")
        assert page.status_code == 200
        assert str(CHAT_ID) not in page.text
        csrf = csrf_from(page.text)

        replied = client.post(
            f"/inbox/{first.id}/reply",
            data={"csrf": csrf, "text": "Thanks"},
            follow_redirects=False,
        )
        assert replied.status_code == 303
        assert adapter.sent == [(CHAT_ID, "Thanks")]

        ignored = client.post(
            f"/inbox/{second.id}/ignore",
            data={"csrf": csrf, "reason": "spam"},
            follow_redirects=False,
        )
        assert ignored.status_code == 303


def test_reports_page_generates_lists_and_renders_safely(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/reports")
        assert page.status_code == 200
        csrf = csrf_from(page.text)
        generated = client.post(
            "/reports/weekly",
            data={"csrf": csrf, "week": "2026-W47"},
            follow_redirects=False,
        )
        assert generated.status_code == 303

        initialized.layout.reports.mkdir(parents=True, exist_ok=True)
        malicious = initialized.layout.reports / "weekly-2026-W48.md"
        malicious.write_text("# Report\n<script>alert(1)</script>\n", encoding="utf-8")

        listing = client.get("/reports")
        assert "weekly-2026-W47.md" in listing.text
        assert "weekly-2026-W48.md" in listing.text

        view = client.get("/reports/view/weekly-2026-W48.md")
        assert view.status_code == 200
        assert "<script>" not in view.text
        assert "&lt;script&gt;" in view.text or "alert(1)" in view.text

        download = client.get("/reports/download/weekly-2026-W47.md")
        assert download.status_code == 200
        assert download.headers["content-type"].startswith("text/markdown")

        traversal = client.get("/reports/view/..%2F..%2Fsecret.md")
        assert traversal.status_code in {404, 400}


def test_profiles_page_and_dashboard_counts(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Pending approval" in dashboard.text
        profiles = client.get("/profiles")
        assert profiles.status_code == 200
        assert "acme" in profiles.text
        assert ADMIN_TOKEN not in profiles.text


def test_status_requires_auth_but_accepts_bearer(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        assert client.get("/status").status_code == 401
        assert (
            client.get(
                "/status",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            ).status_code
            == 200
        )


def test_htmx_queue_error_returns_200_and_message(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    _, approval_id = pending_item(db_session, profile_setup)
    with make_client(initialized) as client:
        login(client)
        page = client.get("/queue")
        csrf = csrf_from(page.text)

        missing_csrf = client.post(
            f"/queue/{approval_id}/approve",
            headers={"HX-Request": "true"},
            data={},
        )
        assert missing_csrf.status_code == 200
        assert "CSRF" in missing_csrf.text

        missing_approval = client.post(
            "/queue/999999/approve",
            headers={"HX-Request": "true"},
            data={"csrf": csrf},
        )
        assert missing_approval.status_code == 200
        assert "does not exist" in missing_approval.text
        assert "<tr" in missing_approval.text


def test_htmx_inbox_error_returns_200_and_message(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/inbox")
        csrf = csrf_from(page.text)

        response = client.post(
            "/inbox/999999/reply",
            headers={"HX-Request": "true"},
            data={"csrf": csrf, "text": "hello"},
        )
        assert response.status_code == 200
        assert "does not exist" in response.text
        assert "<tr" in response.text


def test_logout_all_revokes_the_current_session(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/")
        csrf = csrf_from(page.text)

        response = client.post(
            "/logout-all",
            data={"csrf": csrf},
            follow_redirects=False,
        )
        after = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert after.status_code == 303
    assert after.headers["location"] == "/login"


def test_audit_page_lists_entries(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/audit")

    assert page.status_code == 200
    assert "Audit" in page.text
    assert "serve.started" in page.text


def test_cost_page_shows_agent_run_totals(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    AgentRunRepository(db_session).add(
        profile_setup.scope,
        agent="writer",
        status="succeeded",
        model="fake/fake",
        input={},
        started_at=datetime.now(UTC),
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.12,
    )
    db_session.commit()

    with make_client(initialized) as client:
        login(client)
        page = client.get("/cost")

    assert page.status_code == 200
    assert "$0.1200" in page.text
    assert "acme" in page.text
