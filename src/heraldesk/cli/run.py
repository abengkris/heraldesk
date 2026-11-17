"""``heraldesk run`` pipeline commands."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table
from rich.text import Text

from heraldesk.agents.errors import AgentError
from heraldesk.cli.common import abort, console, session_for
from heraldesk.config import Config, ConfigError, load_config
from heraldesk.domain.errors import ProfileNotFoundError, ProfileValidationError
from heraldesk.domain.profiles import load_profile, resolve_scope
from heraldesk.llm.errors import LLMError
from heraldesk.llm.factory import build_llm, resolve_model
from heraldesk.llm.types import LLMProvider
from heraldesk.pipelines.daily_content import DailyContentError, run_daily_content
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.tools.web_search import DuckDuckGoHTMLSearch, FakeWebSearch, WebSearch

run_app = typer.Typer(
    no_args_is_help=True,
    help="Run deterministic pipelines.",
)


def build_search(config: Config, provider: LLMProvider) -> WebSearch:
    """Choose the search provider. ``auto`` stays offline with the fake LLM."""
    choice = config.research.search_provider
    if choice == "fake" or (choice == "auto" and provider.name == "fake"):
        return FakeWebSearch()
    return DuckDuckGoHTMLSearch(timeout_seconds=config.research.timeout_seconds)


@run_app.command("daily")
def daily_command(
    slug: Annotated[str, typer.Argument(help="Profile slug, for example 'acme'.")],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Simulate only. Nothing is written to the database.",
        ),
    ] = False,
) -> None:
    """Run the daily_content pipeline for one profile."""
    with session_for() as (layout, session):
        try:
            config = load_config(layout.home)
            provider = build_llm(config, layout)
            scope = resolve_scope(session, slug)
            definition = load_profile(layout.profiles / slug)
        except (
            ConfigError,
            LLMError,
            ProfileNotFoundError,
            ProfileValidationError,
            OSError,
        ) as exc:
            abort(str(exc))

        try:
            result = run_daily_content(
                session,
                layout,
                scope,
                definition,
                provider=provider,
                search=build_search(config, provider),
                model=resolve_model(config, provider),
                cap_usd=config.llm.max_daily_cost_usd,
                dry_run=dry_run,
                adapter=FakeAdapter(),
            )
        except (DailyContentError, AgentError, LLMError, ValueError) as exc:
            abort(str(exc))

        if dry_run:
            console.print(
                f"[bold]Daily content simulation:[/bold] {definition.slug} "
                "[yellow](dry run, nothing persisted)[/yellow]"
            )
        else:
            console.print(
                f"[bold]Daily content:[/bold] {definition.slug} "
                f"(run #{result.pipeline_run_id}, {result.status})"
            )

        if not result.items:
            console.print("No drafts were produced. Every idea may have been a near-duplicate.")
        else:
            table = Table(show_lines=False)
            table.add_column("ID", justify="right")
            table.add_column("Platform", style="bold")
            table.add_column("State")
            table.add_column("Risk")
            table.add_column("Reason")
            risk_style = {
                "low": "green",
                "medium": "yellow",
                "high": "red",
                "blocked": "red",
            }
            for item in result.items:
                item_id = "sim" if item.item_id is None else str(item.item_id)
                risk = item.risk_level or "-"
                style = risk_style.get(risk, "white")
                table.add_row(
                    item_id,
                    item.platform,
                    item.state,
                    f"[{style}]{risk}[/]",
                    item.reason or "",
                )
            console.print(table)

            console.print("\n[bold]Drafts[/bold]")
            for index, item in enumerate(result.items, start=1):
                label = f"simulated {index}" if item.item_id is None else f"#{item.item_id}"
                console.print(f"[bold]{label}[/bold] ({item.platform}, {item.state})")
                console.print(Text(item.body))
                console.print()

        console.print(
            f"Skipped ideas (similar within 60 days): {result.skipped_ideas} | "
            f"LLM cost: ${result.total_cost_usd:.6f}"
        )
        for note in result.notes:
            console.print(f"[yellow]note:[/yellow] {note}")


@run_app.command("scheduler")
def scheduler_command(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List what would publish without writing anything.",
        ),
    ] = False,
) -> None:
    """Run the scheduler once. Alias of ``heraldesk scheduler run``."""
    from heraldesk.cli.scheduler import execute_scheduler

    execute_scheduler(dry_run=dry_run)
