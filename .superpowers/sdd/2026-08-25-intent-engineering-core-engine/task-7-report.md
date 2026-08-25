# Task 7 report — idempotent local sync orchestration

## Summary

Implemented provider-neutral extraction and deterministic fixture reasoning, immutable
sync outcomes, and per-connector durable orchestration. A connector now advances its
checkpoint only after evidence storage, ChangeSet validation/application, and derived
case persistence complete. Connector failures are isolated, summarized with the fixed
redaction `connector failed`, and do not prevent later connectors from running.

## Files

- `src/intent_engineering/extract/__init__.py`
- `src/intent_engineering/extract/base.py`
- `src/intent_engineering/extract/deterministic.py`
- `src/intent_engineering/sync/__init__.py`
- `src/intent_engineering/sync/models.py`
- `src/intent_engineering/sync/orchestrator.py`
- `tests/integration/sync/__init__.py`
- `tests/integration/sync/conftest.py`
- `tests/integration/sync/test_idempotency.py`
- `tests/integration/sync/test_partial_failure.py`

## Design decisions

- `SemanticReasoner` consumes the existing `EvidenceDelta` and produces the existing
  `CandidateAssertion` and `ChangeSet` types. `DeterministicReasoner` maps only an
  explicit fixture `subject_id`; already-present subjects produce a valid empty
  ChangeSet.
- `SyncOrchestrator` uses `checkpoint_after_discovery(..., prior=prior)`. It does not
  write an unchanged cursor, preserving the complete prior checkpoint on a repeated
  identical run while retaining Task 6's empty-batch cursor behavior.
- Graph application is skipped for an empty ChangeSet so a no-op sync cannot bump the
  graph version. Case persistence uses Task 5's `DriftObservation` and
  `ReconciliationCase` fingerprint vocabulary; an application can supply a detector
  callable over `(EvidenceDelta, Graph)`.
- The built-in default detector yields no observations because Task 5's `detect_drift`
  requires a fully formed `DetectionInput`, which source-agnostic sync cannot derive
  without application semantic mapping.

## Verification

RED, after adding the integration harnesses before production modules:

```console
$ .venv/bin/pytest tests/integration/sync -v
ImportError while loading tests/integration/sync/conftest.py
ModuleNotFoundError: No module named 'intent_engineering.extract'
```

The first implementation run exposed and then fixed an awaited generator-expression
bug (`TypeError: 'async_generator' object is not iterable`). The focused GREEN command
then passed:

```console
$ .venv/bin/pytest tests/integration/sync -v
2 passed in 0.09s
```

Required unit/contract/integration command:

```console
$ .venv/bin/pytest tests/unit tests/contract tests/integration -v
133 passed in 1.08s
```

Static checks (Ruff initially reported two import-order violations, fixed by Ruff):

```console
$ .venv/bin/ruff check src/intent_engineering/extract src/intent_engineering/sync tests/integration/sync
All checks passed!

$ .venv/bin/mypy --strict src/intent_engineering/extract/base.py src/intent_engineering/extract/deterministic.py src/intent_engineering/sync/models.py src/intent_engineering/sync/orchestrator.py
Success: no issues found in 4 source files
```

Full suite:

```console
$ .venv/bin/pytest -v
133 passed in 1.12s
```

## Commits

- Product and integration tests: `27940df5d7ffcbd5884502a8d653adab4006408d`
- This report: recorded in the follow-up documentation commit.

## Risks and deviations

- `EvidenceDelta.prior_versions` remains empty, matching the approved illustrative
  orchestration contract; no existing connector/store API specifies a canonical
  previous-version mapping.
- The reasoner intentionally implements only deterministic fixture node additions.
  Refinement, edge extraction, and construction of Task 5 `DetectionInput` records
  require application-specific semantic mapping and remain outside this task.

## Fix round 1 — retry recovery and failure isolation

### Root cause and design

The original sync delta used only `EvidenceStore.put()` successes. A failure after an
evidence append therefore left its cursor uncommitted while filtering the same record
out of the retry delta. The connector `try` block also began after checkpoint lookup
and caught only `ConnectorError`, so ordinary operational exceptions escaped.

The recovery uses the committed checkpoint as the only consumption boundary: every
record rediscovered beyond it enters `EvidenceDelta.added`, while `evidence_added`
continues to count only new durable writes. Durable predecessor links are computed
from `EvidenceStore.versions()` before each put and remain correct when a retry sees
an already-written row. A per-connector progress accumulator preserves durable
evidence, graph, and case counts if a later stage fails. The full connector transaction
is guarded by `except Exception`, deliberately allowing cancellation and other
`BaseException` signals to propagate. Duplicate connector IDs are rejected before
accessing any store, and the deterministic fixture reasoner now defaults to a fixed
UTC timestamp.

### RED

```console
$ .venv/bin/pytest tests/integration/sync -v
8 failed, 3 passed in 0.13s
```

The failures proved: evidence appended before a reasoner failure was omitted on retry;
raw connector/detector/case errors escaped; prior versions were empty; duplicate IDs
were accepted; fixture ChangeSets varied by wall clock; and failed aggregate counts
were zeroed.

### GREEN and verification

```console
$ .venv/bin/pytest tests/integration/sync -v
13 passed in 0.10s

$ .venv/bin/pytest tests/unit tests/contract tests/integration -v
144 passed in 1.30s

$ .venv/bin/ruff check src/intent_engineering/extract src/intent_engineering/sync tests/integration/sync
All checks passed!

$ .venv/bin/mypy --strict src/intent_engineering/extract/base.py src/intent_engineering/extract/deterministic.py src/intent_engineering/sync/models.py src/intent_engineering/sync/orchestrator.py
Success: no issues found in 4 source files

$ .venv/bin/pytest -v
144 passed in 1.38s
```

### Fix commit

- Product and regression tests: `8cf437572f79a9e6e5531cc3406cf4b915d3a33e`

## Fix round 2 — Markdown manifest cursors

### Root cause and design

Task 6 Markdown discovery always returned its full current snapshot and its checkpoint
was a single file-content hash. The Task 7 recovery correctly treats records beyond a
committed checkpoint as pending, but that made an identical Markdown scan re-enter
reasoning and case detection. The cursor could not represent every included document.

Markdown now encodes a canonical `markdown:v1:` JSON manifest of sorted POSIX
path/content-version pairs. Discovery scans under the existing AnyIO thread boundary,
retains that complete snapshot for `next_checkpoint()`, and compares it to a valid
prior manifest to return only new or changed documents. Legacy hash (and malformed)
cursors intentionally do one safe full rescan, then migrate to the manifest. Empty
incremental Markdown scans return a manifest rather than `None`, while Git continues
to return `None`; the shared checkpoint helper therefore preserves Git cursors but
commits Markdown deletion/no-op snapshots correctly. Sync skips the semantic boundary
entirely when a connector's delta has no records.

### RED

```console
$ .venv/bin/python -m pytest tests/integration/capture/test_markdown_connector.py tests/integration/sync -v
4 failed, 18 passed in 0.14s
```

The failures showed that Markdown still returned a single hash, ignored a prior cursor,
re-invoked custom reasoners on an identical snapshot, and did not migrate a legacy
checkpoint. The direct pytest executable reproduces the carried environment-only
`ModuleNotFoundError: No module named 'tests'` for the isolated capture target, so the
module invocation was used for the focused capture-plus-sync command.

### GREEN and verification

```console
$ .venv/bin/python -m pytest tests/integration/capture/test_markdown_connector.py tests/integration/sync -v
22 passed in 0.16s

$ .venv/bin/pytest tests/unit tests/contract tests/integration -v
149 passed in 1.48s

$ .venv/bin/ruff check src/intent_engineering/capture/markdown/connector.py src/intent_engineering/capture/checkpoints.py src/intent_engineering/sync/orchestrator.py tests/integration/capture/test_markdown_connector.py tests/integration/sync
All checks passed!

$ .venv/bin/mypy --strict src/intent_engineering/capture/markdown/connector.py src/intent_engineering/capture/checkpoints.py src/intent_engineering/sync/orchestrator.py
Success: no issues found in 3 source files

$ .venv/bin/pytest -v
149 passed in 1.36s
```

### Fix commit

- Product and regression tests: `f0d2e779ca8a0b9564893b522eba018b1838ff7f`
