# Roadmap

Each milestone is done only when its acceptance criteria pass and tests exist. The
implementation record lives in `CHANGELOG.md` and `docs/decisions.md`; this file is the
current summary.

- [x] M0 Foundation and packaging (`init`, `doctor`, SQLite, CI)
- [x] M1 Core domain (profiles from files, accounts, audit log, kill switch, ProfileScope)
- [x] M2 LLM layer and agent runtime (fake provider, cost tracking)
- [x] M3 `daily_content` pipeline with fake publisher
- [x] M4 Approval flow and autonomy (CLI; Telegram approvals arrived in M6)
- [x] M5 Scheduler and publisher (fake adapter, retries, at-most-once)
- [x] M6 Telegram notifications and inbox basics
- [x] M7 Server and background jobs (FastAPI, scheduler sweep, Telegram polling)
- [x] M8 Community and analyst CLI (manual inbox triage, deterministic reports)
- [x] M9 v1.0.0 hardening and release docs (backup/restore, doctor, changelog, README)
- [x] M10 Real Nostr publishing (NIP-01 kind 1, dedicated/throwaway key recommended)
- [x] M11 Local Web UI and REST API (Jinja2 + HTMX, single-admin token, loopback-first)
- [x] M12 Third-party adapter plugins (`heraldesk.platforms` entry points)
- [x] M13 Production distribution and hardening (Threads text adapter, `upgrade` with a
  pre-migration backup, Docker/Compose/systemd/Caddy, release workflow, webhook secret,
  stale-schema guard, full-draft approval views, CI wheel/audit/image gates)
- [x] M14 Session revocation and API throttling (session epoch + logout-all, API Bearer
  limiter, trusted-proxy client key, request-size caps, admin-token doctor warning)
- [x] M15 Backup/restore safety (telegram.token under credentials, pre-restore safety
  backup, staged replacement, post-restore migrations and session-epoch rotation)
- [x] M16 Inbox retention (manual `inbox prune`, dry-run, pending opt-in, audit)
- [x] M17 Audit and cost visibility (read-only UI pages and `/api/v1/audit|cost`)
- [x] M18 API contract and read models (authenticated OpenAPI, profiles and run traces)
- [x] M19 Secret-key rotation (mandatory backup, staged re-encryption, verification,
  rollback on swap failure, session revocation)
- [x] M20 Operations and editing (rotating redacted logs, `/api/v1/ready`, content
  calendar, validated profile/policy editor, examples and plugin template)
- [x] M21 Threads lifecycle (token refresh command, recorded expiry, doctor warning)
- [x] M22 API pagination/filters and account read model
- [x] M23 Automatic inbox retention and supply-chain pinning (SHA actions, SBOM/provenance)
- [x] M24 Backup lifecycle (pruning with audit, dry-run, server-lock restore guard)
- [x] M25 PyPI publishing (PyPI + TestPyPI trusted publishing live, twine checks, sdist
  install smoke, release runbook)
- [x] M26 Web UI daily controls (draft edit, publish now, global kill switch)
- [x] M27 Python 3.13 support (full suite and CI)
- [x] M28 Python 3.14 support (coincurve conditional; pure-Python BIP-340 fallback,
  3.14 CI job)
- [x] M29 Draft Room backend (DR1: threads and messages, scoped service, Editor agent,
  bounded context, cost and message limits, read-only route; disabled by default)
- [x] M30 Draft Room conversation UI (DR2: thread list, thread page, Editor messages,
  archive, CSRF and inline errors; still no draft or profile write actions)
- [x] M31 Draft Room draft creation (DR3: Writer and Compliance roles, draft and
  compliance artifacts, "Create draft" with lint + Compliance into pending_approval)
- [x] M32 Draft Room profile patches (DR4: Profile role, patch artifact with diff and
  base-hash check, atomic apply with validation, reload and audit)
- [x] M33 Draft Room hardening (DR5: edge-case and idempotency tests, accessibility and
  docs alignment)

## Infrastructure status

M0–M33 are complete. The core is feature-complete for a single-user, self-hosted
deployment: CLI, Web UI, REST API, scheduler, Telegram, Nostr and Threads publishing,
backups and restore, key rotation, PyPI and container releases, and Python 3.12–3.14.

## Future goals

These are product goals, not committed milestones. Each starts only after its own design
and acceptance criteria are written.

- [x] **All-in-one Heraldesk.** The native Draft Room is complete (DR1–DR5): planning,
  Writer drafts, Compliance review and profile patches, all disabled by default and with
  no approve or publish path. See [`docs/draft-room.md`](docs/draft-room.md).
- [ ] **Real X/Facebook/Instagram/LinkedIn adapters**, or first-party plugins, when
  official API access and testable behavior are available.
- [ ] **Optional headless mode** (`heraldesk mcp`) for power users who bring their own
  agent. Not the default product and not a replacement for the Web UI.
- [ ] **Voice-aware copy-edit suggestions.** Off by default; deterministic typo checks
  first, then an optional LLM diff that preserves slang, register and code-switching.
  Never auto-applies and never blocks approval. See
  [`docs/copyedit.md`](docs/copyedit.md).

Out of scope: multi-user/multi-tenant, billing, hosted cloud, native mobile app, video
generation, auto-DM outreach, scraping without official APIs.
