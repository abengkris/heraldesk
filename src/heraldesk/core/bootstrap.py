"""Bootstrap a Heraldesk data folder.

``heraldesk init`` creates the folder layout, the Fernet key, a documented default
``heraldesk.yaml`` and an empty SQLite database stamped with the Alembic baseline.
The function is idempotent and never overwrites an existing config or key.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.util.exc import CommandError

from heraldesk import paths
from heraldesk.config import ConfigError, load_config
from heraldesk.core.crypto import CryptoError, ensure_key_file
from heraldesk.db.engine import inspect_database
from heraldesk.db.migrations import MigrationError, alembic_config

DEFAULT_CONFIG_YAML = """\
# Heraldesk global configuration.
# LLM API keys belong in .env (plaintext, gitignored) or the environment, never here.
version: 1
llm:
  # auto: fake provider when default_model is empty or starts with "fake/",
  # LiteLLM otherwise. "fake" and "litellm" force one provider.
  provider: auto
  # LiteLLM model name, for example "deepseek/deepseek-chat" or "ollama/llama3.2".
  default_model: ""
  max_daily_cost_usd: 5.0
  timeout_seconds: 60
  # Optional folder with <role>.json fake-provider fixtures.
  fake_fixtures_dir: ""
server:
  # Loopback only by default. A non-loopback host requires authentication.
  host: 127.0.0.1
  port: 8000
telegram:
  enabled: false
  allowed_chat_ids: []
"""


class BootstrapError(Exception):
    """Raised when a data folder cannot be initialised."""


@dataclass(frozen=True, slots=True)
class InitResult:
    """What ``heraldesk init`` created or kept."""

    layout: paths.Layout
    key_created: bool
    config_created: bool
    db_created: bool
    revision: str | None


def initialize(home: Path | None = None) -> InitResult:
    """Create or repair a data folder and return what happened.

    Raises :class:`BootstrapError` for an unusable folder, an invalid existing
    configuration or a failed migration.
    """
    layout = paths.ensure_layout(home)

    try:
        key_created = ensure_key_file(layout.key_path)
    except CryptoError as exc:
        raise BootstrapError(str(exc)) from exc

    if layout.config_path.exists() and not layout.config_path.is_file():
        raise BootstrapError(f"{layout.config_path} exists but is not a file")
    config_created = False
    if not layout.config_path.exists():
        try:
            layout.config_path.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
        except OSError as exc:
            raise BootstrapError(f"cannot write {layout.config_path}: {exc}") from exc
        config_created = True

    try:
        load_config(layout.home)
    except ConfigError as exc:
        raise BootstrapError(str(exc)) from exc

    db_created = not layout.db_path.exists()
    try:
        command.upgrade(alembic_config(layout.db_path), "head")
    except (CommandError, MigrationError) as exc:
        raise BootstrapError(f"cannot initialise the database: {exc}") from exc

    status = inspect_database(layout.db_path)
    if status.error is not None:
        raise BootstrapError(f"cannot read the database: {status.error}")

    return InitResult(
        layout=layout,
        key_created=key_created,
        config_created=config_created,
        db_created=db_created,
        revision=status.revision,
    )
