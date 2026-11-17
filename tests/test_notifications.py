"""M6 notification tests: idempotency, policy mutes, redaction and publisher wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import (
    ContentItemRepository,
    TelegramNotificationRepository,
)
from heraldesk.domain.approvals import request_approval
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.publisher import publish_item
from heraldesk.scheduler.runner import run_scheduler
from heraldesk.telegram.fake import FakeTelegramAdapter
from heraldesk.telegram.notifier import NotificationService
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
CHAT_ID = 123456789
TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def cipher_for(setup: ProfileSetup) -> CredentialCipher:
    return CredentialCipher.from_key_file(setup.layout.key_path)


def service(
    setup: ProfileSetup,
    session: Session,
    adapter: FakeTelegramAdapter | None = None,
    *,
    chat_ids: tuple[int, ...] = (CHAT_ID,),
) -> tuple[NotificationService, FakeTelegramAdapter]:
    telegram = adapter or FakeTelegramAdapter()
    notifier = NotificationService(
        session,
        setup.layout,
        telegram,
        allowed_chat_ids=chat_ids,
        redactor=SecretRegistry.from_environment(extra_values=[TOKEN]),
    )
    return notifier, telegram


def activate(session: Session, setup: ProfileSetup) -> None:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)


def pending_item(session: Session, setup: ProfileSetup, *, body: str = "Dashboard launch."):
    item = ContentItemRepository(session).add(
        setup.scope,
        platform="fake",
        body=body,
        state=ContentState.PENDING_APPROVAL.value,
        risk_level="low",
    )
    approval = request_approval(session, setup.scope, item.id, now=NOW)
    stored = ContentItemRepository(session).get(setup.scope, item.id)
    assert stored is not None
    return stored, approval


def notifications(session: Session, setup: ProfileSetup):
    return TelegramNotificationRepository(session).list(setup.scope, limit=100)


def audit_actions(session: Session, setup: ProfileSetup) -> list[str]:
    return [entry.action for entry in AuditService(session).history(setup.scope, limit=1000)]


def write_policy(setup: ProfileSetup, body: str) -> None:
    (setup.definition.path / "policy.yaml").write_text(body, encoding="utf-8")


def test_approval_notification_is_sent_once(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    notifier, telegram = service(profile_setup, db_session)
    item, approval = pending_item(db_session, profile_setup)

    first = notifier.notify_approval_requested(
        profile_setup.scope,
        slug="acme",
        item=item,
        approval=approval,
        now=NOW,
    )
    second = notifier.notify_approval_requested(
        profile_setup.scope,
        slug="acme",
        item=item,
        approval=approval,
        now=NOW,
    )

    assert first.status == "sent"
    assert second.duplicate is True
    assert telegram.send_calls == 1
    rows = notifications(db_session, profile_setup)
    assert len(rows) == 1
    assert rows[0].status == "sent"
    assert rows[0].event == "approval_requested"
    assert audit_actions(db_session, profile_setup).count("telegram.sent") == 1


def test_notify_off_mutes_without_sending(profile_setup: ProfileSetup, db_session: Session) -> None:
    write_policy(profile_setup, "default_mode: draft_only\nnotify: off\n")
    notifier, telegram = service(profile_setup, db_session)
    item, approval = pending_item(db_session, profile_setup)

    outcome = notifier.notify_approval_requested(
        profile_setup.scope,
        slug="acme",
        item=item,
        approval=approval,
        now=NOW,
    )

    assert outcome.status == "suppressed"
    assert telegram.send_calls == 0
    rows = notifications(db_session, profile_setup)
    assert len(rows) == 1
    assert rows[0].status == "suppressed"
    assert "telegram.suppressed" in audit_actions(db_session, profile_setup)


def test_approvals_only_mutes_publish_but_not_approval(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    write_policy(profile_setup, "default_mode: draft_only\nnotify: approvals_only\n")
    notifier, telegram = service(profile_setup, db_session)
    item, approval = pending_item(db_session, profile_setup)

    approved = notifier.notify_approval_requested(
        profile_setup.scope,
        slug="acme",
        item=item,
        approval=approval,
        now=NOW,
    )
    published = notifier.notify_published(
        profile_setup.scope,
        slug="acme",
        item=item,
        now=NOW,
    )

    assert approved.status == "sent"
    assert published.status == "suppressed"
    assert telegram.send_calls == 1


def test_killswitch_notification_is_sent_once(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    notifier, telegram = service(profile_setup, db_session)

    first = notifier.notify_killswitch(
        None,
        slug=None,
        enabled=True,
        event_id="42",
        now=NOW,
    )
    second = notifier.notify_killswitch(
        None,
        slug=None,
        enabled=True,
        event_id="42",
        now=NOW,
    )

    cleared = notifier.notify_killswitch(
        None,
        slug=None,
        enabled=False,
        event_id="43",
        now=NOW,
    )

    assert first.status == "sent"
    assert second.duplicate is True
    assert cleared.status == "sent"
    assert telegram.send_calls == 2
    assert "Global kill switch engaged." in telegram.sent[0][1]
    assert "Global kill switch cleared." in telegram.sent[1][1]


def test_manual_publish_notifies_and_deduplicates(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    activate(db_session, profile_setup)
    notifier, telegram = service(profile_setup, db_session)
    item = ContentItemRepository(db_session).add(
        profile_setup.scope,
        platform="fake",
        body="Manual publish body.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )

    first = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=FakeAdapter(),
        cipher=cipher_for(profile_setup),
        now=NOW,
        actor="user",
        trigger="manual",
        notifier=notifier,
    )
    second = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=FakeAdapter(),
        cipher=cipher_for(profile_setup),
        now=NOW,
        actor="user",
        trigger="manual",
        notifier=notifier,
    )

    assert first.status.value == "published"
    assert second.status.value == "skipped"
    assert telegram.send_calls == 1
    rows = notifications(db_session, profile_setup)
    assert len(rows) == 1
    assert rows[0].event == "published"


def test_scheduler_publish_notifies_once(profile_setup: ProfileSetup, db_session: Session) -> None:
    activate(db_session, profile_setup)
    notifier, telegram = service(profile_setup, db_session)
    item = ContentItemRepository(db_session).add(
        profile_setup.scope,
        platform="fake",
        body="Scheduled publish body.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    ContentItemRepository(db_session).set_publish_policy(
        profile_setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    transition(
        db_session,
        profile_setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=NOW - timedelta(minutes=1),
    )

    first = run_scheduler(
        db_session,
        profile_setup.layout,
        now=NOW,
        adapter_factory=lambda _platform: FakeAdapter(),
        cipher=cipher_for(profile_setup),
        notifier=notifier,
    )
    second = run_scheduler(
        db_session,
        profile_setup.layout,
        now=NOW + timedelta(minutes=1),
        adapter_factory=lambda _platform: FakeAdapter(),
        cipher=cipher_for(profile_setup),
        notifier=notifier,
    )

    assert len(first.published) == 1
    assert second.due == 0
    assert telegram.send_calls == 1
    rows = notifications(db_session, profile_setup)
    assert len(rows) == 1
    assert rows[0].event == "published"


def test_failed_final_notifies_after_three_attempts(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    activate(db_session, profile_setup)
    notifier, telegram = service(profile_setup, db_session)
    repository = ContentItemRepository(db_session)
    item = repository.add(
        profile_setup.scope,
        platform="fake",
        body="This will fail.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(
        profile_setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    transition(
        db_session,
        profile_setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=NOW - timedelta(minutes=1),
    )
    repository.record_publish_attempt(
        profile_setup.scope,
        item.id,
        attempts=2,
        error="earlier failure",
    )

    result = run_scheduler(
        db_session,
        profile_setup.layout,
        now=NOW,
        adapter_factory=lambda _platform: FakeAdapter(fail_times=1),
        cipher=cipher_for(profile_setup),
        notifier=notifier,
        retry_backoff_seconds=(60, 300),
    )

    assert len(result.failed) == 1
    assert telegram.send_calls == 1
    rows = notifications(db_session, profile_setup)
    assert len(rows) == 1
    assert rows[0].event == "failed_final"


def test_manual_final_failure_notifies(profile_setup: ProfileSetup, db_session: Session) -> None:
    activate(db_session, profile_setup)
    notifier, telegram = service(profile_setup, db_session)
    repository = ContentItemRepository(db_session)
    item = repository.add(
        profile_setup.scope,
        platform="fake",
        body="Manual final failure.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(
        profile_setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    transition(
        db_session,
        profile_setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=NOW - timedelta(minutes=1),
    )
    repository.record_publish_attempt(
        profile_setup.scope,
        item.id,
        attempts=2,
        error="earlier failure",
    )

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=FakeAdapter(fail_times=1),
        cipher=cipher_for(profile_setup),
        now=NOW,
        actor="user",
        trigger="manual",
        notifier=notifier,
    )

    assert outcome.status.value == "failed"
    assert outcome.retry_at is None
    assert telegram.send_calls == 1
    rows = notifications(db_session, profile_setup)
    assert len(rows) == 1
    assert rows[0].event == "failed_final"


def test_dry_run_scheduler_sends_nothing(profile_setup: ProfileSetup, db_session: Session) -> None:
    activate(db_session, profile_setup)
    notifier, telegram = service(profile_setup, db_session)
    repository = ContentItemRepository(db_session)
    item = repository.add(
        profile_setup.scope,
        platform="fake",
        body="Dry run body.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(
        profile_setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    transition(
        db_session,
        profile_setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=NOW - timedelta(minutes=1),
    )
    platform_adapter = FakeAdapter()

    result = run_scheduler(
        db_session,
        profile_setup.layout,
        now=NOW,
        dry_run=True,
        adapter_factory=lambda _platform: platform_adapter,
        cipher=cipher_for(profile_setup),
        notifier=notifier,
    )

    assert result.planned
    assert platform_adapter.publish_calls == 0
    assert telegram.send_calls == 0
    assert notifications(db_session, profile_setup) == []
    assert "telegram.sent" not in audit_actions(db_session, profile_setup)


def test_token_body_and_chat_id_are_redacted(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    notifier, telegram = service(profile_setup, db_session)
    item, approval = pending_item(
        db_session,
        profile_setup,
        body=f"Body with token {TOKEN} inside.",
    )

    notifier.notify_approval_requested(
        profile_setup.scope,
        slug="acme",
        item=item,
        approval=approval,
        now=NOW,
    )

    assert len(telegram.sent) == 1
    assert TOKEN not in telegram.sent[0][1]
    rows = notifications(db_session, profile_setup)
    assert rows[0].message_preview is not None
    assert TOKEN not in rows[0].message_preview
    payloads = " ".join(
        str(entry.payload) for entry in AuditService(db_session).history(profile_setup.scope)
    )
    assert TOKEN not in payloads
    assert str(CHAT_ID) not in payloads
    assert rows[0].recipients == ["12***89"]
