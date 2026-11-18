# Roadmap

Each milestone is done only when its acceptance criteria pass and tests exist. Details in `docs/architecture.md` section 13.

- [ ] M0 Foundation and packaging (`init`, `doctor`, SQLite, CI)
- [x] M1 Core domain (profiles from files, accounts, audit log, kill switch, ProfileScope)
- [x] M2 LLM layer and agent runtime (fake provider, cost tracking)
- [x] M3 `daily_content` pipeline with fake publisher
- [x] M4 Approval flow and autonomy (Telegram deferred)
- [x] M5 Scheduler and publisher (fake adapter; real Threads/Nostr adapters deferred with a documented `NotSupported` reason)
- [x] M6 Telegram notifications and inbox basics (Community manager and analyst deferred; see `docs/decisions.md`)
- [x] M7 Server and background jobs (FastAPI, scheduler sweep, Telegram polling; web UI and `/api/v1` deferred)
- [x] M8 Community and analyst CLI (manual inbox triage, deterministic reports; plugin loader and extra adapters deferred)
- [x] M9 v1.0.0 hardening and release docs (backup/restore, doctor checks, changelog, README; Docker/PyPI/plugin publishing deferred)
- [x] M10 Real Nostr publishing (NIP-01 kind 1, throwaway key, CLI only; other real adapters, web UI and new LLM features deferred)
- [x] M11 Local Web UI and REST API (Jinja2 + HTMX, single-admin auth, loopback-first; no multi-user or remote deployment automation)

Out of scope: multi-user/multi-tenant, billing, hosted cloud, native mobile app, video generation,
auto-DM outreach, scraping without official APIs.
