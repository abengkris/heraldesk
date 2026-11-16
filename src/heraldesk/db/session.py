"""Session factories and the CLI/database session context.

Repository code never creates its own engine. Callers open a short-lived session with
:func:`database_session`, which commits on success and rolls back on any exception.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from heraldesk import paths
from heraldesk.db.engine import build_engine


class DatabaseNotInitialized(Exception):
    """Raised when the SQLite database does not exist yet."""


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to *engine*."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def database_session(layout: paths.Layout) -> Iterator[Session]:
    """Yield a session that commits on success and rolls back on failure."""
    if not layout.db_path.is_file():
        raise DatabaseNotInitialized(
            f"database {layout.db_path} does not exist; run 'heraldesk init' first"
        )
    engine = build_engine(layout.db_path)
    factory = session_factory(engine)
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()
