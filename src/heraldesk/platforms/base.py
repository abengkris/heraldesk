"""Platform adapter contract.

M1 needs credential validation so that ``heraldesk account test`` can run offline.
M5 adds publishing through :meth:`PlatformAdapter.publish`. Adapters never contain
business logic and never receive credentials outside the adapter call.

M2 adds provisional per-platform post limits used when building Writer prompts. Real
adapters own the authoritative values from M5 onwards.

M5 ships only the fake adapter. Threads, Nostr, X, Facebook, Instagram and LinkedIn
return :class:`NotSupportedError` with a documented reason: the publisher deliberately
does not guess at platform APIs that cannot be verified offline. See
``docs/platforms/README.md``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

KNOWN_PLATFORMS = frozenset({"fake", "threads", "nostr", "x", "facebook", "instagram", "linkedin"})


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Decrypted account data handed to an adapter for one call."""

    platform: str
    handle: str
    credentials: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """One post handed to an adapter.

    ``idempotency_key`` is Heraldesk's own stable key for the content item. Adapters use
    it when the platform supports idempotency, and tests use it to prove that a retry
    cannot double-post.
    """

    content_item_id: int
    platform: str
    body: str
    idempotency_key: str
    media: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PublishResult:
    """What the platform returned after accepting a post."""

    external_id: str
    url: str | None = None


class PublishError(Exception):
    """Raised when a platform rejects or fails a publish call.

    The publisher redacts the message before it reaches the database, the audit log or
    the CLI. Adapters must never include credentials in the message.
    """


class NotSupportedError(PublishError):
    """Raised when an adapter cannot publish on its platform.

    M5 uses this for every platform whose real adapter is not implemented yet, and for
    platform features that require app review. It is never a code failure.
    """


class PlatformAdapter(Protocol):
    """The subset of the platform contract Heraldesk needs in M1 and M5."""

    platform: str

    async def validate_credentials(self, account: AccountRef) -> bool:
        """Return ``True`` when the platform accepts the supplied credentials."""
        ...

    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult:
        """Publish one item and return the platform's external id."""
        ...


@dataclass(frozen=True, slots=True)
class PlatformConstraints:
    """What one platform accepts in a single post.

    These values are provisional metadata for prompt building, not publishing logic. Each
    real adapter replaces them with its own ``constraints()`` in a later milestone.
    """

    platform: str
    max_characters: int
    max_hashtags: int = 10
    notes: str = ""


PROVISIONAL_CONSTRAINTS: dict[str, PlatformConstraints] = {
    "fake": PlatformConstraints(
        platform="fake",
        max_characters=5000,
        max_hashtags=10,
        notes="Offline test adapter. Limits are arbitrary and stable for tests.",
    ),
    "x": PlatformConstraints(
        platform="x",
        max_characters=280,
        max_hashtags=5,
        notes="One post. Links count against the limit.",
    ),
    "threads": PlatformConstraints(
        platform="threads",
        max_characters=500,
        max_hashtags=5,
        notes="One post, text plus optional link.",
    ),
    "nostr": PlatformConstraints(
        platform="nostr",
        max_characters=8000,
        max_hashtags=10,
        notes="Relay limits vary; keep notes short and self-contained.",
    ),
    "facebook": PlatformConstraints(
        platform="facebook",
        max_characters=63206,
        max_hashtags=10,
        notes="Page post. Hashtags add little reach.",
    ),
    "instagram": PlatformConstraints(
        platform="instagram",
        max_characters=2200,
        max_hashtags=30,
        notes="Caption only. Media is required by the platform itself.",
    ),
    "linkedin": PlatformConstraints(
        platform="linkedin",
        max_characters=3000,
        max_hashtags=5,
        notes="Professional tone; links usually reduce reach.",
    ),
}


def constraints_for(platform: str) -> PlatformConstraints | None:
    """Return the provisional constraints for *platform*, or ``None`` when unknown."""
    return PROVISIONAL_CONSTRAINTS.get(platform.strip().lower())
