# Security Policy

## Reporting a vulnerability

Please do NOT open a public issue. Use GitHub's "Report a vulnerability" (Security
Advisories) on this repository. Include steps to reproduce, affected version and impact.
We aim to acknowledge reports within 7 days.

## Supported versions

Security fixes are provided for the current minor release and the one before it. The
project follows semantic versioning; upgrade with `heraldesk upgrade`, which takes a
credential backup before applying migrations.

| Version | Supported |
|---|---|
| 1.12.x | yes |
| 1.11.x | security fixes until 1.13.0 |
| older | no |

## Scope

Credential storage, authentication of the local web UI/API, Telegram webhook forgery,
prompt-injection paths that lead to unintended publishing, profile isolation bypass, and
supply-chain issues.

## Operator checklist

- Keep the server on `127.0.0.1` and put an HTTPS reverse proxy in front. A non-loopback
  bind requires `--allow-remote`, an explicit `HERALDESK_ADMIN_TOKEN` of at least 32
  characters and `HERALDESK_TRUST_PROXY=1`.
- Generate the admin token with a CSPRNG:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Set `HERALDESK_TELEGRAM_WEBHOOK_SECRET` whenever Telegram is enabled on a remote or
  webhook-mode server. Without it the server refuses to start, because an unsigned
  webhook could forge `/approve` commands.
- Optionally set `telegram.allowed_user_ids` to restrict commands to specific senders.
- Use `POST /logout-all` or `heraldesk admin logout-all` after suspected cookie theft; this rotates the
  instance session epoch immediately.
- Keep the reverse proxy as the only client of the Heraldesk port and make it overwrite `X-Forwarded-For`.
- Keep `secret.key` mode 600. Rotate it with `heraldesk admin rotate-key` while the server
  is stopped; it takes a mandatory credential backup first. Rotation re-encrypts the
  persisted `telegram.token` file only; environment/`.env` token overrides are left in place.
  If session revocation fails, rotation aborts before touching credentials. Losing the key
  without a backup makes stored credentials unreadable.
- Back up with credentials (`heraldesk backup --with-credentials`) and store the archive
  off the host. This includes `secret.key` and `telegram.token`; `.env` is still excluded,
  so back it up separately.
- Run `heraldesk doctor` after every upgrade and `heraldesk upgrade` before starting a new
  release. `serve` refuses a stale schema.
- Install platform adapter plugins only from sources you trust: plugin code runs in the
  Heraldesk process with your user's permissions.
- No telemetry. Outbound traffic is limited to your LLM provider, platform APIs, Telegram
  and the Researcher's web search.

## Security design notes

See `docs/architecture.md`, section 10, and `docs/decisions.md` for the token model,
encrypted credential storage, webhook handling, redaction rules and the loopback-first
server defaults.
