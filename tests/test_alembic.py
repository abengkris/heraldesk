"""Tests for the Alembic baseline."""

from __future__ import annotations

from alembic import command
from sqlalchemy import inspect

from heraldesk.core.bootstrap import InitResult
from heraldesk.db.engine import build_engine, inspect_database
from heraldesk.db.migrations import alembic_config, scripts_directory


def test_baseline_creates_only_the_version_table(initialized: InitResult) -> None:
    engine = build_engine(initialized.layout.db_path)
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
    finally:
        engine.dispose()
    assert tables == {"alembic_version"}


def test_migration_scripts_are_present() -> None:
    directory = scripts_directory()
    assert (directory / "env.py").is_file()
    assert list((directory / "versions").glob("0001*.py"))


def test_downgrade_to_base_clears_the_revision(initialized: InitResult) -> None:
    db_path = initialized.layout.db_path
    command.downgrade(alembic_config(db_path), "base")
    assert inspect_database(db_path).revision is None
