"""Nostr adapter: publish NIP-01 kind 1 text notes to relays.

Signing uses BIP-340 Schnorr. Python 3.12/3.13 prefer libsecp256k1 through ``coincurve``;
where that optional binding is unavailable (notably CPython 3.14) a small pure-Python
BIP-340 fallback is used. The private key is read from the encrypted account credential
(``nsec``) for the duration of one publish, never logged, never put in an event and never
returned in an error.

Relay policy: broadcast to every configured relay, accept the event when at least one
relay answers ``["OK", <id>, true, ...]``. One rejecting or timing-out relay does not fail
a publish that another relay accepted. Tests inject a fake relay client; this module only
opens sockets through :class:`WebSocketRelayClient`.

Nostr events are immutable and cannot be deleted. Use a throwaway key for experiments and
abandon it afterwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

try:
    from coincurve import PrivateKey, PublicKeyXOnly

    _HAS_COINCURVE = True
except ImportError:  # CPython 3.14 currently has no coincurve wheel
    _HAS_COINCURVE = False

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from heraldesk.config import DEFAULT_NOSTR_RELAYS, DEFAULT_NOSTR_TIMEOUT_SECONDS
from heraldesk.platforms import bip340
from heraldesk.platforms.base import (
    AccountRef,
    PlatformAdapter,
    PublishError,
    PublishRequest,
    PublishResult,
)
from heraldesk.platforms.nostr_keys import NostrKeyError, decode_nsec, encode_npub

KIND_TEXT_NOTE: Final = 1
MAX_HASHTAGS: Final = 10
MAX_RELAY_MESSAGE: Final = 200
MAX_ERROR_LENGTH: Final = 500
_MAX_RELAY_MESSAGE_SIZE: Final = 1024 * 1024

_HASHTAG_PATTERN: Final = re.compile(r"(?<![\w#])#([A-Za-z0-9_]{1,64})")


class NostrConfigError(PublishError):
    """Raised when a relay list or Nostr account configuration is unusable."""


@dataclass(frozen=True, slots=True)
class RelayAck:
    """One relay's answer to an ``EVENT`` message."""

    relay: str
    accepted: bool
    message: str = ""


@dataclass(frozen=True, slots=True)
class NostrEvent:
    """A signed NIP-01 event."""

    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: tuple[tuple[str, ...], ...]
    content: str
    sig: str

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON object sent inside ``["EVENT", ...]``."""
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": [list(tag) for tag in self.tags],
            "content": self.content,
            "sig": self.sig,
        }


class RelayClient(Protocol):
    """The relay transport the adapter needs. Realized by WebSocket or by a test fake."""

    async def publish(
        self,
        relay: str,
        event_json: str,
        event_id: str,
        *,
        timeout: float,
    ) -> RelayAck:
        """Send one event and return the relay's ``OK`` answer."""
        ...


def parse_relay_list(value: str | Sequence[str]) -> tuple[str, ...]:
    """Normalise and validate relay URLs.

    Accepts a comma/whitespace separated string or a sequence. Only ``wss://`` URLs are
    accepted; an empty list is an error rather than a silent fallback.
    """
    raw: list[str]
    if isinstance(value, str):
        raw = [part for part in re.split(r"[,\s]+", value.strip()) if part]
    else:
        raw = [part.strip() for part in value if part.strip()]

    relays: list[str] = []
    for relay in raw:
        parsed = urlsplit(relay)
        if parsed.scheme != "wss" or not parsed.hostname:
            raise NostrConfigError("invalid Nostr relay; use a wss:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise NostrConfigError("Nostr relay URLs must not contain credentials")
        normalised = relay.rstrip("/")
        if normalised not in relays:
            relays.append(normalised)
    if not relays:
        raise NostrConfigError("no Nostr relays are configured")
    return tuple(relays)


def _safe_relay(relay: str) -> str:
    parsed = urlsplit(relay.strip())
    return parsed.hostname or "invalid-relay"


def _short(value: str, limit: int = MAX_RELAY_MESSAGE) -> str:
    text = value.replace("\n", " ").strip()
    if len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def hashtags_in(content: str) -> tuple[str, ...]:
    """Return unique lowercased hashtags in order of appearance, at most ten."""
    seen: list[str] = []
    for match in _HASHTAG_PATTERN.finditer(content):
        tag = match.group(1).lower()
        if tag not in seen:
            seen.append(tag)
        if len(seen) >= MAX_HASHTAGS:
            break
    return tuple(seen)


def event_id_for(
    pubkey: str,
    created_at: int,
    kind: int,
    tags: Sequence[Sequence[str]],
    content: str,
) -> str:
    """Compute the NIP-01 event id: SHA-256 of the canonical serialization."""
    serialized = json.dumps(
        [0, pubkey, created_at, kind, [list(tag) for tag in tags], content],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_event(secret: bytes, content: str, created_at: datetime) -> NostrEvent:
    """Build and sign one kind 1 text note.

    The signature is deterministic: BIP-340 allows a 32-byte zero auxiliary value, so the
    same key, time and content always produce the same event id and signature.
    """
    pubkey = _public_key(secret).hex()
    timestamp = int(_as_utc(created_at).timestamp())
    tags = tuple(("t", tag) for tag in hashtags_in(content))
    identifier = event_id_for(pubkey, timestamp, KIND_TEXT_NOTE, tags, content)
    try:
        signature = _sign(secret, bytes.fromhex(identifier))
    except ValueError as exc:
        raise NostrKeyError("cannot sign the Nostr event with this nsec key") from exc
    return NostrEvent(
        id=identifier,
        pubkey=pubkey,
        created_at=timestamp,
        kind=KIND_TEXT_NOTE,
        tags=tags,
        content=content,
        sig=signature.hex(),
    )


def _public_key(secret: bytes) -> bytes:
    if _HAS_COINCURVE:
        return PublicKeyXOnly.from_secret(secret).format()
    return bip340.pubkey_gen(secret)


def _sign(secret: bytes, message: bytes) -> bytes:
    if _HAS_COINCURVE:
        return PrivateKey(secret).sign_schnorr(message, bytes(32))
    return bip340.sign(message, secret)


def _verify(pubkey: bytes, signature: bytes, message: bytes) -> bool:
    if _HAS_COINCURVE:
        return PublicKeyXOnly(pubkey).verify(signature, message)
    return bip340.verify(pubkey, message, signature)


def verify_event(event: NostrEvent) -> bool:
    """Return ``True`` when the id matches the event fields and the signature is valid."""
    computed = event_id_for(
        event.pubkey,
        event.created_at,
        event.kind,
        event.tags,
        event.content,
    )
    if computed != event.id:
        return False
    try:
        return _verify(
            bytes.fromhex(event.pubkey),
            bytes.fromhex(event.sig),
            bytes.fromhex(event.id),
        )
    except ValueError:
        return False


def npub_from_secret(secret: bytes) -> str:
    """Return the public ``npub1...`` identifier for a 32-byte secret."""
    return encode_npub(_public_key(secret))


class WebSocketRelayClient:
    """Minimal ``EVENT`` publisher for one relay over an async WebSocket connection."""

    async def publish(
        self,
        relay: str,
        event_json: str,
        event_id: str,
        *,
        timeout: float,
    ) -> RelayAck:
        """Send the event and wait for the matching ``OK`` message until *timeout*."""
        try:
            async with asyncio.timeout(timeout):
                async with connect(
                    relay,
                    open_timeout=timeout,
                    close_timeout=min(timeout, 2.0),
                    ping_interval=None,
                    max_size=_MAX_RELAY_MESSAGE_SIZE,
                ) as socket:
                    await socket.send(event_json)
                    while True:
                        raw = await socket.recv()
                        message = _parse_relay_message(raw)
                        if message is None:
                            continue
                        if message[0] != "OK" or len(message) < 3 or message[1] != event_id:
                            continue
                        detail = str(message[3]) if len(message) > 3 else ""
                        return RelayAck(relay=relay, accepted=bool(message[2]), message=detail)
        except TimeoutError:
            raise
        except WebSocketException as exc:
            raise NostrConfigError(f"relay connection failed: {type(exc).__name__}") from exc


def _parse_relay_message(raw: str | bytes) -> list[Any] | None:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    try:
        message = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(message, list) or not message:
        return None
    return message


class NostrAdapter:
    """Publish kind 1 notes to Nostr relays. No credential ever leaves this object."""

    platform = "nostr"

    def __init__(
        self,
        relays: str | Sequence[str] | None = None,
        *,
        timeout_seconds: float = DEFAULT_NOSTR_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] | None = None,
        relay_client: RelayClient | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._default_relays = parse_relay_list(DEFAULT_NOSTR_RELAYS if relays is None else relays)
        self._timeout_seconds = timeout_seconds
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._relay_client: RelayClient = (
            relay_client if relay_client is not None else WebSocketRelayClient()
        )

    @property
    def relays(self) -> tuple[str, ...]:
        """Relays used when the account does not carry its own list."""
        return self._default_relays

    def _secret(self, account: AccountRef) -> bytes:
        return decode_nsec(account.credentials.get("nsec", ""))

    def _relays_for(self, account: AccountRef) -> tuple[str, ...]:
        override = account.credentials.get("relays", "").strip()
        return parse_relay_list(override) if override else self._default_relays

    async def validate_credentials(self, account: AccountRef) -> bool:
        """Validate the nsec and relay list offline. Never connects to a relay."""
        try:
            self._secret(account)
            self._relays_for(account)
        except (NostrKeyError, PublishError):
            return False
        return True

    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult:
        """Sign one kind 1 event and accept it when at least one relay answers OK."""
        try:
            secret = self._secret(account)
            relays = self._relays_for(account)
        except (NostrKeyError, PublishError) as exc:
            raise PublishError(f"Nostr account cannot publish: {_short(str(exc))}") from exc

        event = build_event(secret, request.body, self._clock())
        event_json = json.dumps(
            ["EVENT", event.as_dict()],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        acknowledgements = await asyncio.gather(
            *(self._send(relay, event_json, event.id) for relay in relays)
        )
        accepted = [ack for ack in acknowledgements if ack.accepted]
        if not accepted:
            details = "; ".join(
                f"{_safe_relay(ack.relay)}: {_short(ack.message) or 'rejected'}"
                for ack in acknowledgements
            )
            raise PublishError(
                f"no relay accepted Nostr event {event.id}: {details}"[:MAX_ERROR_LENGTH]
            )
        return PublishResult(external_id=event.id, url=f"https://njump.me/{event.id}")

    async def _send(self, relay: str, event_json: str, event_id: str) -> RelayAck:
        try:
            ack = await self._relay_client.publish(
                relay,
                event_json,
                event_id,
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return RelayAck(relay=relay, accepted=False, message="timeout")
        except Exception as exc:  # one relay must never abort the others
            message = _short(str(exc)) or type(exc).__name__
            return RelayAck(relay=relay, accepted=False, message=message)
        return ack


def adapter() -> PlatformAdapter:
    """Return a Nostr adapter with the built-in relay defaults."""
    return NostrAdapter()
