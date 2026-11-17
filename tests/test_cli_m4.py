"""CLI tests for ``heraldesk queue``."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import (
    ApprovalRepository,
    ContentItemRepository,
    ProfileCatalog,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session
from heraldesk.domain.approvals import request_approval

pytestmark = pytest.mark.core

runner = CliRunner()
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
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
    state: str = "pending_approval",
    body: str = "A draft about dashboards.",
    risk_level: str | None = "low",
    approval_now: datetime = NOW,
) -> int:
    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).add(
            scope,
            platform="fake",
            body=body,
            state=state,
            risk_level=risk_level,
        )
        if state == "pending_approval":
            request_approval(session, scope, item.id, now=approval_now)
        return item.id


def test_queue_list_and_approve(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    item_id = add_item(initialized)

    listed = invoke("queue", "list", "acme")
    assert listed.exit_code == 0, listed.output
    assert "Approval queue: acme" in listed.output
    assert "pending_approval" in listed.output
    assert "pending" in listed.output

    approved = invoke("queue", "approve", str(item_id))
    assert approved.exit_code == 0, approved.output
    assert "Approved" in approved.output
    assert "pending_approval -> approved" in approved.output

    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).get(scope, item_id)
        assert item is not None
        assert item.state == "approved"
        approval = ApprovalRepository(session).latest_for_item(scope, item_id)
        assert approval is not None
        assert approval.state == "approved"


def test_queue_reject_redacts_the_reason(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    item_id = add_item(initialized)

    rejected = invoke(
        "queue",
        "reject",
        str(item_id),
        "--reason",
        f"off-brand and leaks {SECRET}",
    )
    assert rejected.exit_code == 0, rejected.output
    assert "Rejected" in rejected.output
    assert "pending_approval -> rejected" in rejected.output
    assert SECRET not in rejected.output

    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).get(scope, item_id)
        assert item is not None
        assert item.state == "rejected"
        approval = ApprovalRepository(session).latest_for_item(scope, item_id)
        assert approval is not None
        assert approval.state == "rejected"
        assert SECRET not in (approval.reason or "")


def test_queue_edit_body_and_body_file(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    blocked_id = add_item(initialized, state="blocked")
    rejected_id = add_item(initialized, state="rejected")

    edited = invoke(
        "queue",
        "edit",
        str(blocked_id),
        "--body",
        "A fixed draft about dashboards.",
    )
    assert edited.exit_code == 0, edited.output
    assert "blocked -> drafted" in edited.output
    assert "Compliance" in edited.output

    body_file = initialized.layout.home / "body.txt"
    body_file.write_text("A body supplied from a file.", encoding="utf-8")
    from_file = invoke(
        "queue",
        "edit",
        str(rejected_id),
        "--body-file",
        str(body_file),
    )
    assert from_file.exit_code == 0, from_file.output
    assert "rejected -> drafted" in from_file.output

    with db(initialized) as session:
        scope = scope_for(session)
        first = ContentItemRepository(session).get(scope, blocked_id)
        second = ContentItemRepository(session).get(scope, rejected_id)
        assert first is not None and first.body == "A fixed draft about dashboards."
        assert second is not None and second.body == "A body supplied from a file."
        assert first.state == "drafted"
        assert second.state == "drafted"


def test_queue_edit_requires_exactly_one_body_source(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    item_id = add_item(initialized)
    body_file = initialized.layout.home / "body.txt"
    body_file.write_text("from file", encoding="utf-8")

    missing = invoke("queue", "edit", str(item_id))
    assert missing.exit_code == 1
    assert "--body" in missing.output

    both = invoke(
        "queue",
        "edit",
        str(item_id),
        "--body",
        "inline",
        "--body-file",
        str(body_file),
    )
    assert both.exit_code == 1
    assert "only one" in both.output


def test_queue_list_sweeps_expired_approvals(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    item_id = add_item(
        initialized,
        approval_now=NOW - timedelta(hours=73),
    )

    listed = invoke("queue", "list", "acme")
    assert listed.exit_code == 0, listed.output
    assert "Expired 1 pending approval" in listed.output

    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).get(scope, item_id)
        assert item is not None
        assert item.state == "cancelled"
        approval = ApprovalRepository(session).latest_for_item(scope, item_id)
        assert approval is not None
        assert approval.state == "expired"


def test_queue_approve_refuses_under_the_global_kill_switch(
    initialized: InitResult,
) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    item_id = add_item(initialized)
    assert invoke("stop-all").exit_code == 0

    refused = invoke("queue", "approve", str(item_id))
    assert refused.exit_code == 1
    assert "global_kill_switch" in refused.output

    with db(initialized) as session:
        scope = scope_for(session)
        item = ContentItemRepository(session).get(scope, item_id)
        assert item is not None
        assert item.state == "pending_approval"


def test_queue_list_redacts_secrets_in_bodies(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    add_item(initialized, body=f"Contact {SECRET} for the dashboard.")

    listed = invoke("queue", "list", "acme")
    assert listed.exit_code == 0, listed.output
    assert SECRET not in listed.output


def test_queue_commands_fail_cleanly(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0

    unknown = invoke("queue", "approve", "9999")
    assert unknown.exit_code == 1
    assert "does not exist" in unknown.output


def test_queue_fails_without_an_initialised_home() -> None:
    no_home = runner.invoke(app, ["queue", "list", "acme"])
    assert no_home.exit_code == 1
    assert "heraldesk init" in no_home.output


def test_daily_run_creates_a_queue_entry(initialized: InitResult) -> None:
    assert invoke("profile", "new", "acme").exit_code == 0
    assert invoke("run", "daily", "acme").exit_code == 0

    listed = invoke("queue", "list", "acme")
    assert listed.exit_code == 0, listed.output
    assert "pending_approval" in listed.output
    assert "pending" in listed.output

    with db(initialized) as session:
        scope = scope_for(session)
        approvals = ApprovalRepository(session).list(scope)
        assert len(approvals) == 1
        assert approvals[0].state == "pending"
