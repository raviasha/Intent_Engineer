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
