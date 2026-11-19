# Threads adapter

Status: implemented. Text-only posts through the official Meta Threads Graph API.

## Credentials

An account stores two encrypted credentials:

- `access_token` — a **Threads user access token** (required). Use a long-lived token
  (60 days) for daily use.
- `user_id` — the Threads user id returned by the token exchange (optional; the adapter
  calls `/me` when it is absent).

```bash
# Read both values from the environment so they never enter shell history.
export THREADS_TOKEN='<long-lived user access token>'
export THREADS_USER_ID='<threads user id>'
uv run heraldesk account add acme --platform threads --handle @acme \
    --credential-from-env THREADS_TOKEN --credential-from-env THREADS_USER_ID
uv run heraldesk account test acme --platform threads
```

`account test --platform threads` calls `GET /v1.0/me`; it is the only account check that
uses the network. The token is decrypted only for the adapter call and never appears in
CLI output, audit payloads or error messages.

### Getting a token (official flow)

1. Create a Meta app with the Threads use case and request `threads_basic` and
   `threads_content_publish`.
2. Add your own Threads account as a **Threads Tester** (App roles). This is enough to
   publish for your own account without App Review.
3. Exchange the authorization code for a short-lived token, then exchange that for a
   long-lived token. Token endpoints do **not** use the `v1.0` prefix:

```text
POST https://graph.threads.com/oauth/access_token
GET  https://graph.threads.com/access_token?grant_type=th_exchange_token
GET  https://graph.threads.com/refresh_access_token?grant_type=th_refresh_token
```

Short-lived tokens last 1 hour; long-lived tokens last 60 days. A refresh returns a **new**
token string, so store it again. Refreshing requires a token at least 24 hours old, and a
private profile cannot extend the 90-day permission grant.

Advanced access (publishing for accounts other than your testers) requires App Review and
Business Verification. A single-user self-hosted install does not need it.

## Publishing flow

Text uses the documented two-step flow:

```text
POST /v1.0/{user-id}/threads          media_type=TEXT, text=<body>  -> container id
GET  /v1.0/{container-id}?fields=status,error_message                -> wait for FINISHED
POST /v1.0/{user-id}/threads_publish  creation_id=<container id>     -> media id
GET  /v1.0/{media-id}?fields=permalink                               -> best-effort URL
```

- `external_id` is the published **media id**, not the ephemeral container id.
- `url` is the permalink when the API returns one. It can be omitted for flagged posts.
- The adapter polls the container at most once per second for up to 30 seconds, then fails
  the publish. The publisher's normal 60 s / 300 s retries create a fresh container.
- `media` may contain `link_attachment` and `topic_tag`; both are passed through when
  present.

## Limits

| Limit | Value |
|---|---|
| Post text | **500** characters; emoji are counted by **UTF-8 bytes** |
| Links | 5 unique URLs across text and `link_attachment` |
| Topic tag | 1–50 characters, no `.` or `&`, one per post |
| Publishing | 250 posts per rolling 24 hours, enforced at `threads_publish` |
| Container lifetime | 24 hours |

The adapter rejects text over 500 UTF-8 bytes before any network call. This is deliberately
conservative: it may reject a post that mixes many non-ASCII characters, but it never sends
a deterministic rejection into the retry path. The exact server-side emoji algorithm is not
published; see `docs/open-questions.md`.

## Idempotency

The Threads API documents **no idempotency key**. Heraldesk cannot ask Meta to deduplicate a
retried publish. Safety comes from Heraldesk's own publisher: `external_id` is persisted
before a retry, and an item with an external id is never published again. The adapter also
caches results per idempotency key inside one process. A timeout after Meta accepted a post
but before the response arrived is the documented human-reconciliation case; check the
account before retrying manually.

## Not implemented

- Token refresh jobs and expiry warnings (store tokens, refresh manually every 60 days).
- Replies, mentions, insights and media posts (`image`, `video`, carousels).
- `auto_publish_text=true` (the response shape is not documented; the explicit two-step
  flow is used instead).

## Manual acceptance (not run in CI)

CI mocks every HTTP call. To verify a real account, publish a harmless text post:

```bash
export THREADS_TOKEN='<long-lived user access token>'
export HERALDESK_HOME="$HOME/.heraldesk"
uv run heraldesk account test acme --platform threads
uv run heraldesk run daily acme
uv run heraldesk queue list acme
uv run heraldesk queue show <id>
uv run heraldesk queue approve <id>
uv run heraldesk publish <id>
```

Then open the permalink printed by `heraldesk publish`.

## Tests

`tests/test_threads_adapter.py` covers credential validation, the two-step publish flow,
container statuses, optional link/topic fields, byte-limit rejection, missing permalinks,
in-process idempotency and token-free error messages. Every HTTP call is faked.

## Sources

- [Threads API overview](https://developers.facebook.com/documentation/threads/overview)
- [Get started / access tokens](https://developers.facebook.com/documentation/threads/get-started/get-access-tokens-and-permissions)
- [Long-lived tokens](https://developers.facebook.com/documentation/threads/get-started/long-lived-tokens)
- [Create posts](https://developers.facebook.com/documentation/threads/create-posts)
- [Publishing reference](https://developers.facebook.com/documentation/threads/reference/publishing)
- [Handle errors](https://developers.facebook.com/docs/graph-api/guides/error-handling)
