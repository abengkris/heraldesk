# Open questions

Agents and contributors: when the spec is ambiguous or conflicts with real API behavior, add the question here
instead of guessing. Format: `- [ ] (date, milestone) question`. The maintainer answers and moves it to decisions.md.

## Blocking M0

- [x] (2026-11-16, M0) **License: MIT or Apache-2.0?**
  **RESOLVED 2026-11-16: MIT.** MIT chosen for simplicity and permissiveness. `LICENSE` stays as is (Copyright
  (c) 2026 Abeng), no switch to Apache-2.0. Recorded in `docs/decisions.md`.

- [x] (2026-11-16, M0) **Which document is the single source of truth, and how do we reconcile the three
  divergent copies of the design principles?**
  **RESOLVED 2026-11-16.** Precedence: `docs/architecture.md` (EN) is design truth, then `AGENTS.md` as operational
  rules for agents (it must not contradict architecture; architecture wins and `AGENTS.md` gets fixed), and
  `docs/id/arsitektur.md` is a translation that never wins. The canonical principles are the 8 from architecture
  section 1, with the auto-post rule folded into principle 4. `AGENTS.md` must contain exactly those 8 plus section
  15, no reordering, no extra items. `AGENTS.md` currently has 10 items and is fixed in M0. Recorded in
  `docs/decisions.md`.

- [x] (2026-11-16, M0) **Where do secrets live, and must `heraldesk init` create `secret.key` immediately?**
  **RESOLVED 2026-11-16.** LLM keys are plaintext in `.env` via pydantic-settings, never in `heraldesk.yaml`, never
  in git. Social account credentials are Fernet-encrypted with `secret.key` (mode 600). `init` MUST create
  `secret.key` even with no accounts. `restore` of a backup without `secret.key` MUST fail with: "Backup has no
  secret.key, cannot decrypt social credentials. Restore profiles and config only with --without-credentials, or
  provide secret.key". No silent skip, no replacement key. Recorded in `docs/decisions.md`.

- [x] (2026-11-16, M0) **May `heraldesk doctor` make network calls by default, and does M0 CI include Windows?**
  **RESOLVED 2026-11-16.** `doctor` is offline by default with no network calls; only `doctor --online` may check the
  LLM provider, platforms and Telegram. CI: Linux and macOS run the full suite, Windows runs core tests only (paths,
  config, content transitions, lint). Recorded in `docs/decisions.md`.

- [x] (2026-11-16, M0) **How much of the §4 data model must exist in the M0 Alembic baseline?**
  **RESOLVED 2026-11-16.** The M0 baseline is empty (one initial migration, no tables). Tables are added per
  milestone: M1 `profiles`, `social_accounts`, `audit_log`, business `jobs`; M3 `content_ideas`, `content_items`;
  M4 `approvals`; and so on. The M0 command surface is exactly `init`, `doctor`, `serve` (stub that prints "not
  implemented yet" and exits 0), `--help`, `--version`. Recorded in `docs/decisions.md`.

No open questions remain for M0. Later ambiguities are added here when their milestone starts.
