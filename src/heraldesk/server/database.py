"""One shared SQLite engine for the server process.

The CLI opens a short-lived engine per command. A long-running server must not, so this
holder builds the engine once, hands out sessions per request or background job, and
disposes it during graceful shutdown.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from heraldesk import paths
from heraldesk.db.engine import build_engine
from heraldesk.db.session import DatabaseNotInitialized, session_factory


class ServerDatabase:
    """Own the server's SQLAlchemy engine and session factory."""

    def __init__(self, layout: paths.Layout) -> None:
        if not layout.db_path.is_file():
            raise DatabaseNotInitialized(
                f"database {layout.db_path} does not exist; run 'heraldesk init' first"
            )
        self._layout = layout
        self._engine: Engine = build_engine(layout.db_path)
        self._factory: sessionmaker[Session] = session_factory(self._engine)

    @property
    def engine(self) -> Engine:
        """Return the shared engine (used by read-only inspections)."""
        return self._engine

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session that commits on success and rolls back on failure."""
        session = self._factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def is_healthy(self) -> bool:
        """Return ``True`` when a trivial query succeeds."""
        try:
            with self._engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
        except Exception:
            return False
        return True

    def close(self) -> None:
        """Dispose the engine. Safe to call more than once."""
        self._engine.dispose()
