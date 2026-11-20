"""End-to-end CLI tests for M9 backup/restore and error hardening."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk import __version__, paths
from heraldesk.cli.main import app
from heraldesk.core.audit import AuditService
from heraldesk.core.backup import NO_KEY_MESSAGE, create_backup
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.engine import inspect_database
from heraldesk.db.migrations import alembic_config
from heraldesk.db.session import database_session

pytestmark = pytest.mark.core

runner = CliRunner()
TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def invoke(*args: str):
    return runner.invoke(app, list(args))


@contextmanager
def db(initialized: InitResult) -> Iterator[Session]:
    with database_session(initialized.layout) as session:
        yield session


def make_archive(initialized: InitResult, tmp_path: Path, *, credentials: bool) -> Path:
    assert invoke("profile", "new", "acme").exit_code == 0
    archive = tmp_path / ("credential.zip" if credentials else "plain.zip")
    create_backup(initialized.layout, output=archive, include_credentials=credentials)
    return archive


def test_backup_command_writes_archive_and_audits(
    initialized: InitResult,
    tmp_path: Path,
) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    archive = tmp_path / "manual-backup.zip"

    result = invoke("backup", "--output", str(archive), "--with-credentials")

    assert result.exit_code == 0, result.output
    assert archive.is_file()
    assert "Backup written" in result.output
    assert "secret.key included" in result.output
    with db(initialized) as session:
        actions = [entry.action for entry in AuditService(session).history(None)]
    assert "backup.created" in actions


def test_restore_command_restores_a_full_backup(
    initialized: InitResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_archive(initialized, tmp_path, credentials=True)
    restored_home = tmp_path / "restored-home"
    monkeypatch.setenv("HERALDESK_HOME", str(restored_home))

    result = invoke("restore", str(archive))

    assert result.exit_code == 0, result.output
    assert "Database restored: yes" in result.output
    assert "Credentials restored: yes" in result.output
    layout = paths.layout_for(restored_home)
    assert layout.db_path.is_file()
    assert layout.key_path.is_file()
    assert layout.config_path.is_file()
    assert (layout.profiles / "acme" / "soul.md").is_file()
    with database_session(layout) as session:
        actions = [entry.action for entry in AuditService(session).history(None)]
    assert "backup.restored" in actions


def test_restore_without_key_exits_cleanly(
    initialized: InitResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_archive(initialized, tmp_path, credentials=False)
    monkeypatch.setenv("HERALDESK_HOME", str(tmp_path / "restored-home"))

    result = invoke("restore", str(archive))

    assert result.exit_code == 1
    assert NO_KEY_MESSAGE in " ".join(result.output.split())
    assert "Traceback" not in result.output


def test_restore_without_credentials_restores_files_only(
    initialized: InitResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = make_archive(initialized, tmp_path, credentials=False)
    restored_home = tmp_path / "restored-home"
    monkeypatch.setenv("HERALDESK_HOME", str(restored_home))

    result = invoke("restore", str(archive), "--without-credentials")

    assert result.exit_code == 0, result.output
    assert "Database restored: no" in result.output
    layout = paths.layout_for(restored_home)
    assert not layout.db_path.exists()
    assert not layout.key_path.exists()
    assert layout.config_path.is_file()
    assert (layout.profiles / "acme" / "soul.md").is_file()


def test_restore_missing_archive_exits_cleanly(
    initialized: InitResult,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERALDESK_HOME", str(tmp_path / "restored-home"))

    result = invoke("restore", str(tmp_path / "missing.zip"))

    assert result.exit_code == 1
    assert "does not exist" in " ".join(result.output.split())
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "args",
    [
        ("profile", "list"),
        ("profile", "new", "acme"),
        ("account", "add", "acme", "--platform", "fake", "--handle", "@x", "-c", "token=x"),
        ("account", "test", "acme"),
        ("agent", "run", "writer", "acme", "--idea", "x"),
        ("run", "daily", "acme"),
        ("queue", "list", "acme"),
        ("publish", "1"),
        ("scheduler", "run"),
        ("telegram", "test"),
        ("inbox", "list"),
        ("report", "weekly"),
        ("runs", "acme"),
        ("backup",),
        ("stop-all",),
        ("serve",),
    ],
)
def test_db_commands_fail_cleanly_without_an_initialised_home(
    home: Path,
    args: tuple[str, ...],
) -> None:
    result = invoke(*args)

    assert result.exit_code == 1, result.output
    assert "heraldesk init" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "args",
    [("backup",), ("inbox", "list"), ("report", "weekly"), ("scheduler", "run")],
)
def test_missing_database_after_init_exits_cleanly(
    initialized: InitResult,
    args: tuple[str, ...],
) -> None:
    initialized.layout.db_path.unlink()

    result = invoke(*args)

    assert result.exit_code == 1
    assert "heraldesk init" in result.output
    assert "Traceback" not in result.output


def test_invalid_config_errors_never_print_a_token(
    initialized: InitResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (initialized.layout.home / "heraldesk.yaml").write_text(
        "version: 1\nllm:\n  max_daily_cost_usd: not-a-number\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERALDESK_TELEGRAM_BOT_TOKEN", TOKEN)

    for args in (("doctor",), ("serve",), ("telegram", "test")):
        result = invoke(*args)
        assert result.exit_code == 1, result.output
        assert TOKEN not in result.output
        assert "Traceback" not in result.output


def test_version_matches_the_package() -> None:
    result = invoke("--version")

    assert result.exit_code == 0
    assert f"heraldesk {__version__}" in result.output


def test_restore_migrates_an_older_backup(initialized: InitResult, tmp_path: Path) -> None:
    archive = make_archive(initialized, tmp_path, credentials=True)
    command.downgrade(alembic_config(initialized.layout.db_path), "0009")
    create_backup(initialized.layout, output=archive, include_credentials=True)

    result = invoke("restore", str(archive), "--force")

    assert result.exit_code == 0, result.output
    assert "Schema revision: 0011" in result.output
    assert "Safety backup:" in result.output
    assert inspect_database(initialized.layout.db_path).revision == "0011"


def test_backup_prune_command(initialized: InitResult) -> None:
    for index in range(3):
        (initialized.layout.backups / f"heraldesk-backup-{index}.zip").write_text(
            "backup",
            encoding="utf-8",
        )

    result = invoke("backup-prune", "--keep", "1")

    assert result.exit_code == 0, result.output
    assert "Pruned" in result.output
    remaining = list(initialized.layout.backups.glob("heraldesk-backup-*.zip"))
    assert len(remaining) == 1
