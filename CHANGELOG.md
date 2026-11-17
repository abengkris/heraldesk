# Changelog

All notable changes to Heraldesk are recorded here. The `1.0.0` release folded the
M0-M9 implementation milestones together; `1.1.0` adds the first real platform adapter.

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

### Deferred

- Local web UI, REST `/api/v1`, admin authentication and remote deployment examples.
- Real Threads, Nostr, X, Facebook, Instagram and LinkedIn adapters.
- Plugin entry-point loading and third-party adapter templates.
- Docker image and PyPI publishing.
