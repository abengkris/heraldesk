"""End-to-end CLI tests for the M8 inbox and report commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import InboxRepository, TelegramMessageRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session
from heraldesk.domain.profiles import ProfileStatus, resolve_scope, set_profile_status
from heraldesk.telegram.fake import FakeTelegramAdapter

pytestmark = pytest.mark.core

runner = CliRunner()
CHAT_ID = 123456789
TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def invoke(*args: str):
    return runner.invoke(app, list(args))


@contextmanager
def db(initialized: InitResult) -> Iterator[Session]:
    with database_session(initialized.layout) as session:
        yield session


def ensure_profile(initialized: InitResult) -> None:
    profile_dir = initialized.layout.profiles / "acme"
    if not profile_dir.is_dir():
        assert invoke("profile", "new", "acme").exit_code == 0


def make_message(
    initialized: InitResult,
    text: str = "hello",
    *,
    created_at: datetime | None = None,
    handled: bool = False,
) -> int:
    """Insert one inbox message and return its id.

    ``created_at`` defaults to the current time, not a fixed date: a message that a test
    calls "new" must stay newer than any prune threshold as the wall clock moves forward.
    Pass ``created_at`` explicitly when a test needs a specific stamp.
    """
    ensure_profile(initialized)
    with db(initialized) as session:
        scope: ProfileScope = resolve_scope(session, "acme")
        set_profile_status(session, scope, ProfileStatus.ACTIVE)
        stamp = created_at if created_at is not None else datetime.now(UTC)
        message = TelegramMessageRepository(session).add(
            scope,
            chat_id=CHAT_ID,
            sender_id=555666777,
            text=text,
            command=None,
            content_item_id=None,
            response=None,
            created_at=stamp,
        )
        if handled:
            InboxRepository(session).mark_handled(
                message.id,
                response="reply",
                triaged_at=stamp,
            )
        return message.id


def test_inbox_list_defaults_to_pending(initialized: InitResult) -> None:
    pending = make_message(initialized, text="please answer")
    handled = make_message(initialized, text="already handled")
    with db(initialized) as session:
        InboxRepository(session).mark_handled(
            handled,
            response="reply",
            triaged_at=datetime(2026, 11, 17, tzinfo=UTC),
        )

    result = invoke("inbox", "list")

    assert result.exit_code == 0, result.output
    assert "Inbox: pending" in result.output
    assert "please answer" in result.output
    assert "already handled" not in result.output
    assert pending


def test_inbox_list_status_and_limit(initialized: InitResult) -> None:
    first = make_message(initialized, text="first")
    make_message(initialized, text="second")
    with db(initialized) as session:
        InboxRepository(session).mark_ignored(
            first,
            reason="spam",
            triaged_at=datetime(2026, 11, 17, tzinfo=UTC),
        )

    ignored = invoke("inbox", "list", "--status", "ignored")
    limited = invoke("inbox", "list", "--limit", "1")

    assert ignored.exit_code == 0, ignored.output
    assert "Inbox: ignored" in ignored.output
    assert "first" in ignored.output
    assert "second" not in ignored.output
    assert limited.exit_code == 0, limited.output
    assert "second" in limited.output
    assert "first" not in limited.output


def test_inbox_show_and_missing(initialized: InitResult) -> None:
    item_id = make_message(initialized, text="show this")

    result = invoke("inbox", "show", str(item_id))
    missing = invoke("inbox", "show", "99999")

    assert result.exit_code == 0, result.output
    assert f"Inbox item #{item_id}" in result.output
    assert "pending" in result.output
    assert "show this" in result.output
    assert missing.exit_code == 1
    assert "does not exist" in missing.output


def test_inbox_reply_uses_the_adapter_and_marks_handled(
    initialized: InitResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = make_message(initialized)
    adapter = FakeTelegramAdapter()
    monkeypatch.setattr(
        "heraldesk.community.resolve_reply_transport",
        lambda layout: (adapter, SecretRegistry.from_environment(extra_values=[TOKEN])),
    )

    result = invoke("inbox", "reply", str(item_id), "--text", f"thanks {TOKEN}")

    assert result.exit_code == 0, result.output
    assert "Replied" in result.output
    assert "handled" in result.output
    assert TOKEN not in result.output
    assert len(adapter.sent) == 1
    assert adapter.sent[0][0] == CHAT_ID
    assert TOKEN not in adapter.sent[0][1]
    with db(initialized) as session:
        message = InboxRepository(session).get(item_id)
        assert message is not None
        assert message.status == "handled"
        assert message.triaged_at is not None
        assert message.response is not None and TOKEN not in message.response
        entries = [
            entry
            for entry in AuditService(session).history(resolve_scope(session, "acme"))
            if entry.action == "inbox.replied"
        ]
        assert len(entries) == 1


def test_inbox_reply_rejects_empty_and_repeated_actions(
    initialized: InitResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item_id = make_message(initialized)
    adapter = FakeTelegramAdapter()
    monkeypatch.setattr(
        "heraldesk.community.resolve_reply_transport",
        lambda layout: (adapter, SecretRegistry.from_environment(extra_values=[TOKEN])),
    )

    empty = invoke("inbox", "reply", str(item_id), "--text", "   ")
    first = invoke("inbox", "reply", str(item_id), "--text", "ok")
    second = invoke("inbox", "reply", str(item_id), "--text", "again")

    assert empty.exit_code == 1
    assert "must not be empty" in empty.output
    assert first.exit_code == 0, first.output
    assert second.exit_code == 1
    assert "already handled" in second.output
    assert len(adapter.sent) == 1


def test_inbox_ignore_default_and_reason(initialized: InitResult) -> None:
    default_id = make_message(initialized)
    spam_id = make_message(initialized)

    default = invoke("inbox", "ignore", str(default_id))
    spam = invoke("inbox", "ignore", str(spam_id), "--reason", "spam")
    repeated = invoke("inbox", "ignore", str(spam_id))

    assert default.exit_code == 0, default.output
    assert "reason: other" in default.output
    assert spam.exit_code == 0, spam.output
    assert "reason: spam" in spam.output
    assert repeated.exit_code == 1
    assert "already ignored" in repeated.output


def test_report_weekly_and_monthly_write_files(initialized: InitResult) -> None:
    weekly = invoke("report", "weekly", "--week", "2026-W46")
    monthly = invoke("report", "monthly", "--month", "2026-11")

    assert weekly.exit_code == 0, weekly.output
    assert monthly.exit_code == 0, monthly.output
    weekly_path = initialized.layout.reports / "weekly-2026-W46.md"
    monthly_path = initialized.layout.reports / "monthly-2026-11.md"
    assert weekly_path.is_file()
    assert monthly_path.is_file()
    assert "# Weekly report: 2026-W46" in weekly_path.read_text(encoding="utf-8")
    assert "# Monthly report: 2026-11" in monthly_path.read_text(encoding="utf-8")
    with db(initialized) as session:
        actions = [entry.action for entry in AuditService(session).history(None)]
        assert actions.count("report.generated") == 2


def test_report_rejects_an_invalid_period(initialized: InitResult) -> None:
    result = invoke("report", "weekly", "--week", "2026-99")

    assert result.exit_code == 1
    assert "YYYY-Www" in result.output or "does not exist" in result.output


def test_commands_fail_without_an_initialised_home(home: Path) -> None:
    inbox = invoke("inbox", "list")
    report = invoke("report", "weekly")

    assert inbox.exit_code == 1
    assert "heraldesk init" in inbox.output
    assert report.exit_code == 1
    assert "heraldesk init" in report.output


def test_inbox_prune_removes_only_old_triaged_messages(initialized: InitResult) -> None:
    old = datetime.now(UTC) - timedelta(days=40)
    old_handled = make_message(initialized, "old handled", created_at=old, handled=True)
    old_pending = make_message(initialized, "old pending", created_at=old)
    new_handled = make_message(initialized, "new handled", handled=True)

    dry_run = invoke("inbox", "prune", "--days", "30", "--dry-run")
    assert dry_run.exit_code == 0, dry_run.output
    assert "Would prune" in dry_run.output
    assert "1" in dry_run.output

    pruned = invoke("inbox", "prune", "--days", "30")
    assert pruned.exit_code == 0, pruned.output
    assert "Pruned" in pruned.output
    with db(initialized) as session:
        repository = InboxRepository(session)
        assert repository.get(old_handled) is None
        assert repository.get(old_pending) is not None
        assert repository.get(new_handled) is not None
        actions = {entry.action for entry in AuditService(session).history(None, limit=100)}
    assert "inbox.pruned" in actions

    pending_too = invoke("inbox", "prune", "--days", "30", "--include-pending")
    assert pending_too.exit_code == 0, pending_too.output
    with db(initialized) as session:
        assert InboxRepository(session).get(old_pending) is None


def test_inbox_prune_keeps_a_default_dated_message(initialized: InitResult) -> None:
    """A message seeded without an explicit date must count as recent at any wall-clock time.

    Regression for the fixture default: it used to fall back to a fixed 2026-11-17 stamp, so a
    message the test treats as "new" silently aged past the 30-day threshold and the prune count
    changed. Seeding one explicit old row and one default row makes the expected count exact.
    """
    old = datetime.now(UTC) - timedelta(days=40)
    make_message(initialized, "old handled", created_at=old, handled=True)
    recent = make_message(initialized, "recent handled", handled=True)

    dry_run = invoke("inbox", "prune", "--days", "30", "--dry-run")
    assert dry_run.exit_code == 0, dry_run.output
    assert "Would prune: 1 inbox message(s)" in dry_run.output

    pruned = invoke("inbox", "prune", "--days", "30")
    assert pruned.exit_code == 0, pruned.output
    with db(initialized) as session:
        repository = InboxRepository(session)
        assert repository.get(recent) is not None
