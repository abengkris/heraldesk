"""``heraldesk telegram``: offline test sends, manual sends and the inbox view."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from heraldesk import paths
from heraldesk.cli.common import abort, console, session_for
from heraldesk.config import Config, ConfigError, load_config
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import TelegramMessageRepository
from heraldesk.domain.errors import ProfileNotFoundError, ProfileValidationError
from heraldesk.domain.profiles import profile_summary, resolve_scope
from heraldesk.telegram.base import TelegramAdapter, TelegramError
from heraldesk.telegram.bot_api import BotApiTelegramAdapter
from heraldesk.telegram.credentials import (
    TelegramConfigError,
    mask_token,
    resolve_bot_token,
    store_bot_token,
    token_source,
)
from heraldesk.telegram.fake import FakeTelegramAdapter
from heraldesk.telegram.formatting import mask_chat_id, mask_sender_id, preview

telegram_app = typer.Typer(
    no_args_is_help=True,
    help="Test Telegram delivery and inspect the inbound inbox.",
)
inbox_app = typer.Typer(no_args_is_help=True, help="Inspect stored inbound messages.")
token_app = typer.Typer(no_args_is_help=True, help="Manage the bot token.")
telegram_app.add_typer(inbox_app, name="inbox")
telegram_app.add_typer(token_app, name="token")

DEFAULT_PREVIEW = 96


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redactor_for(token: str | None) -> SecretRegistry:
    return SecretRegistry.from_environment(extra_values=[token] if token else [])


def _load_config(layout: paths.Layout) -> Config:
    try:
        return load_config(layout.home)
    except ConfigError as exc:
        abort(str(exc))


def _token(layout: paths.Layout) -> str | None:
    try:
        return resolve_bot_token(layout)
    except TelegramConfigError as exc:
        abort(str(exc))


def _target_chat_id(config: Config, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    if config.telegram.allowed_chat_ids:
        return config.telegram.allowed_chat_ids[0]
    abort("no telegram chat id is configured; set telegram.allowed_chat_ids or pass --chat-id")


@telegram_app.command("test")
def test_command(
    chat_id: Annotated[
        int | None,
        typer.Option("--chat-id", help="Chat id to use instead of the first allowlisted id."),
    ] = None,
) -> None:
    """Send one test message through the fake adapter. No network is used."""
    with session_for() as (layout, session):
        config = _load_config(layout)
        token = _token(layout)
        target = _target_chat_id(config, chat_id)
        text = f"Heraldesk Telegram test at {datetime.now(UTC).isoformat(timespec='seconds')}."
        adapter = FakeTelegramAdapter()
        try:
            adapter.send_message(target, text)
        except TelegramError as exc:
            abort(str(exc))
        redactor = _redactor_for(token)
        preview_text = preview(redactor.redact(text))
        AuditService(session).record(
            None,
            actor="user",
            action="telegram.sent",
            entity="telegram",
            payload={
                "event": "test",
                "recipients": [mask_chat_id(target)],
                "message_sha256": _hash(text),
                "adapter": adapter.name,
            },
        )
        source = token_source(layout)
    console.print("[bold]Telegram test[/bold] [yellow](fake adapter, no network)[/yellow]")
    console.print(f"Bot token: {mask_token(token)} (source: {source})")
    console.print(f"Chat: {mask_chat_id(target)}")
    console.print(f"Preview: {escape(preview_text)}")


@telegram_app.command("send")
def send_command(
    slug: Annotated[str, typer.Argument(help="Profile slug.")],
    text: Annotated[str, typer.Argument(help="Message body. It is redacted in output.")],
    chat_id: Annotated[
        int | None,
        typer.Option("--chat-id", help="Chat id to use instead of the first allowlisted id."),
    ] = None,
    real: Annotated[
        bool,
        typer.Option(
            "--real",
            help=(
                "Send through the Telegram Bot API. Requires telegram.enabled, a stored "
                "token and a chat id in telegram.allowed_chat_ids."
            ),
        ),
    ] = False,
) -> None:
    """Send a test message: an offline preview by default, a real send with --real."""
    if not text.strip():
        abort("message text must not be empty")
    with session_for() as (layout, session):
        config = _load_config(layout)
        try:
            scope = resolve_scope(session, slug)
            summary = profile_summary(session, scope)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        token = _token(layout)
        target = _target_chat_id(config, chat_id)
        redactor = _redactor_for(token)
        body = redactor.redact(text)
        adapter: TelegramAdapter
        if real:
            if not config.telegram.enabled:
                abort(
                    "telegram is disabled; set telegram.enabled: true in heraldesk.yaml "
                    "before using --real"
                )
            if token is None:
                abort("no bot token is stored; run 'heraldesk telegram token set' first")
            if target not in config.telegram.allowed_chat_ids:
                abort(
                    f"chat id {mask_chat_id(target)} is not in telegram.allowed_chat_ids; "
                    "add it before sending for real"
                )
            adapter = BotApiTelegramAdapter(token)
        else:
            adapter = FakeTelegramAdapter()
        try:
            result = adapter.send_message(target, body)
        except TelegramError as exc:
            abort(str(exc))
        AuditService(session).record(
            scope,
            actor="user",
            action="telegram.sent",
            entity="telegram",
            payload={
                "event": "manual",
                "profile": summary.slug,
                "recipients": [mask_chat_id(target)],
                "message_sha256": _hash(body),
                "message_length": len(body),
                "adapter": adapter.name,
            },
        )
    if real:
        console.print(
            f"[bold]Telegram send[/bold] for {escape(summary.slug)} "
            f"(message id {result.message_id})"
        )
    else:
        console.print(
            f"[bold]Telegram send[/bold] [yellow](fake adapter, no network)[/yellow] "
            f"for {escape(summary.slug)}"
        )
    console.print(f"Chat: {mask_chat_id(target)}")
    console.print(f"Preview: {escape(preview(redactor.redact(body), limit=DEFAULT_PREVIEW))}")


@inbox_app.command("list")
def inbox_list_command(
    slug: Annotated[str, typer.Argument(help="Profile slug.")],
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=200, help="How many messages to show.")
    ] = 20,
) -> None:
    """Show the newest inbound messages, redacted and chat ids masked."""
    with session_for() as (layout, session):
        try:
            scope = resolve_scope(session, slug)
            summary = profile_summary(session, scope)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        messages = TelegramMessageRepository(session).list(scope, limit=limit)
        token = _token(layout)
        redactor = _redactor_for(token)

    if not messages:
        console.print(f"No Telegram messages stored for [bold]{escape(summary.slug)}[/bold].")
        return

    table = Table(title=f"Telegram inbox: {summary.slug}", show_lines=False)
    table.add_column("When")
    table.add_column("Command")
    table.add_column("Chat")
    table.add_column("Sender")
    table.add_column("Item", justify="right")
    table.add_column("Text")
    for message in messages:
        table.add_row(
            message.created_at.isoformat(timespec="seconds"),
            message.command or "-",
            mask_chat_id(message.chat_id),
            mask_sender_id(message.sender_id),
            "-" if message.content_item_id is None else str(message.content_item_id),
            escape(preview(redactor.redact(message.text), limit=DEFAULT_PREVIEW)),
        )
    console.print(table)


@token_app.command("set")
def token_set_command(
    token: Annotated[
        str | None,
        typer.Option("--token", help="Bot token. Omit to be prompted without echo."),
    ] = None,
) -> None:
    """Encrypt the bot token into ``telegram.token`` with ``secret.key``."""
    value = token if token is not None else typer.prompt("Bot token", hide_input=True)
    with session_for() as (layout, session):
        try:
            path = store_bot_token(layout, value)
        except TelegramConfigError as exc:
            abort(str(exc))
        AuditService(session).record(
            None,
            actor="user",
            action="telegram.token_set",
            entity="telegram",
            payload={"source": "encrypted-file"},
        )
    console.print("[bold]Telegram token stored[/bold] (Fernet, mode 600).")
    console.print(f"Path: {path}")
    console.print(f"Fingerprint: {mask_token(value.strip())}")


@token_app.command("status")
def token_status_command() -> None:
    """Show where the token comes from and a safe fingerprint."""
    with session_for() as (layout, _session):
        source = token_source(layout)
        token = _token(layout)
    console.print(f"Token: {mask_token(token)}")
    console.print(f"Source: {source}")
    if source == "encrypted-file":
        console.print(f"Path: {layout.telegram_token_path}")
