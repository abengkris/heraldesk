"""Platform adapter registry.

Only the fake adapter is implemented in M1. ``account test`` deliberately exercises the
fake adapter for every platform until the real adapters land in M5.
"""

from __future__ import annotations

from heraldesk.platforms.base import KNOWN_PLATFORMS, AccountRef, PlatformAdapter
from heraldesk.platforms.fake import FakeAdapter

__all__ = ["KNOWN_PLATFORMS", "AccountRef", "PlatformAdapter", "adapter_for"]


def adapter_for(platform: str) -> PlatformAdapter:
    """Return the adapter used for an account check.

    M1 always returns :class:`FakeAdapter`, even for platforms whose real adapter is not
    implemented yet. The CLI labels the result as a fake-adapter check.
    """
    _ = platform
    return FakeAdapter()
