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

Out of scope: multi-user/multi-tenant, billing, hosted cloud, native mobile app, video
generation, auto-DM outreach, scraping without official APIs.

Planned next: real X/Facebook/Instagram/LinkedIn adapters (or plugins), content calendar,
audit and cost pages, inbox retention, key rotation, API pagination and OpenAPI.
