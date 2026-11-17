"""M9 hardening regression tests: off switches and tracked-file hygiene."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.config import load_config
from heraldesk.core.locks import engage_global_kill_switch
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.profiles import ProfileStatus, set_profile_status
from heraldesk.server.background import BackgroundJobs
from heraldesk.server.database import ServerDatabase
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

PROJECT_ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


def make_due_item(session: Session, setup: ProfileSetup) -> int:
    set_profile_status(session, setup.scope, ProfileStatus.ACTIVE)
    repository = ContentItemRepository(session)
    item = repository.add(
        setup.scope,
        platform="fake",
        body="A due item that must not publish while halted.",
        state=ContentState.APPROVED.value,
        risk_level="low",
    )
    repository.set_publish_policy(
        setup.scope,
        item.id,
        mode="require_approval",
        delay_minutes=None,
    )
    transition(
        session,
        setup.scope,
        item.id,
        ContentState.SCHEDULED,
        reason="M9 kill-switch regression",
        schedule_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    session.commit()
    return item.id


def read_state(session: Session, setup: ProfileSetup, item_id: int) -> str:
    item = ContentItemRepository(session).get(setup.scope, item_id)
    assert item is not None
    return item.state


def test_global_kill_switch_blocks_manual_and_scheduled_publish(
    initialized,
    profile_setup: ProfileSetup,
    db_session: Session,
) -> None:
    item_id = make_due_item(db_session, profile_setup)
    engage_global_kill_switch(initialized.layout)

    manual = runner.invoke(app, ["publish", str(item_id)])
    sweep = runner.invoke(app, ["scheduler", "run"])

    assert manual.exit_code == 1
    assert "global_kill_switch" in manual.output
    assert sweep.exit_code == 0, sweep.output
    assert "skipped 1" in sweep.output
    assert read_state(db_session, profile_setup, item_id) == "scheduled"


def test_serve_background_sweep_respects_the_global_kill_switch(
    initialized,
    profile_setup: ProfileSetup,
    db_session: Session,
) -> None:
    item_id = make_due_item(db_session, profile_setup)
    engage_global_kill_switch(initialized.layout)
    config = load_config(initialized.layout.home)
    database = ServerDatabase(initialized.layout)
    jobs = BackgroundJobs(database, initialized.layout, config)
    try:
        result = jobs.run_scheduler_once()
    finally:
        database.close()

    assert result.published == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "global_kill_switch"
    assert read_state(db_session, profile_setup, item_id) == "scheduled"


def test_tracked_files_do_not_include_private_runtime_artifacts() -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    completed = subprocess.run(
        [git, "-C", str(PROJECT_ROOT), "ls-files"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("git metadata is not available")

    tracked = [Path(line) for line in completed.stdout.splitlines() if line.strip()]
    forbidden = [
        path
        for path in tracked
        if path.name in {".env", "secret.key"}
        or path.suffix == ".db"
        or path.name.endswith((".db-wal", ".db-shm"))
        or path.parts[0] in {"reports", "backups"}
    ]
    assert forbidden == []
