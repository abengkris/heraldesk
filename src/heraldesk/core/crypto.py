"""Credential encryption helpers.

Social account credentials are encrypted at rest with Fernet. The key lives in
``secret.key`` inside the data folder and must stay private (mode ``0o600``).
LLM API keys are not handled here: they live in ``.env`` in plaintext by decision.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet

KEY_MODE = 0o600


class CryptoError(Exception):
    """Raised when the encryption key is missing, invalid or not readable."""


def generate_key() -> bytes:
    """Generate a new Fernet key."""
    return Fernet.generate_key()


def validate_key(key: bytes) -> None:
    """Raise :class:`CryptoError` when *key* is not a usable Fernet key."""
    try:
        Fernet(key)
    except (TypeError, ValueError) as exc:
        raise CryptoError("secret.key is not a valid Fernet key") from exc


def _restrict_mode(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        path.chmod(KEY_MODE)
    except OSError as exc:
        raise CryptoError(f"cannot set permissions on {path}: {exc}") from exc


def ensure_key_file(path: Path) -> bool:
    """Create the key file when it is missing and tighten its permissions.

    Returns ``True`` when the key was created, ``False`` when an existing key was kept.
    """
    if path.exists():
        if not path.is_file():
            raise CryptoError(f"{path} exists but is not a file")
        validate_key(path.read_bytes())
        _restrict_mode(path)
        return False

    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_MODE)
    except OSError as exc:
        raise CryptoError(f"cannot create {path}: {exc}") from exc
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(generate_key())
    _restrict_mode(path)
    return True


def key_is_secure(path: Path) -> bool:
    """Return ``True`` when the key is not readable by group or others on POSIX.

    Windows does not expose POSIX permission bits; callers should skip this check there.
    """
    if os.name != "posix":
        return True
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
