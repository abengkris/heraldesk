# Contributing to Heraldesk

Thanks for helping. Humans and AI-assisted contributions are both welcome, as long as you understand
and can explain what you submit.

## Setup
```bash
git clone <your fork>
cd heraldesk
uv sync
uv run pytest
```

## Workflow
1. Open or pick an issue. For big changes, discuss first.
2. Create a branch: `feat/short-name` or `fix/short-name`.
3. Write tests with your change. Tests must pass offline.
4. Run `uv run ruff check . && uv run ruff format . && uv run mypy src/heraldesk && uv run pytest`.
5. Open a pull request using Conventional Commit style titles.

## Rules
- Follow `AGENTS.md` (principles, conventions, testing rules). It applies to humans too.
- Never commit secrets, real account data or personal data.
- New platform adapters must use official APIs and pass the adapter contract tests.
- No telemetry, no new runtime services.

## Time-dependent tests
A test may use the real clock. It must not assume where the real clock sits in a week, a
month or a TTL window, and it must not compare a hardcoded date against a code path that
reads the clock itself. Seed from `tests/conftest.py:fresh_now()`, pass `now=` on both sides
of a call, or pin the query (for example `/calendar?week=YYYY-MM-DD`).

`main` has already gone red twice this way: approval seeds aged past the 72-hour TTL, and a
calendar item moved into the next ISO week. Run the suite under a shifted clock before
blaming a diff:

```bash
FAKETIME_DONT_FAKE_MONOTONIC=1 faketime '+10 days' uv run pytest -q
```

The `Clock drift` workflow does this weekly at `+3`, `+10`, `-3` and `-10` days.

## Acceptable use
We will not accept features whose main purpose is fake-account networks, impersonation, spam or
evading platform rules.

## Code of conduct
Be respectful and constructive. Maintainers may remove content or contributors that are abusive.
