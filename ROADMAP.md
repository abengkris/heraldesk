# Roadmap

Each milestone is done only when its acceptance criteria pass and tests exist. Details in `docs/architecture.md` section 13.

- [ ] M0 Foundation and packaging (`init`, `doctor`, SQLite, CI)
- [x] M1 Core domain (profiles from files, accounts, audit log, kill switch, ProfileScope)
- [x] M2 LLM layer and agent runtime (fake provider, cost tracking)
- [x] M3 `daily_content` pipeline with fake publisher
- [x] M4 Approval flow and autonomy (Telegram deferred)
- [x] M5 Scheduler and publisher (fake adapter; real Threads/Nostr adapters deferred with a documented `NotSupported` reason)
- [x] M6 Telegram notifications and inbox basics (Community manager and analyst deferred; see `docs/decisions.md`)
- [ ] M7 Local web UI
- [ ] M8 Plugins and more adapters
- [ ] M9 v0.1.0 release (PyPI, Docker, docs)

Out of scope: multi-user/multi-tenant, billing, hosted cloud, native mobile app, video generation,
auto-DM outreach, scraping without official APIs.
