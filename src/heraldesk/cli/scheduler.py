"""``heraldesk scheduler``: run one deterministic publishing sweep."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.crypto import CredentialCipher, CryptoError
from heraldesk.domain.errors import ContentError
from heraldesk.scheduler.runner import SchedulerEntry, SchedulerResult, run_scheduler

scheduler_app = typer.Typer(
    no_args_is_help=True,
    help="Publish scheduled content when it is due.",
)

MAX_REASON = 120


def _utc_label(value: datetime | None) -> str:
    if value is None:
        return "-"
    current = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return current.strftime("%Y-%m-%d %H:%M UTC")


def _label(entry: SchedulerEntry) -> str:
    if entry.outcome is None:
        return "would retry" if entry.action == "retry" else "would publish"
    return entry.outcome


def _reason(entry: SchedulerEntry) -> str:
    if not entry.reason:
        return ""
    text = escape(entry.reason.replace("\n", " ").strip())
    if len(text) > MAX_REASON:
        return f"{text[: MAX_REASON - 3]}..."
    return text


def _print_result(result: SchedulerResult) -> None:
    if result.dry_run:
        console.print("[bold]Scheduler dry run[/bold] [yellow](nothing was written)[/yellow]")
    else:
        console.print(
            f"[bold]Scheduler run[/bold] ({result.profiles_checked} profile(s) checked, "
            f"{result.expired_approvals} expired approval(s) swept)"
        )
    if not result.entries:
        console.print("No content is due.")
        return

    table = Table(show_lines=False)
    table.add_column("ID", justify="right")
    table.add_column("Profile", style="bold")
    table.add_column("Platform")
    table.add_column("State")
    table.add_column("Scheduled")
    table.add_column("Tries", justify="right")
    table.add_column("Result")
    table.add_column("Detail")
    for entry in result.entries:
        table.add_row(
            str(entry.item_id),
            escape(entry.profile_slug),
            escape(entry.platform),
            entry.state,
            _utc_label(entry.scheduled_at),
            str(entry.publish_attempts),
            _label(entry),
            _reason(entry),
        )
    console.print(table)

    if result.dry_run:
        console.print(
            f"Would publish {len(result.planned)} item(s). "
            "Run [cyan]heraldesk scheduler run[/cyan] to publish them."
        )
    else:
        console.print(
            f"Published {len(result.published)} | failed {len(result.failed)} | "
            f"skipped {len(result.skipped)}"
        )


def execute_scheduler(*, dry_run: bool) -> SchedulerResult:
    """Run one scheduler sweep and print it. Shared by both CLI spellings."""
    with session_for() as (layout, session):
        cipher: CredentialCipher | None = None
        if not dry_run:
            try:
                cipher = CredentialCipher.from_key_file(layout.key_path)
            except CryptoError as exc:
                abort(str(exc))
        try:
            result = run_scheduler(
                session,
                layout,
                dry_run=dry_run,
                cipher=cipher,
            )
        except (ContentError, CryptoError) as exc:
            abort(str(exc))
    _print_result(result)
    return result


@scheduler_app.command("run")
def run_command(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List what would publish without writing anything.",
        ),
    ] = False,
) -> None:
    """Publish every due item, or list them without writing with --dry-run."""
    execute_scheduler(dry_run=dry_run)
