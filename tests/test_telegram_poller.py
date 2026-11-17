"""M7 Telegram polling: offsets, allowlist and update-id deduplication."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from heraldesk.db.repositories import TelegramMessageRepository
from heraldesk.telegram.fake import FakeTelegramAdapter
from heraldesk.telegram.poller import TelegramPoller
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

CHAT_ID = 123456789


def make_poller(adapter: FakeTelegramAdapter) -> TelegramPoller:
    return TelegramPoller(
        adapter,
        allowed_chat_ids=(CHAT_ID,),
        profile_hint="acme",
    )


def test_poller_processes_allowlisted_update_once(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    adapter = FakeTelegramAdapter()
    adapter.enqueue("/queue", CHAT_ID, update_id=7)

    result = make_poller(adapter).poll_once(db_session, profile_setup.layout)

    assert result.received == 1
    assert result.processed == 1
    assert result.duplicates == 0
    assert result.next_offset == 8
    assert adapter.sent
    messages = TelegramMessageRepository(db_session).list(profile_setup.scope)
    assert len(messages) == 1
    assert messages[0].update_id == 7


def test_same_update_id_is_not_processed_twice(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    adapter = FakeTelegramAdapter()
    replay = adapter.enqueue("/queue", CHAT_ID, update_id=11)

    first = make_poller(adapter).poll_once(db_session, profile_setup.layout)
    assert first.processed == 1

    # A restart creates a new poller with no offset, so Telegram may replay the update.
    adapter.incoming.append(replay)
    second = make_poller(adapter).poll_once(db_session, profile_setup.layout)

    assert second.received == 1
    assert second.duplicates == 1
    assert second.processed == 0
    assert len(TelegramMessageRepository(db_session).list(profile_setup.scope)) == 1


def test_non_allowlisted_chat_is_ignored(profile_setup: ProfileSetup, db_session: Session) -> None:
    adapter = FakeTelegramAdapter()
    adapter.enqueue("/queue", 999, update_id=12)

    result = make_poller(adapter).poll_once(db_session, profile_setup.layout)

    assert result.ignored == 1
    assert result.processed == 0
    assert adapter.sent == []
    assert TelegramMessageRepository(db_session).list(profile_setup.scope) == []


def test_offset_advances_and_new_updates_are_processed(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    adapter = FakeTelegramAdapter()
    adapter.enqueue("hello", CHAT_ID, update_id=3)
    adapter.enqueue("hello again", CHAT_ID, update_id=4)
    poller = make_poller(adapter)

    first = poller.poll_once(db_session, profile_setup.layout)
    assert first.received == 2
    assert poller.offset == 5

    adapter.enqueue("newer", CHAT_ID, update_id=5)
    second = poller.poll_once(db_session, profile_setup.layout)

    assert second.received == 1
    assert second.processed == 1
    assert poller.offset == 6
    assert len(TelegramMessageRepository(db_session).list(profile_setup.scope)) == 3
