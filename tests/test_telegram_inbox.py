"""M6 inbound Telegram tests: approval commands, expiry, help and redaction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import (
    ApprovalRepository,
    ContentItemRepository,
    TelegramMessageRepository,
)
from heraldesk.domain.approvals import request_approval
from heraldesk.domain.content import ContentState
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.telegram.base import TelegramUpdate
from heraldesk.telegram.fake import FakeTelegramAdapter
from heraldesk.telegram.inbox import handle_update
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
CHAT_ID = 123456789
TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def redactor() -> SecretRegistry:
    return SecretRegistry.from_environment(extra_values=[TOKEN])


def pending_item(session: Session, setup: ProfileSetup, *, body: str = "Queue body."):
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body=body,
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
    return item.id, approval


def update(text: str, *, chat_id: int = CHAT_ID) -> TelegramUpdate:
    return TelegramUpdate(chat_id=chat_id, text=text, sender_id=555, update_id=1)


def deliver(session: Session, setup: ProfileSetup, text: str, *, chat_id: int = CHAT_ID):
    adapter = FakeTelegramAdapter()
    outcome = handle_update(
        session,
        setup.layout,
        update(text, chat_id=chat_id),
        allowed_chat_ids=(CHAT_ID,),
        adapter=adapter,
        profile_hint="acme",
        redactor=redactor(),
        now=NOW,
    )
    return outcome, adapter


def test_approve_via_inbox_schedules_require_approval_item(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id, approval = pending_item(db_session, profile_setup)

    outcome, adapter = deliver(db_session, profile_setup, f"/approve {item_id}")

    assert outcome.status == "ok"
    assert outcome.content_item_id == item_id
    assert "scheduled" in (outcome.reply or "")
    assert adapter.sent
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert stored is not None
    assert stored.state == "scheduled"
    decided = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert decided is not None
    assert decided.state == "approved"
    assert decided.channel == "telegram"

    messages = TelegramMessageRepository(db_session).list(profile_setup.scope)
    assert len(messages) == 1
    assert messages[0].command == "approve"
    assert messages[0].content_item_id == item_id
    actions = [
        entry.action for entry in AuditService(db_session).history(profile_setup.scope, limit=100)
    ]
    assert "telegram.received" in actions
    assert "telegram.command" in actions


def test_reject_via_inbox_marks_item_rejected(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id, approval = pending_item(db_session, profile_setup)

    outcome, _adapter = deliver(db_session, profile_setup, f"/reject {item_id} off brand")

    assert outcome.status == "ok"
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert stored is not None
    assert stored.state == "rejected"
    decided = ApprovalRepository(db_session).get(profile_setup.scope, approval.id)
    assert decided is not None
    assert decided.state == "rejected"
    assert decided.channel == "telegram"
    assert decided.reason == "off brand"


def test_expired_approval_cannot_be_approved_via_telegram(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    scope = profile_setup.scope
    set_profile_status(db_session, scope, ProfileStatus.ACTIVE)
    item = ContentItemRepository(db_session).add(
        scope,
        platform="fake",
        body="Expired approval body.",
        state=ContentState.PENDING_APPROVAL.value,
        risk_level="low",
    )
    ApprovalRepository(db_session).add(
        scope,
        content_item_id=item.id,
        expires_at=NOW - timedelta(minutes=1),
    )

    outcome, _adapter = deliver(db_session, profile_setup, f"/approve {item.id}")

    assert outcome.status == "error"
    assert "expired" in (outcome.reply or "").lower()
    stored = ContentItemRepository(db_session).get(scope, item.id)
    assert stored is not None
    assert stored.state == "pending_approval"


def test_queue_command_replies_with_the_queue(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id, _approval = pending_item(db_session, profile_setup)

    outcome, adapter = deliver(db_session, profile_setup, "/queue")

    assert outcome.status == "ok"
    assert f"#{item_id}" in (outcome.reply or "")
    assert adapter.sent


def test_unknown_command_replies_with_help(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    outcome, adapter = deliver(db_session, profile_setup, "/not-a-command")

    assert outcome.status == "help"
    assert "Heraldesk commands" in (outcome.reply or "")
    assert adapter.sent
    assert outcome.message_id is not None


def test_non_allowlisted_chat_is_ignored_without_storage(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    outcome, adapter = deliver(db_session, profile_setup, "/queue", chat_id=999)

    assert outcome.status == "ignored"
    assert outcome.message_id is None
    assert adapter.sent == []
    assert TelegramMessageRepository(db_session).list(profile_setup.scope) == []


def test_inbox_redacts_token_and_chat_id(profile_setup: ProfileSetup, db_session: Session) -> None:
    item_id, _approval = pending_item(db_session, profile_setup)

    outcome, _adapter = deliver(db_session, profile_setup, f"/approve {item_id} {TOKEN}")

    messages = TelegramMessageRepository(db_session).list(profile_setup.scope)
    assert messages
    assert TOKEN not in messages[0].text
    assert "[REDACTED]" in messages[0].text
    payloads = " ".join(
        str(entry.payload)
        for entry in AuditService(db_session).history(profile_setup.scope, limit=100)
    )
    assert TOKEN not in payloads
    assert str(CHAT_ID) not in payloads
    assert "12***89" in payloads
    assert outcome.profile_id == profile_setup.scope.profile_id


def test_sender_allowlist_ignores_other_users(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item_id, _ = pending_item(db_session, profile_setup)
    adapter = FakeTelegramAdapter()

    ignored = handle_update(
        db_session,
        profile_setup.layout,
        update(f"/approve {item_id}"),
        allowed_chat_ids=(CHAT_ID,),
        allowed_user_ids=(999,),
        adapter=adapter,
        profile_hint="acme",
        redactor=redactor(),
        now=NOW,
    )

    assert ignored.status == "ignored"
    item = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert item is not None and item.state == ContentState.PENDING_APPROVAL.value

    accepted = handle_update(
        db_session,
        profile_setup.layout,
        update(f"/approve {item_id}"),
        allowed_chat_ids=(CHAT_ID,),
        allowed_user_ids=(555,),
        adapter=adapter,
        profile_hint="acme",
        redactor=redactor(),
        now=NOW,
    )

    assert accepted.status == "ok"
