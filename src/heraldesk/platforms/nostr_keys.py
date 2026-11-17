"""Nostr key encoding helpers.

Nostr private keys are ``nsec1...`` bech32 strings and public keys are ``npub1...``.
Only the public form is ever displayed; the private form is a credential and must be
encrypted at rest, exactly like any other platform secret.

The module is pure standard library so the CLI can validate a key without importing the
relay client or the signing library. Error messages never echo the input value.
"""

from __future__ import annotations

from typing import Final

NSEC_HRP: Final = "nsec"
NPUB_HRP: Final = "npub"

# secp256k1 group order. A private key must be in 1..n-1.
SECP256K1_ORDER: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

_CHARSET: Final = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CHARSET_INDEX: Final = {character: index for index, character in enumerate(_CHARSET)}
_GENERATORS: Final = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
_MAX_BECH32_LENGTH: Final = 90


class NostrKeyError(ValueError):
    """Raised when a Nostr key cannot be decoded. The message never contains the key."""


def _polymod(values: list[int]) -> int:
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = (checksum & 0x1FFFFFF) << 5 ^ value
        for index, generator in enumerate(_GENERATORS):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _hrp_expand(hrp: str) -> list[int]:
    return (
        [ord(character) >> 5 for character in hrp]
        + [0]
        + [ord(character) & 31 for character in hrp]
    )


def _create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _hrp_expand(hrp) + data
    polymod = _polymod([*values, 0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - index)) & 31 for index in range(6)]


def _verify_checksum(hrp: str, data: list[int]) -> bool:
    return _polymod(_hrp_expand(hrp) + data) == 1


def _convert_bits(
    data: list[int] | bytes,
    from_bits: int,
    to_bits: int,
    *,
    pad: bool,
) -> list[int]:
    accumulator = 0
    bits = 0
    result: list[int] = []
    max_value = (1 << to_bits) - 1
    max_accumulator = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise NostrKeyError("invalid bech32 data")
        accumulator = ((accumulator << from_bits) | value) & max_accumulator
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((accumulator >> bits) & max_value)
    if pad:
        if bits:
            result.append((accumulator << (to_bits - bits)) & max_value)
    elif bits >= from_bits or ((accumulator << (to_bits - bits)) & max_value):
        raise NostrKeyError("invalid bech32 padding")
    return result


def bech32_encode(hrp: str, data: list[int]) -> str:
    """Encode 5-bit groups with a bech32 checksum."""
    combined = data + _create_checksum(hrp, data)
    return f"{hrp}1{''.join(_CHARSET[value] for value in combined)}"


def bech32_decode(value: str) -> tuple[str, list[int]]:
    """Decode a bech32 string into its human-readable part and 5-bit payload.

    Raises :class:`NostrKeyError` for mixed case, an invalid character, a bad checksum or
    a missing separator. The message never contains *value*.
    """
    if not value or len(value) > _MAX_BECH32_LENGTH:
        raise NostrKeyError("invalid bech32 key length")
    if any(character.isupper() for character in value) and any(
        character.islower() for character in value
    ):
        raise NostrKeyError("bech32 keys must not mix upper and lower case")
    lowered = value.lower()
    separator = lowered.rfind("1")
    if separator < 1 or separator + 7 > len(lowered):
        raise NostrKeyError("invalid bech32 key; the separator is missing")
    hrp = lowered[:separator]
    try:
        data = [_CHARSET_INDEX[character] for character in lowered[separator + 1 :]]
    except KeyError as exc:
        raise NostrKeyError("invalid bech32 key; unknown character") from exc
    if not _verify_checksum(hrp, data):
        raise NostrKeyError("invalid bech32 key; checksum mismatch")
    return hrp, data[:-6]


def _decode_key(value: str, expected_hrp: str) -> bytes:
    text = value.strip()
    if not text:
        raise NostrKeyError(f"missing {expected_hrp} key")
    if any(character.isspace() for character in text):
        raise NostrKeyError("keys must not contain whitespace")
    hrp, data = bech32_decode(text)
    if hrp != expected_hrp:
        raise NostrKeyError(f"expected an {expected_hrp} key, got an {hrp or 'unknown'} value")
    payload = bytes(_convert_bits(data, 5, 8, pad=False))
    if len(payload) != 32:
        raise NostrKeyError(f"invalid {expected_hrp} key length")
    return payload


def _validate_scalar(secret: bytes) -> None:
    scalar = int.from_bytes(secret, "big")
    if scalar <= 0 or scalar >= SECP256K1_ORDER:
        raise NostrKeyError("invalid nsec key; the scalar is out of range")


def decode_nsec(value: str) -> bytes:
    """Return the 32-byte secret from an ``nsec1...`` credential."""
    secret = _decode_key(value, NSEC_HRP)
    _validate_scalar(secret)
    return secret


def encode_nsec(secret: bytes) -> str:
    """Encode a 32-byte secret as ``nsec1...``. Used by tests and key generation."""
    if len(secret) != 32:
        raise NostrKeyError("nsec secrets must be 32 bytes")
    _validate_scalar(secret)
    data = _convert_bits(secret, 8, 5, pad=True)
    return bech32_encode(NSEC_HRP, data)


def encode_npub(pubkey: bytes) -> str:
    """Encode a 32-byte x-only public key as ``npub1...``."""
    if len(pubkey) != 32:
        raise NostrKeyError("npub public keys must be 32 bytes")
    data = _convert_bits(pubkey, 8, 5, pad=True)
    return bech32_encode(NPUB_HRP, data)


def decode_npub(value: str) -> bytes:
    """Return the 32-byte x-only public key from an ``npub1...`` identifier."""
    return _decode_key(value, NPUB_HRP)


def is_npub(value: str) -> bool:
    """Return ``True`` when *value* is a valid ``npub1...`` identifier."""
    try:
        decode_npub(value)
    except NostrKeyError:
        return False
    return True


def mask_npub(value: str) -> str:
    """Return a short public form such as ``npub1abcd…wxyz``.

    The npub is public, but listings keep it short so screenshots and logs do not carry a
    full account identifier by accident.
    """
    text = value.strip()
    if len(text) <= 20:
        return "npub1…"
    return f"{text[:10]}…{text[-4:]}"
