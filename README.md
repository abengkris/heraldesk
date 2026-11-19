# Heraldesk

[![CI](https://github.com/abengkris/heraldesk/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/abengkris/heraldesk/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Version](https://img.shields.io/badge/version-1.2.1-blue.svg)](https://github.com/abengkris/heraldesk/releases/tag/v1.2.1)

> Your autonomous social media department. Self-hosted, open source, single-user.

**Version 1.2.1.** Heraldesk runs a small deterministic team of AI roles (strategist,
researcher, writer, designer, community manager, analyst, compliance) that draft, review,
queue and schedule social media content for accounts you manage. You approve; Heraldesk
does the legwork. It runs on your laptop or your own VPS. Your keys, your data.

Heraldesk is CLI-first and also ships a local Web UI + JSON API since 1.2.0. The plugin
loader and real Threads/X/Facebook/Instagram/LinkedIn adapters are not part of 1.2.x. The
fake platform adapter is the offline publishing path. A real Nostr adapter was added in
1.1.0; see [Nostr publishing](#nostr-publishing).

## What works in 1.2.1

- Multiple profiles with their own voice, rules and autonomy policy.
- Draft-first content state machine with lint, Compliance and approval gates.
- Deterministic `daily_content` pipeline using the fake LLM provider or your own LiteLLM key.
- CLI approval queue, edits, rejection and manual publishing.
- Stateless scheduler with durable retries and at-most-once publishing.
- Profile quiet hours and a local-day post cap gate automatic publishing; explicit publishes override them.
- Telegram notifications, allowlisted commands and a stored inbound inbox.
- Manual inbox triage: list, show, reply and ignore. No auto-reply and no AI-generated replies.
- Deterministic weekly and monthly reports, with no LLM calls.
- Backup and restore, doctor hardening, global and per-profile kill switches.
- Real Nostr publishing (NIP-01 kind 1 notes) with a throwaway key.
- Local Web UI and authenticated `/api/v1` for queue, inbox, reports and profiles.
- SQLite only. No Redis, Postgres, Node, telemetry or phone-home.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) for the recommended source install
- A Linux, macOS or Windows machine; the CLI is tested on all three

## Install from source

The 1.2.1 release is installed from the repository. PyPI publishing and Docker images are
not part of this release.

```bash
git clone https://github.com/abengkris/heraldesk.git
cd heraldesk
uv sync
uv run heraldesk init
```

You can also install the checkout with `pipx install .` or `uvx --from . heraldesk`.

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
```

Then set `llm.default_model` in `heraldesk.yaml`, for example `deepseek/deepseek-chat`,
or leave it empty to use the offline fake provider for a first run.

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
switch, the profile status and `policy.yaml`. Automatic publishing is refused inside
`quiet_hours` and after `max_posts_per_day` has been reached, both in the profile
timezone. An explicit `heraldesk publish` overrides those two limits and records a
`publish.policy_override` audit entry. `heraldesk stop-all` halts everything;
`heraldesk stop-all --off` clears the global switch.

## Telegram

```bash
uv run heraldesk telegram test
uv run heraldesk telegram token set
uv run heraldesk telegram token status
uv run heraldesk telegram send acme "manual test"
uv run heraldesk telegram inbox list acme
```

Bot tokens are encrypted with `secret.key`. Only chat IDs in `telegram.allowed_chat_ids`
are served.

## Nostr publishing

Nostr is the first real platform adapter: one NIP-01 **kind 1** text note per approved
content item, signed locally with BIP-340 Schnorr and broadcast to relays. It needs no
app review and no username or password.

> **Use a throwaway key.** Nostr events are immutable. Once a relay accepts an event it
> cannot be deleted. Test with a throwaway key, check the event id on a public explorer,
> then abandon the key.
>
> **Never paste an nsec into chat, a GitHub issue, or a screenshot. Type it only in your own terminal via an environment variable.**
> Keep it in an environment variable or a secure prompt. Heraldesk encrypts it with
> `secret.key` and never prints it.

```bash
# In your own terminal. The nsec is read from the environment, not the command line.
export NOSTR_NSEC_THROWAY='<throwaway nsec>'
uv run heraldesk account add acme --platform nostr \
    --credential-from-env NOSTR_NSEC_THROWAY --label throwaway

# The npub is shown masked; the nsec is never shown.
uv run heraldesk account list acme
uv run heraldesk account test acme --platform nostr

# Draft, approve and publish through the existing flow.
uv run heraldesk run daily acme --dry-run
uv run heraldesk queue list acme
uv run heraldesk queue approve <id>
uv run heraldesk publish <id>
```

Relays come from `nostr.relays` in `heraldesk.yaml` (defaults: `wss://relay.damus.io`,
`wss://nos.lol`) or from `--relay` on the account. One relay accepting the event is
enough; failures use the normal publisher retries. Details and the manual acceptance
steps are in [docs/platforms/nostr.md](docs/platforms/nostr.md).

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
queue, inbox triage, report viewer and profile list. HTMX and CSS are vendored; there is no
Node build and no external CDN.

The API uses `Authorization: Bearer <token>` only:

```bash
curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/status

curl -H "Authorization: Bearer $HERALDESK_ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/queue?status=pending
```

`/health` is public liveness only. `/api/v1`, `/status` and every UI page require auth.
Tokens in a URL query string are rejected. Login is rate limited to five failed attempts
per minute per IP.

`heraldesk admin token --show` prints the raw token for your own terminal. Never put it in
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
only the reverse proxy reach it. Set `HERALDESK_TELEGRAM_WEBHOOK_SECRET` if Telegram
webhooks are used on a VPS.

## Inbox triage

The inbox is manual by design. A human reads each stored message and either replies or
ignores it. There is no auto-reply, sentiment moderation or AI-generated reply.

```bash
uv run heraldesk inbox list
uv run heraldesk inbox list --status handled --limit 50
uv run heraldesk inbox show <id>
uv run heraldesk inbox reply <id> --text "Thanks for the question."
uv run heraldesk inbox ignore <id> --reason spam
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

They never contain `.env`, logs, generated reports or previous backups. `secret.key` is
included only when you ask for it:

```bash
# Private archive in $HERALDESK_HOME/backups/
uv run heraldesk backup

# Credential backup needed for a full database restore
uv run heraldesk backup --with-credentials --output /secure/heraldesk.zip
```

Restore semantics are intentionally strict:

- A backup with `secret.key` can restore the database, config, profiles, agents and key.
- A backup without `secret.key` cannot decrypt social credentials. Restore fails with the
  documented message unless you pass `--without-credentials`.
- `--without-credentials` restores **config, profiles and agents only**. It does **not**
  restore `heraldesk.db` and does **not** restore `secret.key`. Run `heraldesk init`
  afterwards to create a new database and reload the profiles.
- `--force` replaces existing data in the target folder. Without it, restore refuses to
  overwrite an initialised instance.

```bash
# Full restore from a credential backup
uv run heraldesk restore /secure/heraldesk.zip --force

# Files-only restore into a fresh instance
uv run heraldesk restore /secure/heraldesk-no-key.zip --without-credentials
uv run heraldesk init
uv run heraldesk profile reload
```

Upgrade procedure: make a credential backup first, install the new version, run
`heraldesk init` to apply Alembic migrations, then run `heraldesk doctor`.

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

Only FAIL changes the exit code to 1. `--online` is reserved for future network checks.

## Security notes

- `heraldesk.yaml` contains no credentials. LLM keys live in `.env` or the environment.
- Social account credentials are encrypted at rest with `secret.key` (mode 600).
- Backups exclude `.env`; protect credential backups like the original data folder.
- The server binds to `127.0.0.1` by default and refuses non-loopback binds until admin
  authentication exists. Use a local reverse proxy for remote access.
- Audit payloads, reports and CLI output redact credential-shaped values. Full inbox and
  content text is never written to the audit log.
- No telemetry and no phone-home. Outbound traffic is limited to your configured LLM
  provider, platform APIs, Telegram and Researcher web search.
- Heraldesk is for accounts you own or are authorized to manage. No fake-account networks,
  impersonation, spam or platform-rule evasion. You are responsible for platform terms and
  local law.

## Documentation

- [Architecture](docs/architecture.md)
- [Decisions and milestone notes](docs/decisions.md)
- [Roadmap](ROADMAP.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)
- Bahasa Indonesia: [docs/id/arsitektur.md](docs/id/arsitektur.md)

## License

MIT. See [LICENSE](LICENSE).
