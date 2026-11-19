"""Automatic inbox retention tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from heraldesk.config import load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.models import AuditLog
from heraldesk.db.repositories import InboxRepository, TelegramMessageRepository
from heraldesk.server.background import BackgroundJobs
from heraldesk.server.database import ServerDatabase
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core


def build_jobs(initialized: InitResult, *, days: int) -> BackgroundJobs:
    initialized.layout.config_path.write_text(
        f"version: 1\ntelegram:\n  retention_days: {days}\n",
        encoding="utf-8",
    )
    config = load_config(initialized.layout.home)
    database = ServerDatabase(initialized.layout)
    return BackgroundJobs(database, initialized.layout, config)


def test_retention_prunes_only_old_triaged_rows(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    old = datetime.now(UTC) - timedelta(days=40)
    database = ServerDatabase(initialized.layout)
    try:
        with database.session() as session:
            repository = TelegramMessageRepository(session)
            old_handled = repository.add(
                profile_setup.scope,
                chat_id=1,
                sender_id=2,
                text="old handled",
                command=None,
                content_item_id=None,
                response=None,
                created_at=old,
            )
            InboxRepository(session).mark_handled(
                old_handled.id,
                response="ok",
                triaged_at=old,
            )
            old_pending = repository.add(
                profile_setup.scope,
                chat_id=1,
                sender_id=2,
                text="old pending",
                command=None,
                content_item_id=None,
                response=None,
                created_at=old,
            )
    finally:
        database.close()

    jobs = build_jobs(initialized, days=30)
    try:
        deleted = jobs.run_retention_once()
    finally:
        jobs._database.close()
    assert deleted == 1

    database = ServerDatabase(initialized.layout)
    try:
        with database.session() as session:
            repository = InboxRepository(session)
            assert repository.get(old_handled.id) is None
            assert repository.get(old_pending.id) is not None
            actions = set(session.scalars(select(AuditLog.action)).all())
    finally:
        database.close()
    assert "inbox.pruned" in actions


def test_retention_is_disabled_by_default(initialized: InitResult) -> None:
    jobs = build_jobs(initialized, days=0)
    try:
        assert jobs.run_retention_once() == 0
    finally:
        jobs._database.close()
