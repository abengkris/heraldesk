"""``heraldesk runs``: agent run traces and daily cost."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.table import Table

from heraldesk.cli.common import abort, console, session_for
from heraldesk.config import ConfigError, load_config
from heraldesk.db.models import AgentRun
from heraldesk.db.repositories import AgentRunRepository, GlobalCostRepository
from heraldesk.domain.errors import ProfileNotFoundError, ProfileValidationError
from heraldesk.domain.profiles import profile_summary, resolve_scope
from heraldesk.llm.budget import CostTracker, utc_day_window


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_started(run: AgentRun) -> str:
    started = _utc(run.started_at)
    return "?" if started is None else started.strftime("%m-%d %H:%M")


def runs_command(
    slug: Annotated[str, typer.Argument(help="Profile slug, for example 'acme'.")],
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=200, help="How many recent runs to show.")
    ] = 20,
) -> None:
    """Show recent agent runs, tokens and cost for one profile."""
    with session_for() as (layout, session):
        try:
            config = load_config(layout.home)
            scope = resolve_scope(session, slug)
            summary = profile_summary(session, scope)
        except (ConfigError, ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))

        runs = AgentRunRepository(session).list(scope, limit=limit)
        tracker = CostTracker(session, config.llm.max_daily_cost_usd)
        now = datetime.now(UTC)
        profile_cost, _window = tracker.profile_cost_today(
            scope, timezone=summary.timezone, now=now
        )
        local_date = tracker.profile_local_date(timezone=summary.timezone, now=now)
        utc_window = utc_day_window(now)
        global_totals = GlobalCostRepository(session).totals_since(utc_window.start, utc_window.end)

    if not runs:
        console.print(f"No agent runs recorded for [bold]{summary.slug}[/bold] yet.")
    else:
        table = Table(title=f"Agent runs: {summary.slug}")
        table.add_column("ID", justify="right")
        table.add_column("Agent", style="bold", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Model", no_wrap=True)
        table.add_column("Tokens", justify="right", no_wrap=True)
        table.add_column("Cost", justify="right", no_wrap=True)
        table.add_column("Started (UTC)", no_wrap=True)
        status_styles = {"succeeded": "green", "failed": "red", "running": "yellow"}
        for run in runs:
            style = status_styles.get(run.status, "white")
            table.add_row(
                str(run.id),
                run.agent,
                f"[{style}]{run.status}[/]",
                run.model,
                f"{run.tokens_in}/{run.tokens_out}",
                f"{run.cost_usd:.6f}",
                _format_started(run),
            )
        console.print(table)

    console.print(
        f"Profile spend today ({summary.timezone}, {local_date.isoformat()}): ${profile_cost:.6f}"
    )
    console.print(
        f"Global spend today (UTC, {utc_window.label}): "
        f"${global_totals.cost_usd:.6f} of ${config.llm.max_daily_cost_usd:.2f}"
    )
