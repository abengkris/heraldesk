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

## M1 implementation decisions (2026-11-16)

- **M1 schema is exactly four tables.** `profiles`, `social_accounts`, `audit_log` and the business `jobs` table are
  created by migration `0002`. Content, approvals, inbox and metrics stay deferred to their milestones.
- **Profile metadata lives in YAML front matter at the top of `soul.md`.** The architecture lists the profile files
  but no metadata file, while `profiles` needs name, type, timezone and languages. Front matter keeps the file set
  unchanged and still makes the files the source of truth. Without front matter the slug becomes the display name and
  safe defaults apply (`type: other`, `timezone: UTC`, `languages: [en]`).
- **All four profile files are required.** `soul.md`, `brief.md`, `policy.yaml` and `banned.yaml` must exist; `voice/`
  is optional. Missing files fail `profile reload` with the offending path instead of silently using defaults. Empty
  policy/banned YAML files are valid and use safe defaults; an empty `brief.md` is not.
- **`profile new` creates a `draft` profile.** It writes a non-interactive skeleton with `default_mode: draft_only` and
  never activates the profile. The user edits the files, runs `profile reload`, then `profile resume`.
- **The global kill switch is a marker file, not a table.** `$HERALDESK_HOME/killswitch` is created with mode 600 by
  `heraldesk stop-all` and removed by `stop-all --off`. Reason: the marker is independent of profile rows and schema
  tables, so every process can check it cheaply; `stop-all` still requires an initialised instance so the action is
  audited. Per-profile kill switches stay in `profiles.kill_switch`. Precedence is global, per-profile, `paused`,
  `draft`, then allowed.
- **`ProfileScope` is the only profile data path.** `ProfileRepository`, `SocialAccountRepository`, `AuditRepository`
  and `JobRepository` take `scope` as their first argument after `self` and filter every query by `profile_id`.
  `ProfileCatalog` is the single global exception: it creates the first row (before an id exists) and lists metadata
  for `profile list`; it never exposes profile file content. `profiles.id` is itself the profile id, so the
  `profiles` table does not duplicate it in a second `profile_id` column.
- **Audit append-only is enforced in the service, not with SQLite triggers.** `AuditService` and `AuditRepository` have
  no update/delete methods. Payload keys that look like secrets (`token`, `password`, `credential`, `api_key`, ...)
  are rejected so a future caller cannot accidentally write credentials into the audit log. Redacted metadata only,
  as decided for retention.
- **`account test` uses the fake adapter for every platform in M1.** Real adapters arrive in M5. The fake adapter does
  no network I/O and accepts a non-empty `token` credential, which keeps `account add|test` useful offline.
- **Known platform names are validated at add time.** M1 accepts `fake`, `threads`, `nostr`, `x`, `facebook`,
  `instagram` and `linkedin`; adding others is a typo error. This does not claim the real adapters exist yet.
- **M1 tests that are cross-platform carry `pytest.mark.core`.** This makes the Windows CI subset meaningful for the
  new profile, policy, crypto, audit, isolation, account and off-switch tests.

## M2 implementation decisions (2026-11-16)

- **M2 adds exactly one table, `agent_runs`** (migration `0003`). Columns follow architecture section 4.
  `pipeline_run_id` is a nullable integer **without a foreign key** until the `pipeline_runs` table arrives with
  the first pipeline in M3. Reason: creating an empty pipeline table now would pre-empt M3 schema decisions.
- **Runtime dependencies added:** `litellm` (mandated by architecture sections 2 and 5 for provider access) and
  `python-dotenv` (reading provider keys from `$HERALDESK_HOME/.env`; pydantic-settings already depends on it, but
  the dependency is now explicit because Heraldesk imports it directly). LiteLLM is imported lazily inside the
  provider so the fake path, the CLI and CI never pay its import cost.
- **The fake provider is the default for an empty or `fake/...` model.** `llm.provider` is `auto|litellm|fake`;
  `auto` picks fake for an empty model or one starting with `fake/`, and LiteLLM otherwise. CI and all tests use the
  fake provider only. The real-provider check stays manual, as decided in R5/Test realism.
- **Fake fixtures are files, not code.** Bundled fixtures live at `heraldesk/llm/fixtures/<role>.json`;
  `llm.fake_fixtures_dir` points at a user folder that takes precedence. The fake provider also accepts an in-memory
  response script, which is how retry and failure tests are built.
- **Structured output:** the system prompt embeds the Pydantic JSON Schema, the provider is asked for JSON mode, and
  the reply is parsed and validated. Invalid output is retried **at most twice per AgentRunner call** (3 calls total),
  matching architecture section 5. A provider *error* is retried once, per R5. Then the run is marked `failed` and
  `AgentRunError` is raised. Nothing is skipped silently, so the future pipeline fails closed.
- **One `agent_runs` row per AgentRunner call.** `tokens_in`, `tokens_out` and `cost_usd` are totals across every
  attempt of that run. The row and its `agent.run_succeeded` / `agent.run_failed` audit entry are committed
  immediately, so a failure remains visible when the caller's transaction is rolled back. M3 pipelines must account
  for this commit boundary.
- **Daily cost cap is enforced globally on the UTC day**, before every provider call and including retries. Spend is
  read from `agent_runs`, so it survives restarts and equals what `heraldesk runs` reports. Per-profile reporting uses
  midnight in the profile timezone. A cap of `0` blocks every call. The in-run cost accumulated before a retry is
  counted as pending spend, so retries cannot sneak under the cap.
- **`.env` holds both Heraldesk settings and provider keys.** The dotenv source now filters keys whose root is not a
  Heraldesk config field, so `DEEPSEEK_API_KEY=...` no longer fails strict validation. `build_llm` copies missing
  keys from `.env` into the process environment for LiteLLM; real environment variables win. Values are never logged.
- **Prompt precedence:** bundled `heraldesk/agents/prompts/<role>.md`, overridden by
  `$HERALDESK_HOME/agents/<role>.md`. Role names are validated so a prompt path cannot escape the folder. The system
  prompt is built only from the role template, `soul.md`, `banned.yaml`, platform constraints and the output schema.
  Environment variables and credentials are never read into prompts.
- **Secrets are redacted before persistence.** `agent_runs.input`, `agent_runs.output`, `agent_runs.error` and audit
  payloads pass through a registry built from credential-shaped environment variables plus known token shapes
  (`sk-...`, `ghp_...`, bearer headers). A model that echoes a key cannot plant it in the database.
- **Platform limits are provisional prompt metadata.** `platforms/base.PROVISIONAL_CONSTRAINTS` holds documented
  public limits used by Writer prompts; real adapters replace them with `constraints()` in M5.
- **M2 CLI surface adds two commands:** `heraldesk agent run writer <profile> --idea ...` (the manual proof path, and
  the only way to exercise a real provider before pipelines exist) and `heraldesk runs <profile>` (architecture
  section 9 assigns `runs` to M2). Pipelines still arrive in M3; the placeholder modules and other agents stay
  docstrings.
- **Retry budget across a future pipeline:** the R5 limit of 4 LLM calls per draft is not implemented in M2 because
  there is no pipeline. M2 enforces the per-run limit (2 retries). M3 must keep the combined budget when lint returns
  and schema retries land.
- **`tzdata>=2024.1` is an unconditional runtime dependency.** Windows ships no system IANA timezone database (no
  `/usr/share/zoneinfo` equivalent), so `zoneinfo.ZoneInfo(...)` cannot resolve keys such as `UTC` or `Asia/Jakarta`
  and raises `ModuleNotFoundError: No module named 'tzdata'` underneath, which `llm/budget.py` and profile validation
  surface as `ValueError: unknown IANA timezone ...`. This broke the Windows-only core tests in CI #3 (commit
  `7b6a1e7`). The dependency is not marked `sys_platform == "win32"` because minimal container images also lack the
  OS database, and an unconditional dependency keeps one code path everywhere. Tests, budget logic and CI are
  unchanged: no Windows skips, and the fix is data availability, not error handling. Verified by resolving
  `ZoneInfo("UTC")` and `ZoneInfo("Asia/Jakarta")` with `PYTHONTZPATH=""`, which hides the OS database just like
  Windows.

## M3 implementation decisions (2026-11-16)

- **M3 adds three tables in migration `0004`:** `pipeline_runs`, `content_ideas` and `content_items`. It also adds
  the nullable foreign key `agent_runs.pipeline_run_id -> pipeline_runs.id` promised by the M2 decision. Every table
  carries `profile_id`; `content_items` has `state`, `risk_level`, `risk_notes`, `media`, `idea_id`, `account_id`,
  `parent_id` and `created_by_run_id` as documented in architecture section 4.
- **The content state machine lives in `domain/content.py` and only `transition()` changes a state.** The legal M3
  edges are: drafted -> reviewed/blocked/cancelled/needs_review; reviewed -> pending_approval/blocked/cancelled;
  pending_approval -> approved/rejected/blocked/cancelled; approved -> scheduled/cancelled; scheduled ->
  published/failed/cancelled; failed -> scheduled/needs_review/cancelled; needs_review -> cancelled. `published`,
  `blocked`, `rejected` and `cancelled` are terminal. The human edit path blocked/rejected -> drafted is deliberately
  left to the M4 approval service, so M3 cannot bypass Compliance.
- **`heraldesk run daily --dry-run` writes nothing at all.** It runs the same deterministic simulation and prints
  the would-be drafts, but creates no `pipeline_runs`, `content_ideas`, `content_items`, `agent_runs` or audit rows.
  The agent runner gained a `persist=False` mode that skips traces, audit and commits. Persistence tests use
  `heraldesk run daily` without the flag.
- **The fake platform fallback does not create a `social_accounts` row.** When a profile has no account, the
  pipeline targets `fake` in memory so `profile new` can be exercised offline. Accounts are only read, never
  auto-created.
- **Lint configuration lives in `banned.yaml`.** `banned_buzzwords: null` (or omitted) uses the built-in anti-slop
  list, `banned_buzzwords: []` disables that rule, `banned_words` are always enforced, and `allow_em_dash: false`
  rejects U+2014 by default. Two failed lint attempts move a draft to `needs_review`; Compliance is not called for a
  draft that never passed lint.
- **Compliance fails closed.** A provider error, exhausted call budget or invalid Compliance output moves the draft
  to `blocked` with a reason such as `compliance unavailable`. A `blocked` risk verdict also blocks. Nothing that
  fails Compliance can reach `pending_approval` or `approved`.
- **Deduplication uses the last 60 days of ideas and content bodies.** Text is case-folded, punctuation is removed and
  whitespace collapsed. A candidate is a near-duplicate when it equals an existing fingerprint, is contained in it,
  or reaches a `difflib` ratio of 0.8. Duplicates inside the same run are also rejected.
- **The combined per-draft LLM budget is four calls.** The pipeline allocates the remaining budget across Writer lint
  retries and Compliance by lowering `AgentRunner`'s output retry allowance. When the budget is exhausted after lint,
  the draft is blocked as `compliance unavailable`; before lint it goes to `needs_review`.
- **Researcher search is behind `tools.web_search.WebSearch`.** The fake is the default whenever the LLM provider is
  fake (`research.search_provider: auto`), which keeps tests and CI offline. The real provider is DuckDuckGo HTML
  (no API key) with a 10 s timeout. Every URL must be HTTPS, contain no credentials in the URL, resolve to a public
  address, and pass loopback/private/link-local/reserved/metadata checks before the request is made. Search evidence
  is untrusted data wrapped in explicit delimiters.
- **User-facing draft bodies are redacted before persistence and printing.** `SecretRegistry` removes known credential
  values and recognisable token shapes from content bodies, evidence, risk notes and pipeline errors. Tests assert a
  model-echoed `sk-...` token never reaches `content_items`, `agent_runs`, audit payloads or pipeline output.
- **`run daily` accepts `active` and `draft` profiles but refuses `paused` profiles and either kill switch.** M3 is
  hardcoded `draft_only`, so allowing a draft profile to produce drafts cannot publish anything. The autonomy engine
  remains M4.
- **The Designer step is a deterministic placeholder.** No image model is called in M3. Each draft stores a
  `visual_brief` and `alt_text` in `content_items.media` with `source: designer_placeholder_m3`.
- **No approvals service, autonomy engine, scheduler, publisher or Telegram code is added in M3.**
- **Test updates:** revision expectations move from `0003` to `0004`; the CLI command surface gains the `run` group;
  the missing-prompt test now uses a role that still has no prompt (`analyst`). New M3 tests cover every legal and
  illegal state transition, lint accept/reject cases, 60-day dedupe, dry-run zero writes, pipeline end to end,
  Compliance fail-closed, SSRF rejection and secret redaction.

## M4 implementation decisions (2026-11-16)

- **M4 adds exactly one table, `approvals`, in migration `0005`.** Columns follow architecture section 4 plus the
  requested lifecycle: `id`, `profile_id`, `content_item_id`, `state` (`pending|approved|rejected|expired`),
  `decision` (`approve|reject|edit`, nullable), `edited_body`, `reason`, `decided_at`, `channel`
  (`web|telegram|cli`), `created_at`, `updated_at`, `expires_at`. `profile_id` is denormalized from the content item
  so approval queries never rely on a join.
- **One pending approval per content item is enforced twice.** A partial unique index
  `uq_approvals_pending_item` covers `content_item_id WHERE state = 'pending'` on SQLite and PostgreSQL, and
  `ApprovalRepository` rejects a second pending row in the application. The service is idempotent, so a repeated
  request for the same item returns the existing row. Historical rows stay in the table after approval, rejection or
  expiry; a later edit that reopens an item creates a new request.
- **`decision` stores only `approve` or `reject`.** An edit is recorded through `edited_body` and the
  `approval.edited` audit entry because an edit does not decide the request: it either stays pending or reopens it.
  `expired` rows leave `decision` NULL.
- **The M4 autonomy vocabulary is exactly `draft_only`, `require_approval` and `auto_publish`.** `auto_publish` is a
  new `PolicyMode`. The existing `auto` and `auto_after_delay` values stay valid YAML for the M5 scheduler, but the
  M4 engine deliberately treats them as `require_approval` and says so in the decision reason. No draft is
  auto-approved before the scheduler exists.
- **`default_mode` remains the policy key.** M4 does not add a second top-level `mode` key, because that would
  duplicate `default_mode` and make M0-M3 profile files ambiguous. Rule matching is unchanged from R2: most specific
  (rules with matching `intents`) first, then file order, then `default_mode`.
- **Both `draft_only` and `require_approval` create a pending approval row.** The M4 difference is the recorded policy
  mode and the future scheduling rule: `draft_only` never auto-schedules, while `require_approval` may schedule after
  approval in M5. Both are `pending_approval` in M4, and the invariant "one open approval per pending item" applies
  to both.
- **`auto_publish` only auto-approves low risk after Compliance passed.** The pipeline moves the item through
  `pending_approval -> approved -> scheduled` in one transaction. Medium risk, high risk, a blocked assessment, a
  missing risk level or a failed Compliance check falls back to `pending_approval` plus an approval row. No approval
  row is created for the auto-published path.
- **CLI ids are content item ids.** `queue approve|reject|edit <id>` refers to the id shown by `queue list`, not an
  approval id. Because content item ids are globally unique, `ContentItemLocator` is a narrow global exception to
  `ProfileScope`: it selects only `content_items.profile_id` and returns a scope. Every content read and write after
  that still goes through a scoped repository.
- **Kill-switch precedence for approval is global > per-profile > `paused` > policy.** `queue approve` refuses with
  `global_kill_switch`, `profile_kill_switch` or `profile_paused` before any write. `reject` and `edit` remain
  available while halted because they cannot publish anything. A profile in `draft` status may be approved in M4:
  approval is an internal human decision, not publishing, and M5 still gates publishing on an active profile.
- **Human edit paths are strict in M4.** User edits are allowed only for `blocked`, `rejected`, `needs_review`,
  `approved` and `pending_approval`:
  - `blocked`, `rejected`, `needs_review` -> `drafted` (risk and risk notes are cleared so Compliance must
    re-evaluate);
  - `approved` -> `pending_approval` with a new pending approval row;
  - `pending_approval` stays pending, updates the body and restarts the 72-hour expiry window.
  `drafted` and `reviewed` are transient pipeline states, not user-holdable states, so an edit on them is illegal in
  M4. `scheduled`, `published`, `cancelled` and `failed` are also illegal edit targets. Every state change goes
  through `domain.content.transition()`; a same-state pending edit updates the body and approved `edited_body` only.
- **The content state machine opens the M4 human edges.** `blocked`, `rejected` and `needs_review` can move to
  `drafted`; `approved` can move back to `pending_approval`. Only `published` and `cancelled` are truly terminal.
  The pipeline itself still never reopens an item, so only the approval service can exercise these edges.
- **Approval expiry is 72 hours from `expires_at` and is opportunistic in M4.** There is no scheduler yet, so
  `heraldesk queue list` sweeps stale pending approvals, moves the item to `cancelled` through `transition()` and
  marks the approval `expired`. `approve`, `reject` and `edit` refuse a stale approval with `ApprovalExpiredError`
  without writing anything; the next `queue list` performs the cancellation. The M5 scheduler will own a periodic
  sweep.
- **Approval payloads, audit entries and CLI output are redacted.** Edited bodies and rejection reasons pass through
  `SecretRegistry` before persistence. Audit entries contain metadata only: ids, states, risk, channel, body SHA-256,
  body length and a truncated redacted reason. They never contain the full body. CLI output redacts again and prints
  only a body preview.
- **Audit actions for M4:** `approval.requested`, `approval.approved`, `approval.rejected`, `approval.edited`,
  `approval.expired`, in addition to the existing `content.state_changed`. Auto-published items are visible through
  their `content.state_changed` entries because they pass through the same state machine.
- **No Telegram, scheduler, publisher or real posting is added in M4.** The architecture M4 item that mentions
  Telegram is deferred; the CLI approval flow is the only interface in this milestone.
- **Test updates:** revision expectations move from `0004` to `0005`; the CLI command surface gains the `queue`
  group; the Alembic table set gains `approvals`. New M4 tests cover the full autonomy matrix, approve/reject/edit
  happy and illegal paths, all three human reopen edges, 72-hour expiry, kill-switch refusal, profile isolation,
  audit coverage and secret redaction.

## M5 implementation decisions (2026-11-17)

- **M5 adds no new table.** Migration `0006` adds four columns to `content_items` and one index:
  `publish_mode` (`draft_only|require_approval|auto|auto_after_delay|auto_publish`),
  `publish_delay_minutes`, `publish_attempts` (NOT NULL, default 0) and `last_error` (redacted).
  `ix_content_items_due` covers `(state, scheduled_at)`. The content state machine is unchanged;
  `approved -> scheduled`, `scheduled -> published`, `scheduled -> failed` and `failed -> scheduled`
  already existed from M3/M4.
- **The scheduler is a stateless, idempotent sweep over SQLite state, not an APScheduler daemon yet.**
  `heraldesk scheduler run` (and the alias `heraldesk run scheduler`) selects due items, publishes them and
  exits. APScheduler wiring is deferred to the milestone that implements `heraldesk serve` (M7), because
  `serve` is still a stub and a background scheduler cannot be exercised by the CLI acceptance tests.
  This still satisfies the M5 acceptance criteria: no job lives only in memory, so restarting the process
  loses nothing, and publishing is guarded against double-posting.
- **Publishing state is durable and at-most-once at the database level.** An item with `external_id`
  is never sent again. Adapters also receive `heraldesk:content:<id>` as the idempotency key. The fake
  adapter returns the first result for a repeated key, which is how the retry tests prove that a retry
  cannot double-post. Human reconciliation remains the fallback for a provider that accepted a post but
  lost its response, as decided in R1.
- **Retries are one initial attempt plus two retries.** A provider failure moves `scheduled -> failed`,
  stores a redacted error in `last_error`, and sets `scheduled_at` to the retry time. The default backoff
  is 60 seconds then 300 seconds. The scheduler's due query also selects `failed` items whose attempt
  count is below three and whose retry time has arrived; it moves them `failed -> scheduled` and tries
  again. After the third failure the item stays `failed` with no retry time until a human runs
  `heraldesk publish <id>`, which resets the counter and tries once more.
- **M5 implements `auto` and `auto_after_delay`.** `auto` auto-approves an item after Compliance passes
  and schedules it immediately. `auto_after_delay` schedules it after the matching rule's
  `delay_minutes`, or 30 minutes when the mode comes from `default_mode`; the delay is stored in
  `publish_delay_minutes` so the cancellation window survives restarts. Compliance still gates both
  modes. `draft_only` keeps its M1 meaning: a human may approve it, but the scheduler never
  auto-publishes it; only `heraldesk publish <id>` can.
- **Approval schedules the item and records the policy mode on the item.** The pipeline stores the
  autonomy decision in `content_items.publish_mode` when the item reaches `pending_approval`. Approving a
  `require_approval`, `auto`, `auto_publish` or `auto_after_delay` item moves it to `scheduled` with the
  right due time. Items with no recorded mode (created before M5 or by tests) stay `approved` after
  approval and are published only by the manual command. This is the safe default for legacy rows.
- **Kill-switch precedence is global > per-profile > paused > draft > item policy.** The scheduler checks
  the global marker file, `profiles.kill_switch`, `profiles.status` and finally the item's
  `publish_mode`. A `draft` profile never publishes even though M4 allowed it to approve, matching the M4
  decision that publishing is gated on an active profile.
- **Expired approvals block publishing twice.** The scheduler runs the M4 expiry sweep for every profile
  before selecting items, so stale pending approvals cancel their items. The publisher also refuses an
  item whose latest approval is `pending`, `rejected` or `expired`; this is belt-and-braces for a row that
  reached `scheduled` through an older code path.
- **`scheduler run --dry-run` writes nothing at all.** It performs no expiry sweep, state change, attempt
  increment, audit entry or adapter call. It only lists the due items and the reasons a real run would
  skip them. This follows the M3 `run daily --dry-run` rule.
- **Audit actions and payloads.** `publish.requested`, `publish.succeeded`, `publish.failed` are recorded
  per item, and one global `scheduler.run` entry records counts only. Payloads carry ids, platform, attempt,
  trigger, mode, body SHA-256/length, retry decision and a truncated redacted error. They never contain a
  body, credential or raw provider response.
- **Redaction covers bodies, errors and credentials.** The publisher redacts the outbound body, the
  provider error, and every decrypted credential value for that account before anything is stored in
  `content_items.last_error`, the audit log or CLI output. Known token shapes (`sk-...`, bearer headers)
  are scrubbed even when the value is unknown.
- **CLI surface:** `heraldesk publish <id>` publishes one `approved`, `scheduled` or `failed` item and
  exits 1 on failure; `heraldesk scheduler run [--dry-run]` publishes the global due set. `queue list`
  now also shows `failed` items so a human can find and re-queue them.
- **No real platform adapter is added in M5.** Only `fake` implements `publish`. Threads, Nostr, X,
  Facebook, Instagram and LinkedIn raise `NotSupportedError` from `publish_adapter_for`; the scheduler
  records that as a normal `publish.failed` with a documented reason. **Reason and location:** real
  adapters are deferred because they need app review, live credentials and platform-specific API
  behaviour that cannot be verified offline in this milestone. The user-facing reason is documented in
  `docs/platforms/README.md` (and summarised in this entry); the error message points there. No new
  adapter code is added in M5.
- **M5 acceptance adjustment:** "a scheduled post appears on a test account" is satisfied with the fake
  adapter. A real Threads/Nostr post is not claimed in this milestone; the documented `NotSupported`
  result is the honest outcome, matching the R1 decision that app review delays are not code failures.
- **Test updates:** revision expectations move from `0005` to `0006`; the CLI surface gains `publish` and
  the `scheduler` group; new tests cover due-only selection, future items, all off switches, cancelled and
  expired items, `draft_only`, success, retries with backoff, manual re-queue, idempotency, dry-run zero
  writes, redaction of provider errors and credentials, `auto`/`auto_after_delay`, and the CLI transcript.
