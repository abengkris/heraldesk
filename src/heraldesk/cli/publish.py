"""``heraldesk publish``: manually publish one content item now."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.crypto import CredentialCipher, CryptoError
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.domain.approvals import scope_for_content_item
from heraldesk.domain.errors import ContentError
from heraldesk.platforms import publish_adapter_for
from heraldesk.platforms.base import PublishError
from heraldesk.publisher import PublishStatus, publish_item
from heraldesk.telegram.runtime import notifier_for

MAX_PREVIEW = 120


def _preview(value: str, redactor: SecretRegistry) -> str:
    text = redactor.redact(value).replace("\n", " ").strip()
    if len(text) > MAX_PREVIEW:
        return f"{text[: MAX_PREVIEW - 3]}..."
    return text


def publish_command(
    item_id: Annotated[
        int,
        typer.Argument(metavar="ID", help="Content item id shown by 'queue list'."),
    ],
) -> None:
    """Publish one approved, scheduled or failed item immediately."""
    with session_for() as (layout, session):
        scope = scope_for_content_item(session, item_id)
        if scope is None:
            abort(f"content item {item_id} does not exist")
        item = ContentItemRepository(session).get(scope, item_id)
        if item is None:
            abort(f"content item {item_id} does not exist")

        try:
            cipher = CredentialCipher.from_key_file(layout.key_path)
        except CryptoError as exc:
            abort(str(exc))
        try:
            adapter = publish_adapter_for(item.platform)
        except PublishError:
            adapter = None

        body = item.body
        platform = item.platform
        try:
            outcome = publish_item(
                session,
                layout,
                scope,
                item_id,
                adapter=adapter,
                cipher=cipher,
                actor="user",
                trigger="manual",
                notifier=notifier_for(session, layout),
            )
        except (ContentError, CryptoError) as exc:
            abort(str(exc))

    redactor = SecretRegistry.from_environment()
    console.print(f"[bold]Publish[/bold] content item #{outcome.item_id} on {escape(platform)}")
    console.print(f"Preview: {escape(_preview(body, redactor))}")
    if outcome.status is PublishStatus.PUBLISHED:
        console.print(
            f"[green]Published[/green]: {outcome.from_state} -> {outcome.to_state} "
            f"after {outcome.attempts} attempt(s)."
        )
        console.print(f"External id: {escape(outcome.external_id or '-')}")
        if outcome.url:
            console.print(f"URL: {escape(outcome.url)}")
        return
    if outcome.status is PublishStatus.FAILED:
        console.print(f"[red]Failed[/red]: {escape(outcome.error or 'unknown error')}")
        if outcome.retry_at is not None:
            console.print(
                f"The scheduler will retry after {outcome.retry_at.isoformat()} "
                f"(attempt {outcome.attempts} of 3)."
            )
        else:
            console.print(
                "Retries are exhausted. Fix the cause, then run 'heraldesk publish "
                f"{outcome.item_id}' again to re-queue it."
            )
        raise typer.Exit(code=1)
    console.print(f"[yellow]Skipped[/yellow]: {escape(outcome.reason or 'not publishable')}")
    raise typer.Exit(code=1)
