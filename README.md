# Heraldesk

> Your autonomous social media department. Self-hosted, open source, single-user.

**Version 1.0.0.** Heraldesk runs a small deterministic team of AI roles (strategist,
researcher, writer, designer, community manager, analyst, compliance) that draft, review,
queue and schedule social media content for accounts you manage. You approve; Heraldesk
does the legwork. It runs on your laptop or your own VPS. Your keys, your data.

Heraldesk is CLI-first. The local web UI, REST `/api/v1` namespace, plugin loader and
real Threads/Nostr/X/Facebook/Instagram/LinkedIn adapters are not part of 1.0.0. The fake
platform adapter is the supported publishing path in this release.

## What works in 1.0.0

- Multiple profiles with their own voice, rules and autonomy policy.
- Draft-first content state machine with lint, Compliance and approval gates.
- Deterministic `daily_content` pipeline using the fake LLM provider or your own LiteLLM key.
- CLI approval queue, edits, rejection and manual publishing.
- Stateless scheduler with durable retries and at-most-once publishing.
- Telegram notifications, allowlisted commands and a stored inbound inbox.
- Manual inbox triage: list, show, reply and ignore. No auto-reply and no AI-generated replies.
- Deterministic weekly and monthly reports, with no LLM calls.
- Backup and restore, doctor hardening, global and per-profile kill switches.
- SQLite only. No Redis, Postgres, Node, telemetry or phone-home.

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/) for the recommended source install
- A Linux, macOS or Windows machine; the CLI is tested on all three

## Install from source

The 1.0.0 release is installed from the repository. PyPI publishing and Docker images are
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
switch, the profile status and `policy.yaml`. `heraldesk stop-all` halts everything;
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
