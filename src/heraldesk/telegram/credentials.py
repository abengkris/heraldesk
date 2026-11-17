"""Telegram bot token resolution and encrypted-at-rest storage.

Precedence:

1. ``HERALDESK_TELEGRAM_BOT_TOKEN`` in the process environment,
2. the same key in ``$HERALDESK_HOME/.env``,
3. ``$HERALDESK_HOME/telegram.token``, a Fernet token encrypted with ``secret.key``.

The raw token never enters ``heraldesk.yaml``, the SQLite database, an audit payload or
CLI output. The encrypted file is written with mode ``0o600``.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from heraldesk import paths
from heraldesk.core.crypto import CredentialCipher, CryptoError
from heraldesk.telegram.base import TelegramError

TOKEN_ENV_VAR = "HERALDESK_TELEGRAM_BOT_TOKEN"
TOKEN_FILE_MODE = 0o600


class TelegramConfigError(TelegramError):
    """Raised when the bot token cannot be resolved or stored safely."""


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def resolve_bot_token(layout: paths.Layout) -> str | None:
    """Return the configured token, or ``None`` when Telegram has no token yet.

    Raises :class:`TelegramConfigError` when an encrypted token exists but cannot be
    decrypted, so a wrong ``secret.key`` never looks like "no token configured".
    """
    from_environment = _clean(os.environ.get(TOKEN_ENV_VAR))
    if from_environment is not None:
        return from_environment

    if layout.env_path.is_file():
        try:
            values = dotenv_values(layout.env_path)
        except OSError as exc:
            raise TelegramConfigError(f"cannot read {layout.env_path}: {exc}") from exc
        from_dotenv = _clean(values.get(TOKEN_ENV_VAR))
        if from_dotenv is not None:
            return from_dotenv

    token_path = layout.telegram_token_path
    if not token_path.is_file():
        return None
    try:
        ciphertext = token_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise TelegramConfigError(f"cannot read {token_path}: {exc}") from exc
    try:
        cipher = CredentialCipher.from_key_file(layout.key_path)
        return _clean(cipher.decrypt(ciphertext))
    except CryptoError as exc:
        raise TelegramConfigError(
            "telegram token cannot be decrypted; secret.key does not match the stored token"
        ) from exc


def token_source(layout: paths.Layout) -> str:
    """Return ``environment``, ``.env``, ``encrypted-file`` or ``none``."""
    if _clean(os.environ.get(TOKEN_ENV_VAR)) is not None:
        return "environment"
    if layout.env_path.is_file():
        try:
            values = dotenv_values(layout.env_path)
        except OSError:
            values = {}
        if _clean(values.get(TOKEN_ENV_VAR)) is not None:
            return ".env"
    if layout.telegram_token_path.is_file():
        return "encrypted-file"
    return "none"


def store_bot_token(layout: paths.Layout, token: str) -> Path:
    """Encrypt *token* with ``secret.key`` and write it with mode 600."""
    cleaned = token.strip()
    if not cleaned:
        raise TelegramConfigError("telegram bot token must not be empty")
    try:
        cipher = CredentialCipher.from_key_file(layout.key_path)
    except CryptoError as exc:
        raise TelegramConfigError(str(exc)) from exc
    ciphertext = cipher.encrypt(cleaned)
    path = layout.telegram_token_path
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, TOKEN_FILE_MODE)
    except OSError as exc:
        raise TelegramConfigError(f"cannot write {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(ciphertext)
    except OSError as exc:
        raise TelegramConfigError(f"cannot write {path}: {exc}") from exc
    if os.name == "posix":
        try:
            path.chmod(TOKEN_FILE_MODE)
        except OSError as exc:
            raise TelegramConfigError(f"cannot set permissions on {path}: {exc}") from exc
    return path


def mask_token(token: str | None) -> str:
    """Return a short fingerprint that is safe for CLI output."""
    if not token:
        return "not configured"
    if len(token) < 8:
        return "[REDACTED]"
    return f"****{token[-4:]}"
