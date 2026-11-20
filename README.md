# Heraldesk

[![CI](https://github.com/abengkris/heraldesk/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/abengkris/heraldesk/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Version](https://img.shields.io/badge/version-1.18.5-blue.svg)](https://github.com/abengkris/heraldesk/releases/tag/v1.18.5)

> Your autonomous social media department. Self-hosted, open source, single-user.

**Version 1.18.5.** Heraldesk runs four LLM roles (strategist, researcher, writer,
compliance) plus a deterministic Designer placeholder and manual Community/Analyst
tooling. They draft, review, queue and schedule social media content for accounts you
manage. You approve; Heraldesk does the legwork. It runs on your laptop or your own VPS.
Your keys, your data.

Heraldesk is CLI-first and also ships a local Web UI + JSON API since 1.2.0. Threads
(text) and Nostr are bundled publishers; X/Facebook/Instagram/LinkedIn can be supplied by
an installed plugin. The fake adapter is the offline publishing path. See
[Threads publishing](#threads-publishing) and [Nostr publishing](#nostr-publishing).

## What works in 1.18.5

- Multiple profiles with their own voice, rules and autonomy policy.
- Draft-first content state machine with lint, Compliance and approval gates.
- Deterministic `daily_content` pipeline using the fake LLM provider or your own LiteLLM key.
- CLI approval queue, edits, rejection and manual publishing.
- Stateless scheduler with durable retries and at-most-once publishing.
- Profile quiet hours and a local-day post cap gate automatic publishing; explicit publishes override them.
- Official Threads (text) and Nostr publishers, plus `heraldesk.platforms` adapter plugins.
- `heraldesk upgrade` takes a credential backup before applying migrations.
- Docker, Compose, systemd and Caddy deployment examples for a VPS, with SBOM/provenance on release images.
- Runnable profile and adapter-plugin examples under `examples/`.
- Telegram notifications, allowlisted commands and a stored inbound inbox.
- Manual inbox triage: list, show, reply and ignore. No auto-reply and no AI-generated replies.
- Deterministic weekly and monthly reports, with no LLM calls.
- Backup and restore, doctor hardening, global and per-profile kill switches.
- Real Nostr publishing (NIP-01 kind 1 notes) with a dedicated, throwaway, or original key.
- Local Web UI and authenticated `/api/v1` for queue (including full-draft detail), calendar, inbox, reports, cost, audit, profiles, run traces, readiness and manual publishing.
- SQLite only. No Redis, Postgres, Node, telemetry or phone-home.

## Requirements

- Python 3.12, 3.13 or 3.14 (3.12 is recommended)
- `pipx install --python 3.12 ...` if your default interpreter is newer
- [`uv`](https://docs.astral.sh/uv/) for the recommended source install
- A Linux, macOS or Windows machine; the CLI is tested on all three

## Install from PyPI

Heraldesk supports **Python 3.12, 3.13 and 3.14**. Python 3.14 uses a pure-Python BIP-340
signing fallback because `coincurve` has no cp314 wheel; 3.12/3.13 use libsecp256k1.

```bash
pipx install --python 3.12 heraldesk
heraldesk --version

# uv users
uv tool install --python 3.12 heraldesk
uvx --python 3.12 heraldesk --version
```

`pipx ensurepath` if the `heraldesk` command is not found after install.

## Install from source or a release wheel

The GitHub release also carries the wheel and sdist. Trusted publishing is enabled for
PyPI and TestPyPI; the runbook is in [docs/releasing.md](docs/releasing.md).

```bash
# Repository checkout, with the pinned development environment
git clone https://github.com/abengkris/heraldesk.git
cd heraldesk
uv sync
uv run heraldesk init

# Or install the checkout directly into your user environment
pipx install --python 3.12 .

# Or install the release wheel
pipx install --python 3.12 https://github.com/abengkris/heraldesk/releases/download/v1.18.5/heraldesk-1.18.5-py3-none-any.whl
```

### Docker

```bash
git clone https://github.com/abengkris/heraldesk.git
cd heraldesk
printf 'HERALDESK_ADMIN_TOKEN=%s\n' "$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" > .env
chmod 600 .env
docker compose up -d --build
docker compose exec heraldesk heraldesk init
docker compose exec heraldesk heraldesk doctor
```

Compose binds only `127.0.0.1:8000`; put Caddy or Nginx in front. See
[deploy/README.md](deploy/README.md) for the VPS runbook, systemd unit and hardening
checklist.

## First run

```bash
export HERALDESK_HOME="$HOME/.heraldesk"   # optional; this is the default
uv run heraldesk init
uv run heraldesk doctor
```

`init` creates the data folder, `secret.key` (mode 600), a documented `heraldesk.yaml`
and the SQLite database. It is safe to run again: it keeps your files and applies pending
migrations.

Put LLM API keys in `$HERALDESK_HOME/.env`, never in `heraldesk.yaml`:

```dotenv
DEEPSEEK_API_KEY=your-key-here

# Any OpenAI-compatible gateway also needs its endpoint:
# OPENAI_API_KEY=your-gateway-key
# OPENAI_API_BASE=https://gateway.example/v1
```

Then set `llm.default_model` in `heraldesk.yaml`, for example `deepseek/deepseek-chat`,
or leave it empty to use the offline fake provider for a first run. The provider table,
custom endpoints, OpenCode Console setup and troubleshooting are in
[docs/providers.md](docs/providers.md).

Create and activate a profile:

```bash
uv run heraldesk profile new acme --name "Acme" --type company --timezone UTC --languages en
# edit profiles/acme/soul.md, brief.md, policy.yaml and banned.yaml
uv run heraldesk profile reload acme
uv run heraldesk profile resume acme
```

Optional account setup with the offline fake adapter:

```bash
uv run heraldesk account add acme --platform fake --handle @acme -c token=demo
uv run heraldesk account test acme
```

## Daily workflow

```bash
# Draft content. --dry-run writes nothing.
uv run heraldesk run daily acme

# Review, approve, reject or edit.
uv run heraldesk queue list acme
uv run heraldesk queue show <id>          # full draft before deciding
uv run heraldesk queue approve <id>
uv run heraldesk queue reject <id> --reason "off voice"
uv run heraldesk queue edit <id> --body "Replacement text"

# Publish one item now, or run the due scheduler sweep.
uv run heraldesk publish <id>
uv run heraldesk scheduler run

# Run the API, scheduler and Telegram bot in one process.
uv run heraldesk serve
```

Approvals, publishing and replies obey the global kill switch, the per-profile kill
switch and the profile status. Automatic publishing is refused inside `quiet_hours` and
after `max_posts_per_day` has been reached, both in the profile timezone. An explicit
`heraldesk publish` overrides those two limits and records a `publish.policy_override`
audit entry. Manual inbox replies are blocked by the kill switches and profile status;
quiet hours apply to automatic replies only. `heraldesk stop-all` halts everything;
`heraldesk stop-all --off` clears the global switch. Stop-all does not cancel items that
are already scheduled; use the queue commands and review scheduled items before clearing it.

## Telegram

Create a bot with [@BotFather](https://t.me/BotFather), then store the token through a
hidden prompt so it never enters shell history:

```bash
uv run heraldesk telegram token set
uv run heraldesk telegram token status
```

Enable Telegram in `heraldesk.yaml` and allowlist your chat id:

```yaml
telegram:
  enabled: true
  mode: polling
  allowed_chat_ids: [5267528661]
```

Open a chat with the bot and send `/start` once; a bot cannot start a conversation with
you. Then verify the token and send a real test:

```bash
uv run heraldesk doctor --online            # telegram api calls getMe
uv run heraldesk telegram send --real acme "manual test"
uv run heraldesk serve                      # polling, commands and notifications
```

`telegram send` is an offline preview through the fake adapter unless you pass `--real`.
`telegram test` is always offline. A real send requires `telegram.enabled: true`, a stored
token, and a chat id in `telegram.allowed_chat_ids`; `--chat-id` can select another id from
that list.

Bot tokens are encrypted with `secret.key`. Only chat IDs in `telegram.allowed_chat_ids`
are served. Set `telegram.allowed_user_ids` to restrict which senders may run commands
(`/approve`, `/reject`, `/queue`).

## Nostr publishing

Nostr is the first real platform adapter: one NIP-01 **kind 1** text note per approved
content item, signed locally with BIP-340 Schnorr and broadcast to relays. It needs no
app review and no username or password.

> **Choose the right key.** A dedicated Nostr identity for Heraldesk is recommended; a
> throwaway key is best for the first test. **Your original identity is supported**, but
> Nostr events are immutable (deletion requests are advisory) and the nsec is a full
> identity key. A mistake or compromise is permanent and affects that identity.
>
> **Never paste an nsec into chat, a GitHub issue, or a screenshot.** Use an environment
> variable in your own terminal; Heraldesk encrypts it with `secret.key` and never prints it.

```bash
# Use a dedicated or throwaway key; the original identity also works.
export NOSTR_NSEC='<nsec>'
uv run heraldesk account add acme --platform nostr \
    --credential-from-env NOSTR_NSEC --label automation
unset NOSTR_NSEC

# The npub is shown masked; the nsec is never shown.
uv run heraldesk account list acme
uv run heraldesk account test acme --platform nostr

# Draft for real (a dry run writes nothing, so there is nothing to approve).
uv run heraldesk run daily acme
uv run heraldesk queue list acme
uv run heraldesk queue show <id>
uv run heraldesk queue approve <id>
uv run heraldesk publish <id>
```

Relays come from `nostr.relays` in `heraldesk.yaml` (defaults: `wss://relay.damus.io`,
`wss://nos.lol`) or from `--relay` on the account. One relay accepting the event is
enough; failures use the normal publisher retries. Details and the manual acceptance
steps are in [docs/platforms/nostr.md](docs/platforms/nostr.md).

## Threads publishing

The Threads adapter publishes one **text** thread through the official Meta Graph API:
create a container, wait for it, publish, then read the permalink. It needs a Threads
user access token and (optionally) the user id.

```bash
# Read the token from the environment; never put it on the command line.
export THREADS_TOKEN='<long-lived Threads user access token>'
export THREADS_USER_ID='<threads user id>'

uv run heraldesk account add acme --platform threads --handle @acme \
    --credential-from-env THREADS_TOKEN --credential-from-env THREADS_USER_ID
uv run heraldesk account test acme --platform threads   # calls GET /me
```

A long-lived token lasts 60 days. Refresh it with
`heraldesk account refresh acme --platform threads`; the new token and expiry are encrypted
and `doctor` warns when expiry is within 7 days. Text is limited to 500 UTF-8 bytes and the platform allows 250 posts per
rolling 24 hours. CI mocks every HTTP call; the manual acceptance steps and sources are in
[docs/platforms/threads.md](docs/platforms/threads.md).

## Web UI and API

M11 adds a server-rendered Web UI and a JSON API for daily operation without the terminal.
The CLI remains fully supported. One process still serves everything.

```bash
# Set the single admin token in $HERALDESK_HOME/.env, or use the one derived from secret.key.
# HERALDESK_ADMIN_TOKEN=change-me-to-a-long-random-token

uv run heraldesk admin token          # source + fingerprint only
uv run heraldesk serve                # http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000/login` and enter the token. The UI has a dashboard, approval
queue with full-draft detail, edit/publish controls, content calendar, inbox triage, report
viewer, LLM cost page, audit log, profile list, global kill-switch control and a validated
profile/policy editor. HTMX and CSS are vendored;
there is no Node build and no external CDN.

The API uses `Authorization: Bearer <token>` only. `GET /api/v1/queue/{approval_id}`
returns the full redacted draft so scripts can inspect content before deciding:

```bash
curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/status

curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/queue?status=pending

curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/audit?limit=20

curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/cost

curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/profiles

curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  "http://127.0.0.1:8000/api/v1/runs?profile=acme"

curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  "http://127.0.0.1:8000/api/v1/accounts?profile=acme"

# Pagination and filters
curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  "http://127.0.0.1:8000/api/v1/queue?status=pending&profile=acme&limit=20&offset=0"
curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  "http://127.0.0.1:8000/api/v1/audit?action=auth.login&limit=50&offset=0"

# Authenticated machine-readable contract
curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/openapi.json
```

`/health` is public liveness/schema readiness and returns 503 when the database is behind the packaged migration. `/api/v1`, `/status` and every UI page require auth. API Bearer failures are limited to 10 per minute per client and return `429` with `Retry-After`.
Tokens in a URL query string are rejected. Login is rate limited to five failed attempts
per minute per IP.

Server logs rotate in `$HERALDESK_HOME/logs/heraldesk.log` (5 MiB × 3, mode 600) with
secret redaction. `GET /api/v1/ready` reports schema, scheduler and Telegram readiness.

`heraldesk admin rotate-key` creates a credential backup, re-encrypts every account and the persisted Telegram token, revokes sessions, swaps `secret.key` and verifies the result. It reads the token file directly, so an environment or `.env` override is never copied over the persisted token. Session revocation failure aborts before credentials change. Stop `heraldesk serve` first. `heraldesk admin logout-all` rotates the session epoch and invalidates every signed-in browser; the dashboard has the same action. `heraldesk admin token --show` prints the raw token for your own terminal. Never put it in
chat, a GitHub issue, a screenshot or a shell history you do not control.

### VPS hardening (Caddy + HTTPS)

The recommended deployment keeps Heraldesk on loopback and puts a TLS reverse proxy in
front of it:

```bash
# $HERALDESK_HOME/.env
HERALDESK_ADMIN_TOKEN=<long-random-secret>
HERALDESK_TRUST_PROXY=1
```

```caddyfile
desk.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Then run `uv run heraldesk serve` without `--host`. Proxy mode makes the session cookie
`Secure`; the admin token is still required.

A non-loopback bind is refused unless all three conditions hold: `--allow-remote`, an
explicit `HERALDESK_ADMIN_TOKEN`, and `HERALDESK_TRUST_PROXY=1` behind an HTTPS proxy.
Without proxy mode the server refuses with "Remote mode requires HTTPS via reverse proxy".

The default serve port is `8000`; if you change it, use the same value in the reverse
proxy. **Do not expose the Heraldesk port directly to the internet.** Firewall it and let
only the reverse proxy reach it. Set `HERALDESK_TELEGRAM_WEBHOOK_SECRET` whenever
Telegram is enabled on a remote or webhook-mode server; the server refuses to start
without it so forged `/approve` commands cannot approve content.

## Inbox triage

The inbox is manual by design. A human reads each stored message and either replies or
ignores it. There is no auto-reply, sentiment moderation or AI-generated reply.

```bash
uv run heraldesk inbox list
uv run heraldesk inbox list --status handled --limit 50
uv run heraldesk inbox show <id>
uv run heraldesk inbox reply <id> --text "Thanks for the question."
uv run heraldesk inbox ignore <id> --reason spam

# Delete handled/ignored messages older than 30 days (pending is kept).
# Set telegram.retention_days in heraldesk.yaml for a daily 03:15 UTC job.
uv run heraldesk inbox prune --days 30
uv run heraldesk inbox prune --days 30 --dry-run
```

Replies use the configured Telegram adapter and require `telegram.enabled` plus a bot
token. Tests use the fake adapter. Reply text is redacted before it is sent, stored or
written to the audit log.

## Reports

```bash
uv run heraldesk report weekly
uv run heraldesk report weekly --week 2026-W47
uv run heraldesk report monthly
uv run heraldesk report monthly --month 2026-11
```

Reports are deterministic Markdown files in `$HERALDESK_HOME/reports/`. They contain
period totals, approval rate, cost, inbox counts, publish failures and the top published
posts. The same database state produces the same file. A period with no data still
produces a report with zeros.

## Backup and restore

Backups are private ZIP archives. By default they contain:

```text
manifest.json
heraldesk.db          # consistent SQLite online-backup snapshot
heraldesk.yaml
profiles/**
agents/**             # user prompt overrides
```

They never contain `.env`, logs, generated reports or previous backups. `secret.key` and
the encrypted `telegram.token` are included only when you ask for credentials:

```bash
# Private archive in $HERALDESK_HOME/backups/
uv run heraldesk backup

# Credential backup needed for a full database restore
uv run heraldesk backup --with-credentials --output /secure/heraldesk.zip
```

`heraldesk backup-prune --keep 10` deletes older `heraldesk-backup-*.zip` archives;
`--dry-run` lists them first. Restore refuses while `heraldesk serve` holds `server.lock`.

Restore semantics are intentionally strict:

- A backup with `secret.key` can restore the database, config, profiles, agents and key.
- A backup without `secret.key` cannot decrypt social credentials. Restore fails with the
  documented message unless you pass `--without-credentials`.
- `--without-credentials` restores **config, profiles and agents only**. It does **not**
  restore `heraldesk.db` and does **not** restore `secret.key`. Run `heraldesk init`
  afterwards to create a new database and reload the profiles.
- `--force` replaces existing data in the target folder. Before replacing anything it
  writes a credential safety backup into `backups/`. Without `--force`, restore refuses
  to overwrite an initialised instance.
- Restoring a database applies pending Alembic migrations before the server starts and
  rotates the session epoch, invalidating old browser cookies.

```bash
# Full restore from a credential backup
uv run heraldesk restore /secure/heraldesk.zip --force

# Files-only restore into a fresh instance
uv run heraldesk restore /secure/heraldesk-no-key.zip --without-credentials
uv run heraldesk init
uv run heraldesk profile reload
```

Upgrade procedure: install the new version, then run `heraldesk upgrade`. It creates a
credential backup first, applies Alembic migrations and audits the change. Run
`heraldesk doctor` afterwards. `heraldesk serve` refuses to start on a stale schema.

See [deploy/README.md](deploy/README.md) for Docker, systemd, Caddy and the VPS hardening
checklist.

## Doctor

`heraldesk doctor` is offline by default. It checks:

- Python version and the data folder being writable;
- required folders and `secret.key` permissions;
- SQLite WAL mode and the Alembic revision;
- whether the database migration is up to date;
- `heraldesk.yaml` validity and the `.env` permissions if present;
- a resolvable Telegram token whenever `telegram.enabled` is true;
- the reports directory, or that it can be created;
- loopback-only server binding.

Only FAIL changes the exit code to 1. `--online` adds three explicit network probes:

- the LLM provider, using one minimal completion when a real LiteLLM model is configured
  (the fake provider makes no call);
- the Telegram Bot API's `getMe`, when `telegram.enabled` is true and a token is present;
- one Nostr relay WebSocket handshake, only when at least one Nostr account exists. An
  unreachable relay is a WARN, not a failure.

Online probes never print an API key, bot token or nsec.

## Security notes

- `heraldesk.yaml` contains no credentials. LLM keys live in `.env` or the environment.
- Social account credentials are encrypted at rest with `secret.key` (mode 600).
- Rotate it with `heraldesk admin rotate-key`; a credential backup is mandatory and the old key remains the restore source if the swap is interrupted.
- Backups exclude `.env`; they include `telegram.token` with `--with-credentials`.
  Protect credential backups like the original data folder.
- The server binds to `127.0.0.1` by default. A non-loopback bind requires
  `--allow-remote`, an explicit `HERALDESK_ADMIN_TOKEN` of at least 32 characters and
  `HERALDESK_TRUST_PROXY=1` behind an HTTPS reverse proxy.
- Audit payloads, reports and CLI output redact credential-shaped values. Full inbox and
  content text is never written to the audit log.
- No telemetry and no phone-home. Outbound traffic is limited to your configured LLM
  provider, platform APIs, Telegram and Researcher web search.
- Heraldesk is for accounts you own or are authorized to manage. No fake-account networks,
  impersonation, spam or platform-rule evasion. You are responsible for platform terms and
  local law.

## Documentation

- [Architecture](docs/architecture.md)
- [LLM providers](docs/providers.md)
- [Decisions and milestone notes](docs/decisions.md)
- [Roadmap](ROADMAP.md)
- [Releasing](docs/releasing.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Deployment](deploy/README.md)
- [Changelog](CHANGELOG.md)
- Bahasa Indonesia: [docs/id/arsitektur.md](docs/id/arsitektur.md)

## License

MIT. See [LICENSE](LICENSE).
