# Changelog

All notable changes to Heraldesk are recorded here. The `1.0.0` release folded the
M0-M9 implementation milestones together; `1.1.0` adds the first real platform adapter;
`1.2.0` adds the local Web UI + REST API; `1.2.1` enforces quiet hours and daily post caps;
`1.3.0` adds third-party adapter plugins; `1.4.0` adds online doctor probes; `1.5.0` adds
the Threads adapter and production distribution/hardening.

## [1.16.0] - 2026-11-19

### Added

- Queue detail edit form using the audited approval service.
- "Publish now" from the Web UI for approved/scheduled/failed items.
- Global kill-switch control on the dashboard with audit coverage.

### Docs

- README, architecture, decisions and roadmap describe the new daily controls.

## [1.15.0] - 2026-11-19

### Added

- TestPyPI trusted-publishing job alongside the PyPI job, both gated by repository
  variables and GitHub environments.
- CI pre-flight packaging gates: `twine check`, wheel content assertions (`py.typed`,
  Alembic migrations, HTMX asset), wheel install and sdist install smoke tests.
- `docs/releasing.md` runbook for TestPyPI/PyPI trusted publishing, verification,
  troubleshooting and rollback.

### Security

- PyPI publication uses OIDC trusted publishing with attestations; no PyPI token is stored.

## [1.14.0] - 2026-11-19

### Added

- `heraldesk backup-prune --keep N [--dry-run]` deletes old backup archives and audits the
  count.
- A single-process `server.lock` (pid + start time) prevents `restore` while `heraldesk
  serve` is running; stale locks are replaced automatically.

### Docs

- README, AGENTS, SECURITY, architecture, decisions and roadmap document pruning,
  stop-before-restore and the lock edge cases.

## [1.13.0] - 2026-11-19

### Added

- `limit`/`offset` pagination for queue, audit and inbox; queue profile filtering and
  audit action filtering.
- `GET /api/v1/accounts?profile=<slug>` account metadata without credentials.
- `telegram.retention_days` schedules a daily 03:15 UTC purge of handled/ignored inbox
  rows, with `source=scheduler` audit entries.

### Security

- GitHub Actions are pinned to commit SHAs, and release container images now build with
  SBOM and provenance attestations.

### Docs

- README, architecture, decisions, security policy and roadmap describe the new paging,
  retention schedule and supply-chain settings.

## [1.12.0] - 2026-11-19

### Added

- Rotating, secret-redacted file logs at `$HERALDESK_HOME/logs/heraldesk.log` and an
  authenticated readiness endpoint (`/api/v1/ready`).
- Content calendar (`/calendar`, `/api/v1/calendar`) and a validated Web UI editor for
  `soul.md`, `brief.md`, `policy.yaml` and `banned.yaml`.
- Runnable `examples/acme` profile and `examples/plugin` adapter template.
- `heraldesk account refresh --platform threads`, encrypted token expiry storage and a
  `doctor` warning when expiry is missing or within 7 days.

### Docs

- README, architecture, decisions, roadmap and Threads docs cover the new operations,
  editing, examples and refresh workflow.

## [1.11.1] - 2026-11-19

### Security

- Key rotation now decrypts the persisted `telegram.token` directly. Environment and
  `.env` overrides can no longer be copied into the persisted credential during rotation.
  Missing or corrupt token files abort before any key or database change.
- Session revocation is now a hard prerequisite. A failure exits non-zero with a clear
  message and aborts before credentials are touched; it can no longer be silently
  suppressed.
- Added structured stage logging (counts and booleans only; no secrets).

### Tests

- Environment and `.env` overrides do not replace the persisted token.
- A corrupt token file aborts with the old key intact.
- Rotation does not create a token file from the environment.
- Session-revocation failure exits non-zero and leaves credentials unchanged.

## [1.11.0] - 2026-11-19

### Added

- `heraldesk admin rotate-key [--dry-run]` re-encrypts social accounts and the stored
  Telegram token with a new `secret.key`, after a mandatory credential backup. It stages,
  swaps, verifies, rolls back the database on key-swap failure and revokes UI sessions.

### Docs

- SECURITY, README, architecture, decisions and roadmap document the rotation procedure,
  the backup requirement and the stop-the-server rule.

## [1.10.0] - 2026-11-19

### Added

- Authenticated `GET /api/v1/openapi.json` for the machine-readable API contract.
- `GET /api/v1/profiles` for profile metadata.
- `GET /api/v1/runs?profile=<slug>` for recent agent-run traces with redacted errors and
  no prompt/output content.

### Docs

- README, architecture, decisions and roadmap describe the expanded API and its privacy
  boundaries.

## [1.9.1] - 2026-11-19

### Changed

- Updated CI and release actions: `setup-uv` v7, `upload-artifact` v7,
  `download-artifact` v8, `docker/metadata-action` v6 and `softprops/action-gh-release`
  v3. Dependabot's Python 3.15 base-image bump was rejected because the package pins
  `requires-python` to 3.12.

## [1.9.0] - 2026-11-19

### Added

- Read-only audit log page (`/audit`) and `GET /api/v1/audit`, with redacted and truncated
  payloads across profiles.
- LLM cost page (`/cost`) and `GET /api/v1/cost` with today, 7-day and 30-day rollups per
  profile and instance-wide.

### Docs

- README and architecture list the new audit/cost surfaces and the cross-profile read
  exceptions.

## [1.8.0] - 2026-11-19

### Added

- `heraldesk inbox prune --days N [--dry-run] [--include-pending]` deletes old
  handled/ignored inbox messages and audits the count. Pending messages are preserved by
  default.

### Docs

- README, AGENTS, security policy, architecture and decisions now describe the manual
  retention workflow and its limits.

## [1.7.1] - 2026-11-19

### Fixed

- The pre-restore safety-backup test now restores into a separate target instance instead
  of holding the source SQLite WAL open. This fixes the Windows CI failure; runtime code
  is unchanged.

## [1.7.0] - 2026-11-19

### Added

- Credential backups now include the encrypted `telegram.token`.
- `restore --force` writes a pre-restore safety backup, stages files before replacing the
  live instance, applies pending migrations, rotates the session epoch and reports the
  schema revision.

### Docs

- Backup/restore scope, `.env` exclusion and the safety workflow are documented in the
  README, architecture and decisions log.

## [1.6.0] - 2026-11-19

### Security

- Added a separate `/api/v1` Bearer-failure limiter (10/min per client) with `429`,
  `Retry-After` and one audit entry per block window.
- Added an instance session epoch: `POST /logout-all` and `heraldesk admin logout-all`
  revoke every UI session immediately. Restoration rotates the epoch.
- Trust-proxy rate-limit keys now use the proxy-appended (rightmost) `X-Forwarded-For`
  hop instead of a spoofable client-supplied prefix.
- Added request-size caps (64 KiB login, 1 MiB other mutations) and the remaining
  hardening headers already covered by CSP/HSTS.
- Short explicit admin tokens now warn in `doctor`; remote mode already refuses them.

## [1.5.1] - 2026-11-19

### Fixed

- The CI wheel-install smoke job now creates its virtual environment with the uv-managed
  Python 3.12 instead of the runner's default Python 3.14. No runtime code changed.

### Docs

- Finished the post-1.5 documentation reconciliation: API scope, `/health` readiness,
  Telegram sender allowlist and the historical 1.0 deferred list.

## [1.5.0] - 2026-11-19

### Added

- Official Threads text adapter (create container, wait, publish, permalink) with `/me`
  credential validation, a local 500-UTF-8-byte guard, token-free errors and
  `docs/platforms/threads.md`.
- `heraldesk upgrade` creates a credential backup, applies pending migrations and audits
  the change. `heraldesk serve` refuses a stale schema and `/health` returns 503 with the
  expected revision.
- `heraldesk plugins list [--check]` and account creation for installed plugin platforms.
- `heraldesk queue show <id>`, a Web UI queue detail page and `GET /api/v1/queue/{id}`
  expose the full redacted draft before approval. `heraldesk publish` prints a permalink
  when the adapter returns one.
- Dockerfile, `.dockerignore`, `docker-compose.yml`, systemd unit, Caddyfile and a VPS
  runbook in `deploy/`.
- Tag-triggered release workflow (wheel/sdist, optional PyPI trusted publishing, GHCR
  image, GitHub release), CI wheel-install smoke, Docker build and `pip-audit` jobs, and
  Dependabot.
- `py.typed`, Beta package classifier, dependency upper bounds, repository/changelog URLs.

### Changed

- Telegram webhook mode and remote Telegram deployments now require
  `HERALDESK_TELEGRAM_WEBHOOK_SECRET`; an unsigned webhook cannot forge approvals.
  Optional `telegram.allowed_user_ids` restricts command senders.
- Remote binds reject explicit admin tokens shorter than 32 characters.
- Manual inbox replies obey the global/profile kill switches and profile status.
- HSTS (in proxy mode), `Cache-Control: no-store`, `Permissions-Policy`, COOP and CORP
  headers are set.

### Docs

- Reconciled the architecture, README, roadmap, security policy and platform docs with the
  shipped 1.5 implementation; historical decisions are marked superseded.

## [1.4.0] - 2026-11-19

### Added

- `heraldesk doctor --online` now probes the configured LLM provider with one minimal
  completion, the Telegram Bot API with `getMe` when Telegram is enabled, and one Nostr
  relay only when a Nostr account exists. An unreachable relay is a WARN.
- Added `BotApiTelegramAdapter.get_me()`.

## [1.3.0] - 2026-11-19

### Added

- Third-party platform adapters through the `heraldesk.platforms` entry-point group.
  Discovery is lazy and cached; built-ins win, account checks stay offline, and a broken
  plugin raises `PluginError` without stopping other platforms.
- Plugin documentation and tests for discovery, caching, platform matching, built-in
  precedence and failure modes.

### Tests

- Reuse one Alembic-migrated data folder per test session and copy it per test. The full
  suite drops from roughly 12 minutes to about 3 minutes; `test_alembic.py` and
  `test_init.py` still exercise the real migration and bootstrap paths.

## [1.2.1] - 2026-11-19

### Fixed

- Enforced `quiet_hours` and `max_posts_per_day` for automatic publishing in the profile
  timezone. Previously both fields were parsed and documented but never applied.
- Deferred `auto`, `auto_publish` and `auto_after_delay` schedules that would land inside
  quiet hours to the end of the window.
- `heraldesk scheduler run --dry-run` now reports `quiet_hours`, `daily_post_cap` and
  `policy_unavailable` skips just like a real sweep.
- Automatic publishing fails closed when `policy.yaml` is missing or malformed; a manual
  publish still works and records `publish.policy_override`.

## [1.2.0] - 2026-11-18

### M11 - Local Web UI and REST API

- Added a Jinja2 + HTMX Web UI with dashboard, approval queue, inbox triage, reports and
  profiles. HTMX and CSS are vendored; there is no Node build.
- Added the authenticated `/api/v1` JSON API for status, queue, approvals, inbox, reports
  and manual publishing. Web/API actions reuse the existing domain services and audit log.
- Added single-admin authentication: `HERALDESK_ADMIN_TOKEN` or a secret.key-derived
  token, signed HttpOnly session cookies, CSRF on every UI POST, login rate limiting and
  Bearer-only API access.
- Added loopback-by-default remote hardening: `--allow-remote` requires an explicit token
  and `HERALDESK_TRUST_PROXY=1` behind HTTPS; Secure cookies and query-token rejection.
- Added `heraldesk admin token [--show]`, `--no-web`, and Markdown report sanitization.
- New dependencies: `jinja2`, `python-multipart`, `markdown`, `bleach`.

## [1.1.0] - 2026-11-17

### M10 - Real Nostr publishing

- Added the NIP-01 kind 1 Nostr adapter with BIP-340 signing, configurable relays and
  per-relay timeouts. One accepting relay is enough; failures reuse the publisher retry
  and at-most-once rules.
- Added `--credential-from-env`, `--label` and `--relay` to `heraldesk account add`, plus
  `heraldesk account list` with a masked npub.
- Added the `nostr` section to `heraldesk.yaml`, the optional `social_accounts.label`
  column (migration `0010`), nsec/npub redaction and `AccountRef` credential hiding.
- Added `coincurve` (BIP-340 Schnorr signing) and `websockets` (relay transport) as
  runtime dependencies, with `uv.lock` updated.
- Documented the throwaway-key rule: Nostr events are immutable and cannot be deleted.

## [1.0.0] - 2026-11-17

### M0 - Foundation and packaging

- Added the `uv`/hatchling Python 3.12 project layout and the `heraldesk`/`hdesk`
  console scripts.
- Added `heraldesk init` with the data folder, Fernet `secret.key`, documented
  `heraldesk.yaml`, SQLite WAL database and Alembic baseline.
- Added the offline `heraldesk doctor`, loopback-only server configuration and the
  Linux/macOS/Windows CI matrix.

### M1 - Core domain

- Added profile files and YAML front matter validation, `profile new/list/reload` and
  profile status controls.
- Added encrypted social accounts, the append-only audit log, global/per-profile kill
  switches and `ProfileScope` repository isolation.

### M2 - LLM layer and agent runtime

- Added the LiteLLM wrapper, structured outputs, validation retries and `agent_runs`
  traces with token and USD cost accounting.
- Added the fake provider and in-memory/file fixtures, prompt overrides, secret redaction
  and the global daily cost cap.
- Added `heraldesk agent run` and `heraldesk runs`.

### M3 - Daily content pipeline

- Added `pipeline_runs`, `content_ideas` and `content_items` with the content state
  machine.
- Added the deterministic Researcher, Writer, Designer placeholder and Compliance steps,
  linting, deduplication, approval queueing and `heraldesk run daily --dry-run`.

### M4 - Approvals and autonomy

- Added the `approvals` table, one-open-approval guarantee and 72-hour expiry.
- Added `draft_only`, `require_approval`, `auto_publish`, `auto` and `auto_after_delay`
  policy handling, human edit paths and `heraldesk queue` commands.

### M5 - Scheduler and publisher

- Added durable publisher metadata, due-item selection, retries with backoff and
  at-most-once external ids.
- Added `heraldesk publish` and the stateless `heraldesk scheduler run` sweep, with
  credential/body redaction and the fake adapter as the supported publishing path.

### M6 - Telegram notifications and inbox basics

- Added `telegram_messages` and the idempotent `telegram_notifications` ledger.
- Added encrypted bot-token storage, allowlisted chats, approval/publish/failure/kill-switch
  notifications and inbound `/approve`, `/reject`, `/queue`, `/help` commands.
- Added `heraldesk telegram test/send/inbox/token`.

### M7 - Single-process server and background jobs

- Added `heraldesk serve` with FastAPI `/health`, `/status` and `/webhook/telegram`.
- Added the APScheduler publish sweep and Telegram polling, graceful shutdown audit and
  Telegram update-id deduplication.

### M8 - Manual inbox triage and deterministic reports

- Added triage status/reason fields to the existing Telegram inbox without creating a new
  social API: `heraldesk inbox list/show/reply/ignore`.
- Added instance-wide `heraldesk report weekly|monthly`, computed only from the database
  with no LLM calls, including a created-at cohort and idempotent Markdown output.
- Added audit actions for inbox actions and report generation.

### M9 - Hardening and 1.0.0 release

- Added `heraldesk backup` and `heraldesk restore` with SQLite online snapshots, safe ZIP
  validation, credential opt-in and the M0 files-only restore rule.
- Hardened `heraldesk doctor` with migration-head, Telegram-token and reports-directory
  checks.
- Added regression coverage for kill switches, missing-instance errors, secret-free output
  and tracked-file hygiene.
- Rewrote the English README and bumped the package version to `1.0.0`.

### Deferred (as of 1.0.0)

- Local web UI, REST `/api/v1`, admin authentication and remote deployment examples.
- Real Threads, Nostr, X, Facebook, Instagram and LinkedIn adapters.
- Plugin entry-point loading and third-party adapter templates.
- Docker image and PyPI publishing.

Later delivered in 1.1–1.5: Nostr, Threads, the Web UI/API, remote mode, adapter plugins,
Docker/Compose/deploy assets and the tag-triggered PyPI/GHCR workflow. X, Facebook,
Instagram and LinkedIn remain plugin/future work.
