# Pull request

## Summary

<!-- What changed, and why? Link the issue or discussion. -->

Closes #

## Type of change

- [ ] Bug fix
- [ ] Feature
- [ ] Test or tooling
- [ ] Documentation
- [ ] Refactor with no behaviour change
- [ ] Breaking change (explain below)

## Testing

<!-- Paste the commands you ran and their results. Tests must run offline. -->

```text
uv run pytest
uv run pytest -m core
uv run ruff check .
uv run ruff format --check .
uv run mypy src/heraldesk
uv run mypy --strict src/heraldesk/core src/heraldesk/domain
```

## Checklist

### Correctness and safety

- [ ] Tests were added or updated and pass offline with the fake LLM/provider.
- [ ] State changes go through the existing domain transitions and `ProfileScope`.
- [ ] Audit entries are written for actions that already audit, with no secrets or full content.
- [ ] Agent output remains structured (Pydantic), validated and retried on failure.
- [ ] No credentials, API keys, tokens, nsecs, chat ids, real account data or private logs are included.
- [ ] No telemetry, phone-home behaviour or new runtime service was added.
- [ ] Platform work uses official APIs only (no scraping or ToS-violating automation).
- [ ] A new dependency, if any, is justified in `docs/decisions.md`.
- [ ] Schema changes include an Alembic migration and backup/restore implications are documented.

### Scope

- [ ] One logical change; no unrelated refactors.
- [ ] `LICENSE`, `SECURITY.md` and CI release workflows were not changed unless this task requires it.
- [ ] Docs, commands in `AGENTS.md`, `docs/decisions.md` or `docs/open-questions.md` are updated where needed.
- [ ] Commit titles use Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`).

### UI (when applicable)

- [ ] Works with JS disabled; htmx is progressive enhancement only.
- [ ] CSRF is present on every write form; expired htmx sessions redirect to login.
- [ ] No inline styles, inline scripts, `on*=` handlers or external URLs.
- [ ] Checked at 360px, 768px and 1280px in light and dark mode.
- [ ] Keyboard-operable with visible focus; status is not conveyed by color alone.
- [ ] Screenshots or recordings attached below.

## Screenshots

<!-- Before/after for UI changes; write "n/a" otherwise. Remove secrets. -->

## Risks and rollback

<!-- Failure modes, migrations, operator steps, or how to revert. -->

## Reviewer notes

<!-- Anything the reviewer should focus on, or known follow-ups. -->
