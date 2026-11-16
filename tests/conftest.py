"""Shared test fixtures.

Every test gets an isolated ``HERALDESK_HOME`` so the real data folder is never touched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.bootstrap import InitResult, initialize
from heraldesk.db.engine import build_engine
from heraldesk.db.session import session_factory
from heraldesk.domain.profiles import ProfileDefinition, load_profile, write_profile_skeleton


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point HERALDESK_HOME at a temporary folder for every test."""
    monkeypatch.setenv("HERALDESK_HOME", str(tmp_path / "isolated-home"))


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a temporary data folder path and make it the active HERALDESK_HOME."""
    path = tmp_path / "heraldesk-home"
    monkeypatch.setenv("HERALDESK_HOME", str(path))
    return path


@pytest.fixture
def initialized(home: Path) -> InitResult:
    """Initialise the temporary data folder."""
    return initialize(home)


@pytest.fixture
def db_session(initialized: InitResult) -> Iterator[Session]:
    """Yield a session against the test database."""
    engine = build_engine(initialized.layout.db_path)
    factory = session_factory(engine)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture
def make_definition(initialized: InitResult) -> Callable[[str], ProfileDefinition]:
    """Return a factory that writes and loads a valid profile skeleton."""

    def _make(slug: str) -> ProfileDefinition:
        directory = write_profile_skeleton(initialized.layout.profiles, slug)
        return load_profile(directory)

    return _make
