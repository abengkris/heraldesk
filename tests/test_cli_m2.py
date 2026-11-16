"""End-to-end CLI tests for the M2 agent runtime and run traces."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult

pytestmark = pytest.mark.core

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


def write_config(initialized: InitResult, body: str) -> None:
    initialized.layout.config_path.write_text(body, encoding="utf-8")


def test_agent_writer_and_runs_with_the_fake_provider(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0

    run = invoke("agent", "run", "writer", "acme", "--idea", "Announce the dashboard")
    assert run.exit_code == 0, run.output
    assert "run #" in run.output
    assert "fake/fake" in run.output
    assert "tokens" in run.output
    assert "cost $" in run.output
    assert "We shipped a smaller dashboard" in run.output

    traces = invoke("runs", "acme")
    assert traces.exit_code == 0, traces.output
    assert "Agent runs: acme" in traces.output
    assert "writer" in traces.output
    assert "succeeded" in traces.output
    assert "Global spend today" in traces.output
    assert "Profile spend today" in traces.output


def test_runs_without_traces_is_not_an_error(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0

    traces = invoke("runs", "acme")
    assert traces.exit_code == 0
    assert "No agent runs recorded" in traces.output
    assert "Global spend today" in traces.output


def test_agent_run_fails_loudly_when_the_cost_cap_is_zero(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    write_config(
        initialized,
        """
version: 1
llm:
  provider: fake
  max_daily_cost_usd: 0
""",
    )

    result = invoke("agent", "run", "writer", "acme", "--idea", "Too expensive")
    assert result.exit_code == 1
    assert "cost cap" in result.output.lower()

    traces = invoke("runs", "acme")
    assert traces.exit_code == 0
    assert "failed" in traces.output


def test_unknown_agent_role_is_rejected(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    result = invoke("agent", "run", "strategist", "acme", "--idea", "Plan")
    assert result.exit_code == 1
    assert "not implemented" in result.output


def test_litellm_without_a_model_is_a_clear_config_error(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    write_config(
        initialized,
        """
version: 1
llm:
  provider: litellm
  default_model: ""
""",
    )
    result = invoke("agent", "run", "writer", "acme", "--idea", "Real provider")
    assert result.exit_code == 1
    assert "default_model" in result.output


def test_commands_fail_cleanly_without_an_initialised_home(home: Path) -> None:
    result = invoke("runs", "acme")
    assert result.exit_code == 1
    assert "heraldesk init" in result.output
