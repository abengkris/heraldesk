"""CLI tests for ``heraldesk publish`` and ``heraldesk scheduler run``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import ContentItemRepository, ProfileCatalog
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session
from heraldesk.domain.content import ContentState, transition

pytestmark = pytest.mark.core

runner = CliRunner()
SECRET = "sk-ABCDEFGHIJKLMNOP1234"


def invoke(*args: str):
    return runner.invoke(app, list(args))


@contextmanager
def db(initialized: InitResult) -> Iterator[Session]:
    with database_session(initialized.layout) as session:
        yield session


def scope_for(session: Session, slug: str = "acme") -> ProfileScope:
    profile_id = ProfileCatalog(session).resolve(slug)
    assert profile_id is not None
    return ProfileScope(profile_id)


def add_item(
    initialized: InitResult,
    *,
    state: str = "approved",
    when: datetime | None = None,
    body: str = "A post about dashboards.",
) -> int:
    due_at = when if when is not None else datetime.now(UTC) - timedelta(minutes=1)
    with db(initialized) as session:
        scope = scope_for(session)
        repository = ContentItemRepository(session)
        item = repository.add(
            scope,
            platform="fake",
            body=body,
            state="approved" if state == "scheduled" else state,
            risk_level="low",
        )
        if state == "scheduled":
            transition(
                session,
                scope,
                item.id,
                ContentState.SCHEDULED,
                reason="test schedule",
                schedule_at=due_at,
            )
        return item.id


def active_profile() -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    resumed = invoke("profile", "resume", "acme")
    assert resumed.exit_code == 0, resumed.output


def test_scheduler_dry_run_lists_and_writes_nothing(initialized: InitResult) -> None:
    active_profile()
    item_id = add_item(initialized, state="scheduled")

    result = invoke("scheduler", "run", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Scheduler dry run" in result.output
    assert "nothing was written" in result.output
    assert "Would publish 1 item(s)" in result.output
    assert str(item_id) in result.output

    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).get(scope, item_id)
        assert item is not None
        assert item.state == "scheduled"
        assert item.publish_attempts == 0
        actions = [entry.action for entry in AuditService(session).history(None, limit=100)]
        assert "scheduler.run" not in actions


def test_scheduler_run_publishes_a_due_item(initialized: InitResult) -> None:
    active_profile()
    item_id = add_item(initialized, state="scheduled")

    result = invoke("scheduler", "run")

    assert result.exit_code == 0, result.output
    assert "Scheduler run" in result.output
    assert "Published 1" in result.output

    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).get(scope, item_id)
        assert item is not None
        assert item.state == "published"
        assert item.external_id == "fake-1"
        actions = [entry.action for entry in AuditService(session).history(None, limit=100)]
        assert "scheduler.run" in actions


def test_run_scheduler_alias_works(initialized: InitResult) -> None:
    active_profile()
    add_item(initialized, state="scheduled")

    result = invoke("run", "scheduler", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "Scheduler dry run" in result.output


def test_scheduler_skips_a_paused_profile(initialized: InitResult) -> None:
    active_profile()
    add_item(initialized, state="scheduled")
    assert invoke("profile", "pause", "acme").exit_code == 0

    result = invoke("scheduler", "run")

    assert result.exit_code == 0, result.output
    assert "skipped 1" in result.output
    with db(initialized) as session:
        item = ContentItemRepository(session).list(scope_for(session))[0]
        assert item.state == "scheduled"


def test_publish_command_publishes_one_item(initialized: InitResult) -> None:
    active_profile()
    item_id = add_item(initialized, state="approved")

    result = invoke("publish", str(item_id))

    assert result.exit_code == 0, result.output
    assert "Published" in result.output
    assert "External id: fake-1" in result.output

    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).get(scope, item_id)
        assert item is not None
        assert item.state == "published"
        assert item.external_id == "fake-1"


def test_publish_from_approved_reports_the_real_transition(initialized: InitResult) -> None:
    active_profile()
    with db(initialized) as session:
        scope = scope_for(session)
        repository = ContentItemRepository(session)
        item = repository.add(
            scope,
            platform="fake",
            body="A manually approved draft-only post.",
            state="approved",
            risk_level="low",
        )
        repository.set_publish_policy(
            scope,
            item.id,
            mode="draft_only",
            delay_minutes=None,
        )
        item_id = item.id

    result = invoke("publish", str(item_id))

    assert result.exit_code == 0, result.output
    assert "approved -> published" in result.output
    with db(initialized) as session:
        stored = ContentItemRepository(session).get(scope_for(session), item_id)
        assert stored is not None
        assert stored.state == "published"


def test_publish_from_scheduled_reports_the_real_transition(initialized: InitResult) -> None:
    active_profile()
    item_id = add_item(initialized, state="scheduled")

    result = invoke("publish", str(item_id))

    assert result.exit_code == 0, result.output
    assert "scheduled -> published" in result.output


def test_publish_from_failed_requeue_reports_the_real_transition(
    initialized: InitResult,
) -> None:
    active_profile()
    with db(initialized) as session:
        scope = scope_for(session)
        repository = ContentItemRepository(session)
        item = repository.add(
            scope,
            platform="fake",
            body="A post that previously failed.",
            state="approved",
            risk_level="low",
        )
        repository.set_publish_policy(
            scope,
            item.id,
            mode="require_approval",
            delay_minutes=None,
        )
        transition(
            session,
            scope,
            item.id,
            ContentState.SCHEDULED,
            reason="test schedule",
            schedule_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        transition(
            session,
            scope,
            item.id,
            ContentState.FAILED,
            reason="provider unavailable",
        )
        repository.record_publish_attempt(
            scope,
            item.id,
            attempts=3,
            error="provider unavailable",
        )
        repository.set_scheduled_at(scope, item.id, scheduled_at=None)
        item_id = item.id

    result = invoke("publish", str(item_id))

    assert result.exit_code == 0, result.output
    assert "failed -> published" in result.output
    with db(initialized) as session:
        stored = ContentItemRepository(session).get(scope_for(session), item_id)
        assert stored is not None
        assert stored.state == "published"
        assert stored.publish_attempts == 1


def test_publish_redacts_the_preview(initialized: InitResult) -> None:
    active_profile()
    item_id = add_item(initialized, state="approved", body=f"Leaked {SECRET} here.")

    result = invoke("publish", str(item_id))

    assert result.exit_code == 0, result.output
    assert SECRET not in result.output
    assert "[REDACTED]" in result.output


def test_publish_unknown_item_fails_cleanly(initialized: InitResult) -> None:
    active_profile()

    result = invoke("publish", "9999")

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_publish_refuses_an_illegal_state(initialized: InitResult) -> None:
    active_profile()
    item_id = add_item(initialized, state="drafted")

    result = invoke("publish", str(item_id))

    assert result.exit_code == 1
    assert "only approved, scheduled or failed" in result.output


def test_queue_list_shows_a_redacted_publish_error(initialized: InitResult) -> None:
    active_profile()
    with db(initialized) as session:
        scope = scope_for(session)
        repository = ContentItemRepository(session)
        item = repository.add(
            scope,
            platform="fake",
            body="A broken post.",
            state="failed",
            risk_level="low",
        )
        repository.record_publish_attempt(
            scope,
            item.id,
            attempts=3,
            error=f"provider rejected {SECRET}",
        )

    listed = invoke("queue", "list", "acme")

    assert listed.exit_code == 0, listed.output
    assert "provider rejected" in listed.output
    assert "[REDACTED]" in listed.output
    assert SECRET not in listed.output


def test_scheduler_fails_without_an_initialised_home() -> None:
    result = runner.invoke(app, ["scheduler", "run"])
    assert result.exit_code == 1
    assert "heraldesk init" in result.output
