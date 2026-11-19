# Heraldesk Architecture

Heraldesk is an open-source, single-user, self-hosted autonomous "social media department". One person runs it
on a laptop or their own VPS to manage one or several brand/person accounts. Roles: Strategist, Researcher,
Writer, Designer, Community Manager, Analyst, Compliance. Humans review and approve.

Interfaces: CLI, Telegram, local Web UI, local REST API. No cloud service, no account, no telemetry.

This document is the spec for contributors and coding agents. Work milestone by milestone (section 13).

## 1. Design principles
1. **Easy install.** One command (`pipx install` / `uvx` / `docker run`). No database server, no broker, no Node at runtime.
2. **One process.** `heraldesk serve` runs API, web, scheduler, worker and Telegram bot.
3. **Deterministic workflows.** Pipelines with explicit state machines. LLMs act inside steps, not as the controller.
4. **Safe by default.** Profiles start as draft-only. Server binds `127.0.0.1`.
5. **BYOK.** User supplies LLM keys or uses local models (Ollama).
6. **Hackable.** Agent identity, prompts and policies are editable text files.
7. **Everything audited.** Append-only local audit log.
8. **Structured outputs.** Pydantic schemas, validated, retried.

## 2. Tech stack
| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| API/Web server | FastAPI + Uvicorn |
| Web UI | Jinja2 + HTMX + Tailwind (prebuilt CSS committed; no Node at runtime) |
| Database | SQLite (WAL) via SQLAlchemy 2 + Alembic. Avoid SQLite-only features that block a future Postgres move |
| Scheduler | APScheduler in-process; jobs persisted in `jobs` table so they resume after restart |
| LLM | LiteLLM (DeepSeek, Claude, OpenAI, Gemini, OpenRouter, Ollama) |
| CLI | Typer + Rich |
| Telegram | aiogram or python-telegram-bot |
| Config | YAML + `.env` (pydantic-settings) |
| Secrets | Fernet encryption at rest; key in `secret.key` (mode 600) or OS keyring |
| Logging | structlog |
| Tests | pytest, pytest-asyncio, respx, fake LLM provider |
| Distribution | PyPI (`heraldesk`), single Docker image, MIT license |
| Deps | `uv` with lockfile, pinned versions |

## 3. Architecture
```
        CLI | Telegram | Web UI (HTMX) | local REST API
                         │
                ┌────────▼────────┐        one process: `heraldesk serve`
                │   Core services │  profiles, content, approvals, inbox
                └───┬─────────┬───┘
         ┌──────────▼──┐   ┌──▼───────────┐
         │ Orchestrator│   │  Scheduler   │ (APScheduler + jobs table)
         │ (pipelines) │   └──┬───────────┘
         └──────┬──────┘      │
         ┌──────▼──────┐   ┌──▼───────────┐
         │Agent Runtime│   │  Publisher   │──► Threads / Nostr / X / FB /
         │ (roles+LLM) │   │  (adapters)  │     IG / LinkedIn / ...
         └──────┬──────┘   └──────────────┘
                └────────┬────────┘
                    ┌────▼─────┐
                    │  SQLite  │ + data folder (profile files)
                    └──────────┘
```

### Data folder (default `~/.heraldesk/`, override with `HERALDESK_HOME`)
```
heraldesk.yaml         # global config (LLM, server, Telegram)
heraldesk.db           # runtime state (SQLite)
secret.key             # credential encryption key (never commit)
profiles/<slug>/
  soul.md              # identity, tone, always/never rules
  brief.md             # brand brief, audience, goals
  policy.yaml          # autonomy policy and posting schedule
  banned.yaml          # banned words/topics, required disclaimers
  voice/               # real writing samples for few-shot
agents/                # optional per-role prompt overrides (*.md)
logs/  backups/
```
**Source of truth:** files in `profiles/` define a profile (users may keep them in git). SQLite holds runtime
state (content, approvals, inbox, metrics, audit). `heraldesk profile reload` validates and loads files.

### Repository layout
```
heraldesk/
  AGENTS.md README.md LICENSE CONTRIBUTING.md SECURITY.md ROADMAP.md
  pyproject.toml uv.lock Dockerfile docker-compose.yml
  alembic/
  src/heraldesk/
    config.py paths.py
    db/ core/(crypto, audit, locks) domain/
    llm/ agents/(base, strategist, researcher, writer, designer, community, analyst, compliance, prompts/*.md)
    pipelines/(daily_content, reply_triage, weekly_report, onboarding)
    platforms/(base, fake, threads, nostr, x, facebook, instagram, linkedin)
    scheduler/ api/ web/(templates, static) cli/ telegram/
    plugins.py
  tests/ docs/ examples/
```

## 4. Data model (SQLite)
Relevant tables carry `profile_id`. Isolation between profiles is enforced in the repository layer: every query
goes through `ProfileScope(profile_id)` and an agent only ever receives context of one profile.

- **profiles**: id, slug, name, type (`company|person|public_figure|other`), timezone, languages, status (`active|paused`), kill_switch, config_hash
- **social_accounts**: id, profile_id, platform, handle, credentials_encrypted, status, last_checked_at
- **content_ideas**: id, profile_id, source, title, angle, evidence (JSON), status
- **content_items**: id, profile_id, account_id, idea_id, platform, body, media (JSON), state, risk_level, risk_notes, scheduled_at, published_at, external_id, parent_id, created_by_run_id
- **approvals**: id, content_item_id, decision (`approve|reject|edit`), edited_body, reason, decided_at, channel (`web|telegram|cli`)
- **inbox_items**: id, account_id, kind (`comment|mention|dm`), external_id, author, text, sentiment, intent, status
- **replies**: id, inbox_item_id, body, state, risk_level
- **metrics_snapshots**: id, content_item_id, captured_at, metrics (JSON)
- **memories**: id, profile_id, kind (`fact|preference|lesson|trend`), text, source_run_id
- **agent_runs**: id, profile_id, agent, pipeline_run_id, input, output, model, tokens_in, tokens_out, cost_usd, status, error, started_at, finished_at
- **pipeline_runs**: id, profile_id, pipeline, trigger, status, started_at, finished_at
- **audit_log**: id, profile_id, actor (`user|agent:<name>|system`), action, entity, entity_id, payload, created_at. Append-only (enforced in the service layer; no update/delete operations exist).
- **jobs**: id, kind, payload, run_at, status, attempts, last_error, idempotency_key

### Content state machine
```
idea → drafted → reviewed → pending_approval → approved → scheduled → published
                    │               │                         │
                    ▼               ▼                         ▼
                 blocked         rejected                   failed (retry ≤ 3)
```
Transitions happen only through `domain.content.transition()`, which validates rules and writes the audit log.

## 5. Agent runtime
Each agent has a `role`, an allowlist of `tools`, and a Pydantic output schema. `AgentRunner.run(agent, input)`
assembles the prompt, calls the LLM, validates the schema, retries up to 2 times, and records `agent_runs`
including tokens and cost.

System prompt = role template (`agents/prompts/<role>.md`, user-overridable in `agents/`) + `soul.md` +
`banned.yaml` + platform constraints + relevant memories.

| Agent | Input | Output | Tools |
|---|---|---|---|
| Strategist | profile, 30-day metrics, calendar | `ContentPlan` (slots, pillars, platforms, goals) | read metrics, calendar |
| Researcher | pillars, competitors, date | `list[Idea]` (title, angle, evidence, sources) | web search, fetch URL |
| Writer | idea + profile + platform | `Draft` (body, variants, hashtags) | read voice samples |
| Designer | draft | `VisualBrief` (visual description, alt text) | optional image generator |
| Community | inbox item + profile | `Triage` and `ReplyDraft` | read comment history |
| Analyst | metrics, content | `WeeklyReport` (findings, recommendations, lessons) | read metrics |
| Compliance | draft or reply | `RiskAssessment` (`low|medium|high|blocked`, reasons, fixes) | banned lists, lint |

Compliance runs on EVERY draft and reply before it reaches the approval queue. `blocked` can never be published
without being edited and re-assessed. Users may disable roles in `heraldesk.yaml` (e.g. Designer) except
Compliance and lint, which are always on.

**Anti-slop lint (deterministic, before the LLM Compliance step).** `quality.lint(text, profile)` rejects em dashes,
buzzwords from a list, generic greetings, exaggerated claims and profile-banned words. Failing drafts return to
Writer up to 2 times, then are flagged for human review. Rules are configurable per profile.

## 6. Pipelines
**`daily_content`** (scheduled per profile): Strategist picks slots → Researcher proposes ideas (reject anything
similar to the last 60 days) → Writer drafts per platform → Designer (optional) → lint → Compliance → apply autonomy
policy → `pending_approval` or `approved` → notify (Telegram/web/terminal).

**`reply_triage`** (periodic polling): fetch new comments/mentions → Community classifies (spam, praise, question,
complaint, crisis) → spam ignored; praise and questions get drafted replies; complaints and crises are escalated to
the human with no auto-sendable draft → Compliance → autonomy policy.

**`weekly_report`**: Analyst compiles metrics, writes the report, stores lessons in `memories`, sends a summary.

**`onboarding`** (`heraldesk profile new`): interactive wizard from brief, account links and old writing samples;
generates draft `soul.md`, `banned.yaml`, `policy.yaml`. The user reviews and edits before the profile becomes `active`.

## 7. Autonomy policy
`profiles/<slug>/policy.yaml`:
```yaml
default_mode: draft_only
rules:
  - { action: post,  risk: low,    mode: auto_after_delay, delay_minutes: 30 }
  - { action: post,  risk: medium, mode: require_approval }
  - { action: post,  risk: high,   mode: require_approval }
  - { action: reply, risk: low, intents: [praise], mode: auto }
  - { action: reply, risk: medium, mode: require_approval }
quiet_hours: { start: "21:00", end: "07:00" }
max_posts_per_day: 3
```
- `blocked` never passes. Topics in `banned_topics` raise risk to `high`.
- `auto_after_delay` gives a cancellation window before publishing.
- Per-profile `kill_switch` and global `heraldesk stop-all` instantly halt publishing and replies.
- The user can approve, reject with a reason, or edit. Edits are stored as `preference` memories to learn style.
- `max_daily_cost_usd` in `heraldesk.yaml`: when exceeded, pipelines stop and the user is notified.

## 8. Platform adapters and plugins
```python
class PlatformAdapter(Protocol):
    platform: str
    async def validate_credentials(self, account) -> bool: ...
    async def publish(self, account, item) -> PublishResult: ...
    async def fetch_interactions(self, account, since) -> list[Interaction]: ...
    async def reply(self, account, interaction, body: str) -> PublishResult: ...
    async def fetch_metrics(self, account, external_ids) -> list[Metrics]: ...
    def constraints(self) -> PlatformConstraints: ...   # max length, media, rate limits
```
- Adapters only talk to APIs; no business logic.
- Shared layer handles rate limits and retry with exponential backoff.
- Publishing is idempotent (idempotency key per `content_item`).
- **Official APIs only.** Return `NotSupported` for missing features. No scraping, no ToS-violating browser automation.
- Implementation order: `fake`, `threads`, `nostr`, then `x`, `facebook`, `instagram`, `linkedin`.
- **Plugins:** extra adapters/agents install as separate packages via Python entry points (`heraldesk.platforms`,
  `heraldesk.agents`). Provide a plugin template in `examples/`.

## 9. Interfaces
**CLI** (command `heraldesk`, short alias `hdesk`)
```
heraldesk init                      # wizard: data folder, key, LLM, Telegram
heraldesk doctor                    # check config, key, connectivity, file permissions
heraldesk serve [--host --port --no-web --no-telegram]
heraldesk profile new|list|reload|pause|resume|killswitch
heraldesk account add|test <profile>
heraldesk run daily <profile> [--dry-run]
heraldesk queue list|approve|reject|edit
heraldesk inbox list|triage
heraldesk report weekly <profile>
heraldesk runs <profile>            # run traces and cost
heraldesk backup | restore <file>
heraldesk upgrade
heraldesk stop-all
```
**Telegram:** draft notifications with Approve / Reject / Edit / Snooze buttons; `/status`, `/pause`, `/stopall`,
morning briefing. Only chat IDs on the allowlist in `heraldesk.yaml` are served.

**Web UI (local):** approval queue (draft diff, risk, reasons), content calendar, inbox, reports, profile and policy
editors with validation, audit log, LLM cost. Server-rendered (HTMX), no frontend build step.

**REST API:** everything under `/api/v1/...` with OpenAPI; web and Telegram use it or the same services.

## 10. Security for self-hosting
- **Bind `127.0.0.1` by default.** With any other `--host`, refuse to start unless an admin password is set; `doctor`
  warns to use an HTTPS reverse proxy (include a Caddy example in `docs/`).
- **Single admin auth:** argon2 password, signed session cookie, CSRF protection, login rate limit, personal API token for scripts.
- **Platform credentials** encrypted at rest, decrypted only inside adapters at use time, never placed in prompts,
  logs or agent output. Logs mask secrets.
- **Prompt injection:** comment/DM text and web pages are untrusted data, wrapped in explicit delimiters. The Community
  agent has no publish tool. Instructions inside data are ignored. Publishing happens only through services applying policy.
- **Tool allowlist per role.** Only the Publisher module may write to platforms.
- **No telemetry, no phone-home.** Outbound traffic is limited to the configured LLM provider, platform APIs and
  the Researcher's web search.
- **Retention:** DM and comment data auto-deleted after N days (default 30, configurable).
- **Supply chain:** pinned and locked dependencies, `pip-audit` in CI, `SECURITY.md` explains reporting.
- **Acceptable use** (README and CONTRIBUTING): own or authorized accounts only; no fake-account networks,
  impersonation or spam. Per-profile `disclose_ai: true` adds an AI-assistance marker. Users are responsible for
  platform ToS and local law.

## 11. Observability and testing
- Structured logs with `profile_id`, `pipeline_run_id`, `agent_run_id`.
- Cost summary (tokens and USD) per profile, agent and day.
- **Fake LLM provider** returning fixture outputs, so every pipeline is testable offline for free.
- Contract tests for each `PlatformAdapter` against `fake`.
- State machine tests: legal and illegal transitions.
- Profile isolation tests: context and queries of profile A never include data of profile B.
- Small evaluation set for Compliance (safe, risky, forbidden) with a minimum accuracy threshold.
- CI: ruff, mypy, pytest, pip-audit, wheel and Docker image build; core tests on Linux, macOS and Windows.

## 12. Operations
- Linux, macOS, Windows (WSL or native CLI). Termux/Android best-effort.
- Docker: single image, volume for `HERALDESK_HOME`; example `docker-compose.yml` and `systemd` unit for VPS.
- `heraldesk upgrade` runs Alembic migrations and makes a backup first.
- `heraldesk backup` copies `heraldesk.db` (SQLite online backup API), `profiles/` and `heraldesk.yaml`;
  `secret.key` only when explicitly requested.

## 13. Implementation roadmap (in order)
Each milestone is done when acceptance criteria pass and tests exist.

**M0 Foundation and packaging.** Repo, `pyproject` + `uv`, folders, `paths.py`, `config.py`, `heraldesk init` and `doctor`,
SQLite + Alembic, CI. *Accept:* `pipx install .` then `heraldesk init` and `doctor` work on Linux and macOS; CI green.

**M1 Core domain.** Profiles from files (schema validation, `reload`), social accounts with encrypted credentials,
audit log, kill switch, `ProfileScope`. *Accept:* profile isolation tests pass; `profile new/list` and `account add/test` work.

**M2 LLM layer and agent runtime.** LiteLLM wrapper, structured output + retry, `agent_runs` and cost tracking, fake
provider, base Agent class, prompts as `.md` files with user override. *Accept:* one agent runs with fake and real
provider; cost recorded; daily cost cap works.

**M3 `daily_content` with fake publisher.** Researcher, Writer, Compliance, `quality.lint`, state machine, queue.
*Accept:* `heraldesk run daily <profile> --dry-run` produces drafts in correct states; all runs recorded.

**M4 Approval and Telegram.** Approval service, autonomy policy, Telegram bot with buttons, edit and reject, chat ID allowlist.
*Accept:* a draft appears in Telegram, gets approved and moves to `approved`; edits are saved as preference memories.

**M5 Scheduler, publisher, first adapters.** APScheduler + persistent `jobs`, `threads` and `nostr` adapters, idempotency, retry.
*Accept:* a scheduled post appears on a test account; restarting the process loses no jobs; retries never double-post.

**M6 Community and Analyst.** `reply_triage`, crisis escalation, metrics collection, `weekly_report`.
*Accept:* test comments are classified and follow policy; weekly report is delivered.
*Repository note (2026-11-17):* the first M6 pass implements Telegram notifications and inbox basics
instead; `reply_triage`, metrics and `weekly_report` remain future work. See `docs/decisions.md`.

**M7 Local web UI.** Approval queue, calendar, inbox, reports, profile and policy editors, audit, cost; password auth.
*Accept:* the full approval flow works from the web; starting on a non-loopback host without a password is refused.
*Repository note (2026-11-17):* the first M7 pass implements the single-process server instead: FastAPI
`/health`, `/status` and `/webhook/telegram`, the APScheduler background sweeps and Telegram polling.
The web UI, password auth and the `/api/v1` namespace remain future work. See `docs/decisions.md`.

**M8 Plugins and more adapters.** Entry-point loader, plugin template, X, Facebook, Instagram, LinkedIn adapters.
*Accept:* a sample third-party plugin is detected without touching core code.
*Repository note (2026-11-19):* the entry-point plugin loader shipped as M12 with docs and tests.
X, Facebook, Instagram and LinkedIn adapters remain future work.

**M9 Release v0.1.0.** Docker image, PyPI publish, `docker-compose.yml` and `systemd` examples, docs (local quickstart,
VPS + Caddy deploy, writing `soul.md`, writing plugins), `SECURITY.md`, `CONTRIBUTING.md`, sample profiles in `examples/`.
*Accept:* a new user goes from zero to a first draft in under 15 minutes by following the README.

## 14. Out of scope
Multi-user and multi-tenant, billing, official hosted cloud, native mobile app, video generation, auto-DM outreach,
scraping platforms without official APIs.
