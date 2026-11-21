# Open questions

> This file is a chronological record of milestone questions. Resolved entries describe the
> behavior at that milestone; the current behavior is defined by `docs/architecture.md`, the
> README and the code. Entries marked superseded no longer describe the release.

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

## Resolved during M5

- [x] (2026-11-17, M5) **Is the scheduler an APScheduler daemon or a one-shot sweep?**
  **RESOLVED 2026-11-17: a one-shot sweep in M5.** `heraldesk scheduler run` performs one deterministic
  pass over SQLite state. APScheduler wiring moves to the milestone that implements `heraldesk serve`
  (M7), because `serve` is still a stub. Durability and no-double-post follow from the database state,
  not from an in-memory job store. Recorded in `docs/decisions.md`.
- [x] (2026-11-17, M5) **Do Threads and Nostr get real adapters in M5?**
  **RESOLVED 2026-11-17: no.** Only `fake` implements `publish`. Every other platform raises
  `NotSupportedError` with a reason documented in `docs/platforms/README.md`; the scheduler records it as
  a normal `publish.failed`. This matches R1: app review and unverifiable platform behaviour are not code
  failures. Recorded in `docs/decisions.md`.
- [x] (2026-11-17, M5) **How many publish attempts, and what is the backoff?**
  **RESOLVED 2026-11-17: three attempts total (one initial plus two retries) with 60 s then 300 s
  backoff.** Attempts and the redacted error are stored on `content_items`; exhausted items stay `failed`
  until `heraldesk publish <id>` re-queues them. Recorded in `docs/decisions.md`.
- [x] (2026-11-17, M5) **Does the scheduler publish `draft_only` items after a human approves them?**
  **RESOLVED 2026-11-17: no.** `draft_only` means manual publishing only. After approval the item stays
  `approved`; `heraldesk publish <id>` is the only way out. Recorded in `docs/decisions.md`.

## Resolved during M6

- [x] (2026-11-17, M6) **Where is the Telegram bot token stored?**
  **RESOLVED 2026-11-17:** environment, `.env`, or an encrypted `telegram.token` file protected by
  `secret.key`. Never in `heraldesk.yaml` or SQLite plaintext. `telegram token set|status` manages the
  encrypted file. Recorded in `docs/decisions.md`.
- [x] (2026-11-17, M6) **Which chat receives profile notifications?**
  **RESOLVED 2026-11-17:** every id in `telegram.allowed_chat_ids`; zero ids produces a suppressed
  ledger row. Chat ids are partially masked in audit and CLI output. Recorded in
  `docs/decisions.md`.
- [x] (2026-11-17, M6) **What happens to notifications muted by `notify: off|approvals_only`?**
  **RESOLVED 2026-11-17:** one `suppressed` notification row and one `telegram.suppressed` audit entry,
  with no send. This keeps suppression final across scheduler re-runs. Recorded in
  `docs/decisions.md`.
- [x] (2026-11-17, M6) **Do manual publishes notify, or only scheduler publishes?**
  **RESOLVED 2026-11-17:** both. The notification call lives in `publish_item`, so `heraldesk publish`
  and the scheduler share the same code and idempotency keys. Recorded in `docs/decisions.md`.
- [x] (2026-11-17, M6) **How does an inbound message choose a profile?**
  **RESOLVED 2026-11-17:** approve/reject resolve it from the content item; `/queue` uses an optional
  profile hint, then the single registered profile, otherwise replies with the profile list. No
  chat-to-profile mapping is added in M6. Recorded in `docs/decisions.md`.

## Resolved during M7

- [x] (2026-11-17, M7) **Telegram does not allow `getUpdates` while a webhook is registered. How can the
  server support both?**
  **RESOLVED 2026-11-17:** add `telegram.mode: polling | webhook`, default `polling`. Polling mode runs
  the 10-second `getUpdates` job and keeps the webhook endpoint for local testing; webhook mode does not
  register the poll job but keeps the endpoint. `telegram.enabled` remains the master switch. Recorded in
  `docs/decisions.md`.
- [x] (2026-11-17, M7) **Does M7 create an `apscheduler_jobs` table or use `SQLAlchemyJobStore`?**
  **RESOLVED 2026-11-17: no.** Recurring triggers are static in-memory definitions and all durable work
  stays in the existing business tables. This supersedes the earlier R5 wording. Recorded in
  `docs/decisions.md`.
- [x] (2026-11-17, M7) **May `heraldesk serve` bind a non-loopback host before password auth exists?**
  **RESOLVED 2026-11-17: no.** Any non-loopback host is refused with a clear error. Remote access goes
  through a local reverse proxy until the web UI milestone adds authentication. Recorded in
  `docs/decisions.md`. **Superseded by M11:** remote binds are allowed with `--allow-remote`,
  an explicit admin token of at least 32 characters and `HERALDESK_TRUST_PROXY=1` behind HTTPS.
- [x] (2026-11-17, M7) **What happens when the same Telegram update arrives twice?**
  **RESOLVED 2026-11-17:** `telegram_messages.update_id` has a unique index and the inbox returns
  `duplicate` without storing, replying or auditing. This covers webhook retries and polling replay
  after a restart. Migration `0008` adds the column and index. Recorded in `docs/decisions.md`.


## Resolved during M13

- [x] (2026-11-19, M13) **How strict should the Threads text limit be?**
  **RESOLVED 2026-11-19:** reject locally when the UTF-8 encoding exceeds 500 bytes, before
  any network call. The platform counts emoji by bytes but does not publish the exact
  algorithm; the conservative guard may reject some mixed-script text but never wastes the
  publisher's retries on a deterministic rejection. Recorded in `docs/platforms/threads.md`.

- [x] (2026-11-19, M13) **Does Threads support an idempotency key?**
  **RESOLVED 2026-11-19: no documented key.** Heraldesk uses its persisted `external_id`
  plus an in-process cache; a timeout after acceptance remains the documented
  human-reconciliation case. Do not blind-retry `threads_publish`.

- [x] (2026-11-19, M13) **Should the adapter use `auto_publish_text=true`?**
  **RESOLVED 2026-11-19: no.** Its response shape is undocumented. Use the explicit
  create-container / status / publish flow.


## Open

- [ ] **Should draft creation share the Web UI's reject-instead-of-mask behavior?**
  The Queue editor rejects bodies that contain secret-like text or `[REDACTED]` instead of
  silently masking them. The Writer and `heraldesk queue edit` still rely on redaction on
  write. Decide whether all draft creation paths should reject, and whether the CLI needs
  an interactive confirmation. Not changed yet.


- [ ] (2026-11-21, Web UI) **Should the inbox classify crisis or complaint messages?**
  The current inbox sorts by status then age and leaves room for a future marker. No
  categorization, storage or UI was added in the redesign; decide the taxonomy and whether it
  belongs in the domain or the web layer.

- [ ] (2026-11-21, Web UI) **Should lint findings be stored for display?**
  The Draft Room and queue show lint/Compliance results from existing run fields. Persisting a
  separate lint findings history would need new storage; decide whether that is worth it.
