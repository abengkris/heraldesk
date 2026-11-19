"""``heraldesk inbox``: manual triage of stored inbound messages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from heraldesk.cli.common import abort, console, session_for
from heraldesk.community import (
    IgnoreReason,
    InboxError,
    InboxStatus,
    ignore_item,
    list_items,
    reply_item,
    show_item,
)
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import InboxRepository
from heraldesk.telegram.formatting import mask_chat_id, mask_sender_id, preview

inbox_app = typer.Typer(
    no_args_is_help=True,
    help="List, show, reply to or ignore inbound messages manually.",
)

DEFAULT_PREVIEW = 96


def _redactor() -> SecretRegistry:
    return SecretRegistry.from_environment()


def _profile_label(slug: str | None) -> str:
    return "-" if slug is None else escape(slug)


@inbox_app.command("list")
def list_command(
    status: Annotated[
        InboxStatus,
        typer.Option("--status", help="Only show this triage status."),
    ] = InboxStatus.PENDING,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=500, help="How many inbox items to show."),
    ] = 20,
) -> None:
    """List inbox items, newest first. Defaults to pending, limit 20."""
    with session_for() as (_, session):
        try:
            items = list_items(session, status=status, limit=limit)
        except InboxError as exc:
            abort(str(exc))

    if not items:
        console.print(f"No inbox items with status [bold]{status.value}[/bold].")
        return

    redactor = _redactor()
    table = Table(title=f"Inbox: {status.value}", show_lines=False)
    table.add_column("ID", justify="right")
    table.add_column("When")
    table.add_column("Profile")
    table.add_column("Chat")
    table.add_column("Sender")
    table.add_column("Command")
    table.add_column("Text")
    for item in items:
        table.add_row(
            str(item.id),
            item.created_at.isoformat(timespec="seconds"),
            _profile_label(item.profile_slug),
            mask_chat_id(item.chat_id),
            mask_sender_id(item.sender_id),
            item.command or "-",
            escape(preview(redactor.redact(item.text), limit=DEFAULT_PREVIEW)),
        )
    console.print(table)


@inbox_app.command("show")
def show_command(
    item_id: Annotated[int, typer.Argument(metavar="ID", help="Inbox item id.")],
) -> None:
    """Show one inbox item, redacted and with ids masked."""
    with session_for() as (_, session):
        try:
            item = show_item(session, item_id)
        except InboxError as exc:
            abort(str(exc))

    redactor = _redactor()
    table = Table(title=f"Inbox item #{item.id}", show_lines=False)
    table.add_column("Field", style="bold", no_wrap=True)
    table.add_column("Value")
    table.add_row("Profile", _profile_label(item.profile_slug))
    table.add_row("Status", item.status)
    table.add_row("Ignore reason", item.ignore_reason or "-")
    table.add_row("Chat", mask_chat_id(item.chat_id))
    table.add_row("Sender", mask_sender_id(item.sender_id))
    table.add_row("Command", item.command or "-")
    table.add_row(
        "Content item",
        "-" if item.content_item_id is None else str(item.content_item_id),
    )
    table.add_row("Created", item.created_at.isoformat(timespec="seconds"))
    table.add_row(
        "Triaged",
        "-" if item.triaged_at is None else item.triaged_at.isoformat(timespec="seconds"),
    )
    table.add_row("Text", escape(redactor.redact(item.text)))
    table.add_row(
        "Response",
        "-" if item.response is None else escape(redactor.redact(item.response)),
    )
    console.print(table)


@inbox_app.command("reply")
def reply_command(
    item_id: Annotated[int, typer.Argument(metavar="ID", help="Inbox item id.")],
    text: Annotated[str, typer.Option("--text", help="Reply body. It is redacted before storage.")],
) -> None:
    """Send one manual reply through the Telegram adapter, then mark the item handled."""
    with session_for() as (layout, session):
        try:
            item = reply_item(session, layout, item_id, text)
        except InboxError as exc:
            abort(str(exc))

    redactor = _redactor()
    console.print(
        f"[bold]Replied[/bold] to inbox item #{item.id} "
        f"for {_profile_label(item.profile_slug)}; status is now [green]handled[/green]."
    )
    response = item.response or ""
    console.print(
        f"Chat: {mask_chat_id(item.chat_id)}  "
        f"Preview: {escape(preview(redactor.redact(response), limit=DEFAULT_PREVIEW))}"
    )


@inbox_app.command("ignore")
def ignore_command(
    item_id: Annotated[int, typer.Argument(metavar="ID", help="Inbox item id.")],
    reason: Annotated[
        IgnoreReason,
        typer.Option("--reason", help="Why the item is ignored."),
    ] = IgnoreReason.OTHER,
) -> None:
    """Mark one inbox item ignored without sending a reply."""
    with session_for() as (_, session):
        try:
            item = ignore_item(session, item_id, reason=reason)
        except InboxError as exc:
            abort(str(exc))

    console.print(
        f"[bold]Ignored[/bold] inbox item #{item.id} "
        f"for {_profile_label(item.profile_slug)} "
        f"(reason: [yellow]{item.ignore_reason}[/yellow])."
    )


@inbox_app.command("prune")
def prune_command(
    days: Annotated[
        int,
        typer.Option("--days", min=1, max=3650, help="Retain messages newer than N days."),
    ] = 30,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Count eligible messages without deleting."),
    ] = False,
    include_pending: Annotated[
        bool,
        typer.Option(
            "--include-pending",
            help="Also delete untriaged messages older than N days.",
        ),
    ] = False,
) -> None:
    """Delete handled/ignored inbox messages older than N days."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    with session_for() as (_, session):
        repository = InboxRepository(session)
        if dry_run:
            count = repository.count_older_than(cutoff, include_pending=include_pending)
            console.print(
                f"[bold]Would prune:[/bold] {count} inbox message(s) older than {days} day(s)."
            )
            return
        deleted = repository.delete_older_than(cutoff, include_pending=include_pending)
        AuditService(session).record(
            None,
            actor="user",
            action="inbox.pruned",
            entity="inbox",
            payload={
                "days": days,
                "deleted": deleted,
                "include_pending": include_pending,
            },
        )
    console.print(f"[green]Pruned[/green] {deleted} inbox message(s) older than {days} day(s).")
