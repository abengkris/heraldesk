"""Platform adapter registry.

``fake`` and ``nostr`` implement publishing. ``nostr`` signs NIP-01 kind 1 notes with the
account's encrypted nsec and broadcasts them to relays. Every other platform stays
:class:`NotSupportedError` so no unverified API call can happen; the reason is documented
in ``docs/platforms/README.md``.

``adapter_for`` is the offline account-check path: Nostr validates the key and relay list
locally, every other platform keeps the fake adapter.
"""

from __future__ import annotations

from collections.abc import Callable

from heraldesk.config import ConfigError, load_config
from heraldesk.platforms.base import (
    KNOWN_PLATFORMS,
    AccountRef,
    NotSupportedError,
    PlatformAdapter,
    PublishError,
    PublishRequest,
    PublishResult,
)
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.platforms.nostr import NostrAdapter

__all__ = [
    "KNOWN_PLATFORMS",
    "AccountRef",
    "PlatformAdapter",
    "PublishError",
    "PublishRequest",
    "PublishResult",
    "adapter_for",
    "publish_adapter_for",
]


def _nostr_adapter() -> PlatformAdapter:
    """Build a Nostr adapter from ``heraldesk.yaml`` relay defaults."""
    try:
        settings = load_config().nostr
    except ConfigError as exc:
        raise PublishError(f"cannot load the Nostr relay configuration: {exc}") from exc
    return NostrAdapter(relays=settings.relays, timeout_seconds=settings.timeout_seconds)


_PUBLISH_ADAPTERS: dict[str, Callable[[], PlatformAdapter]] = {
    "fake": FakeAdapter,
    "nostr": _nostr_adapter,
}


def adapter_for(platform: str) -> PlatformAdapter:
    """Return the adapter used for an offline account check.

    Nostr validates the nsec and relay list without network I/O. Every other platform uses
    :class:`FakeAdapter`; the CLI labels the result as a fake-adapter check.
    """
    if platform.strip().lower() == "nostr":
        return NostrAdapter()
    return FakeAdapter()


def publish_adapter_for(platform: str) -> PlatformAdapter:
    """Return the publisher adapter for *platform* or raise :class:`NotSupportedError`."""
    key = platform.strip().lower()
    factory = _PUBLISH_ADAPTERS.get(key)
    if factory is None:
        raise NotSupportedError(
            f"no publisher adapter for platform '{key}'; "
            "see docs/platforms/README.md for the documented reason"
        )
    return factory()
