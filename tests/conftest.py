"""Shared test fixtures.

Every test gets an isolated ``HERALDESK_HOME`` so the real data folder is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from heraldesk.core.bootstrap import InitResult, initialize


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
