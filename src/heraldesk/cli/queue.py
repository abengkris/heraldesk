"""``heraldesk queue``: review, approve, reject and edit queued content."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from sqlalchemy.orm import Session

from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import ApprovalRepository, ContentItemRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import (
    approve,
    edit_item,
    expire_pending_approvals,
    list_queue,
    now_or_utc,
    reject,
    scope_for_content_item,
)
from heraldesk.domain.errors import (
    ApprovalError,
    ContentError,
    ProfileNotFoundError,
    ProfileValidationError,
)
from heraldesk.domain.profiles import profile_summary, resolve_scope

queue_app = typer.Typer(
    no_args_is_help=True,
    help="Review, approve, reject and edit queued content.",
)

MAX_BODY_FILE_BYTES = 64 * 1024


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _expires_label(expires_at: datetime | None, now: datetime) -> str:
    if expires_at is None:
        return "-"
    remaining = (_utc(expires_at) - now).total_seconds()
    if remaining <= 0:
        return "expired"
    return f"in {remaining / 3600:.0f}h"


def _preview(value: str, redactor: SecretRegistry, limit: int = 72) -> str:
    text = redactor.redact(value).replace("\n", " ").strip()
    if len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


def _scope_for_item(session: Session, item_id: int) -> ProfileScope:
    scope = scope_for_content_item(session, item_id)
    if scope is None:
        abort(f"content item {item_id} does not exist")
    return scope


@queue_app.command("list")
def list_command(
    slug: Annotated[str, typer.Argument(help="Profile slug, for example 'acme'.")],
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=500, help="How many items to show.")
    ] = 50,
) -> None:
    """Show the approval queue and expire approvals older than 72 hours."""
    now = now_or_utc(None)
    with session_for() as (_, session):
        try:
            scope = resolve_scope(session, slug)
            summary = profile_summary(session, scope)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        expired = expire_pending_approvals(session, scope, now=now)
        entries = list_queue(session, scope, limit=limit)

    if expired:
        console.print(
            f"[yellow]Expired {len(expired)} pending approval(s); "
            "affected items were cancelled.[/yellow]"
        )

    if not entries:
        console.print(f"No items in the approval queue for [bold]{summary.slug}[/bold].")
        return

    redactor = SecretRegistry.from_environment()
    console.print(f"[bold]Approval queue: {summary.slug}[/bold]")
    risk_style = {"low": "green", "medium": "yellow", "high": "red", "blocked": "red"}
    for entry in entries:
        risk = entry.risk_level or "-"
        style = risk_style.get(risk, "white")
        approval = "-"
        if entry.approval_id is not None:
            approval = f"#{entry.approval_id} {entry.approval_state}"
        console.print(
            f"  [bold]#{entry.item_id}[/bold]  {escape(entry.platform)}  "
            f"{entry.item_state}  risk=[{style}]{risk}[/]  "
            f"approval={approval}  expires={_expires_label(entry.expires_at, now)}"
        )
        console.print(f"      {escape(_preview(entry.body, redactor, 96))}")
        if entry.reason:
            console.print(
                f"      [yellow]reason:[/yellow] {escape(_preview(entry.reason, redactor, 96))}"
            )


@queue_app.command("show")
def show_command(
    item_id: Annotated[
        int,
        typer.Argument(metavar="ID", help="Content item id shown by 'queue list'."),
    ],
) -> None:
    """Show the full redacted draft, risk and approval state for one item."""
    with session_for() as (_, session):
        scope = _scope_for_item(session, item_id)
        try:
            summary = profile_summary(session, scope)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        item = ContentItemRepository(session).get(scope, item_id)
        approval = ApprovalRepository(session).latest_for_item(scope, item_id)
    if item is None:
        abort(f"content item {item_id} does not exist")

    redactor = SecretRegistry.from_environment()
    console.print(
        f"[bold]Content #{item.id}[/bold]  {escape(summary.slug)} / {escape(item.platform)}"
    )
    console.print(
        f"State: {item.state}  Risk: {item.risk_level or '-'}  Attempts: {item.publish_attempts}"
    )
    if item.scheduled_at is not None:
        console.print(f"Scheduled: {_utc(item.scheduled_at).isoformat()}")
    if item.published_at is not None:
        console.print(f"Published: {_utc(item.published_at).isoformat()}")
    if item.external_id:
        console.print(f"External id: {escape(item.external_id)}")
    if approval is not None:
        expires = "-" if approval.expires_at is None else _utc(approval.expires_at).isoformat()
        console.print(f"Approval: #{approval.id} {approval.state}  expires: {expires}")
        if approval.reason:
            console.print(f"Approval reason: {escape(redactor.redact(approval.reason))}")
    if item.last_error:
        console.print(f"[red]Last error:[/red] {escape(redactor.redact(item.last_error))}")
    if item.risk_notes:
        console.print(f"[yellow]Risk notes:[/yellow] {escape(redactor.redact(item.risk_notes))}")

    console.print("\n[bold]Draft[/bold]")
    console.print(escape(redactor.redact(item.body)))


@queue_app.command("approve")
def approve_command(
    item_id: Annotated[
        int,
        typer.Argument(metavar="ID", help="Content item id shown by 'queue list'."),
    ],
) -> None:
    """Approve one pending content item."""
    with session_for() as (layout, session):
        scope = _scope_for_item(session, item_id)
        try:
            summary = profile_summary(session, scope)
            outcome = approve(session, layout, scope, item_id)
        except (ApprovalError, ContentError, ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
    console.print(
        f"[bold]Approved[/bold] content item #{outcome.content_item_id} for "
        f"{summary.slug} (approval #{outcome.approval_id}): "
        f"{outcome.from_state} -> {outcome.to_state}."
    )


@queue_app.command("reject")
def reject_command(
    item_id: Annotated[
        int,
        typer.Argument(metavar="ID", help="Content item id shown by 'queue list'."),
    ],
    reason: Annotated[
        str | None, typer.Option("--reason", help="Optional rejection reason.")
    ] = None,
) -> None:
    """Reject one pending content item."""
    with session_for() as (_, session):
        scope = _scope_for_item(session, item_id)
        try:
            summary = profile_summary(session, scope)
            outcome = reject(session, scope, item_id, reason=reason)
        except (ApprovalError, ContentError, ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
    redactor = SecretRegistry.from_environment()
    console.print(
        f"[bold]Rejected[/bold] content item #{outcome.content_item_id} for "
        f"{summary.slug} (approval #{outcome.approval_id}): "
        f"{outcome.from_state} -> {outcome.to_state}."
    )
    if outcome.reason:
        console.print(f"Reason: {escape(redactor.redact(outcome.reason))}")


@queue_app.command("edit")
def edit_command(
    item_id: Annotated[
        int,
        typer.Argument(metavar="ID", help="Content item id shown by 'queue list'."),
    ],
    body: Annotated[str | None, typer.Option("--body", help="Replacement body text.")] = None,
    body_file: Annotated[
        Path | None, typer.Option("--body-file", help="Read the replacement body from a file.")
    ] = None,
) -> None:
    """Edit one queued content item. Use --body or --body-file, not both."""
    if body is not None and body_file is not None:
        abort("provide only one of --body or --body-file")
    if body is not None:
        replacement = body
    elif body_file is not None:
        try:
            if body_file.stat().st_size > MAX_BODY_FILE_BYTES:
                abort(f"--body-file must be at most {MAX_BODY_FILE_BYTES} bytes")
            replacement = body_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            abort(f"cannot read {body_file}: {exc}")
    else:
        abort("provide one of --body or --body-file")

    with session_for() as (_, session):
        scope = _scope_for_item(session, item_id)
        try:
            summary = profile_summary(session, scope)
            outcome = edit_item(session, scope, item_id, body=replacement)
        except (ApprovalError, ContentError, ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))

    console.print(
        f"[bold]Edited[/bold] content item #{outcome.content_item_id} for "
        f"{summary.slug}: {outcome.from_state} -> {outcome.to_state}."
    )
    if outcome.approval_id is not None:
        console.print(f"Pending approval: #{outcome.approval_id}")
    if outcome.to_state == "drafted":
        console.print("The item must pass Compliance again before it can be approved.")
