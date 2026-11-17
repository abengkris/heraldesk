"""Shared CLI helpers: console, failure exit and database session context."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.db.session import DatabaseNotInitialized, database_session

console = Console()


def abort(message: str) -> NoReturn:
    """Print a red error and exit with status 1."""
    console.print(f"[bold red]error:[/bold red] {message}")
    raise typer.Exit(code=1)


@contextmanager
def session_for(home: Path | None = None) -> Iterator[tuple[paths.Layout, Session]]:
    """Open a committing database session or exit when the instance is not initialised."""
    layout = paths.layout_for(home if home is not None else paths.resolve_home())
    try:
        with database_session(layout) as session:
            yield layout, session
    except DatabaseNotInitialized as exc:
        abort(str(exc))
    except OperationalError as exc:
        original = str(getattr(exc, "orig", exc))
        if "no such column" in original or "no such table" in original:
            abort(
                "database schema is out of date; run 'heraldesk init' to apply pending migrations"
            )
        abort("database error; run 'heraldesk doctor' for details")
