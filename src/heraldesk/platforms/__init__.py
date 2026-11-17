"""Platform adapter registry.

Only the fake adapter is implemented in M1-M5. ``account test`` exercises the fake adapter
for every platform until real adapters land. The publisher treats every other platform as
:class:`NotSupportedError` so no unverified API call can happen, and the reason is
documented in ``docs/platforms/README.md``.
"""

from __future__ import annotations

from collections.abc import Callable

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

_PUBLISH_ADAPTERS: dict[str, Callable[[], PlatformAdapter]] = {"fake": FakeAdapter}


def adapter_for(platform: str) -> PlatformAdapter:
    """Return the adapter used for an account check.

    M1-M5 always returns :class:`FakeAdapter`, even for platforms whose real adapter is
    not implemented yet. The CLI labels the result as a fake-adapter check.
    """
    _ = platform
    return FakeAdapter()


def publish_adapter_for(platform: str) -> PlatformAdapter:
    """Return the publisher adapter for *platform* or raise :class:`NotSupportedError`."""
    key = platform.strip().lower()
    factory = _PUBLISH_ADAPTERS.get(key)
    if factory is None:
        raise NotSupportedError(
            f"no publisher adapter for platform '{key}' in M5; "
            "see docs/platforms/README.md for the documented reason"
        )
    return factory()
