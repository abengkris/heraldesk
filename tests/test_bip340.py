"""BIP-340 known-answer and fallback tests."""

from __future__ import annotations

import pytest

from heraldesk.platforms import bip340

pytestmark = pytest.mark.core

# BIP-340 test vector 0.
VECTOR_SECRET = bytes.fromhex("00" * 31 + "03")
VECTOR_PUBKEY = bytes.fromhex("F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9")
VECTOR_MESSAGE = bytes(32)
VECTOR_SIGNATURE = bytes.fromhex(
    "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA8215"
    "25F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"
)

# The existing Nostr event vector, previously produced by coincurve.
EVENT_SECRET = bytes.fromhex("00" * 31 + "03")
EVENT_MESSAGE = bytes.fromhex("6dc484d6a2afe1ce12b6ab2038fc4e7477bed175eb25de44eb72e52ee9438b34")
EVENT_SIGNATURE = bytes.fromhex(
    "201fccebdc4c3c8d216be82502cdc5819c5a5a907895addb76cb83541b510476"
    "c41863f21a68e91065fe030df52c11565a3ab37f81ce2616bca0201324e2c3da"
)


def test_bip340_vector_zero() -> None:
    assert bip340.pubkey_gen(VECTOR_SECRET) == VECTOR_PUBKEY
    assert bip340.sign(VECTOR_MESSAGE, VECTOR_SECRET) == VECTOR_SIGNATURE
    assert bip340.verify(VECTOR_PUBKEY, VECTOR_MESSAGE, VECTOR_SIGNATURE)


def test_nostr_event_signature_matches_the_previous_backend() -> None:
    assert bip340.sign(EVENT_MESSAGE, EVENT_SECRET) == EVENT_SIGNATURE
    assert bip340.verify(bip340.pubkey_gen(EVENT_SECRET), EVENT_MESSAGE, EVENT_SIGNATURE)


def test_tampered_message_and_signature_fail() -> None:
    assert not bip340.verify(VECTOR_PUBKEY, bytes([1]) * 32, VECTOR_SIGNATURE)
    tampered = bytearray(VECTOR_SIGNATURE)
    tampered[-1] ^= 1
    assert not bip340.verify(VECTOR_PUBKEY, VECTOR_MESSAGE, bytes(tampered))


def test_invalid_lengths_and_scalars_are_rejected() -> None:
    with pytest.raises(ValueError):
        bip340.pubkey_gen(b"\x01" * 31)
    with pytest.raises(ValueError):
        bip340.pubkey_gen(bytes(32))
    with pytest.raises(ValueError):
        bip340.sign(b"\x00" * 31, VECTOR_SECRET)
    with pytest.raises(ValueError):
        bip340.sign(VECTOR_MESSAGE, bytes(32))
    assert not bip340.verify(b"\x02" * 32, VECTOR_MESSAGE, VECTOR_SIGNATURE)
    assert not bip340.verify(VECTOR_PUBKEY, VECTOR_MESSAGE, b"\x00" * 63)
