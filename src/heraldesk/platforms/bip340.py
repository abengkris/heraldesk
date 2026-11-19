"""Pure-Python BIP-340 Schnorr signatures for secp256k1.

This is the fallback used when the optional ``coincurve`` binding is unavailable (notably
on CPython 3.14, where coincurve has no wheel). It implements the BIP-340 reference
algorithm with the standard library only. The implementation is intentionally small and
well-tested against the published test vectors and the existing Nostr event vectors.

It is not constant-time. Python arithmetic cannot guarantee side-channel resistance, so
the preferred path on Python 3.12/3.13 remains ``coincurve``/libsecp256k1. On 3.14 the
fallback keeps Nostr usable; do not use it as a general-purpose signing service.
"""

from __future__ import annotations

import hashlib
from typing import Final

FIELD_PRIME: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
GROUP_ORDER: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GENERATOR_X: Final = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GENERATOR_Y: Final = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

Point = tuple[int, int] | None
GENERATOR: Final = (GENERATOR_X, GENERATOR_Y)


def _tagged_hash(tag: str, message: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode("ascii")).digest()
    return hashlib.sha256(tag_hash + tag_hash + message).digest()


def _point_add(left: Point, right: Point) -> Point:
    if left is None:
        return right
    if right is None:
        return left
    x1, y1 = left
    x2, y2 = right
    if x1 == x2:
        if (y1 + y2) % FIELD_PRIME == 0:
            return None
        slope = (3 * x1 * x1) * pow(2 * y1, FIELD_PRIME - 2, FIELD_PRIME)
    else:
        slope = (y2 - y1) * pow(x2 - x1, FIELD_PRIME - 2, FIELD_PRIME)
    slope %= FIELD_PRIME
    x3 = (slope * slope - x1 - x2) % FIELD_PRIME
    y3 = (slope * (x1 - x3) - y1) % FIELD_PRIME
    return x3, y3


def _point_mul(scalar: int, point: Point = GENERATOR) -> Point:
    if scalar % GROUP_ORDER == 0 or point is None:
        return None
    result: Point = None
    addend: Point = point
    value = scalar % GROUP_ORDER
    while value:
        if value & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        value >>= 1
    return result


def _lift_x(x: int) -> Point:
    if x >= FIELD_PRIME:
        return None
    y_squared = (pow(x, 3, FIELD_PRIME) + 7) % FIELD_PRIME
    y = pow(y_squared, (FIELD_PRIME + 1) // 4, FIELD_PRIME)
    if (y * y) % FIELD_PRIME != y_squared:
        return None
    return x, y if y % 2 == 0 else FIELD_PRIME - y


def pubkey_gen(seckey: bytes) -> bytes:
    """Return the 32-byte x-only public key for a 32-byte secret."""
    if len(seckey) != 32:
        raise ValueError("secret key must be 32 bytes")
    scalar = int.from_bytes(seckey, "big")
    if not 1 <= scalar < GROUP_ORDER:
        raise ValueError("secret key is out of range")
    point = _point_mul(scalar)
    if point is None:
        raise ValueError("secret key produced the point at infinity")
    return point[0].to_bytes(32, "big")


def sign(message: bytes, seckey: bytes, aux: bytes = b"\x00" * 32) -> bytes:
    """Sign a 32-byte message with BIP-340 and return a 64-byte signature."""
    if len(message) != 32:
        raise ValueError("message must be 32 bytes")
    if len(seckey) != 32:
        raise ValueError("secret key must be 32 bytes")
    if len(aux) != 32:
        raise ValueError("auxiliary randomness must be 32 bytes")
    scalar = int.from_bytes(seckey, "big")
    if not 1 <= scalar < GROUP_ORDER:
        raise ValueError("secret key is out of range")

    point = _point_mul(scalar)
    if point is None:
        raise ValueError("secret key produced the point at infinity")
    px, py = point
    if py % 2 != 0:
        scalar = GROUP_ORDER - scalar

    scalar_bytes = scalar.to_bytes(32, "big")
    aux_hash = _tagged_hash("BIP0340/aux", aux)
    masked = bytes(a ^ b for a, b in zip(scalar_bytes, aux_hash, strict=True))
    nonce = (
        int.from_bytes(
            _tagged_hash(
                "BIP0340/nonce",
                masked + px.to_bytes(32, "big") + message,
            ),
            "big",
        )
        % GROUP_ORDER
    )
    if nonce == 0:
        raise ValueError("nonce is zero")
    nonce_point = _point_mul(nonce)
    if nonce_point is None:
        raise ValueError("nonce produced the point at infinity")
    rx, ry = nonce_point
    if ry % 2 != 0:
        nonce = GROUP_ORDER - nonce

    challenge = (
        int.from_bytes(
            _tagged_hash(
                "BIP0340/challenge",
                rx.to_bytes(32, "big") + px.to_bytes(32, "big") + message,
            ),
            "big",
        )
        % GROUP_ORDER
    )
    signature = rx.to_bytes(32, "big") + ((nonce + challenge * scalar) % GROUP_ORDER).to_bytes(
        32, "big"
    )
    if not verify(pubkey_gen(seckey), message, signature):
        raise ValueError("produced an invalid signature")
    return signature


def verify(pubkey: bytes, message: bytes, signature: bytes) -> bool:
    """Return ``True`` when *signature* is valid for *pubkey* and *message*."""
    if len(pubkey) != 32 or len(message) != 32 or len(signature) != 64:
        return False
    point = _lift_x(int.from_bytes(pubkey, "big"))
    if point is None:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if r >= FIELD_PRIME or s >= GROUP_ORDER:
        return False
    challenge = (
        int.from_bytes(
            _tagged_hash("BIP0340/challenge", signature[:32] + pubkey + message),
            "big",
        )
        % GROUP_ORDER
    )
    candidate = _point_add(_point_mul(s), _point_mul(GROUP_ORDER - challenge, point))
    if candidate is None:
        return False
    x, y = candidate
    return y % 2 == 0 and x == r
