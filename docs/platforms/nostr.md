# Nostr adapter

Status: implemented in M10. This is the first real publishing adapter; every other
non-fake platform still returns `NotSupportedError`.

## Protocol

- One NIP-01 **kind 1** text note per content item.
- `content` is the approved post body.
- `created_at` is the UTC publish time as a Unix timestamp.
- `tags` is empty, plus one `["t", "<hashtag>"]` per hashtag in the body. Hashtags are
  lowercased, deduplicated and capped at ten.
- `id` is the SHA-256 of the canonical NIP-01 serialization
  `[0, pubkey, created_at, kind, tags, content]`.
- `sig` is a BIP-340 Schnorr signature over the 32-byte event id.
- The public handle is the account `npub1...` identifier. The secret is the `nsec1...`
  credential, encrypted at rest with `secret.key`.

## Relays

Relay precedence:

1. the account credential `relays` (comma separated), set with `--relay`;
2. `nostr.relays` in `$HERALDESK_HOME/heraldesk.yaml`;
3. the built-in defaults `wss://relay.damus.io` and `wss://nos.lol`.

Only `wss://` URLs are accepted. The adapter connects to every relay concurrently, sends
`["EVENT", <event>]`, and waits up to `nostr.timeout_seconds` (default 10) per relay for
an `["OK", <event id>, true, ...]` answer.

- **Success:** at least one relay answers `OK true`. One relay rejecting or timing out
  does not fail a publish another relay accepted.
- **Failure:** zero relays accept. The publisher records a redacted `publish.failed`
  error and uses the normal 60 s / 300 s retries, then leaves the item `failed`.
- **At most once:** the publisher never re-sends an item that has an `external_id`, and
  relays deduplicate an identical event id. A relay that accepted an event but lost its
  `OK` response is the documented human-reconciliation case from the M5 design.

## Credentials

- `nsec` never appears in `heraldesk.yaml`, logs, audit payloads, reports, CLI output or
  the event itself.
- Add the account with `--credential-from-env NAME` so the key is not typed on the command
  line or stored in shell history.
- `heraldesk account list` shows a masked npub such as `npub1lycg5…q266`.
- The nsec can only be decrypted with the instance `secret.key`. If that key is lost, the
  account can no longer publish.

## Limits

There is no protocol-wide content limit. The provisional Writer limit for Nostr is 8000
characters; individual relays apply their own policies. Instagram-style media is not
supported: the adapter publishes text only.

## Throwaway-key warning

**Nostr events are immutable. Once a relay accepts an event it cannot be deleted.** For a
first test, use a throwaway key, publish a harmless note, verify the event id on a public
explorer, and then abandon the key. Do not put a long-lived identity key on a machine you
are still configuring.

## Manual acceptance (not run in CI)

In a separate terminal, so the nsec never appears in chat, screenshots or CI logs:

```bash
export NOSTR_NSEC_THROWAY='<throwaway nsec>'
uv run heraldesk account add acme --platform nostr \
    --credential-from-env NOSTR_NSEC_THROWAY --label throwaway
uv run heraldesk account list acme
uv run heraldesk run daily acme --dry-run
uv run heraldesk publish <id>
```

Then open `https://njump.me/<event id>`, confirm the note, and abandon the throwaway key.

Example npub for documentation only, derived from the public BIP-340 test vector, not an
account you should use:

```text
npub1lycg5qvjtrp3qjf5f7zl382j9x6nrjz9sdhenvyxq8c3808qxmus6gq266
```

## Tests

`tests/test_nostr_adapter.py`, `tests/test_publisher_nostr.py` and
`tests/test_cli_m10.py` cover event creation, BIP-340 verification, relay OK/NOT OK,
timeouts, relay configuration, retries, at-most-once publishing, CLI output and secret
redaction. Every relay call is mocked; pytest never uses the network.
