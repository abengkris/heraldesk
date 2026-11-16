"""Tests for the M0 command line surface."""

from __future__ import annotations

import tomllib
from pathlib import Path

from typer.testing import CliRunner

from heraldesk import __version__
from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult

runner = CliRunner()
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_command_surface_is_init_doctor_serve_profile_account_stop_all() -> None:
    commands = {command.name for command in app.registered_commands}
    groups = {group.name for group in app.registered_groups}
    assert commands == {"init", "doctor", "serve", "stop-all", "runs"}
    assert groups == {"profile", "account", "agent"}


def test_help_lists_the_m1_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "doctor", "serve", "profile", "account", "stop-all", "runs", "agent"):
        assert command in result.output


def test_version_option() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_serve_is_a_stub_that_exits_zero() -> None:
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert "not implemented yet" in result.output


def test_serve_accepts_documented_flags() -> None:
    result = runner.invoke(
        app,
        ["serve", "--host", "127.0.0.1", "--port", "9000", "--no-web", "--no-telegram"],
    )
    assert result.exit_code == 0


def test_init_command_creates_and_reports(initialized: InitResult) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "schema revision  0003" in result.output


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
