"""Tests for the M0 command line surface."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from heraldesk import __version__
from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult

runner = CliRunner()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _plain_output(text: str) -> str:
    """Strip ANSI codes and collapse Rich's line wrapping into single spaces."""
    return " ".join(_ANSI_ESCAPE.sub("", text).split())


def test_command_surface_is_init_doctor_serve_profile_account_stop_all() -> None:
    commands = {command.name for command in app.registered_commands}
    groups = {group.name for group in app.registered_groups}
    assert commands == {
        "init",
        "doctor",
        "serve",
        "stop-all",
        "runs",
        "publish",
        "backup",
        "restore",
    }
    assert groups == {
        "profile",
        "account",
        "agent",
        "run",
        "queue",
        "scheduler",
        "telegram",
        "inbox",
        "report",
        "plugins",
        "admin",
    }


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "init",
        "doctor",
        "serve",
        "profile",
        "account",
        "stop-all",
        "runs",
        "agent",
        "run",
        "queue",
        "publish",
        "scheduler",
        "telegram",
        "inbox",
        "report",
        "backup",
        "restore",
    ):
        assert command in result.output


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_serve_help_lists_the_bind_options() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    output = _plain_output(result.output).lower()
    assert "host" in output
    assert "port" in output
    # Help strings prove the options exist even when Rich wraps the option names.
    assert "bind address" in output
    assert "bind port" in output
    assert "disable telegram polling" in output


def test_serve_refuses_a_non_loopback_bind(initialized: InitResult) -> None:
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code == 1
    assert "loopback" in result.output


def test_serve_fails_without_an_initialised_home(home: Path) -> None:
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "heraldesk init" in result.output


def test_init_command_creates_and_reports(initialized: InitResult) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "schema revision  0010" in result.output


def test_init_command_never_prints_the_key(home: Path) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    key = (home / "secret.key").read_text(encoding="utf-8")
    assert key not in result.output


def test_two_console_scripts_are_declared() -> None:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["heraldesk"] == "heraldesk.cli.main:app"
    assert scripts["hdesk"] == "heraldesk.cli.main:app"
