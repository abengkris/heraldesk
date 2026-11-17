"""CLI tests for ``heraldesk telegram``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import TelegramMessageRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session
from heraldesk.domain.profiles import resolve_scope
from heraldesk.telegram.credentials import resolve_bot_token

pytestmark = pytest.mark.core

runner = CliRunner()
CHAT_ID = 123456789
TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def invoke(*args: str):
    return runner.invoke(app, list(args))


def write_config(initialized: InitResult) -> None:
    (initialized.layout.home / "heraldesk.yaml").write_text(
        """\
version: 1
telegram:
  enabled: true
  allowed_chat_ids: [123456789]
""",
        encoding="utf-8",
    )


@contextmanager
def db(initialized: InitResult) -> Iterator[Session]:
    with database_session(initialized.layout) as session:
        yield session


def test_telegram_test_uses_fake_adapter_and_redacts(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    write_config(initialized)
    monkeypatch.setenv("HERALDESK_TELEGRAM_BOT_TOKEN", TOKEN)

    result = invoke("telegram", "test")

    assert result.exit_code == 0, result.output
    assert "fake adapter" in result.output
    assert "12***89" in result.output
    assert str(CHAT_ID) not in result.output
    assert TOKEN not in result.output
    assert "****7890" in result.output


def test_telegram_send_prints_a_redacted_preview(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    write_config(initialized)
    monkeypatch.setenv("HERALDESK_TELEGRAM_BOT_TOKEN", TOKEN)

    result = invoke("telegram", "send", "acme", f"manual {TOKEN}")

    assert result.exit_code == 0, result.output
    assert "manual" in result.output
    assert TOKEN not in result.output
    assert "12***89" in result.output


def test_telegram_inbox_list_shows_redacted_messages(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    write_config(initialized)
    monkeypatch.setenv("HERALDESK_TELEGRAM_BOT_TOKEN", TOKEN)

    with db(initialized) as session:
        scope: ProfileScope = resolve_scope(session, "acme")
        TelegramMessageRepository(session).add(
            scope,
            chat_id=CHAT_ID,
            sender_id=555666777,
            text=f"hello {TOKEN}",
            command="queue",
            content_item_id=None,
            response="ok",
            created_at=datetime(2026, 11, 17, 12, 0, tzinfo=UTC),
        )

    result = invoke("telegram", "inbox", "list", "acme")

    assert result.exit_code == 0, result.output
    assert "Telegram inbox" in result.output
    assert "12***89" in result.output
    assert "55***77" in result.output
    assert str(CHAT_ID) not in result.output
    assert TOKEN not in result.output
    assert "[REDACTED]" in result.output


def test_token_set_and_status_never_print_the_token(initialized: InitResult) -> None:
    write_config(initialized)

    stored = invoke("telegram", "token", "set", "--token", TOKEN)
    assert stored.exit_code == 0, stored.output
    assert TOKEN not in stored.output
    token_path: Path = initialized.layout.telegram_token_path
    assert token_path.is_file()
    assert TOKEN not in token_path.read_text(encoding="ascii")
    assert resolve_bot_token(initialized.layout) == TOKEN

    status = invoke("telegram", "token", "status")
    assert status.exit_code == 0, status.output
    assert "encrypted-file" in status.output
    assert "****7890" in status.output
    assert TOKEN not in status.output


def test_telegram_commands_fail_without_an_initialised_home(home: Path) -> None:
    result = invoke("telegram", "test")
    assert result.exit_code == 1
    assert "heraldesk init" in result.output
