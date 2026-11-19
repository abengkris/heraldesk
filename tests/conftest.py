"""Shared test fixtures.

Every test gets an isolated ``HERALDESK_HOME`` so the real data folder is never touched.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.bootstrap import InitResult, initialize
from heraldesk.db.engine import build_engine
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import session_factory
from heraldesk.domain.profiles import (
    ProfileDefinition,
    load_profile,
    register_definition,
    write_profile_skeleton,
)


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


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> InitResult:
    """Initialise one migrated database for the whole session.

    Every test gets a copy of this data folder. Running Alembic once per session instead
    of once per test is the difference between a two-minute suite and a fifteen-minute
    one; ``test_alembic.py`` and ``test_init.py`` still exercise the real upgrade path.
    """
    home = tmp_path_factory.mktemp("heraldesk-template") / "home"
    result = initialize(home)
    engine = build_engine(result.layout.db_path)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    finally:
        engine.dispose()
    return result


@pytest.fixture
def initialized(home: Path, migrated_template: InitResult) -> InitResult:
    """Copy the session template data folder for one test."""
    shutil.copytree(migrated_template.layout.home, home)
    return replace(migrated_template, layout=paths.layout_for(home))


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


@dataclass(frozen=True, slots=True)
class ProfileSetup:
    """A registered profile and the paths needed to run an agent against it."""

    layout: paths.Layout
    scope: ProfileScope
    definition: ProfileDefinition


@pytest.fixture
def profile_setup(initialized: InitResult, db_session: Session) -> ProfileSetup:
    """Write, register and return one valid profile named ``acme``."""
    definition = load_profile(write_profile_skeleton(initialized.layout.profiles, "acme"))
    summary, _ = register_definition(db_session, definition)
    db_session.commit()
    return ProfileSetup(
        layout=initialized.layout,
        scope=ProfileScope(summary.id),
        definition=definition,
    )
