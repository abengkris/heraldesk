"""Platform adapter contract.

M1 only needs credential validation so that ``heraldesk account test`` can run offline.
Publishing, interaction fetching and metrics are added in M5. Adapters never contain
business logic and never receive credentials outside the adapter call.

M2 adds provisional per-platform post limits used when building Writer prompts. Real
adapters own the authoritative values from M5 onwards.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

KNOWN_PLATFORMS = frozenset({"fake", "threads", "nostr", "x", "facebook", "instagram", "linkedin"})


@dataclass(frozen=True, slots=True)
class AccountRef:
    """Decrypted account data handed to an adapter for one call."""

    platform: str
    handle: str
    credentials: Mapping[str, str]


class PlatformAdapter(Protocol):
    """The M1 subset of the platform adapter protocol."""

    platform: str

    async def validate_credentials(self, account: AccountRef) -> bool:
        """Return ``True`` when the platform accepts the supplied credentials."""
        ...


@dataclass(frozen=True, slots=True)
class PlatformConstraints:
    """What one platform accepts in a single post.

    These values are provisional metadata for prompt building, not publishing logic. Each
    real adapter replaces them with its own ``constraints()`` in M5.
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
