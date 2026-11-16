"""Tests for data folder resolution and creation."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from heraldesk import paths

pytestmark = pytest.mark.core


def test_resolve_home_uses_environment(tmp_path: Path) -> None:
    target = tmp_path / "custom"
    assert paths.resolve_home({"HERALDESK_HOME": str(target)}) == target.resolve()


@pytest.mark.skipif(sys.platform == "win32", reason="HOME expansion differs on Windows")
def test_resolve_home_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    assert paths.resolve_home({"HERALDESK_HOME": "~/data"}) == (fake_home / "data").resolve()


def test_resolve_home_defaults_to_dot_heraldesk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert paths.resolve_home({}) == (fake_home / ".heraldesk").resolve()


def test_resolve_home_ignores_blank_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    assert paths.resolve_home({"HERALDESK_HOME": "   "}) == (fake_home / ".heraldesk").resolve()


def test_layout_paths(tmp_path: Path) -> None:
    layout = paths.layout_for(tmp_path / "data")
    assert layout.home == (tmp_path / "data").resolve()
    assert layout.db_path == layout.home / "heraldesk.db"
    assert layout.config_path == layout.home / "heraldesk.yaml"
    assert layout.key_path == layout.home / "secret.key"
    assert layout.env_path == layout.home / ".env"
    assert [directory.name for directory in layout.subdirs] == [
        "profiles",
        "agents",
        "logs",
        "backups",
    ]


def test_ensure_layout_creates_directories(home: Path) -> None:
    layout = paths.ensure_layout(home)
    assert layout.home.is_dir()
    for directory in layout.subdirs:
        assert directory.is_dir()
    if sys.platform != "win32":
        assert stat.S_IMODE(layout.home.stat().st_mode) == 0o700


def test_ensure_layout_is_idempotent(home: Path) -> None:
    first = paths.ensure_layout(home)
    (first.logs / "keep.txt").write_text("keep", encoding="utf-8")
    second = paths.ensure_layout(home)
    assert first == second
    assert (home / "logs" / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_ensure_layout_rejects_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(paths.LayoutError):
        paths.ensure_layout(blocker)
