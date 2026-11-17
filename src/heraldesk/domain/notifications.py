"""Notification policy and stable event keys.

The notification ledger uses one idempotency key per event. The exact formats are
documented in ``docs/decisions.md`` (M6 section) and must stay stable:

* ``telegram:approval_requested:profile=<id>:item=<id>:approval=<id>``
* ``telegram:published:profile=<id>:item=<id>:attempt=<n>``
* ``telegram:failed_final:profile=<id>:item=<id>:attempt=<n>:at=<epoch>``
* ``telegram:killswitch:profile=<id|global>:state=<on|off>:event=<id>``

``failed_final`` carries the failure timestamp so a later human re-queue that fails again
is a new event, while a repeated scheduler sweep of the same row is not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from heraldesk.domain.policy import NotifyMode


class NotifyEvent(StrEnum):
    """Events that can produce a Telegram notification."""

    APPROVAL_REQUESTED = "approval_requested"
    PUBLISHED = "published"
    FAILED_FINAL = "failed_final"
    KILLSWITCH = "killswitch"


def should_notify(mode: NotifyMode, event: NotifyEvent) -> bool:
    """Return ``True`` when *mode* allows *event*."""
    if mode is NotifyMode.OFF:
        return False
    if mode is NotifyMode.APPROVALS_ONLY:
        return event is NotifyEvent.APPROVAL_REQUESTED
    return True


def approval_key(profile_id: int, item_id: int, approval_id: int) -> str:
    """Stable key for one approval request."""
    return f"telegram:approval_requested:profile={profile_id}:item={item_id}:approval={approval_id}"


def published_key(profile_id: int, item_id: int, attempts: int) -> str:
    """Stable key for one successful publication."""
    return f"telegram:published:profile={profile_id}:item={item_id}:attempt={attempts}"


def failed_final_key(
    profile_id: int,
    item_id: int,
    attempts: int,
    failed_at: datetime,
) -> str:
    """Stable key for one exhausted publish failure."""
    moment = failed_at.replace(tzinfo=UTC) if failed_at.tzinfo is None else failed_at
    return (
        f"telegram:failed_final:profile={profile_id}:item={item_id}:"
        f"attempt={attempts}:at={int(moment.timestamp())}"
    )


def killswitch_key(
    profile_id: int | None,
    *,
    enabled: bool,
    event_id: str,
) -> str:
    """Stable key for one global or per-profile kill-switch change."""
    target = "global" if profile_id is None else str(profile_id)
    state = "on" if enabled else "off"
    return f"telegram:killswitch:profile={target}:state={state}:event={event_id}"
