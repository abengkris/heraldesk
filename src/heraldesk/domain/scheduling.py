"""Publishing-time policy gates: quiet hours and the daily post cap.

``policy.yaml`` carries two limits that must be evaluated when an item becomes due:

* ``quiet_hours``: no automatic publishing inside the window, in the profile timezone;
* ``max_posts_per_day``: the number of published items allowed per local calendar day.

The scheduled publisher is the automatic path, so both gates apply there (including to
retries). An explicit human publish overrides them and records an audit entry instead.
A missing or malformed policy fails closed for automatic publishing: an unreadable limit
is never treated as "no limit".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from heraldesk.db.repositories import ContentItemRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.policy import AutonomyPolicy, QuietHours

QUIET_HOURS_REASON: Final = "quiet_hours"
DAILY_POST_CAP_REASON: Final = "daily_post_cap"
POLICY_UNAVAILABLE_REASON: Final = "policy_unavailable"


@dataclass(frozen=True, slots=True)
class PublishGate:
    """Whether an automatic publish may proceed, plus a stable reason code."""

    allowed: bool
    reason: str | None = None
    detail: str | None = None


def profile_zone(timezone: str) -> ZoneInfo:
    """Return the IANA zone for *timezone* or raise :class:`ValueError`."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown profile timezone {timezone!r}") from exc


def _minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _as_local(moment: datetime, zone: ZoneInfo) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone)


def is_quiet_hours(local_now: datetime, quiet: QuietHours | None) -> bool:
    """Return ``True`` when *local_now* falls inside the quiet window.

    The window is half-open: it starts at ``start`` and ends at ``end``. A window whose
    start is later than its end crosses midnight.
    """
    if quiet is None:
        return False
    current = local_now.hour * 60 + local_now.minute
    start = _minutes(quiet.start)
    end = _minutes(quiet.end)
    if start < end:
        return start <= current < end
    return current >= start or current < end


def next_quiet_hours_end(local_now: datetime, quiet: QuietHours) -> datetime:
    """Return the local time when the quiet window containing *local_now* ends."""
    if not is_quiet_hours(local_now, quiet):
        return local_now
    start = _minutes(quiet.start)
    end = _minutes(quiet.end)
    current = local_now.hour * 60 + local_now.minute
    if start < end or current < end:
        target_date = local_now.date()
    else:
        target_date = local_now.date() + timedelta(days=1)
    # ``fold=1`` picks the later occurrence of an ambiguous end time. On a fall-back day
    # that keeps the item out of the repeated hour instead of publishing before it.
    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        end // 60,
        end % 60,
        tzinfo=local_now.tzinfo,
        fold=1,
    )


def defer_past_quiet_hours(
    moment: datetime,
    timezone: str,
    quiet: QuietHours | None,
) -> datetime:
    """Move *moment* to the end of the quiet window when it lands inside one.

    Returns an aware UTC datetime. When *moment* is outside the window it is returned
    unchanged, converted to UTC.
    """
    zone = profile_zone(timezone)
    local = _as_local(moment, zone)
    if quiet is None or not is_quiet_hours(local, quiet):
        return local.astimezone(UTC)
    return next_quiet_hours_end(local, quiet).astimezone(UTC)


def local_day_bounds(moment: datetime, timezone: str) -> tuple[datetime, datetime]:
    """Return the UTC ``[start, end)`` of the profile-local calendar day of *moment*.

    Converting local midnight separately for each boundary keeps the window exact on
    daylight-saving days, which are 23 or 25 hours long.
    """
    zone = profile_zone(timezone)
    local = _as_local(moment, zone)
    start = datetime.combine(local.date(), dt_time(0, 0), tzinfo=zone)
    end = datetime.combine(local.date() + timedelta(days=1), dt_time(0, 0), tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def published_today(
    session: Session,
    scope: ProfileScope,
    *,
    timezone: str,
    now: datetime,
) -> int:
    """Count items published during the profile-local day containing *now*."""
    start, end = local_day_bounds(now, timezone)
    return ContentItemRepository(session).count_published_between(scope, start=start, end=end)


def evaluate_publish_gate(
    session: Session,
    scope: ProfileScope,
    *,
    timezone: str,
    policy: AutonomyPolicy,
    now: datetime,
) -> PublishGate:
    """Apply quiet hours and the daily cap for one automatic publish attempt."""
    try:
        zone = profile_zone(timezone)
    except ValueError as exc:
        return PublishGate(False, POLICY_UNAVAILABLE_REASON, str(exc))

    local_now = _as_local(now, zone)
    quiet = policy.quiet_hours
    if quiet is not None and is_quiet_hours(local_now, quiet):
        return PublishGate(
            False,
            QUIET_HOURS_REASON,
            f"quiet hours {quiet.start}-{quiet.end} ({timezone})",
        )

    if policy.max_posts_per_day == 0:
        return PublishGate(
            False,
            DAILY_POST_CAP_REASON,
            f"max_posts_per_day is 0 ({timezone})",
        )

    published = published_today(session, scope, timezone=timezone, now=now)
    if published >= policy.max_posts_per_day:
        return PublishGate(
            False,
            DAILY_POST_CAP_REASON,
            f"{published} posts already published today; cap is "
            f"{policy.max_posts_per_day} ({timezone})",
        )
    return PublishGate(True)
