"""``heraldesk report``: deterministic weekly and monthly Markdown reports."""

from __future__ import annotations

from typing import Annotated

import typer

from heraldesk.analyst import ReportError, monthly_period, weekly_period, write_report
from heraldesk.cli.common import abort, console, session_for

report_app = typer.Typer(
    no_args_is_help=True,
    help="Generate deterministic weekly and monthly reports for the whole instance.",
)


@report_app.command("weekly")
def weekly_command(
    week: Annotated[
        str | None,
        typer.Option("--week", help="ISO week in YYYY-Www form. Defaults to the current UTC week."),
    ] = None,
) -> None:
    """Write the weekly report to ``$HERALDESK_HOME/reports``."""
    try:
        period = weekly_period(week)
    except ReportError as exc:
        abort(str(exc))
    with session_for() as (layout, session):
        try:
            path = write_report(session, layout, period)
        except ReportError as exc:
            abort(str(exc))
    console.print(f"[bold]Weekly report[/bold] {period.label} written to {path}")


@report_app.command("monthly")
def monthly_command(
    month: Annotated[
        str | None,
        typer.Option("--month", help="Month in YYYY-MM form. Defaults to the current UTC month."),
    ] = None,
) -> None:
    """Write the monthly report to ``$HERALDESK_HOME/reports``."""
    try:
        period = monthly_period(month)
    except ReportError as exc:
        abort(str(exc))
    with session_for() as (layout, session):
        try:
            path = write_report(session, layout, period)
        except ReportError as exc:
            abort(str(exc))
    console.print(f"[bold]Monthly report[/bold] {period.label} written to {path}")
