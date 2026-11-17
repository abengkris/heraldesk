# Platform adapters

Heraldesk talks to official platform APIs only. Adapters contain no business logic; the
publisher applies the content state machine, policy, retries and audit rules.

## Status

| Platform | Account check | Publish | Notes |
|---|---|---|---|
| `fake` | yes | yes (M5) | Offline adapter used by tests and CI. See `fake.md`. |
| `nostr` | yes (offline key check) | yes (M10) | Real NIP-01 kind 1 notes. See `nostr.md`. |
| `threads` | fake fallback | `NotSupportedError` | Real adapter deferred. |
| `x` | fake fallback | `NotSupportedError` | Real adapter deferred. |
| `facebook` | fake fallback | `NotSupportedError` | Real adapter deferred. |
| `instagram` | fake fallback | `NotSupportedError` | Real adapter deferred; needs Business account plus linked Page. |
| `linkedin` | fake fallback | `NotSupportedError` | Real adapter deferred. |

## Why the remaining adapters return `NotSupportedError`

The publisher is complete for `fake` and `nostr`. Threads, X, Facebook, Instagram and
LinkedIn need platform app review, live credentials and API behaviour that cannot be
verified offline. Rather than guess at an API, `heraldesk.platforms.publish_adapter_for`
raises `NotSupportedError` with this message:

> no publisher adapter for platform '<name>'; see docs/platforms/README.md for the
> documented reason

The scheduler treats that as a normal `publish.failed` outcome: the attempt is recorded,
the item enters the retry path and eventually stays `failed` with the reason visible in
`heraldesk queue list`. This is a documented limitation, not a code failure, and matches
the decision recorded in `docs/decisions.md`.

Real adapters must implement the `PlatformAdapter` contract in
`src/heraldesk/platforms/base.py`, including `publish()`, and document their real
constraints in a new `docs/platforms/<name>.md`.
