"""Tests for ``heraldesk doctor``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult, initialize
from heraldesk.core.doctor import CheckStatus, has_failures, run_checks

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
runner = CliRunner()


def by_name(home: Path, *, online: bool = False) -> dict[str, CheckStatus]:
    return {result.name: result.status for result in run_checks(home, online=online)}


def test_healthy_installation_passes(initialized: InitResult) -> None:
    home = initialized.layout.home
    results = run_checks(home)
    assert not has_failures(results)
    statuses = {result.name: result.status for result in results}
    assert statuses["python"] is CheckStatus.PASS
    assert statuses["data folder"] is CheckStatus.PASS
    assert statuses["secret.key"] is CheckStatus.PASS
    assert statuses["database"] is CheckStatus.PASS
    assert statuses["heraldesk.yaml"] is CheckStatus.PASS


def test_missing_data_folder_fails(home: Path) -> None:
    assert by_name(home)["data folder"] is CheckStatus.FAIL


def test_missing_key_fails(initialized: InitResult) -> None:
    home = initialized.layout.home
    (home / "secret.key").unlink()
    assert by_name(home)["secret.key"] is CheckStatus.FAIL


@posix_only
def test_insecure_key_permissions_fail(initialized: InitResult) -> None:
    home = initialized.layout.home
    os.chmod(home / "secret.key", 0o644)
    assert by_name(home)["secret.key"] is CheckStatus.FAIL


def test_invalid_config_fails(initialized: InitResult) -> None:
    home = initialized.layout.home
    (home / "heraldesk.yaml").write_text("server:\n  port: 70000\n", encoding="utf-8")
    assert by_name(home)["heraldesk.yaml"] is CheckStatus.FAIL


def test_missing_folder_warns(initialized: InitResult) -> None:
    home = initialized.layout.home
    (home / "logs").rmdir()
    assert by_name(home)["folders"] is CheckStatus.WARN


def test_non_loopback_host_warns(initialized: InitResult) -> None:
    home = initialized.layout.home
    (home / "heraldesk.yaml").write_text("version: 1\nserver:\n  host: 0.0.0.0\n", encoding="utf-8")
    assert by_name(home)["server binding"] is CheckStatus.WARN


def test_online_checks_are_opt_in(initialized: InitResult) -> None:
    home = initialized.layout.home
    assert "online checks" not in by_name(home)
    assert by_name(home, online=True)["online checks"] is CheckStatus.WARN


def test_cli_doctor_exits_zero_when_healthy(initialized: InitResult) -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "All checks passed" in result.output


def test_cli_doctor_exits_one_on_failure(initialized: InitResult) -> None:
    home = initialized.layout.home
    (home / "secret.key").unlink()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "Some checks failed" in result.output


def test_cli_doctor_accepts_online_flag(initialized: InitResult) -> None:
    result = runner.invoke(app, ["doctor", "--online"])
    assert result.exit_code == 0
    assert "online checks" in result.output


def test_initialize_helper_is_used_by_fixture(initialized: InitResult) -> None:
    home = initialized.layout.home
    assert (home / "heraldesk.db").is_file()
    assert initialize(home).revision == "0009"
