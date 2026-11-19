"""``heraldesk upgrade``: back up, then apply pending migrations."""

from __future__ import annotations

from heraldesk import paths
from heraldesk.cli.common import abort, console
from heraldesk.core.audit import AuditService
from heraldesk.core.backup import BackupError, create_backup
from heraldesk.core.bootstrap import BootstrapError, initialize
from heraldesk.db.engine import inspect_database
from heraldesk.db.migrations import MigrationError, head_revision
from heraldesk.db.session import DatabaseNotInitialized, database_session


def upgrade_command() -> None:
    """Create a credential backup and apply pending database migrations."""
    layout = paths.layout_for(paths.resolve_home())
    status = inspect_database(layout.db_path)
    if not status.exists or status.error is not None:
        abort("database is not readable; run 'heraldesk init' first")

    try:
        head = head_revision(layout.db_path)
    except MigrationError as exc:
        abort(str(exc))

    if status.revision == head:
        console.print(f"[green]Already up to date:[/green] schema revision {head}")
        return

    try:
        backup = create_backup(layout, include_credentials=True)
    except BackupError as exc:
        abort(f"cannot create a safety backup; upgrade aborted: {exc}")

    console.print(f"[bold]Backup written:[/bold] {backup.path}")
    try:
        result = initialize(layout.home)
    except BootstrapError as exc:
        abort(f"upgrade failed; the backup is at {backup.path}: {exc}")

    try:
        with database_session(layout) as session:
            AuditService(session).record(
                None,
                actor="user",
                action="system.upgraded",
                entity="system",
                entity_id=head[:64],
                payload={
                    "from_revision": status.revision,
                    "to_revision": result.revision,
                    "backup": backup.path.name,
                    "include_key": True,
                },
            )
    except DatabaseNotInitialized as exc:
        abort(f"migration finished but the database cannot be opened: {exc}")

    console.print(
        f"[green]Upgrade complete:[/green] schema {status.revision or 'none'} -> {result.revision}"
    )
