"""Daily cost accounting for LLM usage.

``max_daily_cost_usd`` is enforced globally on the UTC day, before every provider call.
Per-profile reporting uses midnight in the profile timezone. Both windows are derived from
``agent_runs`` so the numbers survive restarts and match what ``heraldesk runs`` shows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from heraldesk.db.repositories import AgentRunRepository, GlobalCostRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.llm.errors import DailyCostCapExceeded


@dataclass(frozen=True, slots=True)
class CostWindow:
    """A half-open time window ``[start, end)`` in UTC."""

    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        """Return the window start as an ISO date for display."""
        return self.start.date().isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_day_window(now: datetime) -> CostWindow:
    """Return the UTC day that contains *now*."""
    current = _as_utc(now)
    start = datetime.combine(current.date(), time.min, tzinfo=UTC)
    return CostWindow(start=start, end=start + timedelta(days=1))


def profile_day_window(now: datetime, timezone: str) -> CostWindow:
    """Return the local calendar day of *timezone* that contains *now*, expressed in UTC."""
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone {timezone!r}") from exc
    local_now = _as_utc(now).astimezone(zone)
    start_local = datetime.combine(local_now.date(), time.min, tzinfo=zone)
    end_local = datetime.combine(local_now.date() + timedelta(days=1), time.min, tzinfo=zone)
    return CostWindow(start=start_local.astimezone(UTC), end=end_local.astimezone(UTC))


def _local_date(now: datetime, timezone: str) -> date:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone {timezone!r}") from exc
    return _as_utc(now).astimezone(zone).date()


class CostTracker:
    """Read spend from ``agent_runs`` and refuse calls that would cross the daily cap."""

    def __init__(self, session: Session, cap_usd: float) -> None:
        if cap_usd < 0:
            raise ValueError("cap_usd must not be negative")
        self._session = session
        self._cap_usd = cap_usd
        self._global = GlobalCostRepository(session)
        self._runs = AgentRunRepository(session)

    @property
    def cap_usd(self) -> float:
        """Return the configured daily cap."""
        return self._cap_usd

    def spent_today_utc(self, now: datetime) -> float:
        """Return every profile's spend in the current UTC day."""
        window = utc_day_window(now)
        return self._global.totals_since(window.start, window.end).cost_usd

    def ensure_within_cap(
        self,
        *,
        now: datetime,
        pending_usd: float = 0.0,
    ) -> None:
        """Raise :class:`DailyCostCapExceeded` when the next call must not happen.

        *pending_usd* is the cost already accumulated earlier in the current run, which is
        not committed to ``agent_runs`` yet.
        """
        if pending_usd < 0:
            raise ValueError("pending_usd must not be negative")
        window = utc_day_window(now)
        spent = self._global.totals_since(window.start, window.end).cost_usd
        if spent + pending_usd >= self._cap_usd:
            raise DailyCostCapExceeded(
                spent_usd=spent,
                pending_usd=pending_usd,
                cap_usd=self._cap_usd,
                window_start=window.start,
            )

    def profile_cost_today(
        self,
        scope: ProfileScope,
        *,
        timezone: str,
        now: datetime,
    ) -> tuple[float, CostWindow]:
        """Return one profile's spend today in its own timezone and the window used."""
        window = profile_day_window(now, timezone)
        totals = self._runs.totals_since(scope, window.start, window.end)
        return totals.cost_usd, window

    def profile_local_date(self, *, timezone: str, now: datetime) -> date:
        """Return the current date in the profile timezone (for report labels)."""
        return _local_date(now, timezone)
