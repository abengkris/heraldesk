"""Minimal Heraldesk adapter plugin. Replace this with an official platform API."""

from __future__ import annotations

from heraldesk.platforms.base import AccountRef, PublishRequest, PublishResult


class ExampleAdapter:
    """A deterministic adapter used to demonstrate the plugin contract."""

    platform = "example"

    async def validate_credentials(self, account: AccountRef) -> bool:
        """Accept any non-empty token. Real plugins should call the official API."""
        return bool(account.credentials.get("token", "").strip())

    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult:
        """Return a stable fake id. Real plugins must import the platform's SDK/API."""
        _ = account
        return PublishResult(external_id=f"example-{request.content_item_id}")
