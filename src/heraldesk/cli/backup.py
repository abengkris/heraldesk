"""``heraldesk backup`` and ``heraldesk restore``: private instance archives."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Annotated

import typer

from heraldesk import paths
from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.audit import AuditService
from heraldesk.core.backup import BackupError, create_backup, restore_backup
from heraldesk.db.session import DatabaseNotInitialized, database_session
from heraldesk.web.auth import AuthConfigError, rotate_session_epoch, session_epoch_path


def backup_command(
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write the archive to this path instead of backups/."),
    ] = None,
    with_credentials: Annotated[
        bool,
        typer.Option(
            "--with-credentials",
            help="Include secret.key. Without it, the backup cannot restore the database.",
        ),
    ] = False,
) -> None:
    """Create a ZIP backup of the database, config, profiles and agent overrides."""
    with session_for() as (layout, session):
        try:
            result = create_backup(
                layout,
                output=output,
                include_credentials=with_credentials,
            )
        except BackupError as exc:
            abort(str(exc))
        AuditService(session).record(
            None,
            actor="user",
            action="backup.created",
            entity="backup",
            entity_id=result.path.name[:64],
            payload={
                "path": str(result.path),
                "include_key": result.includes_credentials,
                "entries": len(result.entries),
            },
        )

    console.print(f"[bold]Backup written:[/bold] {result.path}")
    console.print(f"Entries: {len(result.entries)}")
    if result.includes_credentials:
        console.print("[green]Credentials:[/green] secret.key included")
    else:
        console.print(
            "[yellow]Credentials:[/yellow] excluded; use --with-credentials "
            "for a database-restorable backup"
        )


def restore_command(
    archive: Annotated[Path, typer.Argument(metavar="ARCHIVE", help="Backup ZIP to restore.")],
    without_credentials: Annotated[
        bool,
        typer.Option(
            "--without-credentials",
            help="Restore config, profiles and agents only; never the database or secret.key.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Replace existing database, config, key, profiles and agents.",
        ),
    ] = False,
) -> None:
    """Restore a backup after validating it. Use --force to replace existing data."""
    layout = paths.layout_for(paths.resolve_home())
    try:
        result = restore_backup(
            layout,
            archive,
            without_credentials=without_credentials,
            force=force,
        )
    except BackupError as exc:
        abort(str(exc))

    with contextlib.suppress(AuthConfigError):
        rotate_session_epoch(session_epoch_path(layout))

    if result.restored_db:
        try:
            with database_session(layout) as session:
                AuditService(session).record(
                    None,
                    actor="user",
                    action="backup.restored",
                    entity="backup",
                    entity_id=result.archive.name[:64],
                    payload={
                        "path": str(result.archive),
                        "restored_db": True,
                        "key_restored": result.restored_credentials,
                        "entries": len(result.entries),
                    },
                )
        except DatabaseNotInitialized as exc:
            abort(f"database was restored but cannot be opened: {exc}")

    console.print(f"[bold]Restore complete:[/bold] {result.archive}")
    console.print(f"Data folder: {layout.home}")
    console.print(f"Database restored: {'yes' if result.restored_db else 'no'}")
    console.print(f"Credentials restored: {'yes' if result.restored_credentials else 'no'}")
    if not result.restored_db:
        console.print(
            "[yellow]Files only:[/yellow] run 'heraldesk init' to create a new database, "
            "then reload profiles."
        )
