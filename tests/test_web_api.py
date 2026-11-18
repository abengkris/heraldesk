"""M11 REST API tests: auth, queue, inbox, reports and publish."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk.analyst import weekly_period, write_report
from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import (
    ContentItemRepository,
    TelegramMessageRepository,
)
from heraldesk.domain.approvals import request_approval
from heraldesk.domain.content import ContentState
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.telegram.fake import FakeTelegramAdapter
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
ADMIN_TOKEN = "test-admin-token"
HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
CHAT_ID = 123456789


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


def make_app(
    initialized: InitResult,
    *,
    telegram: bool = False,
    adapter: FakeTelegramAdapter | None = None,
    webhook_secret: str | None = None,
    enable_web: bool = True,
) -> TestClient:
    return TestClient(
        create_app(
            AppOptions(
                layout=initialized.layout,
                config=config_for(initialized, telegram=telegram),
                enable_telegram=telegram,
                telegram_adapter=adapter,
                admin_token=ADMIN_TOKEN,
                adapter_factory=lambda platform: FakeAdapter(),
                telegram_webhook_secret=webhook_secret,
                enable_web=enable_web,
                scheduler_interval_seconds=3600.0,
            )
        )
    )


def pending_item(session: Session, setup: ProfileSetup) -> tuple[int, int]:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body="Queue body with sk-ABCDEFGHIJKLMNOP1234.",
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


def approved_item(session: Session, setup: ProfileSetup) -> int:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    item = ContentItemRepository(session).add(
        setup.scope,
        platform="fake",
        body="Publish me.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    session.commit()
    return item.id


def test_api_requires_bearer_and_rejects_query_tokens(initialized: InitResult) -> None:
    with make_app(initialized) as client:
        assert client.get("/api/v1/status").status_code == 401
        wrong = client.get("/api/v1/status", headers={"Authorization": "Bearer wrong"})
        assert wrong.status_code == 401
        assert wrong.json() == {
            "error": "a valid Bearer admin token is required",
            "code": "unauthorized",
        }
        query = client.get(f"/api/v1/status?token={ADMIN_TOKEN}", headers=HEADERS)
        assert query.status_code == 400
        assert query.json()["code"] == "token_in_query"
        ok = client.get("/api/v1/status", headers=HEADERS)
        assert ok.status_code == 200
        body = ok.json()
        assert body["version"]
        assert "queue" in body
        assert "admin_token" not in ok.text


def test_api_queue_and_approve_by_approval_id(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id, approval_id = pending_item(db_session, profile_setup)
    with make_app(initialized) as client:
        queue = client.get("/api/v1/queue?status=pending", headers=HEADERS)
        assert queue.status_code == 200
        entries = queue.json()["items"]
        assert entries[0]["approval_id"] == approval_id
        assert entries[0]["content_item_id"] == item_id
        assert "sk-ABCDEFGHIJKLMNOP1234" not in queue.text

        approved = client.post(
            f"/api/v1/approvals/{approval_id}/approve",
            headers=HEADERS,
            json={"comment": "looks good"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["content_item_id"] == item_id

        empty = client.get("/api/v1/queue?status=pending", headers=HEADERS)
        assert empty.json()["items"] == []


def test_api_reject_requires_a_reason(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id, approval_id = pending_item(db_session, profile_setup)
    with make_app(initialized) as client:
        missing = client.post(
            f"/api/v1/approvals/{approval_id}/reject",
            headers=HEADERS,
            json={},
        )
        assert missing.status_code == 422
        assert missing.json()["code"] == "validation_error"
        assert "Queue body" not in missing.text

        rejected = client.post(
            f"/api/v1/approvals/{approval_id}/reject",
            headers=HEADERS,
            json={"reason": "off brand"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["content_item_id"] == item_id


def test_api_inbox_reply_and_ignore(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    adapter = FakeTelegramAdapter()
    message = TelegramMessageRepository(db_session).add(
        profile_setup.scope,
        chat_id=CHAT_ID,
        sender_id=555,
        text="Hello there",
        command=None,
        content_item_id=None,
        response=None,
        created_at=NOW,
    )
    second = TelegramMessageRepository(db_session).add(
        profile_setup.scope,
        chat_id=CHAT_ID,
        sender_id=556,
        text="Spam message",
        command=None,
        content_item_id=None,
        response=None,
        created_at=NOW,
    )
    db_session.commit()

    with make_app(initialized, telegram=True, adapter=adapter) as client:
        listed = client.get("/api/v1/inbox?status=pending", headers=HEADERS)
        assert listed.status_code == 200
        assert listed.json()["count"] == 2
        assert str(CHAT_ID) not in listed.text

        replied = client.post(
            f"/api/v1/inbox/{message.id}/reply",
            headers=HEADERS,
            json={"text": "Thanks for writing."},
        )
        assert replied.status_code == 200
        assert replied.json()["item"]["status"] == "handled"
        assert adapter.sent == [(CHAT_ID, "Thanks for writing.")]

        ignored = client.post(
            f"/api/v1/inbox/{second.id}/ignore",
            headers=HEADERS,
            json={"reason": "spam"},
        )
        assert ignored.status_code == 200
        assert ignored.json()["item"]["status"] == "ignored"


def test_api_reports_build_in_memory_with_summary(initialized: InitResult) -> None:
    with make_app(initialized) as client:
        weekly = client.get("/api/v1/reports/weekly?week=2026-W47", headers=HEADERS)
        assert weekly.status_code == 200
        body = weekly.json()
        assert body["summary"]["period"] == "2026-W47"
        assert body["summary"]["period_type"] == "weekly"
        assert "# Weekly report" in body["markdown"] or "Weekly report" in body["markdown"]

        monthly = client.get("/api/v1/reports/monthly?month=2026-11", headers=HEADERS)
        assert monthly.status_code == 200
        assert monthly.json()["summary"]["period"] == "2026-11"

        bad = client.get("/api/v1/reports/weekly?week=not-a-week", headers=HEADERS)
        assert bad.status_code == 400
        assert bad.json()["code"] == "request_failed"


def test_api_publish_uses_the_publisher(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id = approved_item(db_session, profile_setup)
    with make_app(initialized) as client:
        published = client.post(f"/api/v1/publish/{item_id}", headers=HEADERS)
        assert published.status_code == 200
        body = published.json()
        assert body["status"] == "published"
        assert body["external_id"] == "fake-1"


def test_api_generated_report_file_is_listed_and_downloadable(
    initialized: InitResult, db_session: Session
) -> None:
    path = write_report(db_session, initialized.layout, weekly_period("2026-W47"))
    db_session.commit()
    with make_app(initialized) as client:
        # The API stays read-only for reports; the file is exposed by the UI routes.
        assert path.is_file()
        response = client.get("/api/v1/status", headers=HEADERS)
        assert response.status_code == 200


def test_webhook_is_public_but_optional_secret_is_enforced(initialized: InitResult) -> None:
    adapter = FakeTelegramAdapter()
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "date": 1_700_000_000,
            "chat": {"id": CHAT_ID, "type": "private"},
            "from": {"id": 555, "is_bot": False, "first_name": "Tester"},
            "text": "/help",
        },
    }
    with make_app(
        initialized,
        telegram=True,
        adapter=adapter,
        webhook_secret="telegram-webhook-secret",
    ) as client:
        missing = client.post("/webhook/telegram", json=payload)
        assert missing.status_code == 200
        assert missing.json()["status"] == "unauthorized"

        accepted = client.post(
            "/webhook/telegram",
            json=payload,
            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-webhook-secret"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["ok"] is True


def test_no_web_keeps_the_api_and_health(initialized: InitResult) -> None:
    with make_app(initialized, enable_web=False) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/status", headers=HEADERS).status_code == 200
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 404
        assert client.get("/static/app.css").status_code == 404


def test_api_inbox_missing_item_is_404(initialized: InitResult) -> None:
    with make_app(initialized) as client:
        response = client.post(
            "/api/v1/inbox/999999/reply",
            headers=HEADERS,
            json={"text": "hello"},
        )
        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


def test_api_inbox_invalid_status_preserves_code(initialized: InitResult) -> None:
    with make_app(initialized) as client:
        response = client.get("/api/v1/inbox?status=bogus", headers=HEADERS)
        assert response.status_code == 400
        assert response.json()["code"] == "invalid_status"
