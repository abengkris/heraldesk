# Examples

## `acme/` profile

A complete, safe profile you can copy or point `HERALDESK_HOME` at:

```bash
export HERALDESK_HOME="$(mktemp -d)"
uv run heraldesk init
cp -R examples/acme "$HERALDESK_HOME/profiles/"
uv run heraldesk profile reload acme
uv run heraldesk profile resume acme
uv run heraldesk run daily acme --dry-run
```

`policy.yaml` is `draft_only`, so nothing publishes without a human. Replace the brief and
voice sample before using it for a real account.

## `plugin/` adapter template

A minimal third-party adapter package that declares the `heraldesk.platforms` entry point:

```bash
uv pip install -e examples/plugin
uv run heraldesk plugins list --check
uv run heraldesk account add acme --platform example --handle @demo \
    --credential-from-env EXAMPLE_TOKEN
```

The adapter accepts a non-empty token and returns a fake external id. Replace `publish()`
with your official platform API and write `docs/platforms/<name>.md` before relying on it.
