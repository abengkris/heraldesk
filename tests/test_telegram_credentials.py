"""M6 Telegram token storage and precedence tests."""

from __future__ import annotations

import os
import stat

import pytest

from heraldesk.core.bootstrap import InitResult
from heraldesk.core.redact import SecretRegistry
from heraldesk.telegram.credentials import (
    TOKEN_ENV_VAR,
    mask_token,
    resolve_bot_token,
    store_bot_token,
    token_source,
)

pytestmark = pytest.mark.core

TOKEN = "123456789:AAHfakeTelegramTokenValue1234567890"


def test_environment_wins_over_dotenv(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = initialized.layout
    layout.env_path.write_text(f"{TOKEN_ENV_VAR}=dotenv-token-value\n", encoding="utf-8")
    monkeypatch.setenv(TOKEN_ENV_VAR, TOKEN)

    assert resolve_bot_token(layout) == TOKEN
    assert token_source(layout) == "environment"


def test_dotenv_is_used_when_environment_is_empty(initialized: InitResult) -> None:
    layout = initialized.layout
    layout.env_path.write_text(f"{TOKEN_ENV_VAR}=dotenv-token-value\n", encoding="utf-8")

    assert resolve_bot_token(layout) == "dotenv-token-value"
    assert token_source(layout) == ".env"


def test_encrypted_file_round_trip(initialized: InitResult) -> None:
    layout = initialized.layout

    path = store_bot_token(layout, TOKEN)

    assert path.is_file()
    assert TOKEN not in path.read_text(encoding="ascii")
    assert resolve_bot_token(layout) == TOKEN
    assert token_source(layout) == "encrypted-file"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def test_missing_token_is_none(initialized: InitResult) -> None:
    assert resolve_bot_token(initialized.layout) is None
    assert token_source(initialized.layout) == "none"


def test_mask_token_never_returns_the_full_secret() -> None:
    masked = mask_token(TOKEN)
    assert TOKEN not in masked
    assert masked.endswith(TOKEN[-4:])
    assert mask_token(None) == "not configured"


def test_secret_registry_redacts_the_token() -> None:
    registry = SecretRegistry.from_environment(extra_values=[TOKEN])
    assert TOKEN not in registry.redact(f"token is {TOKEN}")
    assert "[REDACTED]" in registry.redact(f"token is {TOKEN}")
