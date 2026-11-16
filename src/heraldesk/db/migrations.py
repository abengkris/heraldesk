"""Locate and configure the Alembic migration environment.

Migrations are shipped inside the wheel (as ``heraldesk/_alembic``) so that an installed
Heraldesk can initialise its database. When running from a source checkout the repository
``alembic/`` directory is used instead.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config as AlembicConfig

from heraldesk.db.engine import sqlite_url

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_SCRIPTS_DIR = PACKAGE_ROOT / "_alembic"


class MigrationError(Exception):
    """Raised when the migration scripts cannot be located."""


def scripts_directory() -> Path:
    """Return the directory that holds ``env.py`` and ``versions/``."""
    if PACKAGED_SCRIPTS_DIR.is_dir():
        return PACKAGED_SCRIPTS_DIR
    repository_copy = PACKAGE_ROOT.parents[1] / "alembic"
    if repository_copy.is_dir():
        return repository_copy
    raise MigrationError("cannot find the Alembic migration scripts")


def alembic_config(db_path: Path) -> AlembicConfig:
    """Build an Alembic config that targets *db_path*."""
    config = AlembicConfig()
    config.set_main_option("script_location", str(scripts_directory()))
    config.set_main_option("sqlalchemy.url", sqlite_url(db_path))
    return config
