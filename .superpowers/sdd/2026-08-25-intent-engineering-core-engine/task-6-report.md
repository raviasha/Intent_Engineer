# Task 6 report — Markdown and Git connectors

## Summary

Implemented provider-neutral asynchronous capture contracts, deterministic local
Markdown and Git connectors, and a typed checkpoint helper. Markdown uses sorted
POSIX-relative paths, `ProjectConfig.source_exclusions`, and SHA-256 content
versions. Git uses argument-list `subprocess.run` calls with `shell=False`,
oldest-first SHA discovery, and captures commit metadata plus changed paths without
storing diffs.

## Files

- `src/intent_engineering/capture/__init__.py`
- `src/intent_engineering/capture/base.py`
- `src/intent_engineering/capture/checkpoints.py`
- `src/intent_engineering/capture/markdown/__init__.py`
- `src/intent_engineering/capture/markdown/connector.py`
- `src/intent_engineering/capture/git/__init__.py`
- `src/intent_engineering/capture/git/connector.py`
- `tests/contract/capture/__init__.py`
- `tests/contract/capture/test_connector_contract.py`
- `tests/integration/capture/__init__.py`
- `tests/integration/capture/test_markdown_connector.py`
- `tests/integration/capture/test_git_connector.py`

## Design decisions

- `RawSourceObject` contains only source-neutral fields; shared
  `normalize_raw_source()` creates the existing immutable `EvidenceRecord` rather
  than duplicating evidence vocabulary.
- Evidence IDs are deterministic SHA-256 hashes of connector identity, external
  ID, external version, and content hash.
- Markdown versions are `sha256:` hashes of file bytes, so touching a file without
  changing bytes does not create a new version. Filesystem reads and stats execute
  through AnyIO thread boundaries.
- Git external object IDs are `commit:<sha>` and their external version/checkpoint
  cursor is the commit SHA. Git metadata and path lookups execute through AnyIO
  thread boundaries; no Git diff is fetched or retained.
- `checkpoint_after_discovery()` returns the existing `SyncCheckpoint` model for
  a connector's `next_checkpoint()` cursor, leaving durable CAS persistence to the
  established checkpoint store.

## TDD and verification evidence

### RED

```text
.venv/bin/pytest tests/contract/capture tests/integration/capture -v
```

Result: failed during collection with three expected
`ModuleNotFoundError: No module named 'intent_engineering.capture'` errors for the
new contract, Markdown integration, and Git integration tests. The command was
rerun after adding the checkpoint-helper regression and failed for the same missing
capture package.

### GREEN

```text
.venv/bin/pytest tests/contract/capture tests/integration/capture -v
```

Result: `2 passed in 0.24s` after implementation and the shared evidence payload
immutability expectation was corrected to tuples.

### Focused lint and typing

```text
.venv/bin/ruff check src/intent_engineering/capture tests/contract/capture tests/integration/capture
.venv/bin/mypy --strict src/intent_engineering/capture/base.py src/intent_engineering/capture/checkpoints.py src/intent_engineering/capture/markdown/connector.py src/intent_engineering/capture/git/connector.py
```

Result: `All checks passed!` and `Success: no issues found in 4 source files`.

### Full suite

```text
.venv/bin/pytest -v
```

Result: `123 passed in 0.52s`.

## Commit

Product and test changes: `27ead2bdfa82ef1e7fb6f6c49259b013a31c21c0`
(`feat: ingest markdown and git evidence`).

## Risks and deviations

- Markdown discovery rescans included Markdown files on each run; immutable
  evidence IDs and content versions make an unchanged rescan a no-op when the
  orchestrator persists evidence. A future incremental file index can optimize this
  without changing the connector contract.
- Git commit metadata is read via NUL-delimited Git output. Git commit messages
  cannot contain NUL bytes, so this preserves subject/body field boundaries.
- No diffs are stored by design; only the commit metadata and changed path list are
  captured.

## Fix round 1 — review findings

### Root causes and corrections

- `checkpoint_after_discovery()` wrote `Connector.next_checkpoint(())` directly,
  replacing a prior Git SHA with `None`. It now accepts the existing typed prior
  checkpoint and retains its cursor for an empty successful discovery batch.
- Git called `rev-list HEAD` before checking whether `HEAD` existed. It now verifies
  that the path is a work tree and treats only a missing verified `HEAD` as the
  expected empty repository result.
- Git path collection did not request per-parent merge output. It now uses
  `git diff-tree -m ... -z`; NUL-delimited paths are deduplicated and sorted, a
  documented deterministic policy that preserves unusual filename characters.
- Markdown and Git operational discovery/fetch failures escaped as native
  filesystem/subprocess exceptions. Their async public boundaries now raise a safe
  `ConnectorError` without command stderr or local filesystem details.
- Markdown used only lexical `..` validation. Discovery skips and fetch rejects
  candidates whose resolved path is outside the resolved project root.

### RED regressions

```text
.venv/bin/pytest tests/contract/capture tests/integration/capture -v
```

Result: `6 failed, 2 passed in 0.82s`. The failures reproduced the missing prior
checkpoint argument, unborn-HEAD `CalledProcessError`, absent merge paths,
unwrapped Git/Markdown discovery failures, and discovery of an external Markdown
symlink. The failure-contract tests include fetch failures; they completed after
the public-boundary fixes. The merge regression's initial direct-parent assertion
was corrected to the valid two-parent merge invariant before implementation; the
corrected behavior is asserted by its changed-path expectation.

### GREEN and quality verification

```text
.venv/bin/pytest tests/contract/capture tests/integration/capture -v
.venv/bin/ruff check src/intent_engineering/capture tests/contract/capture tests/integration/capture
.venv/bin/mypy --strict src/intent_engineering/capture/base.py src/intent_engineering/capture/checkpoints.py src/intent_engineering/capture/markdown/connector.py src/intent_engineering/capture/git/connector.py
```

Result: `8 passed in 0.80s`; `All checks passed!`; and `Success: no issues found
in 4 source files`.

### Full verification

```text
.venv/bin/pytest -v
```

Result: `129 passed in 1.13s`.

### Fix commit

`e762a7851b42a91d2e4ffba8fbb9fd49ae4f8f07` (`fix: harden local connector boundaries`).

## Fix round 2 — version mismatch boundary normalization

### Root cause and correction

- Markdown's public `fetch()` wrapper converted filesystem and decoding errors but
  not the `ValueError` raised when a file's bytes no longer matched the discovered
  SHA-256 version. Git checked a supplied object version before entering its
  protected fetch block. Both cases now return a safe, context-free
  `ConnectorError("… fetch failed")` at the connector boundary.

### RED regressions

```text
.venv/bin/pytest tests/contract/capture tests/integration/capture -v
```

Result: `2 failed, 8 passed in 0.84s`. The Markdown content-change race raised
`ValueError: Markdown object changed before fetch`, and the supplied Git version
mismatch raised `ValueError: Git object version must equal its commit SHA`.

### GREEN and quality verification

```text
.venv/bin/pytest tests/contract/capture tests/integration/capture -v
.venv/bin/ruff check src/intent_engineering/capture tests/contract/capture tests/integration/capture
.venv/bin/mypy --strict src/intent_engineering/capture/base.py src/intent_engineering/capture/checkpoints.py src/intent_engineering/capture/markdown/connector.py src/intent_engineering/capture/git/connector.py
```

Result: `10 passed in 0.80s`; `All checks passed!`; and `Success: no issues found
in 4 source files`.

### Full verification

```text
.venv/bin/pytest -v
```

Result: `131 passed in 1.16s`.

### Fix commit

`b5fae515435684123d5d9ceb83d78793ba9e01e1` (`fix: normalize connector version mismatches`).
