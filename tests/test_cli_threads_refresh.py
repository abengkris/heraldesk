"""CLI tests for Threads token refresh."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.crypto import CredentialCipher
from heraldesk.db.models import AuditLog
from heraldesk.db.repositories import ProfileCatalog, SocialAccountRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session
from heraldesk.domain.accounts import add_account
from heraldesk.platforms.base import PublishError
from heraldesk.platforms.threads import ThreadsToken

pytestmark = pytest.mark.core

runner = CliRunner()
OLD_TOKEN = "th-old-token"
NEW_TOKEN = "th-new-token"


def invoke(*args: str):
    return runner.invoke(app, list(args))


def prepare_threads_account(initialized: InitResult) -> tuple[int, ProfileScope]:
    assert invoke("profile", "new", "acme").exit_code == 0
    with database_session(initialized.layout) as session:
        scope = ProfileScope(ProfileCatalog(session).resolve("acme") or 0)
        account = add_account(
            session,
            scope,
            platform="threads",
            handle="@acme",
            credentials={"access_token": OLD_TOKEN, "user_id": "17841400000000001"},
            cipher=CredentialCipher.from_key_file(initialized.layout.key_path),
        )
        return account.id, scope


class FakeThreadsAdapter:
    async def refresh_token(self, token: str) -> ThreadsToken:
        assert token == OLD_TOKEN
        return ThreadsToken(access_token=NEW_TOKEN, expires_in=5_184_000)


def test_refresh_updates_credentials_and_audits(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id, scope = prepare_threads_account(initialized)
    monkeypatch.setattr("heraldesk.cli.account.ThreadsAdapter", FakeThreadsAdapter)

    result = invoke("account", "refresh", "acme")

    assert result.exit_code == 0, result.output
    assert "Refreshed" in result.output
    assert NEW_TOKEN not in result.output
    with database_session(initialized.layout) as session:
        account = SocialAccountRepository(session).get(scope, account_id)
        assert account is not None
        credentials = CredentialCipher.from_key_file(initialized.layout.key_path).decrypt_json(
            account.credentials_encrypted
        )
        assert credentials["access_token"] == NEW_TOKEN
        assert "expires_at" in credentials
        actions = set(session.scalars(select(AuditLog.action)).all())
    assert "account.refreshed" in actions


def test_refresh_failure_keeps_the_old_token(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id, scope = prepare_threads_account(initialized)

    class FailingAdapter:
        async def refresh_token(self, token: str) -> ThreadsToken:
            _ = token
            raise PublishError("Threads API error 190: invalid token")

    monkeypatch.setattr("heraldesk.cli.account.ThreadsAdapter", FailingAdapter)

    result = invoke("account", "refresh", "acme")

    assert result.exit_code == 1
    assert "cannot refresh" in result.output
    with database_session(initialized.layout) as session:
        account = SocialAccountRepository(session).get(scope, account_id)
        assert account is not None
        credentials = CredentialCipher.from_key_file(initialized.layout.key_path).decrypt_json(
            account.credentials_encrypted
        )
    assert credentials["access_token"] == OLD_TOKEN
