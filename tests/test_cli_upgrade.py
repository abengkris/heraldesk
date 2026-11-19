"""CLI tests for ``heraldesk upgrade``."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import select
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.migrations import alembic_config, head_revision
from heraldesk.db.models import AuditLog
from heraldesk.db.session import database_session

pytestmark = pytest.mark.core

runner = CliRunner()


def test_upgrade_is_a_noop_at_head(initialized: InitResult) -> None:
    home = initialized.layout.home
    before = set(home.joinpath("backups").glob("*"))

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "Already up to date" in result.output
    assert set(home.joinpath("backups").glob("*")) == before


def test_upgrade_backs_up_then_migrates(initialized: InitResult) -> None:
    layout = initialized.layout
    command.downgrade(alembic_config(layout.db_path), "0009")

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "Backup written" in result.output
    assert "0009 -> 0010" in result.output
    assert head_revision(layout.db_path) == "0010"
    backups = list(layout.backups.glob("*.zip"))
    assert len(backups) == 1

    with database_session(layout) as session:
        actions = set(session.scalars(select(AuditLog.action)).all())
    assert "system.upgraded" in actions
