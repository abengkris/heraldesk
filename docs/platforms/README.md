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

An installed plugin can supply any platform that has no built-in publisher; see
[Writing a plugin](#writing-a-plugin).

## Why the remaining adapters return `NotSupportedError`

The publisher is complete for `fake` and `nostr`. Threads, X, Facebook, Instagram and
LinkedIn need platform app review, live credentials and API behaviour that cannot be
verified offline. Rather than guess at an API, `heraldesk.platforms.publish_adapter_for`
raises `NotSupportedError` when no built-in or installed plugin supplies the platform:

> no publisher adapter for platform '<name>'; install a plugin or see
> docs/platforms/README.md for the documented reason

The scheduler treats that as a normal `publish.failed` outcome: the attempt is recorded,
the item enters the retry path and eventually stays `failed` with the reason visible in
`heraldesk queue list`. This is a documented limitation, not a code failure, and matches
the decision recorded in `docs/decisions.md`.

## Writing a plugin

Any platform without a built-in adapter can be supplied by an installed Python package.
Declare an entry point in the `heraldesk.platforms` group:

```toml
[project.entry-points."heraldesk.platforms"]
threads = "my_plugin.threads:ThreadsAdapter"
```

The entry point name is the platform key stored on accounts and passed to
`--platform`. The target is a zero-argument callable (a class or a factory) returning an
object that implements the contract in `src/heraldesk/platforms/base.py`:

```python
class ThreadsAdapter:
    platform = "threads"  # must match the entry point name

    async def validate_credentials(self, account: AccountRef) -> bool: ...

    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult: ...
```

Rules:

- **Built-ins win.** A plugin cannot replace `fake` or `nostr`.
- **Discovery is lazy.** Entry-point metadata is read once per process; plugin code is
  imported the first time its adapter is requested. Call
  `heraldesk.plugins.reset_plugin_cache()` after installing a plugin in a running process.
- **Account checks stay offline.** `heraldesk account test` never imports plugin code; it
  keeps the fake adapter for plugin platforms. Publishing uses the real plugin.
- **Trust:** plugin code runs in the Heraldesk process with your user's permissions.
  Install only packages you trust.
- **Publishing rules still apply.** The publisher owns the state machine, policy gates,
  retries, at-most-once `idempotency_key` handling, notifications and audit. Adapters only
  make the platform call.

Real adapters must document their platform constraints in `docs/platforms/<name>.md`.
