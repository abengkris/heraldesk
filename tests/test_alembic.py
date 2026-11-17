"""Tests for the Alembic baseline."""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect

from heraldesk.core.bootstrap import InitResult
from heraldesk.db.engine import build_engine, inspect_database
from heraldesk.db.migrations import alembic_config, scripts_directory


def test_m1_schema_creates_the_core_domain_tables(initialized: InitResult) -> None:
    engine = build_engine(initialized.layout.db_path)
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
    finally:
        engine.dispose()
    assert tables == {
        "alembic_version",
        "profiles",
        "social_accounts",
        "audit_log",
        "jobs",
        "agent_runs",
        "pipeline_runs",
        "content_ideas",
        "content_items",
        "approvals",
        "telegram_messages",
        "telegram_notifications",
    }


def test_m1_profile_owned_tables_have_profile_id(initialized: InitResult) -> None:
    engine = build_engine(initialized.layout.db_path)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            for table in (
                "social_accounts",
                "audit_log",
                "jobs",
                "agent_runs",
                "pipeline_runs",
                "content_ideas",
                "content_items",
                "approvals",
                "telegram_messages",
                "telegram_notifications",
            ):
                columns = {column["name"] for column in inspector.get_columns(table)}
                assert "profile_id" in columns
            audit_profile_id = next(
                column
                for column in inspector.get_columns("audit_log")
                if column["name"] == "profile_id"
            )
            jobs_profile_id = next(
                column for column in inspector.get_columns("jobs") if column["name"] == "profile_id"
            )
            runs_profile_id = next(
                column
                for column in inspector.get_columns("agent_runs")
                if column["name"] == "profile_id"
            )
    finally:
        engine.dispose()
    assert audit_profile_id["nullable"] is True
    assert jobs_profile_id["nullable"] is True
    assert runs_profile_id["nullable"] is False


def test_m5_publisher_columns_exist(initialized: InitResult) -> None:
    engine = build_engine(initialized.layout.db_path)
    try:
        with engine.connect() as connection:
            columns = {
                column["name"]: column
                for column in inspect(connection).get_columns("content_items")
            }
            indexes = {index["name"] for index in inspect(connection).get_indexes("content_items")}
    finally:
        engine.dispose()
    for name in (
        "publish_mode",
        "publish_delay_minutes",
        "publish_attempts",
        "last_error",
    ):
        assert name in columns
    assert columns["publish_attempts"]["nullable"] is False
    assert "ix_content_items_due" in indexes


def test_m6_telegram_tables_and_columns_exist(initialized: InitResult) -> None:
    engine = build_engine(initialized.layout.db_path)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            message_columns = {
                column["name"] for column in inspector.get_columns("telegram_messages")
            }
            notification_columns = {
                column["name"] for column in inspector.get_columns("telegram_notifications")
            }
            notification_indexes = {
                index["name"] for index in inspector.get_indexes("telegram_notifications")
            }
    finally:
        engine.dispose()
    assert {
        "profile_id",
        "chat_id",
        "sender_id",
        "text",
        "command",
        "content_item_id",
        "response",
        "created_at",
    } <= message_columns
    assert {
        "profile_id",
        "event",
        "idempotency_key",
        "status",
        "message_preview",
        "recipients",
        "error",
        "created_at",
        "sent_at",
    } <= notification_columns
    assert "ix_telegram_notifications_profile_id" in notification_indexes


def test_migration_scripts_are_present() -> None:
    directory = scripts_directory()
    assert (directory / "env.py").is_file()
    assert list((directory / "versions").glob("0001*.py"))
    assert list((directory / "versions").glob("0002*.py"))
    assert list((directory / "versions").glob("0003*.py"))
    assert list((directory / "versions").glob("0004*.py"))
    assert list((directory / "versions").glob("0005*.py"))
    assert list((directory / "versions").glob("0006*.py"))
    assert list((directory / "versions").glob("0007*.py"))


def test_downgrade_to_base_clears_the_revision(initialized: InitResult) -> None:
    db_path = initialized.layout.db_path
    command.downgrade(alembic_config(db_path), "base")
    assert inspect_database(db_path).revision is None
