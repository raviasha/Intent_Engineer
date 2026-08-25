# Task 1 report: safe local GitHub credential resolution

## Commits

- Base commit: `89c9741ce284adef93172648f85e9a0ccb8ba72c`
- Implementation head: `6477a7cefa050b0ae98bb93589d65322def04fb2`
- Implementation commit: `6477a7c` — `feat(github): resolve local credentials safely`
- This report is committed separately after the product and test commit.

## RED evidence

Focused command, run before any GitHub production files existed:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  tests/unit/capture/github/test_auth.py -q
```

Observed: `14 failed`. Every case failed with the expected
`ModuleNotFoundError: No module named 'intent_engineering.capture.github'` from inside the
test body, proving the missing credential boundary rather than a collection/configuration error.
The token-shaped sentinel is assembled only at runtime and did not appear in the RED output.

## GREEN and verification evidence

Required focused gate:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  tests/unit/capture/github/test_auth.py -q
```

Fresh final result: `14 passed in 0.15s`.

Tracked Python lint:

```bash
git ls-files -z -- '*.py' | xargs -0 .venv/bin/ruff check
```

Result: `All checks passed!`.

Type checking:

```bash
.venv/bin/mypy src/intent_engineering
```

Result: `Success: no issues found in 69 source files`.

Packaging/import test:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/unit/test_package.py -q
```

Result: `1 passed in 0.21s`. A separate installed-package smoke import resolved
`intent_engineering.capture.github.GitHubCredentials` successfully.

Full regression suite:

```bash
.venv/bin/python -m pytest -q
```

Fresh final result: `374 passed in 17.16s`.

`git diff --cached --check` also completed with no output before the implementation commit.

## Dependency installation

The Python 3.12 editable development environment was reinstalled with:

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

The install completed successfully. Exact installed versions relevant to this task:

- `intent-engineering==0.1.0` (editable)
- `httpx==0.28.1`
- `pytest-httpx==0.36.2`

No live GitHub credential was read and no GitHub or other HTTP API request was made.

## Implemented boundary

- `GH_TOKEN` is read from the supplied read-only mapping, stripped, and preferred when nonblank.
- Missing or blank `GH_TOKEN` invokes the injected runner exactly once with
  `["gh", "auth", "token"]`.
- The production runner uses `subprocess.run` with `check=True`, `capture_output=True`,
  `text=True`, and `shell=False`, returning stripped stdout only after success.
- Missing CLI, nonzero exit, blank output, non-string environment values, mapping failures, and
  runner failures converge on one fixed actionable `GitHubAuthError`.
- Provider exceptions are replaced outside their active exception handlers, so public errors have
  neither a cause nor a retained context containing stdout, stderr, or environment material.
- `GitHubCredentials` is frozen and strict. Its `SecretStr` token is excluded from dumps and repr;
  only the public `CredentialSource` is serialized.

## Changed files

- `pyproject.toml`
- `src/intent_engineering/capture/github/__init__.py`
- `src/intent_engineering/capture/github/auth.py`
- `src/intent_engineering/capture/github/errors.py`
- `tests/unit/capture/github/test_auth.py`

## Preserved local artifacts

The following five pre-existing untracked files remained untracked and were excluded from both
commits:

- `.coverage 2`
- `.coverage 3`
- `.coverage 4`
- `README 2.md`
- `src/intent_engineering/core/policy/dogfood 2.py`

## Remaining concerns

- This task intentionally stops at credential discovery. It adds no HTTP request, GitHub response
  model, repository configuration, connector discovery, or CLI surface.
- The injected mapping and runner are provider boundaries and may raise arbitrary exceptions. The
  implementation deliberately catches broadly at those two points and replaces failures with the
  fixed redacted error; the narrow Ruff suppressions document that security choice.
- A timeout was not added because the required contract permits but does not require one. A later
  operational hardening task can add a bounded timeout without changing credential precedence or
  the public error contract.
