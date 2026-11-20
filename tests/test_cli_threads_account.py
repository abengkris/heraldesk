"""CLI tests for Threads account credential environment mapping."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.crypto import CredentialCipher
from heraldesk.db.repositories import ProfileCatalog, SocialAccountRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session

pytestmark = pytest.mark.core

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


def test_threads_env_names_map_to_account_credentials(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "abeng").exit_code == 0
    monkeypatch.setenv("THREADS_TOKEN", "th-long-lived-token")
    monkeypatch.setenv("THREADS_USER_ID", "17841400000000001")

    result = invoke(
        "account",
        "add",
        "abeng",
        "--platform",
        "threads",
        "--handle",
        "@abeng",
        "--credential-from-env",
        "THREADS_TOKEN",
        "--credential-from-env",
        "THREADS_USER_ID",
    )

    assert result.exit_code == 0, result.output
    with database_session(initialized.layout) as session:
        profile_id = ProfileCatalog(session).resolve("abeng")
        assert profile_id is not None
        accounts = SocialAccountRepository(session).list(ProfileScope(profile_id))
        assert len(accounts) == 1
        credentials = CredentialCipher.from_key_file(initialized.layout.key_path).decrypt_json(
            accounts[0].credentials_encrypted
        )
    assert credentials == {
        "access_token": "th-long-lived-token",
        "user_id": "17841400000000001",
    }
