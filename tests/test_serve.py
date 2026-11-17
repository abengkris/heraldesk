"""M7 server tests: health, status, webhook, background jobs and shutdown."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from heraldesk import __version__, paths
from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import TelegramMessage
from heraldesk.db.repositories import ApprovalRepository, ContentItemRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session
from heraldesk.domain.approvals import request_approval
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.server.background import BackgroundJobs
from heraldesk.server.database import ServerDatabase
from heraldesk.telegram.fake import FakeTelegramAdapter
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

CHAT_ID = 123456789
TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def write_config(
    initialized: InitResult,
    *,
    telegram_enabled: bool = False,
    mode: str = "polling",
) -> None:
    lines = ["version: 1"]
    if telegram_enabled:
        lines.extend(
            [
                "telegram:",
                "  enabled: true",
                f"  mode: {mode}",
                f"  allowed_chat_ids: [{CHAT_ID}]",
            ]
        )
    (initialized.layout.home / "heraldesk.yaml").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def load_test_config(
    initialized: InitResult,
    *,
    telegram_enabled: bool = False,
    mode: str = "polling",
) -> Config:
    write_config(initialized, telegram_enabled=telegram_enabled, mode=mode)
    return load_config(initialized.layout.home)


def make_app(
    initialized: InitResult,
    *,
    telegram_enabled: bool = False,
    mode: str = "polling",
    adapter: FakeTelegramAdapter | None = None,
    token: str | None = None,
    scheduler_interval: float = 3600.0,
    poll_interval: float = 10.0,
):
    config = load_test_config(initialized, telegram_enabled=telegram_enabled, mode=mode)
    return create_app(
        AppOptions(
            layout=initialized.layout,
            config=config,
            telegram_adapter=adapter,
            telegram_token=token,
            scheduler_interval_seconds=scheduler_interval,
            poll_interval_seconds=poll_interval,
        )
    )


def pending_item(session: Session, setup: ProfileSetup) -> int:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body="Queue body.",
        state=ContentState.PENDING_APPROVAL.value,
        risk_level="low",
    )
    repository.set_publish_policy(
        setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    request_approval(session, setup.scope, item.id, now=datetime.now(UTC))
    session.commit()
    return item.id


def make_scheduled(session: Session, setup: ProfileSetup, *, when: datetime) -> int:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body="A scheduled post.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(
        setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    transition(
        session,
        setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=when,
    )
    session.commit()
    return item.id


def wait_for(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def item_state(layout: paths.Layout, scope: ProfileScope, item_id: int) -> str | None:
    with database_session(layout) as session:
        item = ContentItemRepository(session).get(scope, item_id)
        return None if item is None else item.state


def telegram_messages(layout: paths.Layout) -> int:
    with database_session(layout) as session:
        count = session.scalar(select(func.count()).select_from(TelegramMessage))
        return int(count or 0)


def profile_actions(
    layout: paths.Layout,
    scope: ProfileScope,
    *,
    action: str | None = None,
) -> list[str]:
    with database_session(layout) as session:
        entries = AuditService(session).history(scope, limit=100)
        return [entry.action for entry in entries if action is None or entry.action == action]


def global_actions(layout: paths.Layout) -> list[str]:
    with database_session(layout) as session:
        return [entry.action for entry in AuditService(session).history(None, limit=50)]


def webhook_payload(text: str, *, chat_id: int = CHAT_ID, update_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "date": 1_700_000_000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 555, "is_bot": False, "first_name": "Tester"},
            "text": text,
        },
    }


def test_health_reports_version_and_database(initialized: InitResult) -> None:
    app = make_app(initialized)

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["db"]["ok"] is True
    assert body["db"]["revision"] == "0008"


def test_status_reports_profiles_and_queue_counts(
    initialized: InitResult,
    profile_setup: ProfileSetup,
    db_session: Session,
) -> None:
    repository = ContentItemRepository(db_session)
    repository.add(
        profile_setup.scope,
        platform="fake",
        body="pending",
        state=ContentState.PENDING_APPROVAL.value,
    )
    repository.add(
        profile_setup.scope,
        platform="fake",
        body="scheduled",
        state=ContentState.SCHEDULED.value,
    )
    repository.add(
        profile_setup.scope,
        platform="fake",
        body="published",
        state=ContentState.PUBLISHED.value,
    )
    repository.add(
        profile_setup.scope,
        platform="fake",
        body="failed",
        state=ContentState.FAILED.value,
    )
    db_session.commit()

    app = make_app(initialized)
    with TestClient(app) as client:
        response = client.get("/status")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == __version__
    assert body["queue"]["pending_approval"] == 1
    assert body["queue"]["scheduled"] == 1
    assert body["queue"]["failed"] == 1
    assert body["queue"]["total"] == 3
    profile = next(row for row in body["profiles"] if row["slug"] == "acme")
    assert profile["queue"]["pending_approval"] == 1
    assert profile["queue"]["total"] == 3
    assert body["telegram"]["enabled"] is False
    assert body["telegram"]["configured"] is False
    assert body["telegram"]["allowed_chats"] == 0
    assert str(CHAT_ID) not in response.text


def test_webhook_approve_works(
    initialized: InitResult,
    profile_setup: ProfileSetup,
    db_session: Session,
) -> None:
    item_id = pending_item(db_session, profile_setup)
    adapter = FakeTelegramAdapter()
    app = make_app(initialized, telegram_enabled=True, adapter=adapter)

    with TestClient(app) as client:
        response = client.post(
            "/webhook/telegram",
            json=webhook_payload(f"/approve {item_id}", update_id=21),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["content_item_id"] == item_id
    assert adapter.sent
    assert item_state(initialized.layout, profile_setup.scope, item_id) == "scheduled"
    assert "telegram.received" in profile_actions(initialized.layout, profile_setup.scope)


def test_webhook_refuses_a_non_allowlisted_chat(
    initialized: InitResult,
    profile_setup: ProfileSetup,
) -> None:
    adapter = FakeTelegramAdapter()
    app = make_app(initialized, telegram_enabled=True, adapter=adapter)

    with TestClient(app) as client:
        response = client.post(
            "/webhook/telegram",
            json=webhook_payload("/queue", chat_id=999, update_id=22),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert adapter.sent == []
    assert telegram_messages(initialized.layout) == 0
    assert str(CHAT_ID) not in response.text


def test_webhook_refuses_an_expired_approval(
    initialized: InitResult,
    profile_setup: ProfileSetup,
) -> None:
    adapter = FakeTelegramAdapter()
    app = make_app(initialized, telegram_enabled=True, adapter=adapter)

    with TestClient(app) as client:
        # Create the stale approval after the startup sweep, so the request sees it.
        with database_session(initialized.layout) as session:
            set_profile_status(session, profile_setup.scope, ProfileStatus.ACTIVE)
            item = ContentItemRepository(session).add(
                profile_setup.scope,
                platform="fake",
                body="Expired body.",
                state=ContentState.PENDING_APPROVAL.value,
                risk_level="low",
            )
            ApprovalRepository(session).add(
                profile_setup.scope,
                content_item_id=item.id,
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
            item_id = item.id

        response = client.post(
            "/webhook/telegram",
            json=webhook_payload(f"/approve {item_id}", update_id=23),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "expired" in (body.get("reply") or "").lower()
    assert item_state(initialized.layout, profile_setup.scope, item_id) == "pending_approval"


def test_webhook_never_leaks_the_token_or_full_chat_id(
    initialized: InitResult,
    profile_setup: ProfileSetup,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERALDESK_TELEGRAM_BOT_TOKEN", TOKEN)
    item_id = pending_item(db_session, profile_setup)
    adapter = FakeTelegramAdapter()
    app = make_app(initialized, telegram_enabled=True, adapter=adapter, token=TOKEN)

    with TestClient(app) as client:
        response = client.post(
            "/webhook/telegram",
            json=webhook_payload(f"/approve {item_id} {TOKEN}", update_id=24),
        )

    assert response.status_code == 200
    assert TOKEN not in response.text
    assert str(CHAT_ID) not in response.text
    with database_session(initialized.layout) as session:
        payloads = " ".join(
            str(entry.payload)
            for entry in AuditService(session).history(profile_setup.scope, limit=100)
        )
    assert TOKEN not in payloads
    assert str(CHAT_ID) not in payloads


def test_background_scheduler_publishes_a_due_item_once(
    initialized: InitResult,
    profile_setup: ProfileSetup,
    db_session: Session,
) -> None:
    item_id = make_scheduled(
        db_session,
        profile_setup,
        when=datetime.now(UTC) - timedelta(minutes=1),
    )
    app = make_app(initialized, scheduler_interval=0.3)

    with TestClient(app) as client:
        assert wait_for(
            lambda: item_state(initialized.layout, profile_setup.scope, item_id) == "published"
        )
        time.sleep(0.8)
        assert client.get("/health").status_code == 200
        status = client.get("/status").json()
        assert status["scheduler"]["last_run_at"] is not None
        assert status["scheduler"]["last_run"]["payload"]["profiles_checked"] == 1

    assert profile_actions(initialized.layout, profile_setup.scope, action="publish.succeeded") == [
        "publish.succeeded"
    ]
    with database_session(initialized.layout) as session:
        item = ContentItemRepository(session).get(profile_setup.scope, item_id)
        assert item is not None
        assert item.external_id is not None
        assert item.publish_attempts == 1
        scheduler_runs = AuditService(session).history(None, limit=50)
    assert any(entry.action == "scheduler.run" for entry in scheduler_runs)


def test_polling_job_does_not_double_process_the_same_update_id(
    initialized: InitResult,
    profile_setup: ProfileSetup,
) -> None:
    adapter = FakeTelegramAdapter()
    replay = adapter.enqueue("/queue", CHAT_ID, update_id=41)
    first_app = make_app(
        initialized,
        telegram_enabled=True,
        adapter=adapter,
        poll_interval=0.1,
    )

    with TestClient(first_app):
        assert wait_for(lambda: telegram_messages(initialized.layout) == 1)
        time.sleep(0.2)

    adapter.incoming.append(replay)
    second_app = make_app(
        initialized,
        telegram_enabled=True,
        adapter=adapter,
        poll_interval=0.1,
    )
    with TestClient(second_app):
        time.sleep(0.4)

    assert telegram_messages(initialized.layout) == 1
    assert profile_actions(initialized.layout, profile_setup.scope, action="telegram.received") == [
        "telegram.received"
    ]


def test_graceful_shutdown_records_audit_and_stops_jobs(initialized: InitResult) -> None:
    app = make_app(initialized)

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.context.jobs.running is True

    assert app.state.context.jobs.running is False
    actions = global_actions(initialized.layout)
    assert "serve.started" in actions
    assert "serve.stopped" in actions


def test_scheduler_dry_run_sends_nothing(
    initialized: InitResult,
    profile_setup: ProfileSetup,
    db_session: Session,
) -> None:
    item_id = make_scheduled(
        db_session,
        profile_setup,
        when=datetime.now(UTC) - timedelta(minutes=1),
    )
    config = load_test_config(initialized, telegram_enabled=True)
    adapter = FakeTelegramAdapter()
    database = ServerDatabase(initialized.layout)
    jobs = BackgroundJobs(
        database,
        initialized.layout,
        config,
        adapter=adapter,
        redactor=SecretRegistry.from_environment(extra_values=[TOKEN]),
    )
    try:
        result = jobs.run_scheduler_once(dry_run=True)
    finally:
        database.close()

    assert result.dry_run is True
    assert result.due == 1
    assert result.published == ()
    assert adapter.sent == []
    assert item_state(initialized.layout, profile_setup.scope, item_id) == "scheduled"
    publish_actions = profile_actions(
        initialized.layout,
        profile_setup.scope,
        action="publish.succeeded",
    )
    assert publish_actions == []
    assert "scheduler.run" not in global_actions(initialized.layout)


def test_webhook_mode_disables_polling_but_keeps_the_webhook(
    initialized: InitResult,
    profile_setup: ProfileSetup,
) -> None:
    adapter = FakeTelegramAdapter()
    adapter.enqueue("/queue", CHAT_ID, update_id=51)
    app = make_app(
        initialized,
        telegram_enabled=True,
        mode="webhook",
        adapter=adapter,
        poll_interval=0.1,
    )

    with TestClient(app) as client:
        assert app.state.context.jobs.polls_telegram is False
        assert app.state.context.jobs.run_poll_once() is None
        response = client.post(
            "/webhook/telegram",
            json=webhook_payload("/queue", update_id=52),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert adapter.sent
