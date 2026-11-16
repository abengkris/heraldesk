"""Filesystem layout for the Heraldesk data folder.

The data folder is the single place where Heraldesk keeps its configuration, database,
encryption key and per-profile text files. It defaults to ``~/.heraldesk`` and can be moved
with the ``HERALDESK_HOME`` environment variable.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

HOME_ENV_VAR = "HERALDESK_HOME"
DEFAULT_HOME_NAME = ".heraldesk"
DATA_SUBDIRS = ("profiles", "agents", "logs", "backups")
DB_FILENAME = "heraldesk.db"
CONFIG_FILENAME = "heraldesk.yaml"
KEY_FILENAME = "secret.key"
KILLSWITCH_FILENAME = "killswitch"
ENV_FILENAME = ".env"


class LayoutError(Exception):
    """Raised when the data folder cannot be resolved or created."""


@dataclass(frozen=True, slots=True)
class Layout:
    """Resolved paths inside a Heraldesk data folder."""

    home: Path
    profiles: Path
    agents: Path
    logs: Path
    backups: Path

    @property
    def db_path(self) -> Path:
        """Path to the SQLite database."""
        return self.home / DB_FILENAME

    @property
    def config_path(self) -> Path:
        """Path to the global ``heraldesk.yaml``."""
        return self.home / CONFIG_FILENAME

    @property
    def key_path(self) -> Path:
        """Path to the Fernet key used for credential encryption."""
        return self.home / KEY_FILENAME

    @property
    def env_path(self) -> Path:
        """Path to the gitignored ``.env`` file that holds LLM API keys."""
        return self.home / ENV_FILENAME

    @property
    def killswitch_path(self) -> Path:
        """Marker file for the global kill switch (present means publishing is halted)."""
        return self.home / KILLSWITCH_FILENAME

    @property
    def subdirs(self) -> tuple[Path, ...]:
        """Directories every data folder must contain."""
        return (self.profiles, self.agents, self.logs, self.backups)


def layout_for(home: Path) -> Layout:
    """Build the :class:`Layout` for a data folder without touching the disk."""
    resolved = home.expanduser().resolve()
    return Layout(
        home=resolved,
        profiles=resolved / "profiles",
        agents=resolved / "agents",
        logs=resolved / "logs",
        backups=resolved / "backups",
    )


def resolve_home(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the data folder from ``HERALDESK_HOME`` or the default location."""
    source = os.environ if env is None else env
    raw = source.get(HOME_ENV_VAR, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / DEFAULT_HOME_NAME).resolve()


def ensure_layout(home: Path | None = None) -> Layout:
    """Create the data folder and its required subdirectories.

    Existing directories are left alone, including their permissions. New directories are
    created with mode ``0o700`` on POSIX systems.
    """
    layout = layout_for(home if home is not None else resolve_home())
    if layout.home.exists() and not layout.home.is_dir():
        raise LayoutError(f"data folder {layout.home} exists but is not a directory")
    try:
        layout.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in layout.subdirs:
            directory.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise LayoutError(f"cannot create data folder {layout.home}: {exc}") from exc
    return layout
