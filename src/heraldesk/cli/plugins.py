"""Inspect the built-in and installed platform adapters."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from heraldesk.cli.common import console
from heraldesk.plugins import BUILT_IN_PLATFORMS, PluginError, discover_plugins

plugins_app = typer.Typer(
    help="Inspect platform adapter plugins.",
    no_args_is_help=True,
)

_BUILT_IN_TARGETS = {
    "fake": "heraldesk.platforms.fake:FakeAdapter",
    "threads": "heraldesk.platforms.threads:ThreadsAdapter",
    "nostr": "heraldesk.platforms.nostr:NostrAdapter",
}


@plugins_app.command("list")
def list_plugins(
    check: Annotated[
        bool,
        typer.Option("--check", help="Import each plugin and validate its adapter."),
    ] = False,
) -> None:
    """List built-in adapters and installed ``heraldesk.platforms`` plugins."""
    discovered = discover_plugins()
    table = Table(title="Platform adapters")
    table.add_column("Platform")
    table.add_column("Source")
    table.add_column("Target")
    table.add_column("Check")
    for platform in sorted(BUILT_IN_PLATFORMS):
        table.add_row(
            escape(platform),
            "built-in",
            escape(_BUILT_IN_TARGETS.get(platform, "-")),
            "-",
        )

    failed = False
    for platform, plugin in sorted(discovered.items()):
        status = "not checked"
        if check:
            try:
                adapter = plugin.factory()
            except PluginError as exc:
                status = f"[red]{escape(str(exc))}[/red]"
                failed = True
            else:
                status = f"ok ({type(adapter).__name__})"
        table.add_row(escape(platform), "entry point", escape(plugin.target), status)

    console.print(table)
    if failed:
        raise typer.Exit(1)
