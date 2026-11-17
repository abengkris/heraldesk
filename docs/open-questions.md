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

## Resolved during M3

- [x] (2026-11-16, M3) **Does `--dry-run` persist drafts and run traces?**
  **RESOLVED 2026-11-16: no.** `--dry-run` is a pure simulation. It creates no pipeline run,
  idea, content item, agent run or audit entry, and only prints the would-be drafts. The
  persistence acceptance test uses `heraldesk run daily` without the flag. Recorded in
  `docs/decisions.md`.
- [x] (2026-11-16, M3) **Where are lint rules configured per profile?**
  **RESOLVED 2026-11-16:** in `banned.yaml`. `banned_buzzwords: null` uses the built-in list,
  `banned_buzzwords: []` disables the rule, `banned_words` always applies, and
  `allow_em_dash` defaults to false. No new profile file is introduced.
- [x] (2026-11-16, M3) **Does M3 create `pipeline_runs` even though the scope names only the content tables?**
  **RESOLVED 2026-11-16: yes.** The M2 decision says the table arrives with the first
  pipeline, and the M3 acceptance criterion requires all runs to be recorded. `agent_runs`
  gains its foreign key in migration `0004`.
- [x] (2026-11-16, M3) **May the fake platform fallback create a `social_accounts` row?**
  **RESOLVED 2026-11-16: no.** The fallback exists only in memory so `profile new` plus
  `run daily` works offline; accounts are never auto-created.

## Resolved during M4

- [x] (2026-11-16, M4) **Is `auto_publish` a new policy key or a mode value?**
  **RESOLVED 2026-11-16: a mode value.** `default_mode: auto_publish` and
  `{ action: post, risk: low, mode: auto_publish }` are valid. There is no second top-level
  `mode` key. `auto` and `auto_after_delay` remain parseable but are treated as
  `require_approval` until M5. Recorded in `docs/decisions.md`.
- [x] (2026-11-16, M4) **Does `queue approve|reject|edit <id>` take a content item id or an approval id?**
  **RESOLVED 2026-11-16: the content item id shown by `queue list`.** A narrow
  `ContentItemLocator` resolves the owning profile from the globally unique item id; all
  content reads and writes remain scoped. Recorded in `docs/decisions.md`.
- [x] (2026-11-16, M4) **May a `draft` profile approve content?**
  **RESOLVED 2026-11-16: yes.** Approval is a human internal decision, not publishing.
  Global kill switch, per-profile kill switch and `paused` still refuse. Publishing remains
  gated by M5. Recorded in `docs/decisions.md`.
- [x] (2026-11-16, M4) **Which states accept a human edit?**
  **RESOLVED 2026-11-16:** only `blocked`, `rejected`, `needs_review`, `approved` and
  `pending_approval`. `drafted` and `reviewed` are transient pipeline states, not
  user-holdable states, so edits on them are illegal in M4. `scheduled`, `published`,
  `cancelled` and `failed` are also illegal. Recorded in `docs/decisions.md`.
- [x] (2026-11-16, M4) **How is the 72-hour approval expiry triggered without a scheduler?**
  **RESOLVED 2026-11-16: opportunistically by `heraldesk queue list`.** Approve/reject/edit
  refuse a stale approval and leave the database untouched; the next `queue list` cancels
  the item and marks the approval expired. The M5 scheduler will add a periodic sweep.
  Recorded in `docs/decisions.md`.
