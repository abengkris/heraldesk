# Web UI

Heraldesk's local Web UI is server-rendered with **FastAPI + Jinja2 + vendored htmx + hand-written CSS**. There is no Node build, no Tailwind, no CSS framework, and no CDN. All assets are local; no external requests are made.

## Design tokens

Tokens live in `static/app.css` under `:root` and `:root[data-theme="dark"]`.

| Group | Variables |
|---|---|
| Neutral | `--bg`, `--surface`, `--surface-2`, `--surface-3`, `--text`, `--muted`, `--border`, `--border-strong` |
| Accent (one hue) | `--accent`, `--accent-hover`, `--accent-contrast`, `--accent-soft` |
| Status | `--status-ok`, `--status-warn`, `--status-danger`, `--status-info`, `--status-muted` (plus `-bg` variants) |
| Focus | `--focus` |
| Shadow | `--shadow`, `--shadow-lg` |
| Radius | `--radius` (5px), `--radius-lg` (8px) |
| Spacing (4px grid) | `--s1`…`--s7` |
| Type | `--font`, `--font-mono`, `--fs-base` (15px), `--fs-sm`, `--fs-xs`, `--lh` (1.5) |
| Layout | `--sidebar-w`, `--topbar-h`, `--reading` (70ch) |

## Status → color mapping

A single place maps each risk level and content state to one semantic status color. The markup always pairs color with a text label (`status_badge` renders both).

| Semantic | CSS | Risk | Content state |
|---|---|---|---|
| ok | `status-ok` | `low` | `approved`, `published` |
| warn | `status-warn` | `medium` | `pending_approval`, `needs_review` |
| danger | `status-danger` | `high`, `blocked` | `rejected`, `failed`, `blocked` |
| info | `status-info` | — | `idea`, `reviewed`, `scheduled` |
| muted | `status-muted` | unknown | `drafted`, `cancelled`, unknown |

Use `{% from "_components.html" import status_badge, risk_badge %}` and `{{ status_badge("risk", level) }}` / `{{ status_badge("state", state) }}`.

## Component macros

`templates/_components.html` provides:

- `status_badge(kind, value)` — a `.badge` with the mapped status class and a text label.
- `risk_badge(level)` — `status_badge("risk", level)`.
- `empty_state(title, hint, action)` — consistent empty states.
- `page_header(title, subtitle, actions)` — page heading with optional actions.
- `field(label, name, value, type, help, error, required, autofocus)` — labeled form field.
- `confirm_form(action, csrf, label, message, method, hidden)` — a confirm/risky action form.
- `toast(message, kind)` — a toast for `hx-swap-oob`.

## Layout and navigation

- Desktop: left sidebar (primary group: Queue, Inbox, Calendar, Draft Room when enabled; "More" group: Reports, Cost, Audit, Profiles). The brand links to the Dashboard (`/`).
- Mobile (≤ 767px): the sidebar is hidden and a fixed **bottom bar** shows the primary items plus a "More" `<details>` menu for the secondary items and log out. No horizontal scroll; the bar fits 360px. The "More" menu is a plain `<details>` with normal links, so it closes naturally on full-page navigation (no JS needed).
- Top bar (every authenticated page): app title, the global kill switch (two-step confirm via a native `<details>` when off, one-step disengage when on), theme toggle, and log out.
- The persistent kill-switch banner is shown while the kill switch is engaged.

## Interaction patterns (htmx)

- CSRF: `<body hx-headers='{"X-CSRFToken": "…"}'>` for htmx requests; every plain form keeps a hidden `csrf` input so writes work without JS.
- Progressive enhancement: pages work fully without JS. htmx adds `hx-push-url`, `hx-swap="outerHTML"` on rows/cards, and `hx-swap-oob` for badges and toasts. `hx-boost` is intentionally not used: navigation is full-page, which keeps mobile menus and stale state simple.
- Fragment vs full page: handlers inspect the `HX-Request` header and return a fragment when present, otherwise the full page. An expired session on an htmx request responds with `HX-Redirect: /login`.
- Loading/error: every list has loading, error and empty states; never swap an empty or error fragment silently.

## Theme

Light and dark via `prefers-color-scheme`, plus a manual toggle in `static/app.js` stored in `localStorage` (`heraldesk-theme`). Status colors pass WCAG AA contrast in both modes.

## Legacy class names to remove (final cleanup)

These classes were kept during the redesign and should be deleted once no template references them. Verify with `grep -R "class-name" src/heraldesk/web/templates` before removing.

- `.stat`, `.stat-label`, `.stat-value` — still used by Dashboard; remove after its final pass.
- `.danger-text` — replaced by `status_badge` / `.status-danger`.
- `.reject-form` — replaced by `.form-row` (still used by Queue; remove after Queue actions settle).
- `.grid`, `.grid.two` — still used by Reports and Dashboard; keep until the final cleanup.
- `.alert.ok` — replaced by `.notice`; remove when the last occurrence is gone.
- `.alert.error` — still used by error blocks; keep until the final cleanup.
- `.card.stat` — still used by Dashboard; remove after its final pass.
- `.empty` — still used by the Profiles empty state; replace with `empty_state()` when the
  remaining pages are reworked.

## Manual test checklist

- 360px, 768px and 1280px; light and dark; keyboard only; JS disabled for read and write flows.
- Queue master-detail, Inbox two-pane, Calendar week grid / mobile agenda, Cost `<meter>` rows and cap, Audit filters/pagination/`<details>` payloads, Reports list/generate flow, Profiles editor tabs, Draft Room conversation.

## Queue editing

- The edit textarea is prefilled with the stored, already-redacted body. Submitting trims
  surrounding whitespace (`.strip()`); that normalization is the only text change the web
  layer applies.
- If the submitted text differs from its redacted form, or contains `[REDACTED]`, the
  request is rejected before any write or audit entry. The inline error never repeats the
  match; the textarea keeps the surrounding text with the match masked.
- Preview is read-only: it diffs the submitted text against the stored body fetched
  server-side. The hidden textarea in the preview emits a protective leading newline (the
  first newline after `<textarea>` is ignored by HTML parsers), and Jinja autoescape stays
  on.

## Calendar

- Server-rendered Monday-based weekly grid; prev/next/Today links carry `?profile=` and `week=`.
- Items are chips colored by state (always with a state text label) linking to `/queue/{id}`.
- Single profile: times shown in that profile's timezone and quiet hours (from `policy.yaml`) shaded, including cross-midnight ranges. All profiles: each chip shows its own timezone label; quiet hours are not shaded.
- Mobile (≤ 767px): a day-by-day agenda replaces the grid; no horizontal scroll.
- Read-only helper: `calendar_week` in `web/service.py`.

## Profiles

- The list shows name/slug, type, status, autonomy summary, pending/scheduled/failed counts and
  kill-switch state (display only). A broken `policy.yaml` shows `unknown`, never an error page.
- The editor selects one of the four files by plain links (`?file=soul.md|brief.md|policy.yaml|banned.yaml`).
  Tabs mark the active file and any file whose current content fails validation.
- The raw textarea is the source of truth. `Validate` works with or without JS; with JS the same
  endpoint is called on `keyup changed delay:500ms` and swaps the `aria-live="polite"` panel.
  The endpoint authenticates, requires CSRF and writes/audits nothing.
- `Review changes` shows a read-only line diff against the file on disk plus the validation
  result. `Save` validates again, writes only that file atomically, reloads the profile and
  records the existing `profile.edited` audit payload with `files: [name]`.
- The form carries a SHA-256 `base_hash`. If the file changed on disk, preview/save reject the
  edit with the diff against the current content instead of overwriting.
- If a change loosens autonomy, Save requires the explicit review-step confirmation checkbox.
  The decision is computed server-side from the file on disk and the submitted text; no
  client-supplied flag is trusted. Detection lives in the pure `loosens_autonomy` helper
  (`web/service.py`) and covers `policy.yaml` (default mode, rule modes, new auto rules,
  decreased delays, increased daily limits, shortened/removed quiet hours, `disclose_ai` off,
  widened risk/intents on auto rules) and `banned.yaml` (removed banned words/topics).
  `soul.md` and `brief.md` changes are not classified.
- The stale-hash re-check and the atomic write run under one process-wide lock
  (`profile_editor._PROFILE_EDIT_LOCK`), so two concurrent saves cannot both pass the hash
  check.
- The `policy.yaml` helper (**approach a**) updates only `default_mode`, `max_posts_per_day` and
  `quiet_hours` with a minimal line-level text edit. It preserves comments and key order and
  refuses shapes it cannot edit safely, telling the user to edit the YAML directly. A YAML
  round-trip was rejected because it would drop comments and reorder keys.
- Mobile: single column, helper above the textarea, full-width textarea, and the profiles table
  stacks into labeled rows without horizontal scroll.

Read-only helpers: `profiles_overview` and `loosens_autonomy` in `web/service.py`.

## Cost

- Table-first, newest UTC day first, with a rolling window of 7, 14 or 30 days (default 14;
  an invalid `days` falls back to 14). `?profile=` filters the rows and is preserved in the
  window links.
- Columns: day, profile, tokens in/out, cost USD, and one inline bar per row. The bar is a
  native `<meter>` with `max` set to the largest cost in the window; no inline styles, no JS.
- Day boundaries are UTC, matching the existing global cap and `cost_summary` behavior.
- "Today vs daily cap" uses a `<meter>` with spent, cap and percent as text plus a text
  badge (`within cap`, `near cap`, `over cap`). A non-positive cap is treated as not
  configured for display, with a short note.
- Read-only helper: `cost_breakdown` in `web/service.py`.

## Audit

- Compact newest-first table with UTC time, profile, actor, action, entity and monospace
  entity id. Payloads sit inside `<details>` and are pretty-printed, redacted, and truncated
  with a visible `Truncated.` note.
- GET-form filters: actor (exact), action (exact, or a trailing `*` for a prefix), entity
  (exact), and from/to dates in UTC. Invalid dates, offsets and ranges are ignored with an
  inline notice, never a 500.
- Pagination is stable offset paging with a 50-row page and a 5000-row maximum offset guard;
  Prev/Next links preserve every filter and `?profile=`.
- There are no delete, edit, export or clear routes. The page is read-only.
- Helpers: `list_audit_page` and the extended `AuditCatalog.recent` filters.

## Reports

- The list shows period, kind, size and modified time (UTC) with View and Download links, and
  stays visible when empty. Generate forms are secondary POST forms with CSRF and
  `hx-disabled-elt="find button"`.
- With JS, generation returns the refreshed list plus an out-of-band toast. Without JS, it is
  a normal POST redirect with a flash message.
- The report view keeps the existing sanitized Markdown pipeline inside a ~70ch reading
  column; tables scroll inside their own wrapper.
- Report names must match `weekly-YYYY-Www.md` or `monthly-YYYY-MM.md`, resolve inside the
  reports directory, and not follow a symlink out of it. Downloads use an attachment
  `Content-Disposition` filename and `text/markdown`.

## Navigation model

Navigation links are ordinary full-page loads; `hx-boost` is intentionally not enabled, so a mobile "More" `<details>` menu closes naturally after navigation without JS.

## No external requests

`static/app.css` and `static/app.js` contain no external URLs. A test asserts that rendered core pages and both static assets contain no `http://` or `https://`.
