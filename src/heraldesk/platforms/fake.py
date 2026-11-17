"""Offline fake adapter used by the publisher and by offline account checks.

It performs no network I/O. Credentials are accepted when the mapping contains a
non-empty ``token`` for account checks; publishing ignores credentials entirely so the
M3 fallback can exercise the publisher without creating a social account row.

``fail_times`` lets tests simulate provider outages deterministically. The adapter keeps
an in-memory idempotency map so a retry with the same key returns the first result
instead of posting twice.
"""

from __future__ import annotations

from heraldesk.platforms.base import (
    AccountRef,
    PublishError,
    PublishRequest,
    PublishResult,
)


class FakeAdapter:
    """Deterministic in-process platform adapter for tests and offline checks."""

    platform = "fake"

    def __init__(self, *, fail_times: int = 0, error: str = "fake provider unavailable") -> None:
        if fail_times < 0:
            raise ValueError("fail_times must not be negative")
        self._fail_times = fail_times
        self._error = error
        self.publish_calls = 0
        self.published: list[PublishRequest] = []
        self._results: dict[str, PublishResult] = {}

    async def validate_credentials(self, account: AccountRef) -> bool:
        """Accept a non-empty token and ignore every other credential."""
        return bool(account.credentials.get("token", "").strip())

    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult:
        """Record the post and return a stable external id, or fail on purpose."""
        _ = account
        self.publish_calls += 1
        if self.publish_calls <= self._fail_times:
            raise PublishError(self._error)
        existing = self._results.get(request.idempotency_key)
        if existing is not None:
            return existing
        result = PublishResult(external_id=f"fake-{len(self.published) + 1}")
        self.published.append(request)
        self._results[request.idempotency_key] = result
        return result
