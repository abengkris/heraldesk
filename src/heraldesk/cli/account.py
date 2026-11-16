"""``heraldesk account`` commands."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.table import Table

from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.crypto import CredentialCipher, CryptoError
from heraldesk.domain.accounts import add_account, check_accounts
from heraldesk.domain.errors import (
    AccountError,
    NoAccountsError,
    ProfileNotFoundError,
    ProfileValidationError,
)
from heraldesk.domain.profiles import resolve_scope
from heraldesk.platforms import adapter_for

account_app = typer.Typer(
    no_args_is_help=True,
    help="Add and test social accounts.",
)


def _parse_credentials(items: list[str] | None) -> dict[str, str]:
    if not items:
        raise AccountError(
            "at least one --credential KEY=VALUE is required; values are encrypted at rest"
        )
    credentials: dict[str, str] = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise AccountError("invalid --credential; expected KEY=VALUE")
        credentials[key.strip()] = value
    return credentials


@account_app.command("add")
def add_command(
    profile: Annotated[str, typer.Argument(help="Profile slug.")],
    platform: Annotated[
        str,
        typer.Option(
            "--platform",
            help="fake, threads, nostr, x, facebook, instagram, linkedin",
        ),
    ],
    handle: Annotated[str, typer.Option("--handle", help="Handle shown in listings.")],
    credential: Annotated[
        list[str] | None,
        typer.Option(
            "--credential",
            "-c",
            help="Repeatable KEY=VALUE credential. Values are never printed.",
        ),
    ] = None,
) -> None:
    """Encrypt account credentials and store the account for a profile."""
    try:
        credentials = _parse_credentials(credential)
    except AccountError as exc:
        abort(str(exc))

    with session_for() as (layout, session):
        try:
            cipher = CredentialCipher.from_key_file(layout.key_path)
        except CryptoError as exc:
            abort(str(exc))
        try:
            scope = resolve_scope(session, profile)
            account = add_account(
                session,
                scope,
                platform=platform,
                handle=handle,
                credentials=credentials,
                cipher=cipher,
            )
        except (AccountError, ProfileNotFoundError, ProfileValidationError, CryptoError) as exc:
            abort(str(exc))
        console.print(
            f"[bold]Account added:[/bold] #{account.id} {account.platform}/{account.handle}"
        )
        console.print("Credentials are encrypted with secret.key and were not printed.")


@account_app.command("test")
def test_command(
    profile: Annotated[str, typer.Argument(help="Profile slug.")],
    platform: Annotated[
        str | None, typer.Option("--platform", help="Only test this platform.")
    ] = None,
    handle: Annotated[str | None, typer.Option("--handle", help="Only test this handle.")] = None,
) -> None:
    """Check stored credentials. M1 uses the offline fake adapter."""
    failed = False
    with session_for() as (layout, session):
        try:
            cipher = CredentialCipher.from_key_file(layout.key_path)
        except CryptoError as exc:
            abort(str(exc))
        try:
            scope = resolve_scope(session, profile)
            results = asyncio.run(
                check_accounts(
                    session,
                    scope,
                    cipher=cipher,
                    adapter=adapter_for(platform or "fake"),
                    platform=platform,
                    handle=handle,
                )
            )
        except (
            AccountError,
            NoAccountsError,
            ProfileNotFoundError,
            ProfileValidationError,
            CryptoError,
        ) as exc:
            abort(str(exc))
        table = Table(title="Account checks (fake adapter)")
        table.add_column("ID")
        table.add_column("Platform")
        table.add_column("Handle")
        table.add_column("Result")
        table.add_column("Detail")
        for result in results:
            table.add_row(
                str(result.account_id),
                result.platform,
                result.handle,
                "[green]ok[/green]" if result.ok else "[red]fail[/red]",
                result.detail,
            )
            failed = failed or not result.ok
        console.print(table)
    if failed:
        raise typer.Exit(code=1)
