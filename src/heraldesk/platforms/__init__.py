"""Platform adapter registry.

``fake``, ``threads`` and ``nostr`` implement publishing. ``threads`` uses the official
Meta Threads Graph API; ``nostr`` signs NIP-01 kind 1 notes with the account's encrypted
nsec and broadcasts them to relays. Every other platform stays
:class:`NotSupportedError` unless an installed plugin provides it through the
``heraldesk.platforms`` entry-point group; the reason is documented in
``docs/platforms/README.md``.

``adapter_for`` is the account-check path: Nostr validates the key and relay list
locally, Threads calls ``/me`` with the stored token, every other platform keeps the fake
adapter. Plugins are used for publishing only, so ``account test`` never imports
third-party code.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

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
from heraldesk.platforms.threads import ThreadsAdapter

if TYPE_CHECKING:
    from heraldesk.plugins import PluginAdapter

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
    "threads": ThreadsAdapter,
    "nostr": _nostr_adapter,
}


def _plugin_adapter(key: str) -> PluginAdapter | None:
    """Return a discovered plugin for *key*, importing ``heraldesk.plugins`` lazily.

    The lazy import keeps ``heraldesk.plugins`` from importing this package first and
    creating a cycle through ``platforms.base``.
    """
    from heraldesk.plugins import plugin_adapters

    return plugin_adapters().get(key)


def adapter_for(platform: str) -> PlatformAdapter:
    """Return the adapter used for an offline account check.

    Nostr validates the nsec and relay list without network I/O. Every other platform uses
    :class:`FakeAdapter`; the CLI labels the result as a fake-adapter check.
    """
    key = platform.strip().lower()
    if key == "nostr":
        return NostrAdapter()
    if key == "threads":
        return ThreadsAdapter()
    return FakeAdapter()


def publish_adapter_for(platform: str) -> PlatformAdapter:
    """Return the publisher adapter for *platform* or raise :class:`NotSupportedError`.

    Built-in adapters take precedence. An installed plugin supplies any other platform
    through the ``heraldesk.platforms`` entry-point group. A plugin that cannot be
    imported raises :class:`PluginError`, which is a :class:`PublishError`.
    """
    key = platform.strip().lower()
    factory = _PUBLISH_ADAPTERS.get(key)
    if factory is not None:
        return factory()
    plugin = _plugin_adapter(key)
    if plugin is not None:
        return plugin.factory()
    raise NotSupportedError(
        f"no publisher adapter for platform '{key}'; "
        "install a plugin or see docs/platforms/README.md for the documented reason"
    )
