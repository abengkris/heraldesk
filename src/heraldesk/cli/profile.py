"""``heraldesk profile`` commands."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.locks import is_global_kill_switch_enabled
from heraldesk.domain.errors import ProfileNotFoundError, ProfileValidationError
from heraldesk.domain.profiles import (
    ProfileStatus,
    ProfileType,
    create_profile,
    list_profiles,
    reload_all_profiles,
    reload_profile,
    resolve_scope,
    set_profile_kill_switch,
    set_profile_status,
)

profile_app = typer.Typer(
    no_args_is_help=True,
    help="Create, validate and control profiles.",
)


@profile_app.command("new")
def new_command(
    slug: Annotated[str, typer.Argument(help="Lowercase profile slug, also the folder name.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Display name shown in CLI output.")
    ] = None,
    profile_type: Annotated[
        ProfileType, typer.Option("--type", help="company, person, public_figure or other")
    ] = ProfileType.OTHER,
    timezone: Annotated[
        str, typer.Option("--timezone", help="IANA timezone, for example Europe/Berlin.")
    ] = "UTC",
    languages: Annotated[
        str, typer.Option("--languages", help="Comma-separated language tags, for example en,id.")
    ] = "en",
) -> None:
    """Create a non-interactive profile skeleton in ``profiles/<slug>/``."""
    codes = tuple(part.strip() for part in languages.split(",") if part.strip())
    with session_for() as (layout, session):
        try:
            summary = create_profile(
                session,
                layout,
                slug,
                name=name,
                profile_type=profile_type,
                timezone=timezone,
                languages=codes,
            )
        except FileExistsError as exc:
            abort(f"profile directory already exists: {exc.filename or slug}")
        except (ProfileValidationError, OSError) as exc:
            abort(str(exc))
        console.print(f"[bold]Profile created:[/bold] {summary.slug} ({summary.status})")
        console.print(f"Directory: {layout.profiles / summary.slug}")
    console.print(
        "Edit soul.md, brief.md, policy.yaml and banned.yaml, then run "
        f"[cyan]heraldesk profile reload {slug}[/cyan] and "
        f"[cyan]heraldesk profile resume {slug}[/cyan]."
    )


@profile_app.command("list")
def list_command() -> None:
    """List registered profiles and the global off-switch state."""
    with session_for() as (layout, session):
        profiles = list_profiles(session)
        global_off = is_global_kill_switch_enabled(layout)
    if global_off:
        console.print(
            "[bold red]Global kill switch is engaged. "
            "Publishing and replies are halted for every profile.[/bold red]"
        )
    if not profiles:
        console.print(
            "No profiles registered. Create one with [cyan]heraldesk profile new <slug>[/cyan]."
        )
        return
    table = Table(title="Profiles")
    table.add_column("Slug", style="bold")
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Timezone")
    table.add_column("Languages")
    table.add_column("Status")
    table.add_column("Kill switch")
    for profile in profiles:
        table.add_row(
            profile.slug,
            profile.name,
            profile.profile_type,
            profile.timezone,
            ",".join(profile.languages),
            profile.status,
            "on" if profile.kill_switch else "off",
        )
    console.print(table)


@profile_app.command("reload")
def reload_command(
    slug: Annotated[
        str | None,
        typer.Argument(help="Profile slug. Omit to validate every profile directory."),
    ] = None,
) -> None:
    """Validate profile files and refresh stored metadata and hashes."""
    failed = False
    with session_for() as (layout, session):
        if slug is not None:
            try:
                summary = reload_profile(session, layout, slug)
            except (ProfileNotFoundError, ProfileValidationError, OSError) as exc:
                abort(str(exc))
            console.print(
                f"[bold]Reloaded:[/bold] {summary.slug} "
                f"(status {summary.status}, hash {summary.config_hash[:12]})"
            )
            return
        outcomes = reload_all_profiles(session, layout)
        if not outcomes:
            console.print("No profile directories found.")
            return
        for outcome in outcomes:
            if outcome.ok and outcome.summary is not None:
                console.print(
                    f"[green]ok[/green]   {outcome.slug} "
                    f"(status {outcome.summary.status}, "
                    f"hash {outcome.summary.config_hash[:12]})"
                )
            else:
                failed = True
                console.print(f"[red]fail[/red] {outcome.slug}: {outcome.error}")
    if failed:
        raise typer.Exit(code=1)


@profile_app.command("pause")
def pause_command(
    slug: Annotated[str, typer.Argument(help="Profile slug.")],
) -> None:
    """Pause a profile. Publishing is blocked by the off-switch precedence."""
    with session_for() as (_, session):
        try:
            scope = resolve_scope(session, slug)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        summary, changed = set_profile_status(session, scope, ProfileStatus.PAUSED)
    state = "paused" if changed else f"already {summary.status}"
    console.print(f"[bold]{summary.slug}[/bold] {state}.")


@profile_app.command("resume")
def resume_command(
    slug: Annotated[str, typer.Argument(help="Profile slug.")],
) -> None:
    """Activate a profile. A global or per-profile kill switch still wins."""
    with session_for() as (_, session):
        try:
            scope = resolve_scope(session, slug)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        summary, changed = set_profile_status(session, scope, ProfileStatus.ACTIVE)
    state = "active" if changed else f"already {summary.status}"
    console.print(f"[bold]{summary.slug}[/bold] {state}.")


@profile_app.command("killswitch")
def killswitch_command(
    slug: Annotated[str, typer.Argument(help="Profile slug.")],
    off: Annotated[bool, typer.Option("--off", help="Clear the per-profile kill switch.")] = False,
) -> None:
    """Engage or clear the per-profile kill switch."""
    with session_for() as (_, session):
        try:
            scope = resolve_scope(session, slug)
        except (ProfileNotFoundError, ProfileValidationError) as exc:
            abort(str(exc))
        summary, changed = set_profile_kill_switch(session, scope, not off)
    state = "engaged" if summary.kill_switch else "cleared"
    suffix = "" if changed else " (unchanged)"
    console.print(f"[bold]{summary.slug}[/bold] kill switch {state}{suffix}.")
