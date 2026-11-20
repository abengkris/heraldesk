# Draft Room (proposed)

Status: **DR1–DR4 implemented; DR5 pending.** Backend (1.18.6), conversation UI
(1.18.8), draft creation (1.18.9), and profile patches (1.18.11) are in. Hardening and
accessibility remain.

## Problem

Heraldesk can run a deterministic content pipeline, but the only ways to steer it are
profile files, CLI commands and the approval queue. There is no place to discuss an idea,
ask a role for alternatives, or turn a conversation into a draft without leaving the
product.

The Draft Room adds that planning layer **without** turning Heraldesk into an autonomous
agent. It is a conversation that produces proposals. The existing state machine, approval
boundary and publisher remain the only way content is approved or published.

## Product rules

These are non-negotiable:

1. The Draft Room cannot approve, reject, schedule or publish content.
2. It cannot change a content item's state. Draft creation enters the normal
   `pending_approval` path through the existing approval service.
3. It cannot write profile files directly. A profile change is a proposed diff that the
   user explicitly applies.
4. It cannot run arbitrary tools, shell commands or platform API calls.
5. Every turn is an `agent_runs` row with tokens and cost.
6. It is local-only, behind the existing Web UI authentication and CSRF protection.
7. It is disabled by default with `studio.enabled: false`.

## Roles and scope

The first version exposes three roles:

| Role | What it does | Output |
|---|---|---|
| Editor | Default conversational partner; plans, asks clarifying questions, proposes next steps | text |
| Writer | Turns a concrete idea into a platform-aware draft | `draft` artifact |
| Compliance | Reviews the latest draft in the thread | `compliance` artifact |
| Profile | Proposes a full replacement for one profile file | `profile_patch` artifact |

Strategist and Researcher join later. Researcher needs a citation/source UI before it can
be called safely from a conversation.

Role selection is deterministic:

- default: Editor
- `@writer ...`, `@compliance ...`, `@editor ...` force a role
- the Editor may *suggest* a handoff, but it cannot silently invoke another role

There is no free-running multi-agent loop. In v1 one user turn produces one LLM call. A
future explicit handoff is capped at two calls so a conversation cannot fan out.

## User flow

```text
/studio
  ├─ thread list for the selected profile
  └─ new thread
       ├─ message box
       ├─ role selector / @mention
       └─ artifact cards
```

Example:

```text
You: We need a post about yesterday's outage. Honest, no corporate tone.

Editor: Three angles:
        1. What broke and what changed
        2. The timeline, plainly
        3. What we still don't know
        Choose one and I'll ask the Writer for a Threads draft.

You: @writer angle 1, Threads, under 500 bytes.

Writer: [Draft proposal card]
        "We caused a 40-minute outage yesterday. Here is what failed..."

You: @compliance check it.

Compliance: [Compliance note card]
            Risk: low. Avoid "fully resolved"; the audit is not finished.

You: Create draft.

→ Content item #42, pending approval, visible in /queue
```

## Artifacts

Artifacts are structured JSON stored on the message row. They are proposals, not records
of an action.

| Type | Fields | Actions |
|---|---|---|
| `idea` | title, angle, evidence, platform, why_now | “Use in a draft” |
| `draft` | platform, body, hashtags, variants, risk_notes | “Create draft”, “Copy” |
| `compliance` | verdict (`pass|warn|block`), findings, suggested_edits | “Apply suggestion” (edits the draft artifact) |
| `profile_patch` | file, rationale, diff, proposed_text, base_hash | “View diff”, “Apply patch” |

Only `draft` and `profile_patch` have write actions. All write actions require an explicit
button press, a CSRF token and a confirmation step for profile patches.

### Creating a draft

`Create draft` performs the same checks as the pipeline:

1. Resolve the profile scope and target platform.
2. Enforce platform constraints (for example Threads' 500 UTF-8 bytes).
3. Run the deterministic lint and the Compliance agent.
4. If the result is `block`, create a `blocked` content item and show the reason; do not
   create an approval.
5. Otherwise create a `pending_approval` content item, link it to the message and the
   agent run, and request approval through `domain.approvals.request_approval`.
6. Audit `studio.draft_created`; the item appears in `/queue` exactly like a pipeline
   draft.

The user still approves or rejects it in the queue. The Draft Room never does that.

### Applying a profile patch

Only `soul.md`, `brief.md`, `policy.yaml` and `banned.yaml` may be patched.

1. The artifact records the file's hash when it was proposed.
2. Apply re-reads the file and refuses if the hash changed.
3. The existing profile editor validates the proposed content.
4. The write is atomic, then the profile is reloaded through the normal reload path.
5. Audit `studio.profile_patch_applied` with the file name, old hash and new hash.

## Data model

Two tables, both scoped by `profile_id`:

### `studio_threads`

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `profile_id` | FK profiles | cascade delete |
| `title` | string(200) | derived from the first user message |
| `status` | string(16) | `open` or `archived` |
| `created_at`, `updated_at` | datetime | |

### `studio_messages`

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `thread_id` | FK studio_threads | cascade delete |
| `role` | string(16) | `user`, `editor`, `writer`, `compliance`, `system` |
| `content` | text | redacted before storage |
| `artifact_type` | string(24), nullable | `idea`, `draft`, `compliance`, `profile_patch` |
| `artifact_json` | JSON, nullable | validated by the matching Pydantic schema |
| `agent_run_id` | FK agent_runs, nullable | one row per LLM call |
| `content_item_id` | FK content_items, nullable | set after “Create draft” |
| `cost_usd` | float | copied from the agent run |
| `created_at` | datetime | |

Indexes: `(thread_id, created_at)`, `(profile_id)` via the thread join.

No content state is added. No approval state is added. `approvals.channel` stays
`web|telegram|cli`; a Draft Room approval uses `web`, and the audit payload records
`surface=studio`.

## Service and API

All domain access goes through `ProfileScope(profile_id)`.

Internal service: `heraldesk.studio`

- `create_thread(scope, title)`
- `list_threads(scope)`
- `get_thread(scope, thread_id)`
- `send_message(scope, thread_id, role, text)`
- `create_draft(scope, message_id)`
- `apply_profile_patch(scope, message_id, confirm_hash)`
- `archive_thread(scope, thread_id)`

It must not import the publisher, the scheduler or platform adapters. A test asserts this
boundary.

Web UI (v1):

```text
GET  /studio
POST /studio/threads
GET  /studio/{thread_id}
POST /studio/{thread_id}/messages
POST /studio/{thread_id}/messages/{message_id}/draft
POST /studio/{thread_id}/messages/{message_id}/patch
POST /studio/{thread_id}/archive
```

The JSON API is deferred to v1.1; the service layer is still the only entry point.

## Agent runtime

- Reuse `AgentRunner`; one user turn is one run.
- Add an `Editor` agent with a structured `EditorReply` schema: `reply`,
  `suggested_role`, optional `idea`/`draft` artifact.
- Writer and Compliance reuse their existing schemas and prompts.
- The model is `llm.default_model` for every role in v1. Per-role models are out of scope.
- The thread cost cap is `studio.max_cost_usd` (default `0.25`). The global daily cap still
  applies. When either cap is reached, the next turn is refused before the LLM call.
- At most 40 messages per thread; `studio.max_messages` configures this.

### Context

Each turn receives, in order:

1. the role system prompt
2. the profile files (`soul.md`, `brief.md`, `policy.yaml`, `banned.yaml`)
3. a snapshot of up to 10 pending queue items
4. the next 7 days of the calendar
5. the last 12 messages in the thread
6. the current user message

If the context exceeds the budget, the oldest messages are dropped and a system line says
so. No extra summarization call in v1.

## Configuration

```yaml
studio:
  enabled: false
  max_messages: 40
  max_cost_usd: 0.25
```

`enabled` defaults to false. When false, the `/studio` routes return 404 and the navigation
link is hidden.

## Security and privacy

- The user is trusted; pasted external text is not. Any quoted platform comment or web
  snippet is labelled as untrusted data and cannot change the system rules.
- No platform comments or DMs are fetched.
- No credentials are ever placed in prompts. The existing redactor runs before storage and
  display.
- Profile patches are path-restricted and hash-checked.
- The Draft Room has no publish, approve, kill-switch, account or credential actions.
- Audit records message lengths and hashes, not full message bodies, for routine turns;
  the message body stays in `studio_messages`.
- The feature adds no external services and no new runtime. It is still one process and
  SQLite.

## Acceptance criteria

Each criterion needs an offline test with the fake LLM provider.

- [x] **AC-1** `studio.enabled` defaults to false; `/studio` returns 404 and no nav link is
  rendered when disabled.
- [x] **AC-2** A thread and its messages are isolated by `ProfileScope`; another profile
  receives 404 for the same id.
- [x] **AC-3** Sending a message creates exactly one `studio_messages` row and one
  `agent_runs` row, and records tokens and cost.
- [x] **AC-4** A message turn creates no content item and changes no content state.
- [x] **AC-5** The studio service cannot import the publisher, scheduler or platform
  adapters; a static test enforces this.
- [x] **AC-6** No studio route or service method can approve, reject, schedule or publish.
  A request to an invented `/studio/{id}/publish` returns 404.
- [x] **AC-7** “Create draft” produces a `pending_approval` item, a pending approval row
  and `studio.draft_created` audit entry; it appears in `/queue`.
- [x] **AC-8** A compliance `block` produces a `blocked` item and no approval.
- [x] **AC-9** A draft over the platform limit is rejected before any content item is
  created.
- [x] **AC-10** A profile patch with a stale `base_hash` is refused; a valid patch writes
  atomically, reloads the profile and audits old and new hashes.
- [x] **AC-11** A turn over `studio.max_cost_usd` is refused before the LLM call and
  records no message.
- [x] **AC-12** Context keeps the last 12 messages and emits the omission notice when
  older messages are dropped.
- [x] **AC-13** A secret-like string in a user message is not present in the audit payload,
  logs or rendered error.
- [x] **AC-14** All tests run with the fake provider and make no network call.
- [x] **AC-15** `docs/draft-room.md`, the feature flag and the “proposals only” rule are
  linked from the README.

## Phased delivery

| Phase | Scope | Status |
|---|---|---|
| DR1 | config, tables, service, Editor schema, fake-provider tests | **done in 1.18.6** |
| DR2 | thread list, message pane, Editor role selector, archive | **done in 1.18.8** |
| DR3 | artifact cards and “Create draft” with lint and compliance | **done in 1.18.9** |
| DR4 | profile patches with diff and confirmation | **done in 1.18.11** |
| DR5 | hardening, docs, accessibility, release | next |

## Out of scope

- Telegram chat. Telegram remains notifications and commands.
- Autonomous approval or publishing.
- Free-running multi-agent conversation.
- Shell, HTTP or platform tool execution from chat.
- Platform comment or DM ingestion.
- Analytics-driven planning (no analytics data exists yet).
- Per-role model routing.
- Voice, images or file uploads.
