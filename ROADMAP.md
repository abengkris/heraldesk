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
- [x] M10 Real Nostr publishing (NIP-01 kind 1, throwaway key)
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
- [x] M25 PyPI readiness (TestPyPI/PyPI trusted-publishing workflow, twine checks, sdist
  install smoke, release runbook)

Out of scope: multi-user/multi-tenant, billing, hosted cloud, native mobile app, video
generation, auto-DM outreach, scraping without official APIs.

Planned next: real X/Facebook/Instagram/LinkedIn adapters (or plugins), content calendar,
audit and cost pages, inbox retention, key rotation, API pagination and OpenAPI.
