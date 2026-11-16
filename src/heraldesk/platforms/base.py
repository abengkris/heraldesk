"""Platform adapter contract.

M1 only needs credential validation so that ``heraldesk account test`` can run offline.
Publishing, interaction fetching and metrics are added in M5. Adapters never contain
business logic and never receive credentials outside the adapter call.
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
