"""Heraldesk command line interface.

The CLI exposes bootstrap, profile, account, off-switch, single-agent, daily pipeline,
run-trace, approval-queue, publishing, scheduler, Telegram, manual inbox triage,
deterministic report and backup/restore commands. M7 adds the single-process server, M8
adds ``inbox`` and ``report``, and M9 adds ``backup`` and ``restore``.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from heraldesk import __version__, paths
from heraldesk.cli.account import account_app
from heraldesk.cli.agent import agent_app
from heraldesk.cli.backup import backup_command, restore_command
from heraldesk.cli.common import abort, console, session_for
from heraldesk.cli.inbox import inbox_app
from heraldesk.cli.plugins import plugins_app
from heraldesk.cli.profile import profile_app
from heraldesk.cli.publish import publish_command
from heraldesk.cli.queue import queue_app
from heraldesk.cli.report import report_app
from heraldesk.cli.run import run_app
from heraldesk.cli.runs import runs_command
from heraldesk.cli.scheduler import scheduler_app
from heraldesk.cli.telegram import telegram_app
from heraldesk.config import ConfigError
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import BootstrapError, initialize
from heraldesk.core.crypto import CryptoError
from heraldesk.core.doctor import CheckStatus, has_failures, run_checks
from heraldesk.core.locks import (
    KillSwitchError,
    engage_global_kill_switch,
    release_global_kill_switch,
)
from heraldesk.paths import LayoutError
from heraldesk.telegram.runtime import notifier_for

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Self-hosted autonomous social media department.",
)
app.add_typer(profile_app, name="profile")
app.add_typer(account_app, name="account")
app.add_typer(agent_app, name="agent")
app.add_typer(run_app, name="run")
app.add_typer(queue_app, name="queue")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(telegram_app, name="telegram")
app.add_typer(inbox_app, name="inbox")
app.add_typer(report_app, name="report")
app.add_typer(plugins_app, name="plugins")
app.command("publish")(publish_command)
app.command("runs")(runs_command)
app.command("backup")(backup_command)
app.command("restore")(restore_command)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"heraldesk {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Heraldesk command line interface."""


@app.command("init")
def init_command() -> None:
    """Create the data folder, key, default config and database."""
    try:
        result = initialize()
    except (BootstrapError, CryptoError, LayoutError) as exc:
        abort(str(exc))

    console.print("[bold]Heraldesk is initialised.[/bold]")
    console.print(f"Data folder: {result.layout.home}")
    config_state = "created" if result.config_created else "kept existing"
    key_state = "created (mode 600)" if result.key_created else "kept existing"
    database_state = "created" if result.db_created else "kept existing"
    console.print(f"  heraldesk.yaml   {config_state}")
    console.print(f"  secret.key       {key_state}")
    console.print(f"  heraldesk.db     {database_state}")
    console.print(f"  schema revision  {result.revision or 'unknown'}")
    console.print(
        "\nNext: put your LLM API key in [cyan].env[/cyan] and review [cyan]heraldesk.yaml[/cyan]."
    )


@app.command("doctor")
def doctor_command(
    online: Annotated[
        bool,
        typer.Option(
            "--online",
            help="Also probe the LLM provider, Telegram and configured Nostr relays.",
        ),
    ] = False,
) -> None:
    """Check the installation. Offline by default."""
    results = run_checks(online=online)
    table = Table(title="heraldesk doctor", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Result")
    table.add_column("Detail")
    styles = {
        CheckStatus.PASS: "green",
        CheckStatus.WARN: "yellow",
        CheckStatus.FAIL: "red",
    }
    for result in results:
        table.add_row(
            result.name,
            f"[{styles[result.status]}]{result.status.value}[/]",
            result.detail,
        )
    console.print(table)

    if has_failures(results):
        console.print("[bold red]Some checks failed.[/bold red]")
        raise typer.Exit(code=1)
    warnings = sum(1 for result in results if result.status is CheckStatus.WARN)
    if warnings:
        console.print(f"[yellow]All checks passed with {warnings} warning(s).[/yellow]")
    else:
        console.print("[green]All checks passed.[/green]")


@app.command("stop-all")
def stop_all_command(
    off: Annotated[
        bool, typer.Option("--off", help="Clear the global kill switch instead of engaging it.")
    ] = False,
) -> None:
    """Engage or clear the global kill switch. It outranks every profile setting."""
    with session_for() as (layout, session):
        service = AuditService(session)
        entry_id: int | str | None = None
        try:
            if off:
                changed = release_global_kill_switch(layout)
                if changed:
                    entry = service.record(
                        None,
                        actor="user",
                        action="killswitch.disengaged",
                        entity="killswitch",
                        payload={"scope": "global", "enabled": False},
                    )
                    entry_id = entry.id
                console.print(
                    "[bold red]Global kill switch cleared.[/bold red]"
                    if changed
                    else "Global kill switch was already clear."
                )
            else:
                changed = engage_global_kill_switch(layout)
                if changed:
                    entry = service.record(
                        None,
                        actor="user",
                        action="killswitch.engaged",
                        entity="killswitch",
                        payload={"scope": "global", "enabled": True},
                    )
                    entry_id = entry.id
                console.print(
                    "[bold red]Global kill switch engaged.[/bold red] "
                    "Publishing and replies are halted for every profile."
                    if changed
                    else "Global kill switch was already engaged."
                )
        except KillSwitchError as exc:
            abort(str(exc))
        if changed and entry_id is not None:
            notifier = notifier_for(session, layout)
            if notifier is not None:
                notifier.notify_killswitch(
                    None,
                    slug=None,
                    enabled=not off,
                    event_id=str(entry_id),
                )


@app.command("serve")
def serve_command(
    host: Annotated[
        str | None,
        typer.Option("--host", help="Bind address. Defaults to server.host (127.0.0.1)."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", min=1, max=65535, help="Bind port. Defaults to server.port (8000)."),
    ] = None,
    no_web: Annotated[
        bool,
        typer.Option("--no-web", help="Disable the Web UI; the JSON API stays available."),
    ] = False,
    no_telegram: Annotated[
        bool,
        typer.Option("--no-telegram", help="Disable Telegram polling and notifications."),
    ] = False,
    allow_remote: Annotated[
        bool,
        typer.Option(
            "--allow-remote",
            help=(
                "Allow a non-loopback bind. Requires HERALDESK_ADMIN_TOKEN and "
                "HERALDESK_TRUST_PROXY=1 behind HTTPS."
            ),
        ),
    ] = False,
) -> None:
    """Run the API, Web UI, scheduler and Telegram bot in one process."""
    # Imported lazily so ordinary CLI commands do not pay for the web stack.
    from heraldesk.server.run import ServerError, run_server

    try:
        run_server(
            host=host,
            port=port,
            enable_telegram=not no_telegram,
            enable_web=not no_web,
            allow_remote=allow_remote,
        )
    except (ConfigError, ServerError) as exc:
        abort(str(exc))


admin_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect the single-admin authentication token.",
)
app.add_typer(admin_app, name="admin")


@admin_app.command("token")
def admin_token_command(
    show: Annotated[
        bool,
        typer.Option(
            "--show",
            help="Print the raw token. It is a secret; never put it in logs or screenshots.",
        ),
    ] = False,
) -> None:
    """Show the admin token fingerprint, or the raw token with --show."""
    from heraldesk.web.auth import AuthConfigError, mask_token, resolve_admin_token

    layout = paths.layout_for(paths.resolve_home())
    try:
        resolution = resolve_admin_token(layout)
    except AuthConfigError as exc:
        abort(str(exc))
    console.print(f"Admin token source: [bold]{resolution.source}[/bold]")
    console.print(f"Fingerprint: {mask_token(resolution.token)}")
    if show:
        console.print("[yellow]Raw token (secret; do not log or screenshot):[/yellow]")
        console.print(resolution.token)
