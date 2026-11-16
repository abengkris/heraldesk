"""Credential encryption helpers.

Social account credentials are encrypted at rest with Fernet. The key lives in
``secret.key`` inside the data folder and must stay private (mode ``0o600``).
LLM API keys are not handled here: they live in ``.env`` in plaintext by decision.

Nothing in this module logs or formats a key, plaintext credential or ciphertext.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

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


def load_key(path: Path) -> bytes:
    """Read and validate the key at *path* without ever echoing its contents."""
    if not path.is_file():
        raise CryptoError(f"{path} is missing; run 'heraldesk init' first")
    try:
        key = path.read_bytes()
    except OSError as exc:
        raise CryptoError(f"cannot read {path}: {exc}") from exc
    validate_key(key)
    return key


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


class CredentialCipher:
    """Encrypt and decrypt social account credentials with ``secret.key``.

    Instances hold the Fernet key in memory only. Their representation is redacted so a
    debug log or exception trace cannot print it.
    """

    def __init__(self, key: bytes) -> None:
        validate_key(key)
        self._fernet = Fernet(key)

    @classmethod
    def from_key_file(cls, path: Path) -> CredentialCipher:
        """Build a cipher from an existing ``secret.key`` file."""
        return cls(load_key(path))

    def encrypt(self, plaintext: str | bytes) -> str:
        """Encrypt *plaintext* and return an ASCII Fernet token."""
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        return self._fernet.encrypt(data).decode("ascii")

    def decrypt(self, token: str) -> str:
        """Decrypt a token. A wrong key or corrupt token raises :class:`CryptoError`."""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise CryptoError(
                "credentials cannot be decrypted; secret.key does not match "
                "or the stored value is corrupted"
            ) from exc

    def encrypt_json(self, values: Mapping[str, str]) -> str:
        """Encrypt a string mapping as canonical JSON."""
        payload = json.dumps(dict(values), sort_keys=True, ensure_ascii=False)
        return self.encrypt(payload)

    def decrypt_json(self, token: str) -> dict[str, str]:
        """Decrypt and validate a credential mapping."""
        raw = self.decrypt(token)
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CryptoError("stored credentials are not valid JSON") from exc
        if not isinstance(values, dict):
            raise CryptoError("stored credentials must be a JSON object")
        clean: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise CryptoError("stored credentials must map strings to strings")
            clean[key] = value
        return clean

    def __repr__(self) -> str:
        """Never expose the key material."""
        return "CredentialCipher(<redacted>)"


_SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "private_key",
)


def redact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of *values* with secret-looking entries masked.

    This is a helper for future structured logging. Audit payloads reject secret keys
    outright instead of storing redacted values.
    """
    redacted: dict[str, Any] = {}
    for key, value in values.items():
        lowered = key.lower()
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted
