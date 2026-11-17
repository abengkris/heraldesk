"""Publisher tests for the Nostr adapter: retries, at-most-once and redaction."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher
from heraldesk.db.repositories import ContentItemRepository, SocialAccountRepository
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.platforms.nostr import NostrAdapter
from heraldesk.platforms.nostr_keys import encode_nsec
from heraldesk.publisher import PublishStatus, publish_item
from heraldesk.scheduler.runner import run_scheduler
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SECRET = bytes.fromhex("00" * 31 + "03")
NSEC = encode_nsec(SECRET)
NPUB = "npub1lycg5qvjtrp3qjf5f7zl382j9x6nrjz9sdhenvyxq8c3808qxmus6gq266"
BODY = "A plain draft about Nostr."


class FakeRelayClient:
    def __init__(self, outcomes: dict[str, Any] | None = None, default: Any = True) -> None:
        self.calls: list[str] = []
        self._outcomes = outcomes or {}
        self._default = default

    async def publish(
        self,
        relay: str,
        event_json: str,
        event_id: str,
        *,
        timeout: float,
    ) -> Any:
        _ = event_json, event_id, timeout
        self.calls.append(relay)
        outcome = self._outcomes.get(relay, self._default)
        if isinstance(outcome, BaseException):
            raise outcome
        from heraldesk.platforms.nostr import RelayAck

        return RelayAck(relay=relay, accepted=bool(outcome), message="")


def cipher_for(setup: ProfileSetup) -> CredentialCipher:
    return CredentialCipher.from_key_file(setup.layout.key_path)


def nostr_adapter(client: FakeRelayClient) -> NostrAdapter:
    return NostrAdapter(
        relays=("wss://relay.one", "wss://relay.two"),
        clock=lambda: NOW,
        relay_client=client,
    )


def add_nostr_account(session: Session, setup: ProfileSetup) -> int:
    account = SocialAccountRepository(session).add(
        setup.scope,
        platform="nostr",
        handle=NPUB,
        label="throwaway",
        credentials_encrypted=cipher_for(setup).encrypt_json({"nsec": NSEC}),
    )
    return account.id


def make_scheduled(
    session: Session,
    setup: ProfileSetup,
    *,
    body: str = BODY,
    account_id: int | None = None,
) -> int:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="nostr",
        body=body,
        account_id=account_id,
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(setup.scope, item.id, mode="require_approval", delay_minutes=None)
    transition(
        session,
        setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=NOW,
    )
    return item.id


def test_publish_via_nostr_adapter_records_the_event_id(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    account_id = add_nostr_account(db_session, profile_setup)
    item_id = make_scheduled(db_session, profile_setup, account_id=account_id)
    client = FakeRelayClient()

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item_id,
        adapter=nostr_adapter(client),
        cipher=cipher_for(profile_setup),
        now=NOW,
        actor="user",
        trigger="manual",
    )

    assert outcome.status is PublishStatus.PUBLISHED
    assert outcome.external_id is not None
    assert len(outcome.external_id) == 64
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert stored is not None
    assert stored.external_id == outcome.external_id
    assert stored.state == "published"
    assert len(client.calls) == 2

    payloads = json.dumps(
        [entry.payload for entry in AuditService(db_session).history(profile_setup.scope)]
    )
    assert NSEC not in payloads
    assert NPUB not in payloads


def test_retry_after_relay_failure_then_success(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    account_id = add_nostr_account(db_session, profile_setup)
    item_id = make_scheduled(db_session, profile_setup, account_id=account_id)
    client = FakeRelayClient(default=OSError("relay unreachable"))

    failed = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item_id,
        adapter=nostr_adapter(client),
        cipher=cipher_for(profile_setup),
        now=NOW,
        trigger="scheduler",
    )
    assert failed.status is PublishStatus.FAILED
    assert failed.retry_at is not None
    assert failed.attempts == 1

    retried = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item_id,
        adapter=nostr_adapter(FakeRelayClient()),
        cipher=cipher_for(profile_setup),
        now=failed.retry_at,
        trigger="scheduler",
    )
    assert retried.status is PublishStatus.PUBLISHED
    assert retried.attempts == 2


def test_published_item_is_never_sent_twice(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    account_id = add_nostr_account(db_session, profile_setup)
    item_id = make_scheduled(db_session, profile_setup, account_id=account_id)
    client = FakeRelayClient()
    instance = nostr_adapter(client)

    first = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item_id,
        adapter=instance,
        cipher=cipher_for(profile_setup),
        now=NOW,
        trigger="manual",
    )
    assert first.status is PublishStatus.PUBLISHED
    calls_after_first = len(client.calls)

    second = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item_id,
        adapter=instance,
        cipher=cipher_for(profile_setup),
        now=NOW,
        trigger="manual",
    )
    assert second.status is PublishStatus.SKIPPED
    assert second.external_id == first.external_id
    assert len(client.calls) == calls_after_first


def test_final_failure_stores_a_redacted_error(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    account_id = add_nostr_account(db_session, profile_setup)
    item_id = make_scheduled(db_session, profile_setup, account_id=account_id)
    client = FakeRelayClient(default=RuntimeError(f"relay rejected {NSEC}"))

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item_id,
        adapter=nostr_adapter(client),
        cipher=cipher_for(profile_setup),
        now=NOW,
        trigger="scheduler",
        retry_backoff_seconds=(0, 0),
    )
    assert outcome.status is PublishStatus.FAILED
    assert outcome.error is not None
    assert NSEC not in outcome.error
    assert "[REDACTED]" in outcome.error

    stored = ContentItemRepository(db_session).get(profile_setup.scope, item_id)
    assert stored is not None
    assert stored.state == "failed"
    assert stored.last_error is not None
    assert NSEC not in stored.last_error
    assert "[REDACTED]" in stored.last_error

    payloads = json.dumps(
        [entry.payload for entry in AuditService(db_session).history(profile_setup.scope)]
    )
    assert NSEC not in payloads
    assert "[REDACTED]" in payloads


def test_scheduler_uses_the_nostr_adapter(profile_setup: ProfileSetup, db_session: Session) -> None:
    account_id = add_nostr_account(db_session, profile_setup)
    item_id = make_scheduled(db_session, profile_setup, account_id=account_id)
    client = FakeRelayClient()

    result = run_scheduler(
        db_session,
        profile_setup.layout,
        now=NOW,
        cipher=cipher_for(profile_setup),
        adapter_factory=lambda platform: nostr_adapter(client),
    )

    published = [entry for entry in result.entries if entry.item_id == item_id]
    assert len(published) == 1
    assert published[0].external_id is not None
    assert len(published[0].external_id) == 64
