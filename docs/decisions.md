# Decisions log

Record every notable technical or product decision here (date, decision, reason).

- 2026-11-16: Project name is Heraldesk (replaces "sosmed"/"Herald": too generic or conflicting).
- 2026-11-16: Single-user, open source, self-hosted. No multi-tenant.
- 2026-11-16: SQLite as the only database for v0.x.
- 2026-11-16: **License is MIT**, chosen for simplicity and permissiveness. Supersedes the earlier TBD entry. `LICENSE` stays as is (Copyright (c) 2026 Abeng); do not switch to Apache-2.0.

## M0 orientation decisions (2026-11-16)

These answers close the open questions raised after the first full read of the spec. Until `docs/architecture.md` and
`AGENTS.md` are edited in M0 to match, these decisions override ambiguous or conflicting text in those documents.

### Documents and governance
- **Document precedence.** (1) `docs/architecture.md` (EN) is the design truth. (2) `AGENTS.md` holds operational rules
  for agents and must not contradict architecture; if it does, architecture wins and `AGENTS.md` must be fixed.
  (3) `docs/id/arsitektur.md` is a translation only and never wins. Reason: three partially divergent copies of the
  principles existed, so one authority is needed to stop drift.
- **Canonical principles are the 8 from architecture section 1**, with the auto-post rule folded into principle 4
  (safe by default). `AGENTS.md` must contain exactly those 8 plus section 15, with no reordering and no extra items.
  `AGENTS.md` currently has 10 items (it splits out "official platform APIs only" and "no telemetry"); fix it in M0.
  Those rules remain binding through architecture sections 8 and 10 and section 15.

### Secrets
- **LLM API keys** live plaintext in `.env` via pydantic-settings. Never in `heraldesk.yaml`, never in git (`.env` is
  gitignored). Reason: BYOK key rotation stays a one-line edit in the documented settings layer.
- **Social account credentials** are Fernet-encrypted at rest with `secret.key` (mode 600).
- `heraldesk init` MUST create `secret.key` even when no account exists yet.
- `heraldesk restore` of a backup that excludes `secret.key` MUST fail with: "Backup has no secret.key, cannot decrypt
  social credentials. Restore profiles and config only with --without-credentials, or provide secret.key". Never
  silently skip decryption and never mint a new key that makes old credentials look usable.

### Network and OS matrix
- `heraldesk doctor` is **offline by default**, no network calls. Only `heraldesk doctor --online` may check the LLM
  provider, platform connectivity and Telegram.
- **CI matrix:** Linux and macOS run the full suite; Windows runs core tests only (paths, config, content transitions,
  lint). This resolves the section 11 (three OS) versus M0 acceptance (Linux, macOS) conflict.

### Database and CLI surface
- **The M0 Alembic baseline is empty** (one initial migration with no tables). Tables arrive per milestone: M1
  `profiles`, `social_accounts`, `audit_log`, business `jobs`; M3 `content_ideas`, `content_items`; M4 `approvals`;
  and so on.
- **M0 commands are exactly:** `heraldesk init`, `heraldesk doctor`, `heraldesk serve` (stub that prints
  "not implemented yet" and exits 0), `--help`, `--version`. Everything else (`profile`, `account`, `run`, `queue`,
  `inbox`, `report`, `runs`, `backup`, `restore`, `upgrade`, `stop-all`) comes in a later milestone.

### R1 Platform adapter reality
- The uniform adapter contract stays, but every adapter documents its real limits in `docs/platforms/<name>.md`.
- **Idempotency strategy is at-most-once plus human reconciliation.** Before publishing, check the account's last 20
  posts for the same body hash. If a duplicate is found, mark the item already published, do not re-post, and log it.
  The idempotency key in the DB is ours, not a platform key.
- **Nostr** is keys plus relays, not username/password. Store the relay list and the private key encrypted like other
  credentials.
- **Instagram** requires a Business account plus a linked Facebook Page. Without them the adapter returns
  `NotSupported` with a clear message.
- **M5 acceptance changed** to: "scheduled post appears on a test account OR returns NotSupported with a documented
  reason if app review blocks it". Code must not be blamed for app review delay.

### R2 Autonomy policy semantics
- **Rule matching:** most specific wins first (action + risk + intent), then first match in file order. Document this
  in the `policy.yaml` header.
- `default_mode` is the fallback when no rule matches. `draft_only` and `require_approval` both require a human;
  `draft_only` keeps the item in the draft queue and never auto-schedules, `require_approval` allows scheduling after
  approval.
- **`max_posts_per_day`** counts PUBLISHED items per PROFILE per calendar day in the profile timezone. Approved or
  scheduled items do not count yet.
- **`quiet_hours`** uses the profile timezone and applies to posts AND replies. An `auto_after_delay` post that would
  cross into quiet hours is deferred to the end of quiet hours; never publish inside them.
- **Cancel path:** add state `cancelled`. `auto_after_delay` items can be cancelled via queue, Telegram or web until
  publish time. `heraldesk stop-all` moves all `approved`, `scheduled` and `auto_after_delay` items to `cancelled` and
  logs it.

### R3 Content state machine
- Add states `cancelled` and `needs_review`. Full machine:
  `idea` (in `content_ideas`) -> when Writer starts, create `content_items` in `drafted` with `idea_id` ->
  `reviewed` -> `pending_approval` -> `approved` -> `scheduled` -> `published`.
- Branches: `drafted`/`reviewed`/`pending_approval` -> `blocked`; `pending_approval` -> `rejected`; any of
  `drafted`/`reviewed`/`pending_approval`/`approved`/`scheduled` -> `cancelled`; `failed` -> `scheduled` (retry max 3),
  final `failed` -> `needs_review`; lint failed twice -> `needs_review`; `blocked`/`rejected` plus user edit -> back to
  `drafted` and Compliance must re-evaluate.
- `failed` is NOT under `published`; it is a retry state after `scheduled`.
- **Replies ARE `content_items`** with `parent_id` pointing at the original content item or an inbox item link. The
  approval target is always `content_items`. The `replies` table stores reply-specific metadata only; state and
  approval live in `content_items`.

### R4 ProfileScope enforcement
- **Every profile-owned table MUST have a `profile_id` column:** `profiles`, `social_accounts`, `content_ideas`,
  `content_items`, `approvals`, `inbox_items`, `replies`, `metrics_snapshots`, `memories`, `agent_runs`,
  `pipeline_runs`, `audit_log`, `jobs`.
- For `approvals`, `inbox_items`, `replies` and `metrics_snapshots`, add a denormalized `profile_id` in the migration
  (copied from the parent content item or account). Do not rely on joins alone.
- `audit_log.profile_id` is NULLABLE: NULL means a global action (for example `stop-all` or `init`). All profile
  actions must set it. `jobs.profile_id` is NULLABLE: NULL means a global job, otherwise a profile job.
- **Enforcement is repository-layer only.** Every repository function takes `ProfileScope(profile_id)` as its first
  argument. No direct SQLAlchemy query outside the repository. M1 test: a direct query without scope must fail or be
  impossible by API design. A SQLAlchemy session event is NOT the mechanism; the explicit scope argument is.

### R5 Failure and cost semantics
- **Compliance LLM unavailable: fail closed.** The draft goes to `blocked` with reason "compliance unavailable" and
  is never auto-approved.
- **Any other LLM failure mid-pipeline:** retry once, then mark the `pipeline_run` as `failed`, queue it for manual
  retry and notify the user. Never silently skip a step.
- **`max_daily_cost_usd`** is checked BEFORE each LLM call, is global in `heraldesk.yaml`, and resets at midnight in
  the profile timezone for per-profile reporting and at midnight UTC for cap enforcement. When exceeded:
  `daily_content` stops; `reply_triage` stops auto-replies but still escalates crises to the human (the human sees
  them, no auto-sendable draft is produced).
- **Retry budget:** at most 4 LLM calls per draft in total (2 lint returns plus 2 schema retries combined, not
  multiplied per platform). If exceeded, the draft goes to `needs_review`.

### Other conflicts resolved
- **M3 hardcodes `draft_only`.** The autonomy engine arrives in M4; add a comment in the M3 code.
- **Milestone assignment for previously unassigned scope:** Strategist and Designer in M3 (simple versions);
  `onboarding` in M1 (skeleton) plus M6 polish; REST `/api/v1` in M7; `backup` in M1 plus `restore` polish in M9;
  `upgrade` in M9; `doctor` in M0; `runs` in M2.
- **Off-switch precedence:** global kill switch > per-profile kill switch > `profiles.status` paused > policy
  `default_mode`. Add a third profile status `draft` for a generated profile that is not yet active:
  `profiles.status = active|paused|draft`.
- **Outbound allowlist:** LLM provider, platform APIs, Telegram `api.telegram.org`, and Researcher web search
  (configurable provider, default DuckDuckGo HTML, no API key). Researcher fetch: https only, block private IPs,
  localhost and metadata endpoints (SSRF guard), 10 s timeout, no credentials in the URL.
- **Retention versus audit:** `audit_log.payload` stores REDACTED metadata only (ids, action, risk, truncated body
  hash), never full DM or comment text. Full text in `inbox_items` is deleted after 30 days; the audit record
  remains. This makes retention enforceable.
- **Scheduler:** APScheduler with `SQLAlchemyJobStore` in the same SQLite DB (table `apscheduler_jobs`, separate from
  the business `jobs` table). The business `jobs` table holds Heraldesk jobs such as `daily_content`; APScheduler
  triggers them.
- **Test realism:** CI uses the fake provider only. The real-provider test in M2 is MANUAL, with documented steps, and
  is not in CI.
- **M9 acceptance changed** to: "zero to a first draft with the fake provider in under 15 minutes". Real platform
  publishing may take days because of app review and is not counted.

### Deferred documentation sync (not part of the M0 implementation pass)
- Rewrite `AGENTS.md` to the canonical 8 principles plus section 15.
- Update `docs/architecture.md` section 1 (auto-post rule inside principle 4), section 4 (new states, `profile_id`
  columns, reply model), sections 6-8 (policy semantics, idempotency, `docs/platforms/`), section 10 (outbound
  allowlist, redacted audit payload), section 11 (CI matrix), section 13 (M5 and M9 acceptance). Section 15
  (contributor guidance) currently exists only in the Indonesian doc; add it to the English doc so `AGENTS.md` can
  reference it.
- Update `docs/id/arsitektur.md` to match, marked as a translation.

## M0 implementation decisions (2026-11-16)

- **Python is pinned to 3.12.** `requires-python = ">=3.12,<3.13"` and `.python-version` contains `3.12`, so `uv`
  does not pick the newer interpreter installed on a developer machine. Reason: the architecture names 3.12 as the
  supported runtime and CI tests exactly that.
- **Build backend is hatchling**, configured for a `src/` layout with a dynamic version read from
  `src/heraldesk/__init__.py`. Reason: small, PEP 621 native, no extra build dependencies, and it supports the
  forced include used for migrations.
- **M0 runtime dependencies:** `typer`, `rich` (CLI), `pydantic-settings` + `pyyaml` (config), `sqlalchemy`
  (SQLite), `alembic` (migrations), `cryptography` (Fernet key). **Dev dependencies:** `pytest`, `ruff`, `mypy`,
  `types-pyyaml`.
- **Deferred dependencies and their milestones:** `fastapi` + `uvicorn` and `jinja2` (M7), `structlog` (when
  structured logging lands), `apscheduler` (M5), `aiogram` or `python-telegram-bot` (M4), `keyring` and
  `pip-audit` (later), image tooling (M3/M8). The M0 `serve` command is a stub, so pulling a web stack now would
  violate the "no heavy dependencies without a reason" rule.
- **Migrations ship inside the wheel.** The repository keeps `alembic/` at the path promised by the architecture,
  and the wheel force-includes it as `heraldesk/_alembic`. `heraldesk.db.migrations` resolves the packaged copy
  first and falls back to the repository copy for editable installs. Reason: `pipx install .` must be able to run
  `heraldesk init`, not only `uv run` from a checkout.
- **`heraldesk init` is non-interactive and idempotent.** It creates the folders, writes the default
  `heraldesk.yaml` only if missing, creates `secret.key` only if missing (and re-applies mode 600), then runs
  `alembic upgrade head`. It never overwrites a user-edited config or key, and it fails loudly on an invalid
  existing config. The interactive onboarding wizard (LLM key and Telegram setup) is deferred to M1/M4.
- **The LLM `.env` file lives at `$HERALDESK_HOME/.env`.** Reason: the working directory of an installed command
  is not stable. Precedence is environment > `.env` > `heraldesk.yaml` > defaults. LLM keys never appear in
  `heraldesk.yaml`.
- **Config schema v1 is intentionally small:** `version: 1`, `llm.default_model`, `llm.max_daily_cost_usd`
  (default 5.0), `server.host` (default 127.0.0.1), `server.port` (default 8000), `telegram.enabled`,
  `telegram.allowed_chat_ids`. Unknown keys are rejected so typos fail loudly. New fields are added with their
  milestone and guarded by the version field.
- **`doctor` is offline and has three outcomes.** PASS, WARN (missing optional folder, non-WAL database, no
  revision, non-loopback host, exposed `.env`) and FAIL (bad Python, missing or insecure key, unreadable database,
  invalid config). Only FAIL changes the exit code to 1. `--online` exists and reports that online checks are not
  implemented yet.
- **Files created by `init` keep normal permissions except the key.** `secret.key` is 600; the config and database
  use the process umask. Reason: neither contains plaintext credentials, and forcing 600 on the DB would surprise
  users who back it up.
- **CI uses `astral-sh/setup-uv@v6` pinned to uv 0.13.8** (the version that generated `uv.lock`), with
  `uv sync --locked`, so a stale lockfile fails the build. Lint and type checks run on Ubuntu and macOS; the test
  matrix runs the full suite on Ubuntu and macOS and `pytest -m core` on Windows.
- **Ruff excludes `docs/`.** Ruff formats Python code blocks inside Markdown by default, which would silently
  rewrite `docs/architecture.md` and `docs/id/arsitektur.md`. Docs are excluded from both check and format.
- **No `Dockerfile` or `docker-compose.yml` in M0.** Approved deferral to M9: Docker is not installed in the
  development environment, M0 acceptance does not require it, and an untested image adds no value. M9 owns the
  image, compose file and systemd example.
- **Tests never touch the real data folder.** An autouse fixture points `HERALDESK_HOME` at `tmp_path` for every
  test. Permission tests assert mode bits (not effective access), so they still work when the suite runs as root.

