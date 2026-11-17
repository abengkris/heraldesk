"""Publisher tests: success, retries, redaction, idempotency and off switches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher
from heraldesk.core.locks import engage_global_kill_switch
from heraldesk.db.models import ContentItem
from heraldesk.db.repositories import (
    ApprovalRepository,
    ContentItemRepository,
    SocialAccountRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.publisher import PublishStatus, publish_item
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SECRET = "sk-ABCDEFGHIJKLMNOP1234"
BODY = "A plain draft about dashboards."


def cipher_for(setup: ProfileSetup) -> CredentialCipher:
    return CredentialCipher.from_key_file(setup.layout.key_path)


def make_scheduled(
    session: Session,
    setup: ProfileSetup,
    *,
    when: datetime = NOW,
    body: str = BODY,
    mode: str = "require_approval",
    platform: str = "fake",
    account_id: int | None = None,
    activate: bool = True,
) -> ContentItem:
    if activate:
        set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform=platform,
        body=body,
        account_id=account_id,
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(setup.scope, item.id, mode=mode, delay_minutes=None)
    transition(
        session,
        setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="test schedule",
        schedule_at=when,
    )
    stored = repository.get(setup.scope, item.id)
    assert stored is not None
    return stored


def actions(session: Session, setup: ProfileSetup) -> list[str]:
    return [entry.action for entry in AuditService(session).history(setup.scope, limit=1000)]


def test_publish_success_stores_external_id_and_audit(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_scheduled(db_session, profile_setup)
    adapter = FakeAdapter()

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW,
    )

    assert outcome.status is PublishStatus.PUBLISHED
    assert outcome.external_id == "fake-1"
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "published"
    assert stored.external_id == "fake-1"
    assert stored.published_at is not None
    assert stored.last_error is None
    assert len(adapter.published) == 1
    recorded = actions(db_session, profile_setup)
    assert "publish.requested" in recorded
    assert "publish.succeeded" in recorded
    assert "content.state_changed" in recorded


def test_publish_failure_retries_with_backoff_and_redacts(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_scheduled(db_session, profile_setup)
    adapter = FakeAdapter(fail_times=3, error=f"provider rejected {SECRET}")

    first = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW,
        retry_backoff_seconds=(60, 300),
    )
    assert first.status is PublishStatus.FAILED
    assert first.attempts == 1
    assert first.retry_at == NOW + timedelta(seconds=60)

    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "failed"
    assert stored.publish_attempts == 1
    assert stored.last_error is not None
    assert SECRET not in stored.last_error
    assert "[REDACTED]" in stored.last_error
    retry_at = stored.scheduled_at
    assert retry_at is not None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    assert retry_at == NOW + timedelta(seconds=60)

    second = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW + timedelta(seconds=60),
        retry_backoff_seconds=(60, 300),
    )
    assert second.attempts == 2
    assert second.retry_at == NOW + timedelta(seconds=360)

    third = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW + timedelta(seconds=360),
        retry_backoff_seconds=(60, 300),
    )
    assert third.attempts == 3
    assert third.retry_at is None

    exhausted = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert exhausted is not None
    assert exhausted.state == "failed"
    assert exhausted.publish_attempts == 3
    assert exhausted.scheduled_at is None
    assert exhausted.last_error is not None
    assert SECRET not in exhausted.last_error

    failures = [
        entry
        for entry in AuditService(db_session).history(profile_setup.scope, limit=1000)
        if entry.action == "publish.failed"
    ]
    assert len(failures) == 3
    assert all(SECRET not in str(entry.payload) for entry in failures)


def test_publish_retry_can_succeed(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = make_scheduled(db_session, profile_setup)
    adapter = FakeAdapter(fail_times=1)

    first = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW,
    )
    assert first.status is PublishStatus.FAILED
    assert first.attempts == 1

    second = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW + timedelta(seconds=61),
    )
    assert second.status is PublishStatus.PUBLISHED
    assert second.attempts == 2
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "published"
    assert stored.external_id == "fake-1"


def test_manual_publish_requeues_an_exhausted_failed_item(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_scheduled(db_session, profile_setup)
    repository = ContentItemRepository(db_session)
    transition(db_session, profile_setup.scope, item.id, ContentState.FAILED, reason="test")
    repository.record_publish_attempt(
        profile_setup.scope,
        item.id,
        attempts=3,
        error="exhausted",
    )
    repository.set_scheduled_at(profile_setup.scope, item.id, scheduled_at=None)

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=FakeAdapter(),
        cipher=cipher_for(profile_setup),
        now=NOW,
        trigger="manual",
    )

    assert outcome.status is PublishStatus.PUBLISHED
    assert outcome.attempts == 1
    stored = repository.get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "published"
    assert stored.publish_attempts == 1


def test_publish_is_idempotent(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = make_scheduled(db_session, profile_setup)
    adapter = FakeAdapter()
    cipher = cipher_for(profile_setup)

    first = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher,
        now=NOW,
    )
    second = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher,
        now=NOW,
    )

    assert first.status is PublishStatus.PUBLISHED
    assert second.status is PublishStatus.SKIPPED
    assert "external id" in (second.reason or "")
    assert adapter.publish_calls == 1
    assert len(adapter.published) == 1


def test_publish_skips_under_the_global_kill_switch(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_scheduled(db_session, profile_setup)
    engage_global_kill_switch(profile_setup.layout)

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=FakeAdapter(),
        cipher=cipher_for(profile_setup),
        now=NOW,
    )

    assert outcome.status is PublishStatus.SKIPPED
    assert outcome.reason == "global_kill_switch"
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.state == "scheduled"
    assert "publish.requested" not in actions(db_session, profile_setup)


def test_publish_skips_a_paused_profile(profile_setup: ProfileSetup, db_session: Session) -> None:
    item = make_scheduled(db_session, profile_setup)
    set_profile_status(db_session, profile_setup.scope, ProfileStatus.PAUSED)

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=FakeAdapter(),
        cipher=cipher_for(profile_setup),
        now=NOW,
    )

    assert outcome.status is PublishStatus.SKIPPED
    assert outcome.reason == "profile_paused"


def test_publish_skips_a_non_approved_approval(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_scheduled(db_session, profile_setup)
    approval = ApprovalRepository(db_session).add(
        profile_setup.scope,
        content_item_id=item.id,
        expires_at=NOW + timedelta(hours=1),
    )
    ApprovalRepository(db_session).update_decision(
        profile_setup.scope,
        approval.id,
        state="expired",
        decision=None,
        reason="expired",
        edited_body=None,
        decided_at=NOW,
        channel="cli",
    )

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=FakeAdapter(),
        cipher=cipher_for(profile_setup),
        now=NOW,
    )

    assert outcome.status is PublishStatus.SKIPPED
    assert "expired" in (outcome.reason or "")


def test_missing_adapter_fails_instead_of_pretending(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_scheduled(db_session, profile_setup)

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=None,
        cipher=cipher_for(profile_setup),
        now=NOW,
    )

    assert outcome.status is PublishStatus.FAILED
    assert outcome.error is not None
    assert "no publisher adapter" in outcome.error
    assert "docs/platforms/README.md" in outcome.error


def test_provider_error_never_leaks_account_credentials(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    token = "super-secret-account-token"
    cipher = cipher_for(profile_setup)
    account = SocialAccountRepository(db_session).add(
        profile_setup.scope,
        platform="fake",
        handle="@acme",
        credentials_encrypted=cipher.encrypt_json({"token": token}),
    )
    item = make_scheduled(db_session, profile_setup, account_id=account.id)
    adapter = FakeAdapter(fail_times=1, error=f"auth failed for {token}")

    outcome = publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher,
        now=NOW,
    )

    assert outcome.status is PublishStatus.FAILED
    assert token not in (outcome.error or "")
    stored = ContentItemRepository(db_session).get(profile_setup.scope, item.id)
    assert stored is not None
    assert stored.last_error is not None
    assert token not in stored.last_error
    for entry in AuditService(db_session).history(profile_setup.scope, limit=1000):
        assert token not in str(entry.payload)


def test_profile_isolation_between_publishers(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    from heraldesk.domain.errors import ContentItemNotFoundError
    from heraldesk.domain.profiles import load_profile, register_definition, write_profile_skeleton

    other = load_profile(write_profile_skeleton(profile_setup.layout.profiles, "other"))
    other_summary, _ = register_definition(db_session, other)
    other_scope = ProfileScope(other_summary.id)

    other_item = ContentItemRepository(db_session).add(
        other_scope,
        platform="fake",
        body="Other profile post.",
        state=ContentState.SCHEDULED.value,
    )
    with pytest.raises(ContentItemNotFoundError):
        publish_item(
            db_session,
            profile_setup.layout,
            profile_setup.scope,
            other_item.id,
            adapter=FakeAdapter(),
            cipher=cipher_for(profile_setup),
            now=NOW,
        )


def test_secret_marker_in_body_is_redacted_before_publishing(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    item = make_scheduled(db_session, profile_setup, body=f"Leaked {SECRET} in a post.")
    adapter = FakeAdapter()

    publish_item(
        db_session,
        profile_setup.layout,
        profile_setup.scope,
        item.id,
        adapter=adapter,
        cipher=cipher_for(profile_setup),
        now=NOW,
    )

    assert adapter.published
    assert SECRET not in adapter.published[0].body
    assert "[REDACTED]" in adapter.published[0].body
