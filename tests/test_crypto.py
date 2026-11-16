"""Credential encryption tests."""

from __future__ import annotations

import pytest

from heraldesk.core.bootstrap import InitResult
from heraldesk.core.crypto import (
    CredentialCipher,
    CryptoError,
    generate_key,
    redact_mapping,
)

pytestmark = pytest.mark.core


def test_roundtrip_through_secret_key(initialized: InitResult) -> None:
    cipher = CredentialCipher.from_key_file(initialized.layout.key_path)
    token = cipher.encrypt("super-secret-token")
    assert token != "super-secret-token"
    assert "super-secret-token" not in token
    assert cipher.decrypt(token) == "super-secret-token"


def test_json_roundtrip(initialized: InitResult) -> None:
    cipher = CredentialCipher.from_key_file(initialized.layout.key_path)
    credentials = {"token": "abc123", "username": "brand"}
    token = cipher.encrypt_json(credentials)
    assert "abc123" not in token
    assert cipher.decrypt_json(token) == credentials


def test_wrong_key_fails(initialized: InitResult) -> None:
    cipher = CredentialCipher.from_key_file(initialized.layout.key_path)
    token = cipher.encrypt("super-secret-token")
    other = CredentialCipher(generate_key())
    with pytest.raises(CryptoError, match="cannot be decrypted"):
        other.decrypt(token)


def test_cipher_repr_never_exposes_the_key(initialized: InitResult) -> None:
    key = initialized.layout.key_path.read_bytes()
    cipher = CredentialCipher(key)
    assert key.decode("ascii") not in repr(cipher)
    assert "redacted" in repr(cipher)


def test_missing_key_file_is_a_readable_error(initialized: InitResult) -> None:
    path = initialized.layout.home / "missing.key"
    with pytest.raises(CryptoError, match="missing"):
        CredentialCipher.from_key_file(path)


def test_redact_mapping_masks_secret_like_keys() -> None:
    redacted = redact_mapping(
        {"token": "abc", "api_key": "xyz", "handle": "@brand", "password": "pw"}
    )
    assert redacted == {
        "token": "<redacted>",
        "api_key": "<redacted>",
        "handle": "@brand",
        "password": "<redacted>",
    }
