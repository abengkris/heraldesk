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
- Progressive enhancement: pages work fully without JS. htmx adds `hx-boost`, `hx-push-url`, `hx-swap="outerHTML"` on rows/cards, and `hx-swap-oob` for badges and toasts.
- Fragment vs full page: handlers inspect the `HX-Request` header and return a fragment when present, otherwise the full page. An expired session on an htmx request responds with `HX-Redirect: /login`.
- Loading/error: every list has loading, error and empty states; never swap an empty or error fragment silently.

## Theme

Light and dark via `prefers-color-scheme`, plus a manual toggle in `static/app.js` stored in `localStorage` (`heraldesk-theme`). Status colors pass WCAG AA contrast in both modes.

## Legacy class names to remove (final cleanup)

These classes were kept during the redesign and should be deleted once no template references them. Verify with `grep -R "class-name" src/heraldesk/web/templates` before removing.

- `.stat`, `.stat-label`, `.stat-value` — replaced by the new metric components once Dashboard and Cost are reworked.
- `.danger-text` — replaced by `status_badge` / `.status-danger`.
- `.reject-form` — replaced by `.form-row` (still used by Queue; remove after Queue actions settle).
- `.grid`, `.grid.two` — replaced by the new layout utilities.
- `.facts` — the old detail definition list; unused after the Queue rework.
- `.alert.ok`, `.alert.error` — replaced by `.notice`, toast, and `status_badge`.
- `.card.stat` — replaced by the metric component.

## Manual test checklist

- 360px, 768px and 1280px; light and dark; keyboard only; JS disabled for read and write flows.
- Queue master-detail, Inbox two-pane, Calendar week grid / mobile agenda, Cost meter, Audit `<details>` payloads, Profiles editor tabs, Draft Room conversation.

## Calendar

- Server-rendered Monday-based weekly grid; prev/next/Today links carry `?profile=` and `week=`.
- Items are chips colored by state (always with a state text label) linking to `/queue/{id}`.
- Single profile: times shown in that profile's timezone and quiet hours (from `policy.yaml`) shaded, including cross-midnight ranges. All profiles: each chip shows its own timezone label; quiet hours are not shaded.
- Mobile (≤ 767px): a day-by-day agenda replaces the grid; no horizontal scroll.
- Read-only helper: `calendar_week` in `web/service.py`.

## Navigation model

Navigation links are ordinary full-page loads; `hx-boost` is intentionally not enabled, so a mobile "More" `<details>` menu closes naturally after navigation without JS.

## No external requests

`static/app.css` and `static/app.js` contain no external URLs. A test asserts that rendered core pages and both static assets contain no `http://` or `https://`.
