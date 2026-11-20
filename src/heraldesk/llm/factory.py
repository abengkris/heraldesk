"""Choose and configure an LLM provider from ``heraldesk.yaml``.

Precedence for keys is the process environment, then ``$HERALDESK_HOME/.env``. The file is
read into missing environment variables only, so a real environment variable always wins.
Values are never logged.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from heraldesk import paths
from heraldesk.config import Config
from heraldesk.llm.errors import LLMConfigError
from heraldesk.llm.fake import FakeLLM
from heraldesk.llm.litellm_provider import LiteLLMProvider
from heraldesk.llm.types import LLMProvider

FAKE_MODEL_PREFIX = "fake"


def export_dotenv_keys(layout: paths.Layout) -> tuple[str, ...]:
    """Copy keys from ``.env`` into the environment when they are not already set.

    Returns the names that were exported. Never returns or logs values.
    """
    if not layout.env_path.is_file():
        return ()
    try:
        values = dotenv_values(layout.env_path)
    except OSError as exc:
        raise LLMConfigError(f"cannot read {layout.env_path}: {exc}") from exc
    exported: list[str] = []
    for name, value in values.items():
        if not name or value is None:
            continue
        if os.environ.get(name) is not None:
            continue
        os.environ[name] = value
        exported.append(name)
    return tuple(exported)


def _fake_fixture_dir(config: Config, layout: paths.Layout) -> Path | None:
    raw = config.llm.fake_fixtures_dir.strip()
    if not raw:
        return None
    directory = Path(raw).expanduser()
    if not directory.is_absolute():
        directory = layout.home / directory
    if not directory.is_dir():
        raise LLMConfigError(f"llm.fake_fixtures_dir {directory} is not a directory")
    return directory


def _is_fake_model(model: str) -> bool:
    return not model or model == FAKE_MODEL_PREFIX or model.startswith(f"{FAKE_MODEL_PREFIX}/")


def build_llm(config: Config, layout: paths.Layout) -> LLMProvider:
    """Build the provider selected by ``llm.provider``.

    ``auto`` uses the fake provider for an empty or ``fake`` model and LiteLLM otherwise.
    """
    export_dotenv_keys(layout)
    model = config.llm.default_model.strip()
    choice = config.llm.provider
    if choice == "fake" or (choice == "auto" and _is_fake_model(model)):
        return FakeLLM(
            default_model=model or f"{FAKE_MODEL_PREFIX}/fake",
            fixture_dir=_fake_fixture_dir(config, layout),
        )
    if not model:
        raise LLMConfigError(
            "llm.default_model is empty: set a LiteLLM model in heraldesk.yaml, "
            "or set llm.provider to fake for offline use"
        )
    return LiteLLMProvider(
        model=model,
        timeout_seconds=config.llm.timeout_seconds,
        min_interval_seconds=config.llm.min_interval_seconds,
    )


def resolve_model(config: Config, provider: LLMProvider) -> str:
    """Return the model name to record on a run."""
    return config.llm.default_model.strip() or provider.default_model
