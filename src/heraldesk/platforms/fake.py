"""Offline fake adapter used for ``heraldesk account test`` until M5.

It performs no network I/O. Credentials are accepted when the mapping contains a
non-empty ``token``. Real adapters arrive with the publisher milestone.
"""

from __future__ import annotations

from heraldesk.platforms.base import AccountRef


class FakeAdapter:
    """Deterministic in-process platform adapter for tests and M1 account checks."""

    platform = "fake"

    async def validate_credentials(self, account: AccountRef) -> bool:
        """Accept a non-empty token and ignore every other credential."""
        return bool(account.credentials.get("token", "").strip())
