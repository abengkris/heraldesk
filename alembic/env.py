"""Alembic environment.

The database URL is supplied by ``heraldesk.db.migrations.alembic_config``. When the
environment is invoked directly, it falls back to ``$HERALDESK_HOME/heraldesk.db`` and
finally to ``~/.heraldesk/heraldesk.db``.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from heraldesk.db import models  # noqa: F401  (register models on Base.metadata)
from heraldesk.db.base import Base

config = context.config

if not config.get_main_option("sqlalchemy.url"):
    home = Path(os.environ.get("HERALDESK_HOME") or Path.home() / ".heraldesk").expanduser()
    database = (home / "heraldesk.db").resolve()
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database.as_posix()}")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a database connection."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
