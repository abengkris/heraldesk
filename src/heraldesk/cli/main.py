"""Heraldesk command line interface.

M0 exposes only ``init``, ``doctor`` and a ``serve`` stub. Content, accounts, queues and
reports arrive in later milestones.
"""

from __future__ import annotations

from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from heraldesk import __version__
from heraldesk.core.bootstrap import BootstrapError, initialize
from heraldesk.core.crypto import CryptoError
from heraldesk.core.doctor import CheckStatus, has_failures, run_checks
from heraldesk.paths import LayoutError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Self-hosted autonomous social media department.",
)
console = Console()


def _abort(message: str) -> NoReturn:
    console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(code=1)


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
        _abort(str(exc))

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
        typer.Option("--online", help="Also check network connectivity (not implemented yet)."),
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


@app.command("serve")
def serve_command(
    host: Annotated[str | None, typer.Option(help="Bind address (reserved for M7).")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port (reserved for M7).")] = None,
    no_web: Annotated[
        bool, typer.Option("--no-web", help="Disable the web UI (reserved).")
    ] = False,
    no_telegram: Annotated[
        bool, typer.Option("--no-telegram", help="Disable Telegram (reserved).")
    ] = False,
) -> None:
    """Run the server. Not implemented in M0."""
    _ = (host, port, no_web, no_telegram)
    console.print("heraldesk serve is not implemented yet.")
    console.print("M0 ships the CLI bootstrap only; the server arrives in M7.")
