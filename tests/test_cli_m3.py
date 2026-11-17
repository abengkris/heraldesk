"""CLI tests for ``heraldesk run daily`` and the M3 dry-run contract."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.engine import build_engine
from heraldesk.db.models import (
    AgentRun,
    Approval,
    AuditLog,
    ContentIdea,
    ContentItem,
    PipelineRun,
)
from heraldesk.db.session import session_factory

pytestmark = pytest.mark.core

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


def counts(initialized: InitResult) -> dict[str, int]:
    engine = build_engine(initialized.layout.db_path)
    factory = session_factory(engine)
    session = factory()
    try:
        return {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (
                PipelineRun,
                ContentIdea,
                ContentItem,
                Approval,
                AgentRun,
                AuditLog,
            )
        }
    finally:
        session.close()
        engine.dispose()


def test_dry_run_prints_a_simulation_and_writes_nothing(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    before = counts(initialized)

    result = invoke("run", "daily", "acme", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "dry run, nothing persisted" in result.output
    assert "pending_approval" in result.output
    assert "simulated" in result.output
    assert counts(initialized) == before


def test_persisted_run_creates_drafts_and_traces(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    result = invoke("run", "daily", "acme")
    assert result.exit_code == 0, result.output
    assert "Daily content: acme" in result.output
    assert "pending_approval" in result.output
    assert "run #" in result.output

    after = counts(initialized)
    assert after["pipeline_runs"] == 1
    assert after["content_ideas"] == 1
    assert after["content_items"] == 1
    assert after["approvals"] == 1
    assert after["agent_runs"] == 4


def test_paused_profile_is_refused(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    assert invoke("profile", "pause", "acme").exit_code == 0
    before = counts(initialized)

    result = invoke("run", "daily", "acme")
    assert result.exit_code == 1
    assert "paused" in result.output
    assert counts(initialized) == before


def test_unknown_profile_is_a_clean_error(initialized: InitResult) -> None:
    result = invoke("run", "daily", "missing")
    assert result.exit_code == 1
    assert "not loaded" in result.output


def test_run_fails_cleanly_without_an_initialised_home(home: Path) -> None:
    result = invoke("run", "daily", "acme")
    assert result.exit_code == 1
    assert "heraldesk init" in result.output
