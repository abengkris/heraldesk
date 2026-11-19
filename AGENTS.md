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
Jinja2 + HTMX (vendored) + CSS variables (no Node build), pytest, ruff, mypy, uv.

## Commands (keep this section up to date as the project grows)
- Install dev deps: `uv sync`
- Build distributions: `uv build` (wheel + sdist); release from a tag, see `docs/releasing.md`
- Build the container image: `docker build -t heraldesk:local .`
- Run tests: `uv run pytest`
- Run the core cross-platform subset (what Windows CI runs): `uv run pytest -m core`
- Lint/format: `uv run ruff check . && uv run ruff format .`
- Type check: `uv run mypy src/heraldesk`
- Run the single-process server: `uv run heraldesk serve [--host 127.0.0.1] [--port 8000] [--no-web] [--no-telegram]`
  (Web UI at `/`, JSON API at `/api/v1`, public liveness at `/health`; scheduler sweep every 60 s; Telegram
  polling every 10 s when `telegram.enabled` and `telegram.mode: polling`)
- Revoke all Web UI sessions: `uv run heraldesk admin logout-all`
- Rotate `secret.key` with a credential backup (stop `serve` first): `uv run heraldesk admin rotate-key`
- Show the admin token source/fingerprint: `uv run heraldesk admin token` (`--show` prints the raw secret only
  when explicitly requested; never log or screenshot it)
- Allow a non-loopback bind only behind HTTPS: `uv run heraldesk serve --host 0.0.0.0 --allow-remote` with an
  explicit `HERALDESK_ADMIN_TOKEN` and `HERALDESK_TRUST_PROXY=1`; the default remains `127.0.0.1`
- Initialise an instance: `uv run heraldesk init`
- Upgrade with a credential backup first: `uv run heraldesk upgrade`
- Diagnose an instance offline: `uv run heraldesk doctor` (database migration head, config, `secret.key`,
  Telegram token when enabled, writability and reports directory; `--online` probes the LLM provider,
  Telegram `getMe` when enabled, and Nostr relays when an account exists)
- Create a profile skeleton: `uv run heraldesk profile new <slug> --name "Name" --type company --timezone UTC --languages en,id`
- Validate profile files: `uv run heraldesk profile reload [slug]`
- Control a profile: `uv run heraldesk profile pause|resume|killswitch <slug>`
- Add an account: `uv run heraldesk account add <slug> --platform fake --handle @name --credential token=...`
- Add a Nostr account (throwaway key only; the nsec is read from the environment, never typed):
  `uv run heraldesk account add <slug> --platform nostr --credential-from-env NOSTR_NSEC_THROWAY [--label "throwaway"] [--relay wss://relay.example]`
- List accounts with the Nostr npub masked: `uv run heraldesk account list <slug>`
- Refresh a long-lived Threads token: `uv run heraldesk account refresh <slug> --platform threads`
- List built-in and installed adapter plugins: `uv run heraldesk plugins list [--check]`
- Test accounts: `uv run heraldesk account test <slug>` (fake adapter), `--platform nostr` (local key/relay
  validation, no network) or `--platform threads` (real `GET /me`; needs network)
- Run the Writer once and record the trace: `uv run heraldesk agent run writer <slug> --idea "..."`
- Run the daily content pipeline: `uv run heraldesk run daily <slug> [--dry-run]` (`--dry-run` writes nothing)
- List the approval queue: `uv run heraldesk queue list <slug>`
- Approve or reject queued content: `uv run heraldesk queue approve <id>`, `uv run heraldesk queue reject <id> [--reason "..."]`
- Edit queued content: `uv run heraldesk queue edit <id> --body "..."` or `--body-file <path>`
- Publish one item now: `uv run heraldesk publish <id>`
- Run the scheduler once: `uv run heraldesk scheduler run [--dry-run]` (alias: `uv run heraldesk run scheduler [--dry-run]`)
- Test Telegram offline: `uv run heraldesk telegram test` (fake adapter, redacted preview)
- Send a manual Telegram test message: `uv run heraldesk telegram send <slug> "text"` (fake adapter)
- Show the Telegram inbox: `uv run heraldesk telegram inbox list <slug> [--limit 20]` (redacted)
- Store or inspect the encrypted bot token: `uv run heraldesk telegram token set|status`
- Triage the manual inbox: `uv run heraldesk inbox list [--status pending|handled|ignored] [--limit N]`,
  `uv run heraldesk inbox show <id>`, `uv run heraldesk inbox reply <id> --text "..."`,
  `uv run heraldesk inbox ignore <id> [--reason spam|off-topic|other]`,
  `uv run heraldesk inbox prune [--days 30] [--dry-run] [--include-pending]`
- Generate deterministic reports: `uv run heraldesk report weekly [--week YYYY-Www]`,
  `uv run heraldesk report monthly [--month YYYY-MM]` (written to `$HERALDESK_HOME/reports/`)
- Show agent run traces and cost: `uv run heraldesk runs <slug>`
- Back up the instance: `uv run heraldesk backup [--output PATH] [--with-credentials]`
- Prune old archives: `uv run heraldesk backup-prune [--keep 10] [--dry-run]`
  (`--with-credentials` includes `secret.key`; a backup without it cannot restore the database)
- Restore a backup (stop `serve` first): `uv run heraldesk restore <archive> [--without-credentials] [--force]`
  (`--without-credentials` restores config, profiles and agents only, never the DB or key)
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
