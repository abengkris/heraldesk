"""Tests for manual inbox triage."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from heraldesk.community import (
    IgnoreReason,
    InboxError,
    InboxItemNotFoundError,
    InboxStateError,
    ignore_item,
    list_items,
    reply_item,
    show_item,
)
from heraldesk.core.audit import AuditService
from heraldesk.core.locks import engage_global_kill_switch
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import InboxRepository, TelegramMessageRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.telegram.fake import FakeTelegramAdapter

pytestmark = pytest.mark.core

CHAT_ID = 123456789
TOKEN = "sk-1234567890abcdefghijklmnop"


def add_message(
    db_session: Session,
    scope: ProfileScope | None,
    *,
    text: str = "hello",
    created_at: datetime | None = None,
) -> int:
    if scope is not None:
        set_profile_status(db_session, scope, ProfileStatus.ACTIVE)
    message = TelegramMessageRepository(db_session).add(
        scope,
        chat_id=CHAT_ID,
        sender_id=555666777,
        text=text,
        command=None,
        content_item_id=None,
        response=None,
        created_at=created_at or datetime(2026, 11, 17, 12, 0, tzinfo=UTC),
    )
    return message.id


def test_list_defaults_to_pending_newest_first(db_session: Session, profile_setup) -> None:
    scope = profile_setup.scope
    first = add_message(
        db_session, scope, text="older", created_at=datetime(2026, 11, 17, 10, tzinfo=UTC)
    )
    second = add_message(
        db_session, scope, text="newer", created_at=datetime(2026, 11, 17, 11, tzinfo=UTC)
    )

    items = list_items(db_session, limit=20)

    assert [item.id for item in items] == [second, first]
    assert all(item.status == "pending" for item in items)
    actions = [entry.action for entry in AuditService(db_session).history(None)]
    assert "inbox.listed" in actions


def test_list_filters_by_status_and_limit(db_session: Session, profile_setup) -> None:
    scope = profile_setup.scope
    pending = add_message(db_session, scope, text="pending")
    handled = add_message(db_session, scope, text="handled")
    ignored = add_message(db_session, scope, text="ignored")
    InboxRepository(db_session).mark_handled(
        handled, response="reply", triaged_at=datetime(2026, 11, 17, tzinfo=UTC)
    )
    InboxRepository(db_session).mark_ignored(
        ignored, reason="spam", triaged_at=datetime(2026, 11, 17, tzinfo=UTC)
    )

    assert [item.id for item in list_items(db_session, status="pending", limit=1)] == [pending]
    assert [item.id for item in list_items(db_session, status="handled")] == [handled]
    assert [item.id for item in list_items(db_session, status="ignored")] == [ignored]


def test_show_missing_and_existing(db_session: Session, profile_setup) -> None:
    item_id = add_message(db_session, profile_setup.scope, text="show me")

    item = show_item(db_session, item_id)

    assert item.id == item_id
    assert item.profile_slug == "acme"
    assert item.text == "show me"
    with pytest.raises(InboxItemNotFoundError):
        show_item(db_session, 99999)


def test_reply_sends_marks_handled_and_audits(db_session: Session, profile_setup) -> None:
    item_id = add_message(db_session, profile_setup.scope)
    adapter = FakeTelegramAdapter()

    item = reply_item(
        db_session,
        profile_setup.layout,
        item_id,
        f"thanks {TOKEN}",
        adapter=adapter,
        redactor=SecretRegistry.from_environment(),
        now=datetime(2026, 11, 17, 13, tzinfo=UTC),
    )

    assert item.status == "handled"
    assert item.response is not None and TOKEN not in item.response
    assert "[REDACTED]" in item.response
    assert adapter.sent == [(CHAT_ID, item.response)]
    entries = [
        entry
        for entry in AuditService(db_session).history(profile_setup.scope)
        if entry.action == "inbox.replied"
    ]
    assert len(entries) == 1
    payload = entries[0].payload
    assert payload["message_id"] == item_id
    assert payload["text_sha256"]
    assert TOKEN not in str(payload)


def test_reply_rejects_empty_missing_and_triaged_items(db_session: Session, profile_setup) -> None:
    adapter = FakeTelegramAdapter()
    item_id = add_message(db_session, profile_setup.scope)

    with pytest.raises(InboxError, match="must not be empty"):
        reply_item(db_session, profile_setup.layout, item_id, "   ", adapter=adapter)
    with pytest.raises(InboxItemNotFoundError):
        reply_item(db_session, profile_setup.layout, 99999, "hi", adapter=adapter)

    reply_item(db_session, profile_setup.layout, item_id, "first", adapter=adapter)
    with pytest.raises(InboxStateError, match="already handled"):
        reply_item(db_session, profile_setup.layout, item_id, "second", adapter=adapter)

    other = add_message(db_session, profile_setup.scope)
    ignore_item(db_session, other)
    with pytest.raises(InboxStateError, match="already ignored"):
        reply_item(db_session, profile_setup.layout, other, "hi", adapter=adapter)


def test_reply_provider_failure_leaves_item_pending(db_session: Session, profile_setup) -> None:
    item_id = add_message(db_session, profile_setup.scope)
    adapter = FakeTelegramAdapter(fail_times=1)

    with pytest.raises(InboxError):
        reply_item(db_session, profile_setup.layout, item_id, "hi", adapter=adapter)

    item = show_item(db_session, item_id)
    assert item.status == "pending"
    actions = [entry.action for entry in AuditService(db_session).history(profile_setup.scope)]
    assert "inbox.replied" not in actions


def test_ignore_sets_reason_and_defaults_to_other(db_session: Session, profile_setup) -> None:
    spam_id = add_message(db_session, profile_setup.scope)
    other_id = add_message(db_session, profile_setup.scope)

    ignored = ignore_item(db_session, spam_id, reason=IgnoreReason.SPAM)
    defaulted = ignore_item(db_session, other_id)

    assert ignored.status == "ignored"
    assert ignored.ignore_reason == "spam"
    assert defaulted.ignore_reason == "other"
    actions = [entry.action for entry in AuditService(db_session).history(profile_setup.scope)]
    assert actions.count("inbox.ignored") == 2


def test_unlinked_message_can_be_triaged(db_session: Session) -> None:
    item_id = add_message(db_session, None, text="no profile yet")

    listed = list_items(db_session)
    shown = show_item(db_session, item_id)
    ignored = ignore_item(db_session, item_id, reason=IgnoreReason.OFF_TOPIC)

    assert [item.id for item in listed] == [item_id]
    assert shown.profile_slug is None
    assert ignored.ignore_reason == "off-topic"
    actions = [entry.action for entry in AuditService(db_session).history(None)]
    assert "inbox.shown" in actions
    assert "inbox.ignored" in actions


def test_reply_is_blocked_by_the_global_kill_switch(db_session: Session, profile_setup) -> None:
    item_id = add_message(db_session, profile_setup.scope)
    adapter = FakeTelegramAdapter()
    engage_global_kill_switch(profile_setup.layout)

    with pytest.raises(InboxError, match="global_kill_switch"):
        reply_item(db_session, profile_setup.layout, item_id, "hi", adapter=adapter)

    assert adapter.sent == []
    assert show_item(db_session, item_id).status == "pending"
