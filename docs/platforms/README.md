# Platform adapters

Heraldesk talks to official platform APIs only. Adapters contain no business logic; the
publisher applies the content state machine, policy, retries and audit rules.

## Status

| Platform | Account check | Publish | Notes |
|---|---|---|---|
| `fake` | yes | yes (M5) | Offline adapter used by tests and CI. See `fake.md`. |
| `threads` | fake fallback | `NotSupportedError` in M5 | Real adapter deferred. |
| `nostr` | fake fallback | `NotSupportedError` in M5 | Real adapter deferred. |
| `x` | fake fallback | `NotSupportedError` in M5 | Planned for M8. |
| `facebook` | fake fallback | `NotSupportedError` in M5 | Planned for M8. |
| `instagram` | fake fallback | `NotSupportedError` in M5 | Planned for M8; needs Business account plus linked Page. |
| `linkedin` | fake fallback | `NotSupportedError` in M5 | Planned for M8. |

## Why Threads and Nostr return `NotSupportedError` in M5

The M5 publisher is complete, but only the `fake` adapter is implemented. Threads and Nostr
need platform app review, live credentials and API behaviour that cannot be verified
offline in this milestone. Rather than guess at an API, `heraldesk.platforms.publish_adapter_for`
raises `NotSupportedError` with this message:

> no publisher adapter for platform '<name>' in M5; see docs/platforms/README.md for the
> documented reason

The scheduler treats that as a normal `publish.failed` outcome: the attempt is recorded,
the item enters the retry path and eventually stays `failed` with the reason visible in
`heraldesk queue list`. This is a documented limitation, not a code failure, and matches
the decision recorded in `docs/decisions.md` (M5 section) and `docs/open-questions.md`.

Real adapters arrive in a later milestone (see `ROADMAP.md`, M8). They must implement the
`PlatformAdapter` contract in `src/heraldesk/platforms/base.py`, including `publish()`,
and document their real constraints in a new `docs/platforms/<name>.md`.
