"""Social account add/test tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.crypto import CredentialCipher, generate_key
from heraldesk.db.repositories import ProfileRepository, SocialAccountRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.accounts import add_account, check_accounts
from heraldesk.domain.errors import (
    AccountError,
    AccountExistsError,
    NoAccountsError,
    UnknownPlatformError,
)
from heraldesk.domain.profiles import ProfileDefinition, register_definition
from heraldesk.platforms.fake import FakeAdapter

pytestmark = pytest.mark.core

MakeDefinition = Callable[[str], ProfileDefinition]


def make_scope(session: Session, definition: ProfileDefinition) -> ProfileScope:
    summary, _ = register_definition(session, definition)
    return ProfileScope(summary.id)


def cipher(initialized: InitResult) -> CredentialCipher:
    return CredentialCipher.from_key_file(initialized.layout.key_path)


def test_add_encrypts_credentials_at_rest(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    account = add_account(
        db_session,
        scope,
        platform="fake",
        handle="@alpha",
        credentials={"token": "plaintext-token"},
        cipher=cipher(initialized),
    )
    assert "plaintext-token" not in account.credentials_encrypted
    assert account.status == "unverified"
    assert cipher(initialized).decrypt_json(account.credentials_encrypted) == {
        "token": "plaintext-token"
    }
    assert ProfileRepository(db_session).require(scope).slug == "alpha"


def test_add_rejects_duplicate_account(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    add_account(
        db_session,
        scope,
        platform="fake",
        handle="@alpha",
        credentials={"token": "one"},
        cipher=cipher(initialized),
    )
    with pytest.raises(AccountExistsError):
        add_account(
            db_session,
            scope,
            platform="fake",
            handle="@alpha",
            credentials={"token": "two"},
            cipher=cipher(initialized),
        )


def test_unknown_platform_is_rejected(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    with pytest.raises(UnknownPlatformError, match="unknown platform"):
        add_account(
            db_session,
            scope,
            platform="myspace",
            handle="@alpha",
            credentials={"token": "x"},
            cipher=cipher(initialized),
        )


def test_credentials_and_handle_must_not_be_empty(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    with pytest.raises(AccountError, match="handle"):
        add_account(
            db_session,
            scope,
            platform="fake",
            handle="  ",
            credentials={"token": "x"},
            cipher=cipher(initialized),
        )
    with pytest.raises(AccountError, match="at least one credential"):
        add_account(
            db_session,
            scope,
            platform="fake",
            handle="@alpha",
            credentials={},
            cipher=cipher(initialized),
        )
    with pytest.raises(AccountError, match="must not be empty"):
        add_account(
            db_session,
            scope,
            platform="fake",
            handle="@alpha",
            credentials={"token": "  "},
            cipher=cipher(initialized),
        )


def test_account_test_updates_status_and_audits(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    account = add_account(
        db_session,
        scope,
        platform="fake",
        handle="@alpha",
        credentials={"token": "good"},
        cipher=cipher(initialized),
    )

    results = asyncio.run(
        check_accounts(
            db_session,
            scope,
            cipher=cipher(initialized),
            adapter=FakeAdapter(),
        )
    )

    assert len(results) == 1
    assert results[0].ok is True
    refreshed = SocialAccountRepository(db_session).get(scope, account.id)
    assert refreshed is not None
    assert refreshed.status == "active"
    assert refreshed.last_checked_at is not None
    actions = [entry.action for entry in AuditService(db_session).history(scope)]
    assert actions == ["account.tested", "account.added"]
    for entry in AuditService(db_session).history(scope):
        assert "token" not in entry.payload
        assert "good" not in str(entry.payload)


def test_account_test_reports_rejected_credentials(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    account = add_account(
        db_session,
        scope,
        platform="fake",
        handle="@alpha",
        credentials={"username": "no-token"},
        cipher=cipher(initialized),
    )
    results = asyncio.run(
        check_accounts(
            db_session,
            scope,
            cipher=cipher(initialized),
            adapter=FakeAdapter(),
        )
    )
    assert results[0].ok is False
    refreshed = SocialAccountRepository(db_session).get(scope, account.id)
    assert refreshed is not None
    assert refreshed.status == "error"


def test_wrong_key_fails_without_leaking_credentials(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    add_account(
        db_session,
        scope,
        platform="fake",
        handle="@alpha",
        credentials={"token": "super-secret"},
        cipher=cipher(initialized),
    )
    results = asyncio.run(
        check_accounts(
            db_session,
            scope,
            cipher=CredentialCipher(generate_key()),
            adapter=FakeAdapter(),
        )
    )
    assert results[0].ok is False
    assert "cannot be decrypted" in results[0].detail
    assert "super-secret" not in results[0].detail


def test_account_test_filters(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    add_account(
        db_session,
        scope,
        platform="fake",
        handle="@one",
        credentials={"token": "one"},
        cipher=cipher(initialized),
    )
    add_account(
        db_session,
        scope,
        platform="fake",
        handle="@two",
        credentials={"token": "two"},
        cipher=cipher(initialized),
    )
    results = asyncio.run(
        check_accounts(
            db_session,
            scope,
            cipher=cipher(initialized),
            adapter=FakeAdapter(),
            handle="@two",
        )
    )
    assert [result.handle for result in results] == ["@two"]


def test_account_test_without_matches_is_an_error(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))
    with pytest.raises(NoAccountsError):
        asyncio.run(
            check_accounts(
                db_session,
                scope,
                cipher=cipher(initialized),
                adapter=FakeAdapter(),
            )
        )


def test_threads_account_requires_an_access_token(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    scope = make_scope(db_session, make_definition("alpha"))

    with pytest.raises(AccountError, match="access_token"):
        add_account(
            db_session,
            scope,
            platform="threads",
            handle="@alpha",
            credentials={"user_id": "17841400000000001"},
            cipher=cipher(initialized),
        )

    account = add_account(
        db_session,
        scope,
        platform="threads",
        handle="@alpha",
        credentials={
            "access_token": "th-long-lived-token",
            "user_id": "17841400000000001",
        },
        cipher=cipher(initialized),
    )
    assert account.platform == "threads"


def test_add_accepts_a_platform_from_an_installed_plugin(
    db_session: Session,
    make_definition: MakeDefinition,
    initialized: InitResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heraldesk import plugins

    monkeypatch.setattr(plugins, "plugin_adapters", lambda: {"bluesky": object()})
    scope = make_scope(db_session, make_definition("alpha"))

    account = add_account(
        db_session,
        scope,
        platform="bluesky",
        handle="@alpha.example",
        credentials={"token": "app-password"},
        cipher=cipher(initialized),
    )

    assert account.platform == "bluesky"
