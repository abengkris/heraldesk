"""``heraldesk account`` commands."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlsplit

import typer
from rich.markup import escape
from rich.table import Table

from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher, CryptoError
from heraldesk.db.repositories import SocialAccountRepository
from heraldesk.domain.accounts import add_account, check_accounts
from heraldesk.domain.errors import (
    AccountError,
    NoAccountsError,
    ProfileNotFoundError,
    ProfileValidationError,
)
from heraldesk.domain.profiles import resolve_scope
from heraldesk.platforms import adapter_for
from heraldesk.platforms.base import PublishError
from heraldesk.platforms.nostr import parse_relay_list
from heraldesk.platforms.nostr_keys import is_npub, mask_npub
from heraldesk.platforms.threads import ThreadsAdapter

account_app = typer.Typer(
    no_args_is_help=True,
    help="Add, list and test social accounts.",
)

_CREDENTIAL_ENV_KEYS = {"NSEC": "nsec"}


def _parse_credentials(items: list[str] | None) -> dict[str, str]:
    credentials: dict[str, str] = {}
    for item in items or []:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise AccountError("invalid --credential; expected KEY=VALUE")
        credentials[key.strip()] = value
    return credentials


def _env_credential_key(name: str) -> str:
    """Map an environment variable name to a credential key without guessing values.

    ``NOSTR_NSEC_THROWAY`` becomes ``nsec`` because the name carries the ``NSEC`` marker.
    Any other name is lowercased, so ``MY_PLATFORM_TOKEN`` becomes ``my_platform_token``;
    callers that need a different key should use ``--credential``.
    """
    for marker, key in _CREDENTIAL_ENV_KEYS.items():
        if marker in name.upper():
            return key
    return name.strip().lower()


def _credentials_from_env(names: list[str] | None) -> dict[str, str]:
    credentials: dict[str, str] = {}
    for raw in names or []:
        name = raw.strip()
        if not name:
            raise AccountError("--credential-from-env requires a variable name")
        value = os.environ.get(name, "")
        if not value.strip():
            raise AccountError(
                f"environment variable {name} is missing or empty; export it in your "
                "terminal before adding the account"
            )
        credentials[_env_credential_key(name)] = value
    return credentials


def _relay_summary(value: str) -> str:
    hosts = [urlsplit(part.strip()).hostname or part.strip() for part in value.split(",")]
    if len(hosts) > 3:
        return ", ".join(hosts[:3]) + f" +{len(hosts) - 3}"
    return ", ".join(hosts)


@account_app.command("add")
def add_command(
    profile: Annotated[str, typer.Argument(help="Profile slug.")],
    platform: Annotated[
        str,
        typer.Option(
            "--platform",
            help="fake, threads, nostr, x, facebook, instagram, linkedin or an installed plugin",
        ),
    ],
    handle: Annotated[
        str,
        typer.Option("--handle", help="Handle shown in listings. Nostr defaults to the npub."),
    ] = "",
    label: Annotated[
        str | None,
        typer.Option("--label", help="Optional human label, for example 'throwaway'."),
    ] = None,
    credential: Annotated[
        list[str] | None,
        typer.Option(
            "--credential",
            "-c",
            help="Repeatable KEY=VALUE credential. Values are never printed.",
        ),
    ] = None,
    credential_from_env: Annotated[
        list[str] | None,
        typer.Option(
            "--credential-from-env",
            help=(
                "Repeatable environment variable name to read a credential from. "
                "A name containing NSEC is stored as the nostr nsec."
            ),
        ),
    ] = None,
    relay: Annotated[
        list[str] | None,
        typer.Option("--relay", help="Repeatable wss:// relay for a Nostr account."),
    ] = None,
) -> None:
    """Encrypt account credentials and store the account for a profile."""
    try:
        credentials = _parse_credentials(credential)
        credentials.update(_credentials_from_env(credential_from_env))
        if not credentials:
            raise AccountError(
                "at least one --credential or --credential-from-env is required; "
                "values are encrypted at rest"
            )
        if relay:
            if platform.strip().lower() != "nostr":
                raise AccountError("--relay is only valid with --platform nostr")
            credentials["relays"] = ",".join(parse_relay_list(relay))
    except (AccountError, PublishError) as exc:
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
                label=label,
                credentials=credentials,
                cipher=cipher,
            )
        except (AccountError, ProfileNotFoundError, ProfileValidationError, CryptoError) as exc:
            abort(str(exc))
        console.print(
            f"[bold]Account added:[/bold] #{account.id} {account.platform}/"
            f"{mask_npub(account.handle) if is_npub(account.handle) else account.handle}"
        )
        if account.label:
            console.print(f"Label: {account.label}")
        console.print("Credentials are encrypted with secret.key and were not printed.")


@account_app.command("list")
def list_command(
    profile: Annotated[str, typer.Argument(help="Profile slug.")],
) -> None:
    """List accounts with the public npub masked. The nsec is never shown."""
    with session_for() as (layout, session):
        try:
            cipher = CredentialCipher.from_key_file(layout.key_path)
        except CryptoError as exc:
            abort(str(exc))
        try:
            scope = resolve_scope(session, profile)
            accounts = SocialAccountRepository(session).list(scope)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        if not accounts:
            console.print(f"No accounts for profile '{profile}'.")
            return

        table = Table(title=f"Accounts for {profile}")
        table.add_column("ID")
        table.add_column("Platform")
        table.add_column("Handle")
        table.add_column("Label")
        table.add_column("Status")
        table.add_column("Relays")
        for account in accounts:
            handle = (
                mask_npub(account.handle)
                if account.platform == "nostr" and is_npub(account.handle)
                else account.handle
            )
            relays = "-"
            if account.platform == "nostr":
                try:
                    credentials = cipher.decrypt_json(account.credentials_encrypted)
                except CryptoError:
                    relays = "unavailable"
                else:
                    stored = credentials.get("relays", "").strip()
                    relays = _relay_summary(stored) if stored else "(heraldesk.yaml)"
            table.add_row(
                str(account.id),
                account.platform,
                handle,
                account.label or "-",
                account.status,
                relays,
            )
        console.print(table)


@account_app.command("test")
def test_command(
    profile: Annotated[str, typer.Argument(help="Profile slug.")],
    platform: Annotated[
        str | None, typer.Option("--platform", help="Only test this platform.")
    ] = None,
    handle: Annotated[str | None, typer.Option("--handle", help="Only test this handle.")] = None,
) -> None:
    """Check stored credentials. Nostr checks the nsec offline; others use the fake adapter."""
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
        table = Table(title="Account checks")
        table.add_column("ID")
        table.add_column("Platform")
        table.add_column("Handle")
        table.add_column("Result")
        table.add_column("Detail")
        for result in results:
            masked = mask_npub(result.handle) if is_npub(result.handle) else result.handle
            table.add_row(
                str(result.account_id),
                result.platform,
                masked,
                "[green]ok[/green]" if result.ok else "[red]fail[/red]",
                result.detail,
            )
            failed = failed or not result.ok
        console.print(table)
    if failed:
        raise typer.Exit(code=1)


@account_app.command("refresh")
def refresh_command(
    profile: Annotated[str, typer.Argument(help="Profile slug.")],
    platform: Annotated[
        str,
        typer.Option("--platform", help="Platform to refresh. Currently only threads."),
    ] = "threads",
    handle: Annotated[
        str | None,
        typer.Option("--handle", help="Only refresh this handle."),
    ] = None,
) -> None:
    """Refresh a long-lived Threads access token and store its new expiry."""
    if platform.strip().lower() != "threads":
        abort("token refresh is currently implemented for --platform threads only")

    adapter = ThreadsAdapter()
    with session_for() as (layout, session):
        try:
            scope = resolve_scope(session, profile)
            cipher = CredentialCipher.from_key_file(layout.key_path)
            repository = SocialAccountRepository(session)
            accounts = [
                account
                for account in repository.list(scope)
                if account.platform == "threads"
                and (handle is None or account.handle == handle.strip())
            ]
        except (ProfileNotFoundError, ProfileValidationError, CryptoError) as exc:
            abort(str(exc))
        if not accounts:
            abort("no matching threads account is configured")

        refreshed: list[tuple[str, datetime | None]] = []
        for account in accounts:
            try:
                credentials = cipher.decrypt_json(account.credentials_encrypted)
                token = credentials.get("access_token", "").strip()
                if not token:
                    raise PublishError("stored account has no access_token")
                result = asyncio.run(adapter.refresh_token(token))
            except (CryptoError, PublishError) as exc:
                abort(f"cannot refresh {account.handle}: {exc}")

            credentials["access_token"] = result.access_token
            expires_at: datetime | None = None
            if result.expires_in is not None:
                expires_at = datetime.now(UTC) + timedelta(seconds=result.expires_in)
                credentials["expires_at"] = expires_at.isoformat()
            repository.set_credentials(
                scope,
                account.id,
                credentials_encrypted=cipher.encrypt_json(credentials),
            )
            AuditService(session).record(
                scope,
                actor="user",
                action="account.refreshed",
                entity="social_account",
                entity_id=account.id,
                payload={
                    "platform": account.platform,
                    "handle": account.handle,
                    "expires_in_seconds": result.expires_in,
                    "expires_at": None if expires_at is None else expires_at.isoformat(),
                },
            )
            refreshed.append((account.handle, expires_at))

    for account_handle, expires_at in refreshed:
        expiry = "unknown" if expires_at is None else expires_at.isoformat()
        console.print(f"[green]Refreshed[/green] {escape(account_handle)}; expires {expiry}")
    console.print("The new token is encrypted with secret.key and was not printed.")
