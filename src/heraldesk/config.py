"""Global configuration for Heraldesk.

Configuration is merged from, in increasing priority:

1. defaults defined by the models below,
2. ``$HERALDESK_HOME/heraldesk.yaml``,
3. ``$HERALDESK_HOME/.env``,
4. environment variables with the ``HERALDESK_`` prefix (``__`` separates nested keys).

LLM API keys belong in ``.env`` or the environment and must never be written to
``heraldesk.yaml``. Platform credentials are stored encrypted in the database.
"""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from heraldesk import paths

_yaml_file: ContextVar[Path | None] = ContextVar("heraldesk_yaml_file", default=None)
_dotenv_file: ContextVar[Path | None] = ContextVar("heraldesk_dotenv_file", default=None)


class ConfigError(Exception):
    """Raised when the configuration file cannot be read or validated."""


class _HeraldeskDotEnvSource(DotEnvSettingsSource):
    """Read only Heraldesk settings from ``.env``.

    ``.env`` also holds provider API keys (``DEEPSEEK_API_KEY`` and friends). Those must
    not be treated as configuration fields, otherwise strict validation rejects them.
    """

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        fields = set(self.settings_cls.model_fields)
        return {key: value for key, value in data.items() if key.split("__", 1)[0] in fields}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMSettings(_Section):
    """LLM provider defaults. API keys live in ``.env``, not here."""

    default_model: str = ""
    max_daily_cost_usd: float = Field(default=5.0, ge=0.0)
    provider: Literal["auto", "litellm", "fake"] = "auto"
    timeout_seconds: float = Field(default=60.0, gt=0.0)
    fake_fixtures_dir: str = ""


class ServerSettings(_Section):
    """Local server settings. The default is loopback only."""

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)


class TelegramSettings(_Section):
    """Telegram bot settings. Only allowlisted chat IDs are served."""

    enabled: bool = False
    allowed_chat_ids: list[int] = Field(default_factory=list)


class ResearchSettings(_Section):
    """Researcher web search. ``auto`` uses the fake when the LLM provider is fake."""

    search_provider: Literal["auto", "fake", "duckduckgo"] = "auto"
    timeout_seconds: float = Field(default=10.0, gt=0.0)
    max_results: int = Field(default=5, ge=1, le=20)


class Config(BaseSettings):
    """Validated global configuration."""

    model_config = SettingsConfigDict(
        env_prefix="HERALDESK_",
        env_nested_delimiter="__",
        extra="forbid",
        env_file_encoding="utf-8",
        validate_default=True,
    )

    version: Literal[1] = 1
    llm: LLMSettings = Field(default_factory=LLMSettings)
    research: ResearchSettings = Field(default_factory=ResearchSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert the per-instance dotenv and YAML files, env above both, defaults below."""
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        dotenv_file = _dotenv_file.get()
        if dotenv_file is not None:
            sources.append(
                _HeraldeskDotEnvSource(
                    settings_cls,
                    env_file=dotenv_file,
                    env_file_encoding="utf-8",
                )
            )
        yaml_file = _yaml_file.get()
        if yaml_file is not None:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file))
        sources.append(file_secret_settings)
        return tuple(sources)


def load_config(home: Path | None = None) -> Config:
    """Load and validate configuration for a data folder.

    Raises :class:`ConfigError` with a readable message when the YAML is malformed or a
    value is invalid. A missing ``heraldesk.yaml`` is not an error.
    """
    layout = paths.layout_for(home if home is not None else paths.resolve_home())
    yaml_file = layout.config_path if layout.config_path.is_file() else None
    dotenv_file = layout.env_path if layout.env_path.is_file() else None
    yaml_token = _yaml_file.set(yaml_file)
    dotenv_token = _dotenv_file.set(dotenv_file)
    try:
        return Config()
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration: {exc}") from exc
    except (yaml.YAMLError, OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {yaml_file}: {exc}") from exc
    finally:
        _yaml_file.reset(yaml_token)
        _dotenv_file.reset(dotenv_token)


def is_loopback(host: str) -> bool:
    """Return ``True`` for the loopback host names Heraldesk accepts by default."""
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"}
