"""Nostr adapter tests: keys, events, signing, relays, timeouts and redaction.

No test opens a socket. The relay client is always injected, and async adapter calls run
through ``asyncio.run`` like the rest of the suite.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from heraldesk.config import ConfigError, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.redact import SecretRegistry, looks_like_secret_name
from heraldesk.platforms import bip340, publish_adapter_for
from heraldesk.platforms.base import AccountRef, PublishError, PublishRequest
from heraldesk.platforms.nostr import (
    NostrAdapter,
    RelayAck,
    build_event,
    npub_from_secret,
    parse_relay_list,
    verify_event,
)
from heraldesk.platforms.nostr_keys import (
    NostrKeyError,
    decode_nsec,
    encode_npub,
    encode_nsec,
    is_npub,
    mask_npub,
)

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
SECRET = bytes.fromhex("00" * 31 + "03")
NSEC = encode_nsec(SECRET)
NPUB = npub_from_secret(SECRET)
# Reference values produced by the NIP-01 serialization and BIP-340 signing rules.
EVENT_ID = "6dc484d6a2afe1ce12b6ab2038fc4e7477bed175eb25de44eb72e52ee9438b34"
SIGNATURE = (
    "201fccebdc4c3c8d216be82502cdc5819c5a5a907895addb76cb83541b510476"
    "c41863f21a68e91065fe030df52c11565a3ab37f81ce2616bca0201324e2c3da"
)
BODY = "Hello Nostr"


class FakeRelayClient:
    """In-memory relay transport. Outcomes are per relay and deterministic."""

    def __init__(self, outcomes: dict[str, Any] | None = None, default: Any = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = outcomes or {}
        self._default = default

    async def publish(
        self,
        relay: str,
        event_json: str,
        event_id: str,
        *,
        timeout: float,
    ) -> RelayAck:
        self.calls.append(
            {"relay": relay, "event_json": event_json, "event_id": event_id, "timeout": timeout}
        )
        outcome = self._outcomes.get(relay, self._default)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is True:
            return RelayAck(relay=relay, accepted=True, message="")
        if outcome is False:
            return RelayAck(relay=relay, accepted=False, message="blocked: not allowed")
        return outcome


def account(nsec: str = NSEC, **credentials: str) -> AccountRef:
    return AccountRef(
        platform="nostr",
        handle=NPUB,
        credentials={"nsec": nsec, **credentials},
    )


def request(body: str = BODY) -> PublishRequest:
    return PublishRequest(
        content_item_id=1,
        platform="nostr",
        body=body,
        idempotency_key="heraldesk:content:1",
    )


def adapter(client: FakeRelayClient, relays: tuple[str, ...] | None = None) -> NostrAdapter:
    return NostrAdapter(
        relays=relays or ("wss://relay.one", "wss://relay.two"),
        clock=lambda: NOW,
        relay_client=client,
    )


def test_nsec_round_trip_and_npub_derivation() -> None:
    assert decode_nsec(NSEC) == SECRET
    assert npub_from_secret(SECRET) == NPUB
    assert is_npub(NPUB)
    assert encode_npub(bip340.pubkey_gen(SECRET)) == NPUB
    assert mask_npub(NPUB) == f"{NPUB[:10]}…{NPUB[-4:]}"
    assert "nsec" not in mask_npub(NPUB)


def test_npub_is_not_accepted_as_an_nsec_and_errors_do_not_echo_it() -> None:
    with pytest.raises(NostrKeyError) as excinfo:
        decode_nsec(NPUB)
    assert NPUB not in str(excinfo.value)
    assert "nsec" in str(excinfo.value)


def test_invalid_nsec_messages_never_contain_the_value() -> None:
    bad = "nsec1" + "q" * 58
    with pytest.raises(NostrKeyError) as excinfo:
        decode_nsec(bad)
    assert bad not in str(excinfo.value)


def test_event_matches_reference_id_and_signature() -> None:
    event = build_event(SECRET, BODY, NOW)
    assert event.id == EVENT_ID
    assert event.sig == SIGNATURE
    assert event.pubkey == bip340.pubkey_gen(SECRET).hex()
    assert event.kind == 1
    assert event.created_at == int(NOW.timestamp())
    assert event.tags == ()
    assert verify_event(event)


def test_tampered_event_fails_verification() -> None:
    event = build_event(SECRET, BODY, NOW)
    assert not verify_event(replace(event, content="tampered"))


def test_hashtags_become_t_tags() -> None:
    event = build_event(SECRET, "Launch #Build #build #Nostr2026 #a #b #c #d #e #f #g #h #i", NOW)
    tags = [tag[1] for tag in event.tags]
    assert tags[0] == "build"
    assert tags[1] == "nostr2026"
    assert len(tags) == 10
    assert len(set(tags)) == len(tags)
    assert all(tag[0] == "t" for tag in event.tags)


def test_parse_relay_list_normalises_and_rejects_bad_urls() -> None:
    assert parse_relay_list("wss://a.example, wss://b.example/") == (
        "wss://a.example",
        "wss://b.example",
    )
    assert parse_relay_list(["wss://a.example", "wss://a.example"]) == ("wss://a.example",)
    with pytest.raises(PublishError, match="wss://"):
        parse_relay_list("https://relay.example")
    with pytest.raises(PublishError, match="no Nostr relays"):
        parse_relay_list("")


def test_publish_succeeds_when_one_relay_accepts() -> None:
    client = FakeRelayClient({"wss://relay.two": False})
    result = asyncio.run(adapter(client).publish(account(), request()))

    assert result.external_id == EVENT_ID
    assert result.url == f"https://njump.me/{EVENT_ID}"
    assert {call["relay"] for call in client.calls} == {"wss://relay.one", "wss://relay.two"}
    payload = json.loads(client.calls[0]["event_json"])
    assert payload[0] == "EVENT"
    assert payload[1]["id"] == EVENT_ID
    assert payload[1]["kind"] == 1
    assert payload[1]["content"] == BODY
    assert "nsec" not in client.calls[0]["event_json"]


def test_publish_ignores_a_not_ok_from_one_relay() -> None:
    client = FakeRelayClient(
        {
            "wss://relay.one": RelayAck("wss://relay.one", False, "blocked: spam"),
            "wss://relay.two": RelayAck("wss://relay.two", True, ""),
        }
    )
    result = asyncio.run(adapter(client).publish(account(), request()))
    assert result.external_id == EVENT_ID


def test_all_relays_rejecting_raises_a_redacted_error() -> None:
    client = FakeRelayClient(default=False)
    with pytest.raises(PublishError) as excinfo:
        asyncio.run(adapter(client).publish(account(), request()))
    message = str(excinfo.value)
    assert "no relay accepted" in message
    assert "relay.one" in message
    assert NSEC not in message


def test_relay_timeout_is_per_relay() -> None:
    client = FakeRelayClient({"wss://relay.one": TimeoutError()})
    result = asyncio.run(adapter(client).publish(account(), request()))
    assert result.external_id == EVENT_ID
    assert all(call["timeout"] == 10.0 for call in client.calls)


def test_all_relays_timing_out_raises() -> None:
    client = FakeRelayClient(default=TimeoutError())
    with pytest.raises(PublishError, match="timeout"):
        asyncio.run(adapter(client).publish(account(), request()))


def test_account_relay_override_wins_over_adapter_defaults() -> None:
    client = FakeRelayClient()
    asyncio.run(adapter(client).publish(account(relays="wss://override.example"), request()))
    assert [call["relay"] for call in client.calls] == ["wss://override.example"]


def test_account_validation_is_offline_and_rejects_bad_keys() -> None:
    client = FakeRelayClient()
    instance = adapter(client)
    assert asyncio.run(instance.validate_credentials(account())) is True
    assert asyncio.run(instance.validate_credentials(account(nsec=NPUB))) is False
    assert asyncio.run(instance.validate_credentials(AccountRef("nostr", NPUB, {}))) is False
    assert client.calls == []


def test_bad_relay_override_never_echoes_the_nsec() -> None:
    with pytest.raises(PublishError) as excinfo:
        asyncio.run(
            adapter(FakeRelayClient()).publish(account(relays="https://not-wss.example"), request())
        )
    assert NSEC not in str(excinfo.value)


def test_account_repr_hides_credentials() -> None:
    account_ref = account(relays="wss://relay.one")
    assert NSEC not in repr(account_ref)
    assert "relay.one" not in repr(account_ref)


def test_secret_registry_redacts_nsec_and_npub() -> None:
    assert looks_like_secret_name("NOSTR_NSEC_THROWAY")
    registry = SecretRegistry.from_environment({"NOSTR_NSEC_THROWAY": NSEC})
    text = registry.redact(f"key={NSEC} npub={NPUB}")

    assert NSEC not in text
    assert NPUB not in text
    assert "[REDACTED]" in text


def test_relay_defaults_come_from_heraldesk_yaml(initialized: InitResult) -> None:
    initialized.layout.config_path.write_text(
        "version: 1\nnostr:\n  relays:\n    - wss://custom.example\n  timeout_seconds: 3\n",
        encoding="utf-8",
    )
    config = load_config(initialized.layout.home)
    assert config.nostr.relays == ["wss://custom.example"]
    assert config.nostr.timeout_seconds == 3.0

    instance = publish_adapter_for("nostr")
    assert isinstance(instance, NostrAdapter)
    assert instance.relays == ("wss://custom.example",)


def test_invalid_relay_config_is_rejected(initialized: InitResult) -> None:
    initialized.layout.config_path.write_text(
        "version: 1\nnostr:\n  relays:\n    - http://relay.example\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="wss://"):
        load_config(initialized.layout.home)


def test_adapter_defaults_are_the_two_documented_relays() -> None:
    instance = NostrAdapter(relay_client=FakeRelayClient())
    assert instance.relays == ("wss://relay.damus.io", "wss://nos.lol")


def test_pure_python_signing_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("heraldesk.platforms.nostr._HAS_COINCURVE", False)

    event = build_event(SECRET, BODY, NOW)

    assert event.sig == SIGNATURE
    assert verify_event(event)
    assert npub_from_secret(SECRET) == NPUB
