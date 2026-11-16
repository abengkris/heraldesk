"""``heraldesk agent`` commands.

M2 ships one real agent (Writer) so the runtime can be exercised outside a pipeline. The
pipeline command ``heraldesk run daily`` arrives in M3.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.text import Text

from heraldesk.agents.base import Agent, AgentContext
from heraldesk.agents.errors import AgentError
from heraldesk.agents.runner import AgentRunner
from heraldesk.agents.schemas import Draft, WriterInput
from heraldesk.agents.writer import Writer
from heraldesk.cli.common import abort, console, session_for
from heraldesk.config import ConfigError, load_config
from heraldesk.domain.errors import ProfileNotFoundError, ProfileValidationError
from heraldesk.domain.profiles import load_profile, resolve_scope
from heraldesk.llm.errors import LLMError
from heraldesk.llm.factory import build_llm, resolve_model
from heraldesk.platforms.base import constraints_for

agent_app = typer.Typer(
    no_args_is_help=True,
    help="Run one agent outside a pipeline (M2 ships Writer only).",
)

IMPLEMENTED_AGENTS: dict[str, Agent] = {"writer": Writer()}


@agent_app.command("run")
def run_command(
    role: Annotated[str, typer.Argument(help="Agent role. M2 implements 'writer'.")],
    slug: Annotated[str, typer.Argument(help="Profile slug, for example 'acme'.")],
    idea: Annotated[str, typer.Option("--idea", help="The content idea to write about.")],
    platform: Annotated[
        str, typer.Option("--platform", help="Target platform, for example fake or x.")
    ] = "fake",
    angle: Annotated[
        str | None, typer.Option("--angle", help="Optional angle for the draft.")
    ] = None,
) -> None:
    """Run Writer once with the configured provider and record the trace."""
    agent = IMPLEMENTED_AGENTS.get(role)
    if agent is None:
        abort(
            f"agent '{role}' is not implemented yet; M2 ships: "
            + ", ".join(sorted(IMPLEMENTED_AGENTS))
        )
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

        context = AgentContext(
            scope=scope,
            profile=definition,
            platform=platform,
            constraints=constraints_for(platform),
        )
        runner = AgentRunner(
            session,
            provider,
            layout=layout,
            model=resolve_model(config, provider),
            cap_usd=config.llm.max_daily_cost_usd,
        )
        try:
            result = runner.run(
                agent,
                context,
                WriterInput(idea=idea, platform=platform, angle=angle),
            )
        except (AgentError, ValueError) as exc:
            abort(str(exc))

        draft = result.output
        if isinstance(draft, Draft):
            console.print(Text(draft.body))
            if draft.variants:
                console.print()
                for index, variant in enumerate(draft.variants, start=1):
                    console.print(Text(f"variant {index}: {variant}"))
            if draft.hashtags:
                console.print(Text("hashtags: " + " ".join(draft.hashtags)))
        console.print(
            f"\nrun #{result.run_id} | {result.agent} | model {result.model} | "
            f"tokens {result.tokens_in} in / {result.tokens_out} out | "
            f"cost ${result.cost_usd:.6f} | {result.attempts} attempt(s)"
        )
