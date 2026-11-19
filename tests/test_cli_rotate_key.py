"""CLI tests for ``heraldesk admin rotate-key``."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.crypto import CredentialCipher, load_key
from heraldesk.db.models import AuditLog
from heraldesk.db.repositories import ProfileCatalog, SocialAccountRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session
from heraldesk.domain.accounts import add_account
from heraldesk.domain.profiles import resolve_scope
from heraldesk.telegram.credentials import resolve_bot_token, store_bot_token

pytestmark = pytest.mark.core

runner = CliRunner()
BOT_TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def invoke(*args: str):
    return runner.invoke(app, list(args))


def prepare_account(initialized: InitResult) -> int:
    assert invoke("profile", "new", "acme").exit_code == 0
    with database_session(initialized.layout) as session:
        scope = resolve_scope(session, "acme")
        account = add_account(
            session,
            scope,
            platform="fake",
            handle="@acme",
            credentials={"token": "plaintext-token"},
            cipher=CredentialCipher.from_key_file(initialized.layout.key_path),
        )
        return account.id


def account_scope(initialized: InitResult) -> ProfileScope:
    with database_session(initialized.layout) as session:
        profile_id = ProfileCatalog(session).resolve("acme")
        assert profile_id is not None
        return ProfileScope(profile_id)


def test_rotate_key_reencrypts_accounts_and_telegram_token(initialized: InitResult) -> None:
    account_id = prepare_account(initialized)
    store_bot_token(initialized.layout, BOT_TOKEN)
    old_key = load_key(initialized.layout.key_path)

    result = invoke("admin", "rotate-key")

    assert result.exit_code == 0, result.output
    assert "Key rotated" in result.output
    assert "Backup:" in result.output
    new_key = load_key(initialized.layout.key_path)
    assert new_key != old_key
    assert list(initialized.layout.backups.glob("*.zip"))

    scope = account_scope(initialized)
    with database_session(initialized.layout) as session:
        account = SocialAccountRepository(session).get(scope, account_id)
        assert account is not None
        assert CredentialCipher.from_key_file(initialized.layout.key_path).decrypt_json(
            account.credentials_encrypted
        ) == {"token": "plaintext-token"}
        actions = {entry.action for entry in AuditService(session).history(None, limit=100)}
    assert "system.key_rotated" in actions
    assert resolve_bot_token(initialized.layout) == BOT_TOKEN


def test_rotate_key_dry_run_changes_nothing(initialized: InitResult) -> None:
    prepare_account(initialized)
    old_key = load_key(initialized.layout.key_path)

    result = invoke("admin", "rotate-key", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Would rotate" in result.output
    assert load_key(initialized.layout.key_path) == old_key
    assert list(initialized.layout.backups.glob("*.zip")) == []


def test_rotate_key_aborts_on_undecryptable_credentials(initialized: InitResult) -> None:
    account_id = prepare_account(initialized)
    scope = account_scope(initialized)
    with database_session(initialized.layout) as session:
        SocialAccountRepository(session).set_credentials(
            scope,
            account_id,
            credentials_encrypted="not-a-fernet-token",
        )
    old_key = load_key(initialized.layout.key_path)

    result = invoke("admin", "rotate-key")

    assert result.exit_code == 1
    assert "cannot decrypt" in result.output
    assert load_key(initialized.layout.key_path) == old_key


def test_rotate_key_works_without_accounts(initialized: InitResult) -> None:
    old_key = load_key(initialized.layout.key_path)

    result = invoke("admin", "rotate-key")

    assert result.exit_code == 0, result.output
    assert load_key(initialized.layout.key_path) != old_key
    with database_session(initialized.layout) as session:
        actions = set(session.scalars(select(AuditLog.action)).all())
    assert "system.key_rotated" in actions


def test_rotation_rolls_back_when_the_token_swap_fails(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from pathlib import Path

    account_id = prepare_account(initialized)
    store_bot_token(initialized.layout, BOT_TOKEN)
    old_key = load_key(initialized.layout.key_path)
    real_replace = os.replace

    def flaky_replace(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == initialized.layout.telegram_token_path:
            raise OSError("locked for the test")
        real_replace(source, destination)

    monkeypatch.setattr("heraldesk.cli.rotate_key.os.replace", flaky_replace)

    result = invoke("admin", "rotate-key")

    assert result.exit_code == 1
    assert "rolled back" in result.output
    assert load_key(initialized.layout.key_path) == old_key
    scope = account_scope(initialized)
    with database_session(initialized.layout) as session:
        account = SocialAccountRepository(session).get(scope, account_id)
        assert account is not None
        assert CredentialCipher(old_key).decrypt_json(account.credentials_encrypted) == {
            "token": "plaintext-token"
        }
    assert resolve_bot_token(initialized.layout) == BOT_TOKEN
