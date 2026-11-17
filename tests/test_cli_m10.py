"""M10 CLI tests: Nostr account add/list/test and publish, with no secret leaks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from alembic import command
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.crypto import CredentialCipher
from heraldesk.db.migrations import alembic_config
from heraldesk.db.models import SocialAccount
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.db.session import database_session
from heraldesk.domain.profiles import ProfileStatus, resolve_scope, set_profile_status
from heraldesk.platforms.nostr import NostrAdapter, RelayAck, npub_from_secret
from heraldesk.platforms.nostr_keys import encode_nsec, mask_npub

pytestmark = pytest.mark.core

runner = CliRunner()

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SECRET = bytes.fromhex("00" * 31 + "03")
NSEC = encode_nsec(SECRET)
NPUB = npub_from_secret(SECRET)
ENV_NAME = "NOSTR_NSEC_THROWAY"


class FakeRelayClient:
    def __init__(self, default: object = True) -> None:
        self.calls: list[str] = []
        self._default = default

    async def publish(
        self,
        relay: str,
        event_json: str,
        event_id: str,
        *,
        timeout: float,
    ) -> RelayAck:
        _ = event_json, event_id, timeout
        self.calls.append(relay)
        if isinstance(self._default, BaseException):
            raise self._default
        return RelayAck(relay=relay, accepted=bool(self._default), message="")


@contextmanager
def session_for(initialized: InitResult) -> Iterator[Session]:
    with database_session(initialized.layout) as session:
        yield session


def invoke(*args: str):
    return runner.invoke(app, list(args))


def add_throwaway_account(initialized: InitResult, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv(ENV_NAME, NSEC)
    result = invoke(
        "account",
        "add",
        "acme",
        "--platform",
        "nostr",
        "--credential-from-env",
        ENV_NAME,
        "--label",
        "throwaway",
        "--relay",
        "wss://relay.example",
    )
    assert result.exit_code == 0, result.output
    with session_for(initialized) as session:
        scope = resolve_scope(session, "acme")
        account = session.query(SocialAccount).one()
        assert account.profile_id == scope.profile_id
        return account.id


def test_account_add_and_list_mask_the_npub(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme", "--name", "Acme").exit_code == 0
    account_id = add_throwaway_account(initialized, monkeypatch)

    with session_for(initialized) as session:
        account = session.get(SocialAccount, account_id)
        assert account is not None
        assert account.handle == NPUB
        assert account.label == "throwaway"
        scope = resolve_scope(session, "acme")
        cipher = CredentialCipher.from_key_file(initialized.layout.key_path)
        credentials = cipher.decrypt_json(account.credentials_encrypted)
        assert credentials["nsec"] == NSEC
        assert credentials["relays"] == "wss://relay.example"

        audit = json.dumps(
            [entry.payload for entry in AuditService(session).history(scope)],
            ensure_ascii=False,
        )
        assert NSEC not in audit
        assert NPUB not in audit
        assert mask_npub(NPUB) in audit

    listed = invoke("account", "list", "acme")
    assert listed.exit_code == 0, listed.output
    assert mask_npub(NPUB) in listed.output
    assert NSEC not in listed.output
    assert NPUB not in listed.output
    assert "throwaway" in listed.output
    assert "relay.example" in listed.output


def test_missing_credential_env_names_the_variable_only(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme", "--name", "Acme").exit_code == 0
    monkeypatch.delenv(ENV_NAME, raising=False)

    result = invoke(
        "account",
        "add",
        "acme",
        "--platform",
        "nostr",
        "--credential-from-env",
        ENV_NAME,
    )

    assert result.exit_code == 1
    assert ENV_NAME in result.output
    assert "missing or empty" in result.output
    assert NSEC not in result.output


def test_account_test_validates_nostr_offline(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme", "--name", "Acme").exit_code == 0
    add_throwaway_account(initialized, monkeypatch)

    result = invoke("account", "test", "acme", "--platform", "nostr")

    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    assert mask_npub(NPUB) in result.output
    assert NSEC not in result.output
    assert NPUB not in result.output


def test_publish_cli_uses_the_nostr_adapter(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme", "--name", "Acme").exit_code == 0
    add_throwaway_account(initialized, monkeypatch)
    item_id = _make_approved_item(initialized)
    client = FakeRelayClient()
    monkeypatch.setattr(
        "heraldesk.cli.publish.publish_adapter_for",
        lambda platform: NostrAdapter(
            relays=("wss://relay.example",),
            clock=lambda: NOW,
            relay_client=client,
        ),
    )

    result = invoke("publish", str(item_id))

    assert result.exit_code == 0, result.output
    assert "Published" in result.output
    assert NSEC not in result.output
    assert NPUB not in result.output
    assert client.calls == ["wss://relay.example"]


def test_stale_schema_fails_with_a_clear_migration_hint(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme", "--name", "Acme").exit_code == 0
    command.downgrade(alembic_config(initialized.layout.db_path), "0009")
    monkeypatch.setenv(ENV_NAME, NSEC)

    result = invoke(
        "account",
        "add",
        "acme",
        "--platform",
        "nostr",
        "--credential-from-env",
        ENV_NAME,
    )

    assert result.exit_code == 1
    assert "schema is out of date" in result.output
    assert "heraldesk init" in result.output
    assert "Traceback" not in result.output
    assert NSEC not in result.output


def test_publish_cli_failure_never_leaks_the_nsec(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme", "--name", "Acme").exit_code == 0
    add_throwaway_account(initialized, monkeypatch)
    item_id = _make_approved_item(initialized)
    monkeypatch.setattr(
        "heraldesk.cli.publish.publish_adapter_for",
        lambda platform: NostrAdapter(
            relays=("wss://relay.example",),
            clock=lambda: NOW,
            relay_client=FakeRelayClient(default=RuntimeError(f"rejected {NSEC}")),
        ),
    )

    result = invoke("publish", str(item_id))

    assert result.exit_code == 1
    assert NSEC not in result.output
    assert NPUB not in result.output
    assert "[REDACTED]" in result.output


def _make_approved_item(initialized: InitResult) -> int:
    with session_for(initialized) as session:
        scope = resolve_scope(session, "acme")
        set_profile_status(session, scope, ProfileStatus.ACTIVE)
        item = ContentItemRepository(session).add(
            scope,
            platform="nostr",
            body="Hello from the M10 CLI test.",
            state="approved",
            risk_level="low",
        )
        return item.id
