# AGENTS.md — Instructions for coding agents and contributors

Heraldesk is an open-source, single-user, self-hosted autonomous "social media department"
(Strategist, Researcher, Writer, Designer, Community Manager, Analyst, Compliance).
The full design is in `docs/architecture.md`. Read it before writing code.

## Core principles (never violate)
1. Easy install: one command (`pipx install` / `uvx` / `docker run`). No external services at runtime
   (no Redis, no Postgres, no Node). SQLite only.
2. One process: `heraldesk serve` runs API, web UI, scheduler, worker and Telegram bot.
3. Deterministic pipelines with explicit state machines. LLMs are used inside steps, never to decide the flow.
4. Safe by default: every profile starts in `draft_only`. Server binds to `127.0.0.1` by default.
5. Bring your own key (BYOK). No vendor lock-in. Provider access goes through LiteLLM.
6. Agent identity, prompts and policies are plain text files the user can edit.
7. Every action is written to the append-only audit log.
8. Agent outputs are structured (Pydantic), validated, retried on failure.
9. Official platform APIs only. No scraping, no browser automation that violates a platform's ToS.
10. No telemetry, no phone-home.

## Tech stack
Python 3.12, FastAPI, SQLAlchemy 2 + Alembic (SQLite WAL), APScheduler, LiteLLM, Typer + Rich,
Jinja2 + HTMX + Tailwind (prebuilt CSS), pytest, ruff, mypy, uv.

## Commands (keep this section up to date as the project grows)
- Install dev deps: `uv sync`
- Run tests: `uv run pytest`
- Run the core cross-platform subset (what Windows CI runs): `uv run pytest -m core`
- Lint/format: `uv run ruff check . && uv run ruff format .`
- Type check: `uv run mypy src/heraldesk`
- Run app: `uv run heraldesk serve` (M0 stub)
- Initialise an instance: `uv run heraldesk init`
- Diagnose an instance offline: `uv run heraldesk doctor` (network checks are opt-in with `--online`)
- Create a profile skeleton: `uv run heraldesk profile new <slug> --name "Name" --type company --timezone UTC --languages en,id`
- Validate profile files: `uv run heraldesk profile reload [slug]`
- Control a profile: `uv run heraldesk profile pause|resume|killswitch <slug>`
- Add an account: `uv run heraldesk account add <slug> --platform fake --handle @name --credential token=...`
- Test accounts offline: `uv run heraldesk account test <slug>` (fake adapter in M1)
- Run the Writer once and record the trace: `uv run heraldesk agent run writer <slug> --idea "..."`
- Run the daily content pipeline: `uv run heraldesk run daily <slug> [--dry-run]` (`--dry-run` writes nothing)
- List the approval queue: `uv run heraldesk queue list <slug>`
- Approve or reject queued content: `uv run heraldesk queue approve <id>`, `uv run heraldesk queue reject <id> [--reason "..."]`
- Edit queued content: `uv run heraldesk queue edit <id> --body "..."` or `--body-file <path>`
- Show agent run traces and cost: `uv run heraldesk runs <slug>`
- Halt everything: `uv run heraldesk stop-all` (`--off` clears the global kill switch)
- Try a throwaway instance: `HERALDESK_HOME=$(mktemp -d) uv run heraldesk init`

## Code conventions
- Type hints on all public functions. `mypy --strict` on `core/` and `domain/`.
- Domain logic lives in `src/heraldesk/domain/`. Adapters contain no business logic.
- State changes of content go only through `domain.content.transition()`.
- Every DB query touching profile data goes through `ProfileScope(profile_id)`.
- Never put credentials, API keys or real account data in code, tests, fixtures or logs.
- Keep functions small; prefer explicit over clever. Write docstrings for public APIs.

## Testing rules
- Every feature ships with tests. Use the fake LLM provider and fake platform adapter; tests must run offline.
- Tests must cover: state machine transitions, profile isolation, autonomy policy decisions, adapter contracts.
- Do not mark a task done while tests, ruff or mypy fail.

## Working rules for agents
- Work on ONE milestone at a time (see `ROADMAP.md`). Do not start the next one.
- Before coding: write a short plan (files to touch, tests to add) and wait for confirmation if in plan mode.
- Do not add heavy dependencies without recording the reason in `docs/decisions.md`.
- If the spec is ambiguous or conflicts with a platform's real API behavior, STOP and write the question
  to `docs/open-questions.md`. Do not guess.
- Make small, reviewable commits with clear messages (Conventional Commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`).
- Reject solutions that require external services at runtime.
- Do not edit `LICENSE`, `SECURITY.md` or CI release workflows unless the task says so.

## Language
Code, comments, docs, CLI messages and commit messages are in English. Indonesian docs live in `docs/id/`.
Content language per profile is configurable (`languages` in profile config).

## Acceptable use (also stated in README)
For managing accounts you own or are authorized to manage. No fake-account networks, impersonation or spam.
