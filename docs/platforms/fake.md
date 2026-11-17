# Fake adapter

`heraldesk.platforms.fake.FakeAdapter` is the offline adapter used by tests, CI and the
default `profile new` flow. It performs no network I/O and stores no data outside the
process.

## Contract

- `validate_credentials(account)` accepts a mapping whose `token` is a non-empty string.
- `publish(account, request)` returns a stable external id (`fake-1`, `fake-2`, ...) and
  records the request in `adapter.published`.
- `fail_times=N` makes the first `N` publish calls raise `PublishError` with a configurable
  message. Tests use it to exercise retry and redaction paths deterministically.
- The same `idempotency_key` always returns the first result, so a retry cannot create a
  second post.

## Limits

Arbitrary and stable for tests: 5000 characters and 10 hashtags. Real platform limits are
not modelled here; see the table in `README.md`.

## Credentials

The publisher may call the fake adapter through the in-memory fallback with no social
account row. Credentials are ignored for publishing, which keeps `profile new` plus
`run daily` usable offline without creating accounts.
