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
9. **Official platform APIs only.** No scraping, no browser automation that violates a platform's ToS.
10. **No telemetry, no phone-home.**

## 2. Tech stack
| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| API/Web server | FastAPI + Uvicorn |
| Web UI | Jinja2 + HTMX + custom CSS variables (vendored; no Node at runtime) |
| Database | SQLite (WAL) via SQLAlchemy 2 + Alembic. Avoid SQLite-only features that block a future Postgres move |
| Scheduler | In-process APScheduler triggers; durable work lives in the business tables |
| LLM | LiteLLM (DeepSeek, Claude, OpenAI, Gemini, OpenRouter, Ollama) |
| CLI | Typer + Rich |
| Telegram | Standard-library Bot API client (`urllib`), no bot framework |
| Config | YAML + `.env` (pydantic-settings) |
| Secrets | Fernet encryption at rest; key in `secret.key` (mode 600) |
| Logging | Standard-library `logging`; uvicorn access logs with query redaction |
| Tests | pytest, fake LLM provider, fake platform and Telegram adapters |
| Distribution | GitHub release wheel/sdist, GHCR container image, tag-triggered PyPI workflow |
| Deps | `uv` with lockfile |

## 3. Architecture
```
        CLI | Telegram | Web UI (HTMX) | local REST API
                         │
                ┌────────▼────────┐        one process: `heraldesk serve`
                │   Core services │  profiles, content, approvals, inbox
                └───┬─────────┬───┘
         ┌──────────▼──┐   ┌──▼───────────┐
         │ Orchestrator│   │  Scheduler   │ (APScheduler, in-memory triggers)
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
  pyproject.toml uv.lock Dockerfile docker-compose.yml .dockerignore
  deploy/(heraldesk.service, Caddyfile, README.md)
  .github/workflows/(ci.yml, release.yml)
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

- **profiles**: id, slug, name, type (`company|person|public_figure|other`), timezone, languages, status (`active|paused|draft`), kill_switch, config_hash
- **social_accounts**: id, profile_id, platform, handle, label, credentials_encrypted, status, last_checked_at
- **content_ideas**: id, profile_id, source, title, angle, evidence (JSON), status
- **content_items**: id, profile_id, account_id, idea_id, platform, body, media (JSON), state, risk_level, risk_notes, scheduled_at, published_at, external_id, publish_mode, publish_delay_minutes, publish_attempts, last_error, created_by_run_id
- **approvals**: id, profile_id, content_item_id, state (`pending|approved|rejected|expired`), decision, edited_body, reason, decided_at, channel (`web|telegram|cli`), expires_at, created_at
- **telegram_messages**: id, profile_id, update_id, chat_id, sender_id, text, command, content_item_id, response, status (`pending|handled|ignored`), ignore_reason, triaged_at, created_at. This is the current inbox table.
- **telegram_notifications**: one idempotent ledger row per notification event
- **agent_runs**: id, profile_id, agent, pipeline_run_id, input, output, model, tokens_in, tokens_out, cost_usd, status, error, started_at, finished_at
- **pipeline_runs**: id, profile_id, pipeline, trigger, status, started_at, finished_at
- **audit_log**: id, profile_id, actor (`user|agent:<name>|system`), action, entity, entity_id, payload, created_at. Append-only (enforced in the service layer; no update/delete operations exist).
- **jobs**: id, kind, payload, run_at, status, attempts, last_error, idempotency_key. Reserved for future durable jobs; APScheduler triggers are currently in memory.

Planned but not implemented: `metrics_snapshots`, `memories`, `replies`, and a separate non-Telegram `inbox_items` table.

### Content state machine
```
idea → drafted → reviewed → pending_approval → approved → scheduled → published
                    │               │                         │
                    ▼               ▼                         ▼
                 blocked         rejected                   failed (retry ≤ 3)
```
Transitions happen only through `domain.content.transition()`, which validates rules and writes the audit log.

## 5. Agent runtime
Each LLM agent has a `role` and a Pydantic output schema. `AgentRunner.run(agent, context, payload)`
assembles the prompt, calls the LLM, validates the schema, retries up to 2 times, and records `agent_runs`
including tokens and cost.

System prompt = role template (`agents/prompts/<role>.md`, user-overridable in `agents/`) + `soul.md` +
`banned.yaml` + platform constraints. There is no memories store yet.

| Agent | Status | Input | Output |
|---|---|---|---|
| Strategist | LLM | profile, brief, date | `ContentPlan` slots and pillars |
| Researcher | LLM | pillars, goal, evidence | `list[Idea]` (title, angle, evidence, sources) |
| Writer | LLM | idea + profile + platform | `Draft` (body) |
| Designer | deterministic placeholder | draft + platform | media metadata (`designer_placeholder_m3`) |
| Compliance | LLM | draft + profile + banned lists | `RiskAssessment` (low/medium/high/blocked, reasons, fixes) |
| Community | manual/deterministic service | inbox item | list/show/reply/ignore; no LLM |
| Analyst | deterministic reports | database | weekly/monthly Markdown; no LLM |

Compliance runs on every draft before it reaches the approval queue. `blocked` can never be published
without being edited and re-assessed. Roles are not configurable in `heraldesk.yaml` yet.

**Anti-slop lint (deterministic, before the LLM Compliance step).** `quality.lint(text, profile)` rejects em dashes,
buzzwords from a list and profile-banned words. Failing drafts return to Writer up to 2 times, then are flagged
for human review. Rules are configurable per profile.

## 6. Pipelines
**`daily_content`** (run with `heraldesk run daily <slug>`, not yet scheduled by `serve`): Strategist picks slots →
Researcher proposes ideas (reject anything similar to the last 60 days) → Writer drafts per platform → Designer
placeholder → lint → Compliance → apply autonomy policy → `pending_approval` or `scheduled` → notify
(Telegram/web/terminal). `--dry-run` writes nothing.

**`reply_triage`** (planned): fetch comments/mentions, classify, draft or escalate. The current inbox is manual
only; no platform comments are fetched.

**`weekly_report` / `monthly_report`** (implemented as deterministic services): the analyst builds the report from
the database with no LLM, writes Markdown to `$HERALDESK_HOME/reports/`, and audits the action.

**`onboarding`** (planned): an interactive wizard. `heraldesk profile new` currently writes a non-interactive
skeleton that the user edits and reloads.

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
- `blocked` never passes. `banned_topics` is injected into the prompt; it does not raise the risk level.
- `auto_after_delay` records a delay and defers across quiet hours. There is no `queue cancel` command yet; decline
  the approval before it is approved, or stop the scheduler.
- `quiet_hours` and `max_posts_per_day` are enforced for automatic publishing in the profile timezone.
- Per-profile `kill_switch` and global `heraldesk stop-all` halt publishing, approvals and manual replies. Scheduled
  items are not cancelled automatically.
- The user can approve, reject with a reason, or edit. Edits do not create preference memories yet.
- `max_daily_cost_usd` in `heraldesk.yaml`: when exceeded, pipelines stop and the user is notified.

## 8. Platform adapters and plugins
```python
class PlatformAdapter(Protocol):
    platform: str
    async def validate_credentials(self, account: AccountRef) -> bool: ...
    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult: ...
```
- Adapters only talk to APIs; no business logic.
- Retry/backoff and at-most-once live in the publisher, not in each adapter. Threads and Nostr handle their own
  platform limits; there is no shared rate-limit layer.
- **Official APIs only.** Return `NotSupportedError` for missing adapters. No scraping, no ToS-violating browser
  automation.
- Bundled publishers: `fake`, `threads` (text, official Graph API) and `nostr` (NIP-01 kind 1). X, Facebook,
  Instagram and LinkedIn return `NotSupportedError` unless an installed plugin supplies them.
- **Plugins:** third-party adapters install as separate packages via the `heraldesk.platforms` entry-point group.
  There is no agent-plugin group and no bundled template package; `docs/platforms/README.md` shows the contract.

## 9. Interfaces
**CLI** (command `heraldesk`, short alias `hdesk`)
```
heraldesk init                      # data folder, key, default config, database
heraldesk upgrade                   # credential backup, then pending migrations
heraldesk doctor [--online]         # offline checks + opt-in LLM/Telegram/Nostr probes
heraldesk serve [--host --port --no-web --no-telegram --allow-remote]
heraldesk profile new|list|reload|pause|resume|killswitch
heraldesk account add|list|test <profile>
heraldesk run daily <profile> [--dry-run]
heraldesk queue list|show|approve|reject|edit
heraldesk inbox list|show|reply|ignore
heraldesk report weekly|monthly [--week|--month]
heraldesk runs <profile>            # run traces and cost
heraldesk plugins list [--check]
heraldesk backup | restore <file>
heraldesk admin token [--show]
heraldesk stop-all [--off]
```
**Telegram:** notifications are plain text, not inline buttons. The allowlisted bot handles `/approve`, `/reject`,
`/queue`, `/help` and `/start`. An optional `telegram.allowed_user_ids` restricts command senders.

**Web UI (local):** dashboard, approval queue with a full-draft detail page, content calendar, manual inbox,
report viewer, LLM cost page, read-only audit log, profile list and a validated editor for the four profile text
files. Server-rendered (HTMX), no frontend build step.

**REST API:** authenticated `/api/v1` for status, readiness, queue (list + detail, paged and profile-filtered),
approvals, calendar, inbox, reports, audit (paged/action-filtered), cost, profiles, accounts, agent-run traces and
publishing. Cookies are not accepted; use `Authorization: Bearer`. The OpenAPI schema is served at
`/api/v1/openapi.json` behind auth; FastAPI's public docs routes stay disabled.

## 10. Security for self-hosting
- **Bind `127.0.0.1` by default.** A non-loopback bind requires `--allow-remote`, an explicit
  `HERALDESK_ADMIN_TOKEN` of at least 32 characters and `HERALDESK_TRUST_PROXY=1` behind an HTTPS reverse proxy.
- **Single admin token:** Bearer token for `/api/v1`, a signed HttpOnly session cookie for the UI, CSRF on every
  UI POST, login and API-failure rate limiting, request-size caps and query-token rejection. There is no password
  store. Session cookies carry an instance epoch; `logout-all` revokes every browser.
- **Proxy mode** uses the rightmost `X-Forwarded-For` hop from the documented single TLS proxy; the outer proxy
  must overwrite client-supplied prefixes.
- **Telegram webhooks are signed.** Webhook mode (and any remote Telegram deployment) requires
  `HERALDESK_TELEGRAM_WEBHOOK_SECRET`; the server refuses to start without it. `telegram.allowed_user_ids` is an
  optional second gate.
- **Platform credentials** encrypted at rest, decrypted only inside adapters at use time, never placed in prompts,
  logs or agent output. Logs mask secrets. `heraldesk admin rotate-key` re-encrypts accounts and the stored
  Telegram token after a mandatory backup.
- **Prompt injection:** only profile files and agent payloads are sent to LLM roles today; platform comments are
  not fetched or fed to agents. When comment/DM handling is added, it must be treated as untrusted data.
- **No telemetry, no phone-home.** Outbound traffic is limited to the configured LLM provider, platform APIs,
  Telegram and the Researcher's web search.
- **Retention:** `heraldesk inbox prune --days N` deletes handled/ignored inbox rows older than N days and
  audits the count. Pending messages are kept unless `--include-pending` is passed; there is no automatic schedule.
- **Supply chain:** locked dependencies, `uv sync --locked` in CI, `pip-audit` in CI, SHA-pinned GitHub Actions,
  container SBOM/provenance, Dependabot and `SECURITY.md` for reporting.
- **Acceptable use** (README and CONTRIBUTING): own or authorized accounts only; no fake-account networks,
  impersonation or spam. `disclose_ai` is parsed but not applied to post bodies yet. Users are responsible for
  platform ToS and local law.

## 11. Observability and testing
- Rotating file logging at `logs/heraldesk.log` (5 MiB × 3, mode 600, redacted) plus uvicorn console logs;
  access-log query strings are stripped. There are no request ids yet.
- Cost summary (tokens and USD) per profile and agent via `heraldesk runs` and the reports.
- **Fake LLM provider** returning fixture outputs, so every pipeline is testable offline for free.
- Contract tests for the fake, Nostr and Threads adapters; every external call is mocked.
- State machine tests: legal and illegal transitions.
- Profile isolation tests: context and queries of profile A never include data of profile B.
- Small evaluation set for Compliance is planned, not implemented.
- CI: ruff, mypy, pytest, pip-audit, wheel/sdist install smoke and Docker image build; full suite on Linux/macOS,
  core subset on Windows.

## 12. Operations
- Linux, macOS, Windows (WSL or native CLI). Termux/Android best-effort.
- Docker: `Dockerfile` with a non-root user and `/data` volume, `docker-compose.yml`, and a `/health` healthcheck.
- `deploy/` contains a systemd unit, a Caddyfile and a VPS runbook.
- `heraldesk upgrade` creates a credential backup, then applies Alembic migrations and audits the change.
- `heraldesk admin rotate-key` stages a new key, re-encrypts accounts and the Telegram token, swaps files,
  verifies the result and rolls back the database if the key swap fails. Stop the server first.
- `heraldesk serve` refuses to start on a schema that is not at the packaged Alembic head.
- `heraldesk backup` copies `heraldesk.db` (SQLite online backup API), `profiles/`, `agents/` and `heraldesk.yaml`;
  `secret.key` and `telegram.token` only when explicitly requested. `.env` is not included.
- `restore --force` writes a credential safety backup first, stages files before swapping
  them in, applies pending migrations and rotates the session epoch.

## 13. Implementation roadmap
The completed milestones and the planned next work live in `ROADMAP.md`. Summary: M0–M14 shipped, covering the
foundation, domain, LLM runtime, daily pipeline, approvals, scheduler/publisher, Telegram, server, reports,
hardening, Nostr, Web UI/API, adapter plugins, the Threads text adapter and production distribution.

## 14. Out of scope
Multi-user and multi-tenant, billing, official hosted cloud, native mobile app, video generation, auto-DM outreach,
scraping platforms without official APIs.
