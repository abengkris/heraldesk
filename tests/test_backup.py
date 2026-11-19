"""Tests for backup archives, restore safety and credential handling."""

from __future__ import annotations

import json
import sqlite3
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from heraldesk import paths
from heraldesk.core.backup import (
    NO_KEY_MESSAGE,
    BackupError,
    create_backup,
    restore_backup,
)
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.engine import inspect_database

pytestmark = pytest.mark.core

TOKEN = "sk-1234567890abcdefghijklmnop"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def add_agent_override(initialized: InitResult) -> None:
    (initialized.layout.agents / "writer.md").write_text(
        "Write plainly.\n",
        encoding="utf-8",
    )


def add_generated_and_secret_files(initialized: InitResult) -> None:
    home = initialized.layout.home
    home.joinpath(".env").write_text(f"OPENAI_API_KEY={TOKEN}\n", encoding="utf-8")
    (home / "logs").mkdir(exist_ok=True)
    (home / "logs" / "run.log").write_text("transient log\n", encoding="utf-8")
    (home / "reports").mkdir(exist_ok=True)
    (home / "reports" / "weekly-2026-W47.md").write_text("report\n", encoding="utf-8")
    (home / "backups" / "old.zip").write_text("old backup\n", encoding="utf-8")


def test_default_backup_contains_user_files_and_excludes_generated_data(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    add_agent_override(initialized)
    add_generated_and_secret_files(initialized)
    archive = tmp_path / "backup.zip"

    result = create_backup(initialized.layout, output=archive)

    assert result.path == archive
    assert result.includes_credentials is False
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        manifest = json.loads(bundle.read("manifest.json"))
    assert {"manifest.json", "heraldesk.db", "heraldesk.yaml"} <= names
    assert "profiles/acme/soul.md" in names
    assert "agents/writer.md" in names
    assert "secret.key" not in names
    assert ".env" not in names
    assert not any(name.startswith("logs/") for name in names)
    assert not any(name.startswith("reports/") for name in names)
    assert not any(name.startswith("backups/") for name in names)
    assert manifest["includes_credentials"] is False
    assert manifest["format_version"] == 1


@posix_only
def test_backup_archive_is_private(initialized: InitResult, profile_setup, tmp_path: Path) -> None:
    archive = tmp_path / "backup.zip"

    create_backup(initialized.layout, output=archive)

    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_credential_backup_includes_the_key_but_never_dot_env(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    add_generated_and_secret_files(initialized)
    archive = tmp_path / "backup.zip"

    result = create_backup(initialized.layout, output=archive, include_credentials=True)

    assert result.includes_credentials is True
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        contents = b"".join(bundle.read(name) for name in names if not name.endswith("/"))
    assert "secret.key" in names
    assert ".env" not in names
    assert TOKEN.encode() not in contents


def test_backup_requires_an_initialised_database(tmp_path: Path) -> None:
    layout = paths.layout_for(tmp_path / "empty")
    layout.home.mkdir()

    with pytest.raises(BackupError, match="heraldesk init"):
        create_backup(layout, output=tmp_path / "backup.zip")


def test_full_restore_restores_database_config_profiles_agents_and_key(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    add_agent_override(initialized)
    archive = tmp_path / "backup.zip"
    create_backup(initialized.layout, output=archive, include_credentials=True)
    target = paths.layout_for(tmp_path / "restored")

    result = restore_backup(target, archive)

    assert result.restored_db is True
    assert result.restored_credentials is True
    assert target.db_path.is_file()
    assert target.config_path.is_file()
    assert target.key_path.is_file()
    assert (target.profiles / "acme" / "soul.md").is_file()
    assert (target.agents / "writer.md").is_file()
    assert inspect_database(target.db_path).revision == initialized.revision
    with sqlite3.connect(target.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM profiles").fetchone()[0] >= 1
    if sys.platform != "win32":
        assert stat.S_IMODE(target.key_path.stat().st_mode) == 0o600


def test_restore_without_a_key_fails_with_the_documented_message(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.zip"
    create_backup(initialized.layout, output=archive)
    target = paths.layout_for(tmp_path / "restored")

    with pytest.raises(BackupError) as error:
        restore_backup(target, archive)

    assert str(error.value) == NO_KEY_MESSAGE


def test_without_credentials_restores_files_but_not_database_or_key(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    add_agent_override(initialized)
    archive = tmp_path / "backup.zip"
    create_backup(initialized.layout, output=archive)
    target = paths.layout_for(tmp_path / "restored")

    result = restore_backup(target, archive, without_credentials=True)

    assert result.restored_db is False
    assert result.restored_credentials is False
    assert not target.db_path.exists()
    assert not target.key_path.exists()
    assert target.config_path.is_file()
    assert (target.profiles / "acme" / "soul.md").is_file()
    assert (target.agents / "writer.md").is_file()


def test_restore_refuses_to_overwrite_without_force(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.zip"
    create_backup(initialized.layout, output=archive)
    target = paths.layout_for(tmp_path / "restored")
    target.home.mkdir()
    target.config_path.write_text("version: 1\n", encoding="utf-8")

    with pytest.raises(BackupError, match="already contains data"):
        restore_backup(target, archive, without_credentials=True)

    result = restore_backup(target, archive, without_credentials=True, force=True)

    assert result.restored_db is False
    assert target.config_path.is_file()


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"format_version": 1}))
        bundle.writestr("../evil.txt", "nope")

    target = paths.layout_for(tmp_path / "restored")
    with pytest.raises(BackupError, match="unsafe path"):
        restore_backup(target, archive, without_credentials=True)


def test_restore_rejects_a_corrupt_archive(tmp_path: Path) -> None:
    archive = tmp_path / "broken.zip"
    archive.write_text("not a zip", encoding="utf-8")

    target = paths.layout_for(tmp_path / "restored")
    with pytest.raises(BackupError, match="not a valid backup archive"):
        restore_backup(target, archive, without_credentials=True)


def test_credential_backup_includes_the_telegram_token(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    initialized.layout.telegram_token_path.write_text("encrypted-token", encoding="ascii")
    archive = tmp_path / "backup.zip"

    create_backup(initialized.layout, output=archive, include_credentials=True)

    with zipfile.ZipFile(archive) as bundle:
        assert "telegram.token" in bundle.namelist()

    target = paths.layout_for(tmp_path / "restored")
    result = restore_backup(target, archive)

    assert result.restored_credentials is True
    assert target.telegram_token_path.read_text(encoding="ascii") == "encrypted-token"
    if sys.platform != "win32":
        assert stat.S_IMODE(target.telegram_token_path.stat().st_mode) == 0o600


def test_default_backup_excludes_the_telegram_token(
    initialized: InitResult, profile_setup, tmp_path: Path
) -> None:
    initialized.layout.telegram_token_path.write_text("encrypted-token", encoding="ascii")
    archive = tmp_path / "backup.zip"

    create_backup(initialized.layout, output=archive)

    with zipfile.ZipFile(archive) as bundle:
        assert "telegram.token" not in bundle.namelist()


def test_force_restore_creates_a_safety_backup(
    initialized: InitResult,
    profile_setup,
    tmp_path: Path,
) -> None:
    archive = tmp_path / "backup.zip"
    create_backup(initialized.layout, output=archive, include_credentials=True)

    result = restore_backup(initialized.layout, archive, force=True)

    assert result.safety_backup is not None
    assert result.safety_backup.is_file()
    assert result.safety_backup.parent == initialized.layout.backups
