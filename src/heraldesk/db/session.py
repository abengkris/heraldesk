"""Session factories.

Repository code in M1 will receive a session created here. M0 only provides the factory.
"""

from __future__ import annotations

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a session factory bound to *engine*."""
    return sessionmaker(bind=engine, expire_on_commit=False)
