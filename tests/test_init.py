"""Tests for ``heraldesk init``."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from heraldesk.core.bootstrap import BootstrapError, initialize
from heraldesk.db.engine import inspect_database
from heraldesk.paths import LayoutError

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def test_init_creates_the_data_folder(home: Path) -> None:
    result = initialize(home)
    assert result.layout.home == home.resolve()
    assert result.config_created is True
    assert result.key_created is True
    assert result.db_created is True
    assert result.revision == "0004"
    for directory in ("profiles", "agents", "logs", "backups"):
        assert (home / directory).is_dir()


@posix_only
def test_init_writes_a_private_valid_key(home: Path) -> None:
    initialize(home)
    key_path = home / "secret.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    Fernet(key_path.read_bytes())


def test_init_writes_a_documented_default_config(home: Path) -> None:
    initialize(home)
    body = (home / "heraldesk.yaml").read_text(encoding="utf-8")
    assert "version: 1" in body
    assert "host: 127.0.0.1" in body
    assert "max_daily_cost_usd" in body
    assert "api_key" not in body.lower()


def test_init_creates_a_wal_database_with_the_baseline(home: Path) -> None:
    initialize(home)
    status = inspect_database(home / "heraldesk.db")
    assert status.exists
    assert status.error is None
    assert status.journal_mode == "wal"
    assert status.revision == "0004"


def test_init_is_idempotent_and_keeps_user_edits(home: Path) -> None:
    initialize(home)
    config_path = home / "heraldesk.yaml"
    key_path = home / "secret.key"
    config_path.write_text("version: 1\nserver:\n  port: 8888\n", encoding="utf-8")
    original_key = key_path.read_bytes()

    result = initialize(home)

    assert result.config_created is False
    assert result.key_created is False
    assert result.db_created is False
    assert result.revision == "0004"
    assert "8888" in config_path.read_text(encoding="utf-8")
    assert key_path.read_bytes() == original_key


def test_init_repairs_key_permissions(home: Path) -> None:
    initialize(home)
    key_path = home / "secret.key"
    os.chmod(key_path, 0o644)
    initialize(home)
    if sys.platform != "win32":
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_init_rejects_a_file_as_home(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(LayoutError, match="not a directory"):
        initialize(blocker)


def test_init_rejects_invalid_existing_config(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "heraldesk.yaml").write_text("server:\n  port: not-a-number\n", encoding="utf-8")
    with pytest.raises(BootstrapError, match="invalid configuration"):
        initialize(home)
