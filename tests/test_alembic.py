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
    }


def test_m1_profile_owned_tables_have_profile_id(initialized: InitResult) -> None:
    engine = build_engine(initialized.layout.db_path)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            for table in ("social_accounts", "audit_log", "jobs", "agent_runs"):
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


def test_migration_scripts_are_present() -> None:
    directory = scripts_directory()
    assert (directory / "env.py").is_file()
    assert list((directory / "versions").glob("0001*.py"))
    assert list((directory / "versions").glob("0002*.py"))
    assert list((directory / "versions").glob("0003*.py"))


def test_downgrade_to_base_clears_the_revision(initialized: InitResult) -> None:
    db_path = initialized.layout.db_path
    command.downgrade(alembic_config(db_path), "base")
    assert inspect_database(db_path).revision is None
