# Releasing Heraldesk

Heraldesk publishes from **tags only**. The workflow is
[`.github/workflows/release.yml`](../.github/workflows/release.yml); it builds the wheel and
sdist, publishes a GHCR image, creates the GitHub release, and can publish to PyPI or
TestPyPI through **trusted publishing (OIDC)**. No PyPI token is stored in the repository.

## 1. Prepare the commit

1. Update `src/heraldesk/__init__.py` (`__version__`).
2. Add a dated `CHANGELOG.md` entry and update README version links.
3. Run the full local gates:
   ```bash
   uv sync
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy src/heraldesk
   uv run pytest
   uv build
   uv run twine check dist/*.whl dist/*.tar.gz
   ```
4. Commit and tag:
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z: <summary>"
   git push origin main
   git push origin vX.Y.Z
   ```
   The tag and `__version__` must match. PyPI rejects re-uploading an existing version.

## 2. Configure TestPyPI first

Use TestPyPI to validate the trusted-publisher wiring without touching the real project.

1. Create/verify an account at <https://test.pypi.org>.
2. Add a **pending trusted publisher** at
   <https://test.pypi.org/manage/account/publishing/>:
   - PyPI project name: `heraldesk`
   - Owner: `abengkris`
   - Repository: `heraldesk`
   - Workflow: `release.yml`
   - Environment: `testpypi`
3. In GitHub, create the environment **`testpypi`** (no secrets are needed; optional
   required reviewers are fine).
4. Add a repository Actions variable **`TESTPYPI_PUBLISH=true`**
   (Settings → Secrets and variables → Actions → Variables).
5. Push a tag. The `Publish to TestPyPI` job runs in the `testpypi` environment.
6. Verify:
   ```bash
   pip install --index-url https://test.pypi.org/simple/ \
       --extra-index-url https://pypi.org/simple heraldesk==X.Y.Z
   heraldesk --version
   ```
   The extra index is required because TestPyPI does not host Heraldesk's dependencies.

## 3. Enable PyPI

1. Add a **pending trusted publisher** at
   <https://pypi.org/manage/account/publishing/> with the same owner/repository/workflow and
   environment **`pypi`**.
2. In GitHub, create the environment **`pypi`**.
3. Add the repository variable **`PYPI_PUBLISH=true`**.
4. Push the final tag. The `Publish to PyPI` job runs with `id-token: write` and
   `attestations: true`.
5. Verify:
   ```bash
   pipx install heraldesk==X.Y.Z
   uvx heraldesk --version
   heraldesk init && heraldesk doctor      # in a throwaway HERALDESK_HOME
   ```

Do not enable `TESTPYPI_PUBLISH` and `PYPI_PUBLISH` for the same final tag unless you
intend to publish to both indexes.

## 4. What the workflow publishes

- Wheel: `heraldesk-X.Y.Z-py3-none-any.whl`
- Source distribution: `heraldesk-X.Y.Z.tar.gz`
- Container image: `ghcr.io/abengkris/heraldesk:X.Y.Z` and `:latest`, with SBOM and
  provenance attestations.
- GitHub release with both Python artifacts.

The wheel contains the Web UI templates/static files, bundled prompts, `py.typed` and
`heraldesk/_alembic` migrations. CI asserts those files and installs both the wheel and the
sdist before a release tag is accepted.

## 5. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `invalid-publisher` / 403 | Owner, repository, workflow filename or environment does not match the pending publisher. |
| `File already exists` | That version was already uploaded; publish a new patch version. |
| Job waits indefinitely | The `pypi`/`testpypi` environment has required reviewers. |
| Missing dependency on TestPyPI install | Install with `--extra-index-url https://pypi.org/simple`. |
| Missing README/license warning | Run `uv run twine check dist/*` and fix `pyproject.toml`. |
| GitHub release created but PyPI skipped | `PYPI_PUBLISH` is unset or false; check repository variables. |

## 6. Rollback

PyPI versions are immutable. If a release is broken:

1. **Yank** the version on PyPI (it stays installable only by exact pin).
2. Publish a fixed patch release with a new tag.
3. Keep the GitHub release and note the yanked version in the changelog.

Never force-push or move a published tag. Never upload a rebuilt artifact under the same
version.
