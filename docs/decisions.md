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

## M6 implementation decisions (2026-11-17)

- **M6 scope in this repository is Telegram notifications plus inbox basics.** The architecture
  section 13 row for M6 still names Community and Analyst. The requested milestone is narrower and
  deliberately does not implement `reply_triage`, crisis escalation, metrics collection or
  `weekly_report`; those remain future work. No `heraldesk serve` or APScheduler wiring is added in
  M6. The webhook and polling functions are callable stubs for the M7 server loop.
- **M6 adds two tables in migration `0007`.** `telegram_messages` stores inbound messages
  (`profile_id` nullable, `chat_id`, `sender_id`, redacted `text`, `command`, `content_item_id`,
  `response`, `created_at`). `telegram_notifications` is the outbound ledger (`profile_id`
  nullable, `event`, unique `idempotency_key`, `status` `sent|suppressed|failed`,
  `message_preview`, masked `recipients`, `error`, `created_at`, `sent_at`). Bot tokens are not
  stored in either table.
- **Telegram bot tokens are encrypted with Fernet, never stored in `heraldesk.yaml` or SQLite
  plaintext.** Resolution order is `HERALDESK_TELEGRAM_BOT_TOKEN` in the environment, then the same
  key in `$HERALDESK_HOME/.env`, then `$HERALDESK_HOME/telegram.token` decrypted with `secret.key`.
  `heraldesk telegram token set` writes the encrypted file with mode 600. `telegram token status`
  prints only a fingerprint such as `****7890`. The raw token never reaches an audit payload, a
  notification row, a log line or CLI output.
- **Notifications are addressed to every id in `telegram.allowed_chat_ids`.** Zero recipients is a
  suppressed event, not a crash. Chat ids are stored in full only in `telegram_messages` for
  routing; audit payloads and CLI output use a partial mask such as `12***89`.
- **Policy adds `notify: off | approvals_only | all` and defaults to `all`.** `off` mutes every
  event, `approvals_only` keeps only `approval_requested`, and `all` also allows `published`,
  `failed_final` and `killswitch`. Because YAML 1.1 reads an unquoted `off` as `False`, the parser
  maps boolean `False` to `off` and `True` to `all`, so the documented `notify: off` spelling works.
- **Muted events still create one notification row and one audit entry.** The row has
  `status=suppressed` and a reason such as `notify mode 'off' mutes this event`, and the audit action
  is `telegram.suppressed`. Writing nothing would let a later scheduler run decide to send an old
  event; the ledger makes suppression final. Delivery is at-most-once: a `TelegramError` records
  `status=failed` and is not retried automatically.
- **Exact notification idempotency keys** (must remain stable):
  - `telegram:approval_requested:profile=<profile_id>:item=<item_id>:approval=<approval_id>`
  - `telegram:published:profile=<profile_id>:item=<item_id>:attempt=<publish_attempts>`
  - `telegram:failed_final:profile=<profile_id>:item=<item_id>:attempt=<publish_attempts>:at=<epoch>`
  - `telegram:killswitch:profile=<profile_id|global>:state=<on|off>:event=<audit_id>`

  The `failed_final` key carries the failure timestamp so a later human re-queue that fails again is
  a new event, while repeated scheduler sweeps of the same row are one event.
- **Publish and final-failure notifications live in the publisher service.** `publish_item` accepts
  an optional notifier and emits `published` after success and `failed_final` only when retries are
  exhausted. Both `heraldesk publish <id>` (manual) and the scheduler go through that function, so
  both paths notify and share the same idempotency keys. A second call cannot double-send because the
  item already has an external id or is no longer due.
- **`approval_requested` is emitted by the daily pipeline after `request_approval`.** Dry runs skip
  it completely. The notification service is optional, so tests and offline runs without Telegram
  continue to work.
- **Kill-switch notifications are emitted only when the state actually changes.** Global
  `heraldesk stop-all` uses the global kill-switch audit entry id as the event id; the per-profile
  `heraldesk profile killswitch` command uses its latest audit entry id. Repeating a no-op command
  does not send a second message.
- **Inbound commands call the same domain services as the CLI.** `/approve <id>` and
  `/reject <id> [reason]` call `domain.approvals.approve|reject` with `channel='telegram'`, so the
  kill-switch gate, 72-hour expiry check, state machine and audit entries are identical to
  `queue approve|reject`. `/queue` sweeps expired approvals and lists the queue. `/help`, `/start`
  and unknown commands reply with deterministic help text and never raise.
- **Only allowlisted chats are served.** A message from any other chat id is ignored before storage
  or reply. `handle_update` resolves the profile from the content item for approve/reject; `/queue`
  uses an optional profile hint from the calling webhook/poll stub, falls back to the single
  registered profile, and otherwise replies with the list of profile slugs. This deliberately avoids
  adding a chat-to-profile mapping to `heraldesk.yaml` in M6.
- **Inbound text and replies are redacted before storage.** `telegram_messages.text` and `response`
  pass through `SecretRegistry` with the bot token added as a known secret. Audit payloads carry
  message ids, masked chat/sender ids, command, content item id, a SHA-256 of the redacted text and
  its length, never the text itself. Audit actions are `telegram.received`, `telegram.command`,
  `telegram.sent`, `telegram.suppressed`, `telegram.failed` and `telegram.token_set`.
- **No new Telegram dependency is added.** The real adapter uses `urllib` for `sendMessage` and a
  `getUpdates` polling stub and never puts the token in an exception or repr. Tests always use
  `FakeTelegramAdapter`. `aiogram`/`python-telegram-bot` can replace the adapter in a later milestone
  without changing the notifier or inbox.
- **CLI surface:** `heraldesk telegram test`, `telegram send <profile> <text>`,
  `telegram inbox list <profile> [--limit 20]`, plus the small `telegram token set|status` utility
  needed to create and inspect the encrypted token file. `telegram test` and `telegram send` always
  use the fake adapter and print redacted previews.
- **Test updates:** revision expectations move from `0006` to `0007`; the Alembic table set gains
  `telegram_messages` and `telegram_notifications`; the CLI group set gains `telegram`. New tests
  cover one-send-per-event, mute modes, global and per-profile kill-switch notifications, both manual
  and scheduler publishing, final failures, dry-run silence, token encryption and redaction, inbox
  approve/reject/queue/help, expired approvals, allowlist refusal and chat-id masking.

## M7 implementation decisions (2026-11-17)

- **M7 scope in this repository is the single-process server and background jobs.** The architecture
  section 13 row for M7 names the local web UI. The requested milestone is narrower: `heraldesk serve`
  with `GET /health`, `GET /status`, `POST /webhook/telegram`, the scheduler sweep and Telegram polling.
  The web UI, password auth, content calendar, reports, inbox screens, `/api/v1` namespace, Community
  replies/moderation, analytics dashboard and `weekly_report` remain future work. No M8 work is started.
- **Runtime dependencies added:** `fastapi`, `uvicorn` (architecture section 2 names FastAPI + Uvicorn for
  the API/web server) and `apscheduler>=3.10,<4` (architecture section 2 and R5). `httpx` is a dev
  dependency because FastAPI's `TestClient` requires it. These are the only new dependencies in M7;
  `jinja2` is not added because there is no web UI yet.
- **`telegram.mode` is new config: `polling | webhook`, default `polling`.** Telegram refuses `getUpdates`
  while a webhook is registered, so exactly one receive path may run in the background. In `polling` the
  10-second poll job runs and the webhook endpoint still accepts local POSTs for testing; in `webhook`
  the poll job is not registered and the endpoint handles updates. `telegram.enabled` remains the master
  switch. The default keeps the documented M7 behaviour.
- **Migration `0008` adds one column, not a table.** `telegram_messages.update_id` (nullable) has a unique
  index `ix_telegram_messages_update_id`. Telegram retries webhooks and replays `getUpdates` after a
  restart, so an already recorded update id returns `status="duplicate"` without storage, reply or audit.
  `TelegramUpdateLocator` is a narrow unscoped lookup, documented like `ContentItemLocator`: it returns
  only a boolean and never message text, chat ids or profile data.
- **There is deliberately no `apscheduler_jobs` table or `SQLAlchemyJobStore`.** This supersedes the R5
  wording that named a job store. The recurring triggers are static code (`publish sweep` every 60 s,
  `telegram poll` every 10 s) and every unit of durable work already lives in the business tables
  (content items, approvals, `telegram_notifications`). Persisting the triggers would add a second job
  schema that can disagree with the code and can replay stale triggers after an upgrade; it would not add
  restart safety. A restart recreates the triggers and an immediate first sweep resumes all overdue items,
  which is the M5 stateless-sweep decision extended to the server.
- **The first sweep runs immediately on start, then at the configured interval.** This makes a restart
  recover overdue items without waiting a full minute. Intervals are constructor parameters (defaults
  60 s and 10 s) so tests can use short intervals; they are not exposed in `heraldesk.yaml` in M7.
  APScheduler jobs use `max_instances=1`, `coalesce=True` and `replace_existing=True`.
- **Graceful shutdown is owned by Uvicorn and the FastAPI lifespan.** Uvicorn handles SIGINT/SIGTERM,
  stops accepting requests, runs lifespan shutdown, which calls `BackgroundScheduler.shutdown(wait=True)`,
  writes `serve.stopped`, and disposes the shared engine. `serve.started` records the Telegram active
  flag, mode and intervals. No custom signal handler is installed, so one process has one shutdown path.
- **The server uses one shared SQLite engine for its lifetime.** The CLI opens an engine per command; the
  server opens one engine, hands out a session per request or job, commits on success, rolls back on
  failure, and disposes on shutdown. WAL and the existing busy timeout handle the request and job threads.
- **Binding is loopback-only until admin authentication exists.** `--host`, `server.host` and
  `HERALDESK_SERVER__HOST` all go through the same check. Any non-loopback host is refused with a clear
  error, satisfying the architecture section 10 safety rule and the M7 acceptance intent without shipping
  a half-built password system. A reverse proxy on loopback is the supported remote setup for now.
- **The API surface is exactly the three operational endpoints.** `GET /health` returns 200 with
  `status`, `version` and `db.ok`/`db.revision`, or 503 when the database cannot be read or has no
  schema revision. `GET /status`
  returns profile metadata, per-profile and global queue counts (the states `queue list` shows, plus
  `total`), the last global `scheduler.run` audit payload and Telegram state. It returns chat counts, not
  chat ids. `POST /webhook/telegram` always answers 200 for valid JSON (including ignored, duplicate and
  domain-error outcomes), never echoes the chat id or token, and returns only status/command/item/reply.
  Invalid JSON and non-message payloads are ignored. Disabled Telegram ignores every webhook.
- **Audit actions:** `serve.started`, `serve.stopped`, the existing `scheduler.run` (written by every real
  background sweep, with counts only) and the existing `telegram.received`/`telegram.command` written by
  the inbox for webhook and polled updates. No server audit payload contains a token, a body or a full
  chat id.
- **Logging policy:** the server logs job failures by exception type only. It never logs request bodies,
  message text, tokens or full chat ids. Uvicorn's access log records method/path/status, and the webhook
  path contains none of those values.
- **`--no-web` is accepted for compatibility but is a no-op in M7.** There is no web UI yet; the API is
  always mounted. `--no-telegram` disables polling and notifications for that process.
- **The dry-run guarantee is preserved through the background path.** `BackgroundJobs.run_scheduler_once`
  accepts `dry_run=True`, which calls the existing `run_scheduler` dry run: no state change, attempt
  counter, audit row, adapter call or notification. There is no user-facing `serve --dry-run` flag; the
  guarantee is covered by a test and by the existing `heraldesk scheduler run --dry-run` command.
- **Test updates:** revision expectations move from `0007` to `0008`; the Alembic table set is unchanged,
  but `telegram_messages` gains `update_id` and its unique index. The CLI `serve` stub tests are replaced
  by help/loopback/init checks. New M7 tests cover health, status counts, webhook approve/allowlist/expiry,
  token and chat-id leakage, background publishing exactly once, polling deduplication across a simulated
  restart, graceful shutdown audit, dry-run silence and webhook mode disabling polling.

## M8 implementation decisions (2026-11-17)

- **M8 scope in this repository is the manual CLI inbox and deterministic reports.** The architecture
  section 13 row for M8 names plugin loading and extra platform adapters. The requested milestone is
  narrower: `heraldesk inbox` triage and `heraldesk report weekly|monthly`, both CLI only. There is no
  web UI, no auto-reply, no AI-generated reply, no sentiment moderation and no Instagram/Facebook API.
  The plugin work and real extra adapters remain future work.
- **Migration `0009` is an M8 schema revision, not the M9 release milestone.** It adds three columns to
  the existing M6 table `telegram_messages`: `status` (`pending|handled|ignored`, NOT NULL, default
  `pending`), `triaged_at` (nullable) and `ignore_reason` (nullable `spam|off-topic|other`), plus
  `ix_telegram_messages_status`. No inbox table and no social account is created. The model default and
  the DB server default keep M6 inserts working unchanged.
- **The cross-profile inbox view is one documented exception to `ProfileScope`.** The required CLI has
  no profile argument, and M6 stores messages with a nullable `profile_id` before a command links them.
  `InboxRepository` reads and updates only `telegram_messages`, returns no other profile table and is
  documented like `GlobalCostRepository`. All content and cost queries in reports still use
  `ProfileScope` per registered profile.
- **A human owns every reply.** `inbox reply` refuses missing items, empty or whitespace-only text, and
  items already `handled` or `ignored`. It sends one message through the existing Telegram adapter
  to the original `chat_id`, and only after a successful send marks the item `handled`, stores the
  redacted response and sets `triaged_at`. A provider error leaves the item `pending`.
- **Replies use the configured Bot API adapter in production and the fake adapter in tests.** Resolution
  requires `telegram.enabled` and a token; otherwise the command fails clearly instead of pretending to
  deliver. Tests inject `FakeTelegramAdapter` and never touch the network. The reply body is redacted
  before sending and before storage, and the CLI preview uses the stored redacted response.
- **Ignore reason is bounded to `spam`, `off-topic` and `other`, defaulting to `other`.** `ignore` does
  not send anything; it records the reason and `triaged_at` and moves the item to `ignored`.
- **Audit actions:** `inbox.listed`, `inbox.shown`, `inbox.replied`, `inbox.ignored` and
  `report.generated`. Inbox payloads contain ids, status, command, masked chat/sender ids, reply length
  and SHA-256, never the reply or inbox text. `report.generated` contains period, period type, path and
  aggregate counts, never report bodies.
- **Reports are instance-wide and deterministic.** `report weekly` accepts strict `YYYY-Www` ISO weeks
  (Monday 00:00 UTC to next Monday) and `report monthly` accepts strict `YYYY-MM` months; both default
  to the current UTC period. Files are `$HERALDESK_HOME/reports/weekly-YYYY-Www.md` and
  `monthly-YYYY-MM.md`. `reports/` is created on demand and is not added to `DATA_SUBDIRS`, so `init`
  and `doctor` behaviour is unchanged.
- **Report metrics use the `created_at` cohort, not audit events.** Total created, approved, published,
  failed, approval rate, publish failures, top published posts and inbox status counts all select rows
  created inside the period. "Approved" means an item whose current state is `approved`, `scheduled` or
  `published` (it reached approval). Approval rate is approved divided by created, `0.0%` when the
  period is empty. Cost is the sum of `agent_runs.cost_usd` for runs started inside the period. No
  audit-log event counting is used.
- **No-data periods still generate a report with zeros.** Ordering is newest-first with id as the
  tie-breaker; failure and post previews are truncated and redacted. The file contains no generation
  timestamp, so the same database state always produces byte-identical output and repeat runs are
  idempotent.
- **No new dependencies are added.** The implementation uses the existing SQLAlchemy models, Telegram
  adapter, audit service, redaction registry and Typer/Rich CLI.
- **Test updates:** revision expectations move from `0008` to `0009`; the CLI group set gains `inbox`
  and `report`; the Alembic table set is unchanged. New tests cover status filtering and limits, show,
  reply success and failure, already-triaged refusal, ignore reasons, unlinked messages, report period
  boundaries, created-at cohort metrics, failure/post limits, zero-data reports, byte-identical repeat
  runs, redaction and every new CLI command.

## M9 implementation decisions (2026-11-17)

- **M9 in this repository is hardening and release documentation, not Docker or PyPI publishing.** The
  architecture section 13 row for M9 names a Docker image, PyPI publication, compose/systemd examples and a
  full documentation set. The requested milestone is narrower: backup/restore, doctor hardening, error and
  secret checks, release docs and version 1.0.0. Docker, PyPI, compose, systemd and a release workflow are
  deliberately deferred. No new social API, web UI, LLM feature or plugin loader is added.
- **Backups are private ZIP archives built with the standard library.** `heraldesk backup` snapshots SQLite
  through the `sqlite3` online backup API and writes a `manifest.json`, `heraldesk.db`, `heraldesk.yaml`,
  `profiles/**` and `agents/**` (user prompt overrides). The archive is mode 600. `.env`, `logs/`, generated
  `reports/`, previous backups and SQLite `-wal`/`-shm` files are never included. No new dependency is added.
- **`secret.key` is opt-in.** The default backup excludes it; `--with-credentials` includes it and refuses to
  run when the key is missing. A backup without the key can never restore the database because the encrypted
  social credentials would be unusable.
- **Restore follows the M0 rule exactly.** An archive without `secret.key` fails with: "Backup has no
  secret.key, cannot decrypt social credentials. Restore profiles and config only with --without-credentials,
  or provide secret.key". `--without-credentials` restores config, profiles and agents only; it does not restore
  `heraldesk.db` and does not restore `secret.key`. Full database restore therefore requires a backup made with
  `--with-credentials`.
- **Restore is safe by default.** The archive is validated before the target is touched: unsafe paths (`..`,
  absolute paths, unexpected roots, duplicate entries), invalid ZIPs, invalid manifests, invalid configs and
  unreadable databases are rejected. Existing target data requires `--force`; the default output lives under
  `$HERALDESK_HOME/backups/`. A restored `secret.key` is re-applied with mode 600.
- **Backup and restore are audited.** `backup.created` records the path, entry count and whether the key was
  included. `backup.restored` is written to the restored database after a full restore. `--without-credentials`
  has no restored database to write to, so it prints the files-only result and the next `heraldesk init` step.
  Audit payloads never contain the key, credentials or archive contents.
- **Doctor hardening adds three checks.** `migrations` compares the stored Alembic revision with the installed
  head and fails when the database is behind or unknown. `telegram token` fails when `telegram.enabled` is true
  but no token can be resolved, and never prints the token. `reports` verifies the reports directory is writable,
  or that it can be created from a writable data folder. Existing Python, data-folder, key, database, config,
  `.env` and loopback checks remain.
- **Kill-switch precedence is unchanged.** M9 adds regression tests proving the global switch blocks manual
  publishing, the CLI scheduler sweep and the `heraldesk serve` background sweep. No gate was relaxed.
- **CLI error hardening is tested, not implemented as a blanket exception handler.** All DB-backed commands
  were exercised without an initialised home, with a missing database and with an invalid config. They exit 1
  with a clear message and no traceback; secret-bearing environment values never appear in the output. Backup
  and restore errors use the same `abort()` path.
- **Version 1.0.0 is read from `src/heraldesk/__init__.py`.** `pyproject.toml` keeps `dynamic = ["version"]` and
  already points hatchling at that file, so no duplicate static version is added. `CHANGELOG.md` records the
  M0-M9 milestones and the deferred scope.
- **Test updates:** new `test_backup.py`, `test_cli_m9.py` and `test_hardening.py`; `test_doctor.py` gains the
  migration, Telegram-token and reports checks; the CLI command set gains `backup` and `restore`; the serve
  background path gains a kill-switch regression. The schema and Alembic head remain `0009`; M9 adds no
  migration.

## M10 implementation decisions (2026-11-17)

- **M10 scope is one real adapter: Nostr, throwaway account only, CLI only.** The milestone proves Heraldesk can
  publish for real instead of only through the fake adapter. Threads, Instagram, X, Facebook, LinkedIn, the web UI
  and new LLM features stay out of scope, and M11 is not started.
- **Nostr comes first because it needs no app review.** An account is a key pair plus relays, signing is
  deterministic (BIP-340), and the relay protocol is public. That makes a real end-to-end proof possible without
  waiting on a platform review, which was the reason M5 shipped a documented `NotSupported` result.
- **Runtime dependencies added: `coincurve>=21.0,<22` and `websockets>=15,<18`.** `coincurve` exposes libsecp256k1
  through cffi and provides BIP-340 `sign_schnorr` plus `PublicKeyXOnly`; the implementation is checked against the
  official BIP-340 test vector. `websockets` is an async RFC 6455 client that matches the publisher's async adapter
  call. Rejected alternatives: `nostr-sdk` (heavy Rust binding), `pynostr` (less maintained and pulls a larger
  tree), `websocket-client` (synchronous, needs threads) and a hand-written Schnorr implementation (crypto
  footgun). Both packages ship typed wheels for Linux, macOS and Windows, so `mypy --strict` and the Windows core
  suite stay clean.
- **Migration `0010` adds a nullable `social_accounts.label`.** `heraldesk account add --label` needs a real column
  instead of a cosmetic CLI value, and `account list` shows it to distinguish throwaway keys from long-lived
  accounts. The column is additive; no existing row or M0-M9 behaviour changes. The Alembic head moves to `0010`.
- **Adapter location is `src/heraldesk/platforms/nostr.py` plus `nostr_keys.py`.** The codebase keeps one module per
  platform under `platforms/`, not under `publisher/`. The keys module is pure standard library bech32 so key
  validation does not depend on the relay client; the adapter module owns signing, event construction and relays.
- **Event rules:** one NIP-01 kind 1 text note per content item; `created_at` is the publish time as a Unix
  timestamp; `tags` is empty plus one `["t", tag]` per hashtag (lowercase, deduplicated, at most ten); the id is
  the SHA-256 of `[0,pubkey,created_at,kind,tags,content]`; the signature is BIP-340 over that 32-byte id. Signing
  uses a 32-byte zero auxiliary value, so the same input is deterministic and testable.
- **Relay policy:** precedence is the account credential `relays` (set with `--relay`) over `nostr.relays` in
  `heraldesk.yaml` over the defaults `wss://relay.damus.io` and `wss://nos.lol`. Only `wss://` URLs are accepted.
  The adapter broadcasts concurrently and waits up to 10 seconds per relay for `["OK", <id>, true, ...]`.
  **Success is at least one accepting relay**; one relay rejecting or timing out does not fail a publish another
  relay accepted. Zero accepting relays raises a redacted `PublishError`, and the existing publisher retry and
  at-most-once rules apply unchanged.
- **At-most-once is inherited, not reimplemented.** The publisher still refuses to send an item that has an
  `external_id`, and relays deduplicate identical event ids. A relay that accepted an event but lost its `OK` reply
  remains the documented human-reconciliation case from R1/M5.
- **The nsec is a credential, the npub is public.** `heraldesk account add --credential-from-env NAME` reads the key
  from the environment and stores it only in the existing Fernet-encrypted credential blob. The handle is the npub;
  an nsec supplied as a handle is rejected. `heraldesk account list`, audit payloads and errors show a masked npub
  such as `npub1lycg5…q266`, never the full identifier or the key. `AccountRef.__repr__` now hides all credentials,
  and `SecretRegistry` recognises `nsec1...`/`npub1...` shapes and `*_NSEC*` environment names.
- **`account test --platform nostr` is offline.** It decodes the nsec, derives the npub and validates the relay
  list without opening a socket. The fake adapter remains the check for every other platform.
- **Throwaway warning is mandatory in the docs.** Nostr events are immutable and cannot be deleted, so the README and
  `docs/platforms/nostr.md` tell the user to test with a throwaway key, verify the event id on a public explorer and
  then abandon the key. The manual acceptance run uses the user's terminal only; the nsec is never requested in
  chat, committed to the repository or shown in screenshots.
- **No publisher, scheduler, autonomy or state-machine logic changes.** `publish_adapter_for` gains a `nostr` entry,
  the stale "in M5" wording is removed from the missing-adapter error, and the fake path is untouched. The
  publisher's existing retries, audit actions (`publish.requested|succeeded|failed`) and notification idempotency
  keys are reused as they are.
- **Tests:** `test_nostr_adapter.py` covers bech32/nsec/npub, the reference event id and signature, hashtag tags,
  OK/NOT OK handling, timeouts, relay configuration and redaction. `test_publisher_nostr.py` covers publish, retry,
  at-most-once, redacted final failure and the scheduler path. `test_cli_m10.py` covers account add/list/test,
  publish success and failure, and proves the nsec and full npub never appear in CLI output or audit payloads.
  Every relay call is mocked; pytest stays offline. Revision assertions move from `0009` to `0010`.

## M11 implementation decisions (2026-11-18)

- **M11 adds the local-first Web UI and `/api/v1` on top of M0-M10.** The CLI remains the
  complete control surface. The server is still one FastAPI/Uvicorn process with SQLite
  only, no Node build, no Redis and no telemetry. No schema migration is added.
- **Authentication is a single admin token, never optional.** Resolution order is
  `HERALDESK_ADMIN_TOKEN` in the environment, the same key in `$HERALDESK_HOME/.env`, then
  `HMAC-SHA256(secret.key, "heraldesk-admin-v1")`. The token is never written to
  `heraldesk.yaml`, HTML, JSON, a cookie, an audit payload or a log line. `heraldesk admin
  token` shows the source and a fingerprint; only `--show` prints the raw secret.
- **The API accepts Bearer only; the UI accepts a signed HttpOnly cookie only.** Mixing
  the two would weaken the CSRF boundary. The API returns
  `{"error": "...", "code": "..."}` for every failure, including validation errors; the
  validation handler deliberately does not echo submitted bodies.
- **UI sessions are stateless signed cookies.** The payload carries `iat`, `exp` and a
  random CSRF value and is signed with a key derived from the admin token. The cookie is
  `HttpOnly`, `SameSite=Lax`, `Path=/`, 12 hours, and `Secure` whenever HTTPS proxy mode
  is on. Logout clears the cookie; rotating the admin token invalidates every session.
- **Every UI POST has a CSRF token.** Authenticated forms use the CSRF value inside the
  signed session. The login form uses a short-lived signed double-submit cookie because no
  session exists yet. Login failures do not create a session.
- **Login rate limiting is in-memory: five failed attempts per 60 seconds per IP.** The
  sixth returns 429 with `Retry-After`. `X-Forwarded-For` is trusted only when
  `HERALDESK_TRUST_PROXY=1`. The limiter resets on process restart, which is acceptable for
  a single-user process.
- **A token in a URL is always rejected.** Middleware detects `token`, `access_token` or
  `admin_token` in any query string, logs only that a token was seen, and returns 400. A
  uvicorn access-log filter strips query strings so the token cannot leak through access
  logs either.
- **Remote mode is explicit and HTTPS-only.** The server still binds `127.0.0.1` by default.
  A non-loopback host requires `--allow-remote`, an explicit environment/.env
  `HERALDESK_ADMIN_TOKEN` (a secret-key-derived token is not accepted), and
  `HERALDESK_TRUST_PROXY=1`. Missing proxy mode fails with "Remote mode requires HTTPS via
  reverse proxy". Remote mode sets Secure cookies and logs a warning without secrets. The
  recommended VPS layout remains a loopback bind behind Caddy/Nginx with TLS.
- **`POST /webhook/telegram` is the documented platform-callback exception to "all routes
  require auth".** Telegram cannot send an admin Bearer token; the route stays public only
  when Telegram is enabled, still enforces `allowed_chat_ids`, and now optionally verifies
  `X-Telegram-Bot-Api-Secret-Token` against `HERALDESK_TELEGRAM_WEBHOOK_SECRET` when that
  variable is set. For a VPS, set the secret, firewall the port, and terminate TLS at the
  proxy. A full Telegram secret rotation workflow is deferred to M12.
- **`/health` is public liveness only.** It returns status, version and database revision,
  no profile, queue, inbox or credential data. `/status` and every `/api/v1` route require
  auth. `/static/*` serves only CSS/JS.
- **The UI is Jinja2 + HTMX, with both assets vendored.** `htmx.min.js` and its license are
  in `src/heraldesk/web/static/`; there is no CDN, npm or React build. Forms work as normal
  POST/redirect forms and HTMX replaces the affected row when JavaScript is available.
- **Runtime dependencies added:** `jinja2` (templates), `python-multipart` (forms),
  `markdown` + `bleach` (report rendering and sanitization). `types-bleach` and
  `types-Markdown` are dev-only stubs for strict mypy. HTMX is a static file, not a Python
  dependency.
- **Report rendering is sanitized.** Markdown is rendered with tables/fenced-code support,
  then passed through `bleach` with an allowlist of tags, attributes and protocols. Report
  filenames are restricted to `weekly-YYYY-Www.md` and `monthly-YYYY-MM.md`; traversal and
  unknown names return 404. Downloads serve only those files.
- **Reports GET is read-only.** `GET /api/v1/reports/weekly|monthly` builds the existing
  analyst report in memory and returns `{summary, markdown}` without writing a file. The UI
  "Generate" POST forms call the audited `write_report` service, which creates the file and
  records `report.generated`. This keeps GET semantics clean while preserving the CLI
  behavior.
- **Approval ids are the public identifier for approve/reject.** Both the API
  (`/api/v1/approvals/{id}`) and the UI (`/queue/{id}/approve|reject`) use the approval row
  id returned in the queue listing. A narrow `ApprovalLocator` resolves it to the profile
  scope and content item id; every state change still goes through the existing scoped
  approval service with `channel="web"`. `POST /api/v1/publish/{id}` uses the content item
  id, matching `heraldesk publish`.
- **All Web/API actions reuse existing services.** Approve/reject call
  `domain.approvals`; inbox reply/ignore call `community`; reports call `analyst`; publish
  calls `publisher.publish_item` with the normal adapter factory, cipher, retries,
  at-most-once `external_id` handling and notifications. The only domain addition is an
  optional redacted `comment` on approve.
- **Audit events:** existing `approval.*`, `inbox.*`, `publish.*` and `report.generated`
  entries are reused; M11 adds `auth.login`, `auth.login_failed`,
  `auth.login_rate_limited` and `auth.logout`. Auth payloads never contain the token or a
  full IP.
- **Security headers** are set for every response: `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a strict CSP allowing only
  same-origin assets. There is no CORS configuration; the UI is same-origin only.
- **`--no-web` is now meaningful.** It disables the UI router and `/static` but keeps
  `/health`, `/status`, `/webhook/telegram` and `/api/v1`. Existing CLI commands and output
  are unchanged. `--allow-remote` is additive.
- **Tests:** `test_web_auth.py` covers token resolution, sessions, CSRF, rate limits,
  query-token rejection, access-log redaction, the remote/HTTPS guards and the admin-token
  command. `test_web_api.py` covers every `/api/v1` route, approval-id isolation, inbox
  actions, in-memory reports, publish and generic validation errors. `test_web_ui.py`
  covers login/cookie flags, CSRF, queue/inbox/reports/profiles, sanitized Markdown, no
  secret leakage and the auth boundary. `test_serve.py` and `test_cli.py` are updated for
  authenticated `/status` and the `admin` command group.

## Quiet hours and daily-cap enforcement (2026-11-19)

The parsed policy fields `quiet_hours` and `max_posts_per_day` were previously accepted
but never enforced. They are now gates for automatic publishing, evaluated in the
profile timezone.

- **Where the gate lives.** `domain/scheduling.py` owns the quiet-hours window arithmetic,
  the profile-local day boundaries and the gate result. `publisher.policy_skip_reason`
  applies it to every non-manual publish (scheduler runs and retries); the scheduler's
  dry-run path calls the same helper so its report matches a real run.
- **Quiet hours are half-open** (`start` inclusive, `end` exclusive) and may cross
  midnight. An item due inside the window stays due and is retried after the window ends;
  no state changes and no retry counter increments occur while it waits.
- **`auto`, `auto_publish` and `auto_after_delay` defer across quiet hours.** If the
  computed schedule time lands inside the window, the pipeline records the end of the
  window as `scheduled_at`. The same deferral applies when a human approves a pending
  item whose mode schedules it automatically. This supersedes the narrower M5 wording
  that only mentioned `auto_after_delay`.
- **The daily cap counts PUBLISHED items per profile-local calendar day**, using
  `published_at` and the profile timezone. Retries that eventually publish count once.
  Items skipped by the cap remain scheduled and run on the next local day.
- **`max_posts_per_day: 0` disables automatic publishing entirely.** A literal cap of
  zero is safer than guessing the user meant "unlimited". Manual publishing still works.
- **A human publish overrides the gate.** `heraldesk publish`, the Web UI and the REST API
  call the publisher with `trigger="manual"`. An override records
  `publish.policy_override` with the reason, the human-readable detail, the trigger and
  the profile timezone; the payload never contains the body or credentials. Background
  publishing and retries never override the gate.
- **A missing or malformed `policy.yaml` fails closed for automatic publishing.** The item
  is skipped with `policy_unavailable`, stays `scheduled` or `failed`, and resumes when
  the file is fixed. The sweep itself does not crash. A manual publish still proceeds and
  records the override.
- **Daylight saving is handled with `zoneinfo`.** Local day boundaries convert midnight
  separately on each side, so a DST day is 23 or 25 hours. Deferral uses the later
  occurrence of an ambiguous end time (`fold=1`) so a fall-back hour cannot publish inside
  the quiet window.

## Third-party adapter plugins (2026-11-19)

The original M8 scope included an entry-point loader. It ships now as M12 without adding
a real platform adapter.

- **Entry-point group:** `heraldesk.platforms`. The entry point name is the platform key
  stored on accounts; the target is a zero-argument callable returning an adapter.
- **Contract:** `PlatformAdapter` from `platforms/base.py` (`platform`, `publish`,
  `validate_credentials`). The returned `platform` must equal the entry point name.
- **Built-ins win.** A plugin cannot replace `fake` or `nostr`; duplicate entry point
  names keep the first one.
- **Discovery is lazy and cached.** Only entry-point metadata is read at discovery; plugin
  code is imported on first use. `reset_plugin_cache()` exists for in-process installs.
- **Account checks stay offline.** `adapter_for` never imports plugin code; `account test`
  keeps the fake adapter for plugin platforms. Only publishing loads the plugin.
- **Failures are explicit.** `PluginError` subclasses `PublishError`, so a broken plugin
  is a normal publish failure rather than a crash. The publisher keeps ownership of the
  state machine, policy gates, retries, idempotency, notifications and audit.
- **Trust boundary.** Plugin code runs in the Heraldesk process with the user's
  permissions; the documentation says to install only trusted packages. No sandbox is
  attempted.

## Online doctor probes (2026-11-19)

`heraldesk doctor` stays offline by default. `--online` adds explicit network checks and
never prints an API key, bot token or nsec.

- **LLM:** one completion with `max_tokens=1` only when the resolved provider is LiteLLM.
  The fake provider returns a PASS with "no network call". A failed call FAILs the doctor.
- **Telegram:** `getMe` only when `telegram.enabled` is true and a token resolves through
  the normal environment/`.env`/encrypted-file precedence. A missing token or failed call
  FAILs.
- **Nostr:** one WebSocket handshake, trying the configured relays in order, only when at
  least one registered profile has a Nostr account. Reaching any relay passes; reaching
  none is a WARN because network filtering is not a broken installation.
- **Timeouts:** the configured LLM timeout, the Telegram adapter's 10-second timeout, and
  a relay-probe cap of 5 seconds. There are no retries.
- **Tests:** probes are small private functions, so tests monkeypatch them and never touch
  the network. A default `--online` run makes no external call (fake LLM, disabled
  Telegram, no Nostr account).


## Superseded notes (2026-11-19)

- **R1 "last 20 posts" duplicate check:** superseded by M5. At-most-once uses the persisted
  `external_id` plus the local idempotency key; no adapter fetches remote history.
- **R2 cancellation and stop-all:** no `queue cancel`/Telegram/web cancel exists, and
  `stop-all` does not move scheduled items to `cancelled`. It blocks publishing while engaged.
- **R2/§10 inbox retention:** the 30-day purge is not implemented; inbox text is kept until
  a human acts or the row is deleted.
- **M7 loopback-only until auth:** superseded by M11. Remote mode requires `--allow-remote`,
  an explicit admin token of at least 32 characters and `HERALDESK_TRUST_PROXY=1`.
- **M7 `--no-web` no-op:** superseded by M11; it now disables the UI router and `/static`.
- **M12 and Telegram secret rotation:** M12 shipped the platform plugin loader, not a
  Telegram secret rotation workflow. Webhook secrets are configured per deployment.
- **AGENTS principles:** AGENTS keeps ten core principles. The two extra items (official
  platform APIs only, no telemetry) are also principles 9 and 10 in `architecture.md`;
  this supersedes the M0 instruction to keep exactly eight.


## Production hardening and Threads (2026-11-19)

- **Threads is a bundled adapter.** Text-only posts use the official two-step API:
  `POST /{user-id}/threads`, poll `/{container-id}`, `POST /{user-id}/threads_publish`,
  then read `/{media-id}?fields=permalink`. `external_id` is the media id; the permalink
  is best effort. The access token is sent in the documented `access_token` field and is
  never included in an error message.
- **No remote duplicate check or API idempotency.** At-most-once comes from the persisted
  `external_id`; the adapter caches per idempotency key inside one process.
- **Text is rejected locally above 500 UTF-8 bytes.** The platform counts emoji by bytes
  but does not document the exact algorithm; this conservative guard avoids deterministic
  400s. Token refresh, replies, insights and media posts are future work.
- **Webhook authentication is mandatory in webhook/remote mode.** The server refuses to
  start without `HERALDESK_TELEGRAM_WEBHOOK_SECRET`; optional `telegram.allowed_user_ids`
  adds a sender allowlist. This supersedes the earlier "secret is optional" wording.
- **Remote admin tokens must be at least 32 characters.** The derived token already is;
  short explicit tokens are rejected before startup.
- **Manual replies obey the off switches.** `community.reply_item` checks the global and
  per-profile kill switches and the profile status. Quiet hours apply to automatic
  publishing only, because replies are manual.
- **Schema upgrades are guarded.** `heraldesk upgrade` backs up credentials, migrates and
  audits. `serve` refuses a stale schema and `/health` returns 503 with the expected
  revision.
- **Distribution is part of the release.** Docker/Compose, systemd/Caddy examples, a
  tag-triggered release workflow, CI wheel/install/Docker/audit gates and Dependabot are
  committed. PyPI publishing is gated by the repository variable `PYPI_PUBLISH=true` and
  trusted publishing configuration.
- **Security headers:** HSTS in proxy mode, `Cache-Control: no-store` outside `/static`,
  `Permissions-Policy`, COOP and CORP complement the existing CSP and frame/referrer rules.


## Session revocation and API throttling (2026-11-19)

- **Session epoch.** UI session cookies include an integer `ep` read from
  `$HERALDESK_HOME/session_epoch` (mode 600, atomic replacement). `read_session` rejects a
  cookie whose epoch differs. `POST /logout-all` and `heraldesk admin logout-all` increment
  the file, revoking every browser immediately without rotating the admin token. A missing
  file is epoch 0; an unreadable/invalid file fails closed for existing cookies. Restoring
  a backup rotates the epoch.
- **API throttling.** `/api/v1` Bearer failures use a separate limiter (10 per minute per
  client key). Blocked requests return 429 with `Retry-After`; the first block in a window
  writes one `auth.api_rate_limited` audit entry. Successful authentication resets the key.
  The limiter prunes expired keys above 1024 entries.
- **Proxy client address.** With `HERALDESK_TRUST_PROXY=1`, the rate-limit key is the
  rightmost `X-Forwarded-For` value, the hop appended by the documented single TLS proxy.
  Client-supplied prefixes are ignored. Deployments with multiple proxies must configure
  the outer proxy to overwrite the header.
- **Request size caps.** POST/PUT/PATCH bodies with `Content-Length` over 64 KiB on
  `/login`, or 1 MiB elsewhere, are rejected with 413 before parsing. Chunked bodies are
  not capped at the ASGI layer yet; the reverse proxy should enforce the same limit.
- **Audit scope.** Rejected Telegram webhook attempts are logged (not audited per request)
  to avoid turning a public endpoint into an audit-log write amplifier. API blocks are
  audited once per window/key.


## Backup and restore safety (2026-11-19)

- **Credential backups include `telegram.token`.** It is encrypted with `secret.key`, so
  restoring both makes Telegram notifications work again. `.env` (LLM keys and the
  explicit admin token) remains excluded and must be backed up separately.
- **`restore --force` takes a safety backup first.** The archive is written into
  `backups/` with credentials before any live file is touched. If the safety backup
  cannot be written, the restore aborts.
- **Replacement is staged.** Selected files are extracted and copied into a temporary
  directory inside the data folder, then swapped into place. A failure during copying
  leaves the live instance untouched; the safety backup covers the short replacement
  window.
- **Restore migrates and revokes.** The CLI runs `heraldesk init` after restoring a
  database, applying pending migrations, then reports the schema revision. It rotates the
  session epoch so old browser cookies cannot be reused.
- **Not included:** `.env`, logs, generated reports, previous backups, and the session
  epoch itself (restore creates a fresh epoch).


## Inbox retention (2026-11-19)

- **Manual, audited pruning.** `heraldesk inbox prune --days N` deletes `telegram_messages`
  with `status` in `handled|ignored` and `created_at` older than N days. Pending messages
  stay until triaged unless `--include-pending` is passed. `--dry-run` only counts.
- **No automatic job yet.** Retention is an explicit operator action; a scheduled job would
  need a config key and a background trigger. This keeps deletion predictable and avoids
  surprising data loss.
- **Audit:** one `inbox.pruned` entry with days, deleted count and the pending flag. No
  message text, chat id or sender id enters the payload.
- **Backups:** pruned rows are gone from future backups; existing archives are unaffected.


## Audit and cost visibility (2026-11-19)

- **Read-only views.** `GET /audit`, `GET /cost`, `/api/v1/audit` and `/api/v1/cost` are
  authenticated read models over `audit_log` and `agent_runs`. They never mutate state.
- **Redaction.** Audit payloads are JSON-serialized, passed through `SecretRegistry` and
  truncated to 500 characters before rendering. Profile slugs are resolved from the
  profile catalog; a missing profile renders as `null`.
- **Costs are exact database sums,** not estimates: today (UTC midnight), last 7 days and
  last 30 days, per profile and instance-wide. Token counts include input plus output in
  the UI summary.
- **Cross-profile reads are narrow.** `AuditCatalog.recent` and
  `GlobalCostRepository` are the documented diagnostic exceptions; every write and every
  content-bearing query still goes through `ProfileScope`.


## API contract and read models (2026-11-19)

- **OpenAPI is authenticated.** FastAPI's docs/redoc/OpenAPI routes stay disabled; the
  schema is returned by `GET /api/v1/openapi.json` after Bearer authentication. This keeps
  the route surface discoverable without making it public.
- **Profiles are metadata only.** `GET /api/v1/profiles` returns id, slug, name, type,
  timezone, languages, status and kill-switch state. It never returns `config_hash`,
  credentials or file contents.
- **Run traces exclude prompts and outputs.** `GET /api/v1/runs?profile=<slug>` returns
  agent, status, model, tokens, cost, timestamps, pipeline id and a redacted/truncated
  error. Inputs and outputs stay in the local database; they can contain draft content.
- **Scoped lookups.** Run reads resolve the slug through `ProfileCatalog`, then use
  `AgentRunRepository.list(ProfileScope(...))`.


## Secret-key rotation (2026-11-19)

- **Command:** `heraldesk admin rotate-key [--dry-run]`. It re-encrypts every
  `social_accounts.credentials_encrypted` blob and, when present, the encrypted
  `telegram.token`.
- **Persisted token only.** The rotation reads and decrypts `telegram.token` directly. It
  deliberately does not use `resolve_bot_token`, because that helper gives environment and
  `.env` values precedence; copying an override into the file would replace the persisted
  credential. Environment/`.env` tokens stay where they are and are not written to disk.
- **Mandatory backup:** a credential backup (including the old `secret.key`) is written
  before any change. If it cannot be written, rotation aborts.
- **Ordering:** decrypt everything, generate and stage the new key, update the database in
  one transaction, swap `secret.key` and the token file, then verify every stored blob
  with the new key.
- **Failure handling:** before the key swap, failures leave the old key and database
  untouched. If the token swap fails after the key swap, the old key file is restored from
  memory, the database is re-encrypted with the old key and the command aborts. A failure
  restoring the old key points the operator at the backup. Verification failure also
  points at the backup.
- **Session revocation is a prerequisite.** The epoch is rotated after the backup but
  before any credential changes. If it fails, the command exits non-zero with a clear
  message and the key/database remain untouched. On success, old browser sessions are
  invalid. If the admin token is secret.key-derived, the token changes;
  `heraldesk admin token --show` prints the new value. Explicit environment or `.env`
  tokens are unchanged.
- **Operational rule:** stop `heraldesk serve` before rotating. The CLI cannot detect an
  open server, and SQLite WAL locks differ by platform.


## Operations, editing and Threads lifecycle (2026-11-19)

- **File logs** live at `logs/heraldesk.log`, rotate at 5 MiB with three backups, mode 600,
  and pass through `SecretRegistry` before formatting. Setup failure never stops the
  server; uvicorn console logging remains. `reset_logging()` exists for tests.
- **Readiness** is authenticated at `GET /api/v1/ready`. It reports schema match, scheduler
  running/last run and Telegram mode/polling. A stale schema returns 503.
- **Calendar** shows scheduled and published items from 7 days ago to N days ahead (UTC),
  across profiles, with redacted previews.
- **Profile editor** stages a copy of the four text files, validates it with
  `load_profile`, atomically replaces each file, reloads the registration and audits
  `profile.edited`. Arbitrary paths are impossible: the slug is validated, the directory
  must be registered and filenames are fixed.
- **Examples:** `examples/acme` is a safe draft-only profile; `examples/plugin` is a
  minimal `heraldesk.platforms` package. Tests keep both valid.
- **Threads lifecycle:** `account refresh` calls `/refresh_access_token`, stores the new
  token and `expires_at` in the encrypted blob, and audits the result without the token.
  `doctor` warns when expiry is missing or within 7 days. Live acceptance still needs a
  tester account; CI stays mocked.


## API paging, retention and supply chain (2026-11-19)

- **Paging:** queue, audit and inbox accept `limit` + `offset` (bounded). Queue accepts a
  profile filter; audit accepts an exact action filter; calendar is window-based. Responses
  include `offset` and the returned `count`, not an expensive total count.
- **Accounts read model:** `GET /api/v1/accounts?profile=<slug>` returns id, platform,
  handle, label, status and last check time. It never decrypts or returns credentials.
- **Automatic retention:** `telegram.retention_days` (0 disables) schedules a daily 03:15
  UTC purge of handled/ignored inbox rows. It audits `inbox.pruned` with
  `source=scheduler`, keeps pending rows, and leaves the manual `inbox prune` command.
- **Supply chain:** all GitHub Actions are pinned to commit SHAs with version comments;
  release images enable Buildx provenance and SBOM generation. PyPI trusted publishing
  remains gated by `PYPI_PUBLISH=true`.


## Backup lifecycle and restore lock (2026-11-19)

- **Server lock:** `heraldesk serve` writes `server.lock` containing the pid and UTC start
  time, replacing a stale lock when the pid is dead, and removes it on shutdown. `restore`
  refuses while `server_is_running` returns a live pid. PID reuse is the documented edge
  case; remove a stale lock only after confirming no server is running.
- **Backup pruning:** `heraldesk backup-prune --keep N` removes the oldest
  `heraldesk-backup-*.zip` files by mtime and audits `backup.pruned`. `--dry-run` lists the
  candidates and changes nothing. Existing non-backup ZIPs are ignored.
- **Not implemented:** archive encryption, automatic pruning after each backup, and
  scheduled backups. Operators must protect credential archives themselves.


## PyPI trusted publishing readiness (2026-11-19)

- **OIDC only; no stored PyPI token.** The release workflow requests `id-token: write`,
  uses `pypa/gh-action-pypi-publish` with attestations, and gates publishing on repository
  variables (`TESTPYPI_PUBLISH`, `PYPI_PUBLISH`) plus GitHub environments (`testpypi`,
  `pypi`). The maintainer must create matching pending publishers on PyPI/TestPyPI.
- **TestPyPI first.** A separate job publishes to `https://test.pypi.org/legacy/` with the
  same action. TestPyPI does not host dependencies, so the verification command adds the
  real PyPI index.
- **Tag-only releases.** The version in `src/heraldesk/__init__.py`, the changelog entry and
  the `vX.Y.Z` tag must match. PyPI versions are immutable; rollback is yank + patch.
- **Pre-flight gates:** CI runs `twine check`, asserts `py.typed`, `_alembic` and static
  assets in the wheel, installs the wheel, and installs the sdist into clean virtual
  environments.
- **Artifacts:** wheel/sdist on PyPI and the GitHub release, GHCR image with SBOM and
  provenance, and the release notes.


## Web UI daily controls (2026-11-19)

- **Draft edit** on the queue detail page calls `domain.approvals.edit_item` with
  `channel="web"`, so the existing state rules, redaction and audit apply. The route only
  resolves the approval id to its scoped item.
- **Publish now** calls `web.service.publish_content` with `trigger="manual"`, preserving
  policy gates, the manual override audit, adapter routing, retries and notifications.
- **Global kill switch** on the dashboard engages/clears the existing marker and audits
  `killswitch.engaged|disengaged` with `source=web`. It does not cancel already scheduled
  items.
- **CSRF** is required on both actions; all error messages are redacted.


## Web publish actor and async bridge (2026-11-19)

- Web/API-triggered publishes use the audit actor `user`; `web` is not a valid audit actor.
- `publisher.publish_item` is synchronous but can be called from async FastAPI routes. When
  an event loop is already running, it runs the adapter coroutine in a short worker thread
  instead of calling `asyncio.run` on the running loop. No database session enters the
  worker.


## Docker-based action pinning exception (2026-11-19)

- GitHub Actions are normally pinned to commit SHAs. `pypa/gh-action-pypi-publish` is a
  Docker container action whose image tag is derived from the action ref; there is no
  image for an arbitrary commit SHA. It is therefore pinned to the released tag `v1.14.3` (newer releases bundle a twine that accepts
  Metadata-Version 2.5).
  A SHA pin failed with `ghcr.io/pypa/gh-action-pypi-publish:<sha>: not found`.


## Python 3.13/3.14 support (2026-11-19)

- **3.12, 3.13 and 3.14 are supported.** The full suite passes on all three and CI runs
  3.13 and 3.14 on Ubuntu.
- **3.14 has no coincurve wheel.** The dependency is conditional
  (`python_version < '3.14'`); 3.14 uses `platforms/bip340.py`, a tested pure-Python BIP-340
  implementation. 3.12/3.13 keep libsecp256k1 via coincurve.
- **Trade-off:** the pure-Python fallback is not constant-time. It keeps Nostr usable on
  3.14, but is not a general-purpose signing service. The preferred path remains coincurve
  where available.
- `doctor` accepts 3.12–3.14; the Docker image and default development interpreter remain
  3.12.


## Nostr key choice is guidance, not enforcement (2026-11-19)

The adapter accepts any valid nsec, including a user's original identity. Documentation
recommends a dedicated identity (or a throwaway key for the first test) because events are
immutable and the nsec is a full identity key, but Heraldesk does not reject an original
key. The UI/CLI never print the nsec; it is read from the environment, encrypted with
`secret.key`, and protected backups are the operator's responsibility.


## Threads credential environment names (2026-11-19)

`--credential-from-env` maps `THREADS_TOKEN`/`THREADS_ACCESS_TOKEN` to `access_token` and
`THREADS_USER_ID`/`USER_ID` to `user_id`. Other names still lowercase verbatim; use
`--credential KEY=VALUE` only when a value is not secret, or prefer the environment form.

## Telegram real test send is explicit (2026-11-20)

`heraldesk telegram send` stays offline by default: it uses the fake adapter, prints a
redacted preview and needs no token. A command named "send" that silently makes a network
call was the wrong default, but changing it in place would also surprise existing scripts.
`--real` is the explicit opt-in. It requires `telegram.enabled: true`, a stored token and a
target chat id in `telegram.allowed_chat_ids`, so the CLI cannot be used to message an
arbitrary chat outside the configured allowlist. `telegram test` remains offline.

## Provider parameter compatibility and status errors (2026-11-20)

LiteLLM runs with `drop_params = True`. Some reasoning models accept only their default
temperature, and Heraldesk sends `temperature=0.0` for deterministic runs; without the flag
LiteLLM rejects the request before it reaches the provider. Dropping unsupported parameters
lets the provider use its documented default. Provider failures now map the HTTP status to
a fixed, secret-free hint (401/402/403/404/429/5xx) instead of exposing only the exception
class. The raw provider message is not echoed, because it can contain account identifiers
and request details.

## Product direction: all-in-one first, headless later (2026-11-20)

The primary product is the all-in-one, single-user Heraldesk application: one install, one
process, a Web UI, an approval queue and a deterministic publishing pipeline. A headless
MCP surface for external agents such as Hermes is a later power-user channel, not the
default product. It would turn the front end, conversation and model bill into someone
else's product, and it contradicts the one-command install and one-process principles.

The next product goal is a native Draft Room: a conversational editor-in-chief inside the
Web UI that can propose ideas, drafts and profile patches. It must not approve or publish;
those remain explicit human actions through the existing state machine. The domain layer
stays API-first so an MCP adapter can reuse it later without being the primary interface.

## Copy editing is a suggestion, not a pipeline gate (2026-11-20)

A mandatory KBBI-style proofreader would flatten casual brand voice: `nggak` becomes
`tidak`, `banget` becomes `sangat`, and fragments become corporate sentences. That is a
style change, not a correctness fix. Future copy editing is therefore opt-in
(`copyedit: off|suggest`), suggestion-only, diff-based, and subordinate to the profile
voice files. Deterministic typo checks come first; an optional LLM pass handles ambiguous
cases and must preserve slang, register, code-switching and casing. Nothing auto-applies,
and the step never blocks approval. See `docs/copyedit.md`.

## Web UI keyboard shortcuts never write directly (2026-11-20)

Queue list/detail keyboard shortcuts are an accessibility and speed aid, not a new write
path. `j`/`k` move selection, and `a`/`e`/`r` only move focus onto the Approve/Edit/Reject
controls — a visible confirmation (Enter or a click) is always required. Shortcuts are
disabled while focus is inside an input, textarea or select, and the list is toggled with
`?`. This keeps every write an explicit form/action with CSRF, unchanged from the
JavaScript-less behavior. Recorded in `docs/web-ui.md`.

## No hx-boost: navigation is full-page (2026-11-21)

The Web UI deliberately does not enable `hx-boost` on `<body>`. Navigation is ordinary
full-page loads; htmx is used only for targeted fragments (row selection, panels, badges,
toasts) via `hx-get`/`hx-post` with explicit `hx-target`/`hx-swap`. This keeps the mobile
"More" `<details>` menu, the selected `?profile=` query parameter and focus state simple
and predictable, and avoids a class of stale-URL and history bugs. Recorded in
`docs/web-ui.md`.


## Redaction on write stays in the domain; the Web UI rejects (2026-11-21)

`domain.approvals.edit_item` continues to redact on write as defense in depth: drafts can
be published, so storing secret-like text raw would be a regression. The Web UI therefore
rejects an edit whose submitted body differs from its redacted form, or that contains the
`[REDACTED]` marker, before any write or audit entry. The operator removes the secret-like
content instead of silently publishing a masked body. Surrounding whitespace is still
trimmed by the web layer. The alternative (storing raw) was rejected.


## Profile policy helper edits text, not YAML (2026-11-21)

The `policy.yaml` helper form updates `default_mode`, `max_posts_per_day` and `quiet_hours`
with a conservative line-level text edit. PyYAML round-tripping would silently drop comments
and reorder keys, so it is not used for writes. The helper refuses unsupported shapes (for
example an unexpected `quiet_hours` structure or a partially filled quiet window) and tells
the user to edit the YAML directly. Applying the helper only changes the editor textarea;
nothing is written until the normal review and save steps.
