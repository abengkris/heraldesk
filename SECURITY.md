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
| 1.18.x | yes |
| 1.17.x | security fixes until 1.19.0 |
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
- Optionally set `telegram.allowed_user_ids` to restrict command senders.
- Keep `secret.key` mode 600. Rotate it with `heraldesk admin rotate-key` while the server
  is stopped; it takes a mandatory credential backup first. Rotation re-encrypts the
  persisted `telegram.token` file only; environment/`.env` token overrides are left in place.
  If session revocation fails, rotation aborts before touching credentials. Losing the key
  without a backup makes stored credentials unreadable.
- Back up with credentials (`heraldesk backup --with-credentials`) and store the archive
  off the host. This includes `secret.key` and `telegram.token`; `.env` is still excluded,
  so back it up separately.
- Stop `heraldesk serve` before `restore`; the server lock makes restore refuse while the
  process is running. Prune credential backups explicitly with `backup-prune`.
- Run `heraldesk doctor` after every upgrade and `heraldesk upgrade` before starting a new
  release. `serve` refuses a stale schema.
- Use `POST /logout-all` or `heraldesk admin logout-all` after suspected cookie theft; this
  rotates the instance session epoch immediately.
- Install platform adapter plugins only from sources you trust: plugin code runs in the
  Heraldesk process with your user's permissions.
- No telemetry. Outbound traffic is limited to your LLM provider, platform APIs, Telegram
  and the Researcher's web search.

## Nostr key handling

- A Nostr **nsec is a full identity key**: anyone who obtains it can post and read DMs as
  that identity. Events are immutable; a deletion request is advisory, not guaranteed.
- **Preferred:** use a dedicated long-lived Nostr identity for Heraldesk, or a throwaway
  key for the first test. **Your original identity is supported**, but it carries the same
  permanent consequences as any other nsec with more at stake.
- Import with `heraldesk account add ... --credential-from-env NAME`, never on the command
  line. Unset the variable immediately afterwards. Heraldesk encrypts the nsec with
  `secret.key` and never prints it.
- Any credential backup that includes `secret.key` can decrypt the nsec; protect it like
  the original key.

## Security design notes

See `docs/architecture.md`, section 10, and `docs/decisions.md` for the token model,
encrypted credential storage, webhook handling, redaction rules and the loopback-first
server defaults.
