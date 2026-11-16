"""SQLite engine and inspection helpers.

Heraldesk uses SQLite in WAL mode with foreign keys enabled. The database lives inside the
data folder and is the only runtime state store.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError

BUSY_TIMEOUT_MS = 5000
WAL_MODE = "wal"


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """Result of opening the database and reading its pragmas and revision."""

    exists: bool
    journal_mode: str | None
    revision: str | None
    error: str | None

    @property
    def is_healthy(self) -> bool:
        """Return ``True`` when the file exists and opened without error."""
        return self.exists and self.error is None


def sqlite_url(db_path: Path) -> str:
    """Build a SQLAlchemy URL for an absolute SQLite path."""
    return f"sqlite:///{db_path.expanduser().resolve().as_posix()}"


def build_engine(db_path: Path) -> Engine:
    """Create an engine with WAL, foreign keys and a busy timeout on every connection."""
    engine = create_engine(sqlite_url(db_path), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _configure_connection(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        finally:
            cursor.close()

    return engine


def inspect_database(db_path: Path) -> DatabaseStatus:
    """Open the database and report WAL mode and the Alembic revision."""
    if not db_path.is_file():
        return DatabaseStatus(exists=False, journal_mode=None, revision=None, error=None)

    engine = build_engine(db_path)
    try:
        with engine.connect() as connection:
            raw_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()
            revision: str | None = None
            has_table = connection.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
                )
            ).first()
            if has_table is not None:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        return DatabaseStatus(
            exists=True,
            journal_mode=str(raw_mode).lower(),
            revision=revision,
            error=None,
        )
    except (SQLAlchemyError, OSError) as exc:
        return DatabaseStatus(exists=True, journal_mode=None, revision=None, error=str(exc))
    finally:
        engine.dispose()
