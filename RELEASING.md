# Releasing

Cutting a release of `agentci`. Nothing here publishes until the PyPI one-time
setup has happened; the tag is the only trigger.

## One-time setup (PyPI trusted publishing)

1. Create a PyPI project named **`agentci-py`** (`pip install sparkline` belongs
   to an unrelated product; the `agentci` name on PyPI is not this project).
2. On the PyPI project's **Publishing** page, add a trusted publisher:
   - Owner: `tejas-parjane`
   - Repository: `agentci`
   - Workflow file name: `release.yml`
3. No API token is ever needed. The `pypi` job uses `id-token: write` and
   authenticates by provenance.

## Per release

1. **Version.** Bump `__version__` in `src/agentci/__about__.py` (single source
   of truth; `pyproject.toml` reads it via `[tool.hatch.version]`). First
   release on PyPI is `0.1.1`: `v0.1.0` was tagged but never published.
2. **Changelog.** Cut `## [Unreleased]` into `## [<version>] - YYYY-MM-DD`,
   leave a fresh empty `## [Unreleased]` on top. Body follows Keep a Changelog
   and Semantic Versioning.
3. **Local gates**, from the repo root using `.venv`:
   ```powershell
   .venv\Scripts\python.exe -m pytest -q
   .venv\Scripts\python.exe -m ruff check src tests
   .venv\Scripts\python.exe -m mypy src\agentci
   .venv\Scripts\python.exe scripts\verify.py
   ```
   Add `-- -W error`-style strictness by running `verify.py` unpiped (PowerShell
   mutates `$LASTEXITCODE` otherwise).
4. **Build + inspect** (the artifact that will be published):
   ```powershell
   .venv\Scripts\python.exe -m build
   .venv\Scripts\python.exe -m twine check dist\*
   ```
   Expect `agentci_py-<version>-py3-none-any.whl` and
   `agentci_py-<version>.tar.gz`, both `PASSED`.
5. **Commit + push** `main`. Then tag:
   ```powershell
   git tag v<version>
   git push origin v<version>
   ```
   The `release.yml` workflow runs **checks → build → github-release → pypi**
   on `v*` tags. If the trusted publisher is not configured yet, the `pypi` job
   fails and GitHub Actions shows Bearer missing/401 — re-run after setup, or
   delete and re-push the tag.
6. **Smoke test on the artifact**, from a clean environment:
   ```powershell
   python -m venv fresh && fresh\Scripts\pip install "agentci_py-<version>-py3-none-any.whl[openai-agents]"
   agentci --version
   ```
   Then walk the `README.md` 60-second quickstart.

## Rollback

A published version of a PyPI project is immutable; do not `--force` a second
upload of the same version. Fix forward with a patch bump and re-run the tag
workflow. `github-release` can be deleted in the GitHub UI; the checked-in tag
is the durable record.

## Draft release bodies

`docs/releases/<version>.md` holds the GitHub Release body for each version;
paste it into the release created by the workflow.