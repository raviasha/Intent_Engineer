# Task 9 report — local CLI and `.intent` workspace

## Summary

Implemented the complete local Typer CLI: `init`, `validate`, `ingest`, `sync`,
`drift`, `status`, `explain`, `context`, `reconcile list/show/resolve`,
`render`, and `doctor`. The CLI assembles reviewed YAML/JSONL production stores,
the real Markdown and Git connectors, `SyncOrchestrator`, `ContextProvider`,
case lifecycle service, and safe `GraphRenderer`.

`init` creates the local config, graph, evidence, reconciliation, history,
approvals, and cache paths without overwriting an existing target unless
`--force` is supplied. JSON output is deterministic and versioned, and the CLI
routes operational logs to stderr. Reconciliation resolution builds, validates,
and applies a `ChangeSet` before persisting the lifecycle transition.

## Files

- `src/intent_engineering/cli/app.py`
- `src/intent_engineering/cli/output.py`
- `src/intent_engineering/cli/runtime.py`
- `src/intent_engineering/core/policy/__init__.py`
- `src/intent_engineering/core/policy/project.py`
- `tests/e2e/test_cli_local.py`
- `tests/helpers/cli.py`

## Decisions

- The checked-in stores remain canonical: graph YAML plus evidence, cases, and
  history JSONL. CLI code does not introduce a parallel persistence format.
- `ingest` is the Markdown-only sync convenience command; `sync` defaults to
  Markdown plus Git. An unavailable selected source becomes an isolated sync
  failure so a successful peer yields exit code 3.
- `drift` projects durable nonterminal reconciliation cases. It returns exit
  code 4 whenever cases require review, or when `--require-review` is explicit.
- `render` uses the Task 8 safe renderer and writes only non-canonical generated
  cache views by default.

## TDD evidence

RED command:

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py -v
```

Result: 20 failures, each caused by the expected unregistered CLI commands
(for example, `No such command 'init'`, exit code 2).

GREEN command:

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py -v
```

Result: final focused run: `21 passed in 6.61s` using the installed `intent`
subprocess.

Follow-up RED command:

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py::test_corrupt_graph_is_a_redacted_runtime_failure -v
```

Result: failed as intended because `status` leaked the YAML parser traceback.
After routing command-level graph reads through the CLI redaction boundary, the
same command passed (`1 passed in 0.56s`).

## Verification

```text
.venv/bin/intent --help
.venv/bin/intent reconcile --help
```

Result: both commands exited 0 and listed all documented command surfaces.

```text
.venv/bin/ruff check src/intent_engineering/cli src/intent_engineering/core/policy tests/e2e/test_cli_local.py tests/helpers/cli.py
.venv/bin/mypy --strict src/intent_engineering/cli src/intent_engineering/core/policy
```

Result: Ruff reported `All checks passed!`; mypy reported `Success: no issues
found in 6 source files`.

```text
.venv/bin/python -m pytest
```

Result: final run: `206 passed in 7.64s`.

## Commit

Product and tests: `4983a6a7502e4da7ff282af2c65540d570add6c8`

Follow-up redaction regression: `a9a2b21b59ca047ead01955b3e0ca5dc51d16f09`.

## Risks and deviations

- The existing sync detector boundary has no generic projection from arbitrary
  captured evidence to `DetectionInput`; therefore `drift` reports durable cases
  already produced through the reviewed service rather than inventing a second
  detector path in the CLI.
- `--force` intentionally recreates only known `.intent` file targets and never
  writes credentials or secrets. It does not delete unknown workspace contents.

## Fix round 1 — security and workflow hardening

Commit: `0839fa341df467e41acd5902aa30d24bb8f927e3`

### Changes

- Replaced path-based initialization with no-follow directory-FD traversal and
  writes. A complete valid workspace is byte-preserving on re-init; force only
  repairs a state-empty inconsistent workspace.
- Added fail-closed local actor authorization for evidence, case, node, status,
  explain, context, and reconciliation projections. Unreadable references are
  treated as absent.
- Added deterministic Markdown `intent_engineering` front-matter detection
  projection (including top-level normalized input compatibility) to the Task 5
  detector and durable case store.
- Added locked local resolution service that validates lifecycle/evidence/action
  before applying graph/history/case work and restores exact bytes on failure.
- Added deep structured doctor diagnostics, pre-AnyIO source validation, and a
  minimal scrubbed CLI subprocess environment.

### RED/GREEN evidence

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py::test_complete_init_is_idempotent_and_byte_preserving tests/e2e/test_cli_local.py::test_invalid_source_selection_is_usage_error_without_writes -v
```

RED: 2 failed (second init returned 1; duplicate sources returned 1). GREEN:
2 passed in 0.89s.

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py::test_markdown_sync_produces_a_deterministic_reconciliation_case -v
```

RED: no durable case was created. GREEN: 1 passed in 0.66s.

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py::test_acl_protected_evidence_is_indistinguishable_from_unknown -v
```

RED: ACL evidence was returned by `explain`. GREEN: 1 passed in 1.19s.

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py::test_reconcile_resolve_refuses_missing_case_evidence_without_graph_mutation -v
```

RED: resolution completed with missing evidence and mutated the graph. GREEN:
1 passed in 0.41s.

```text
.venv/bin/python -m pytest tests/e2e/test_cli_local.py::test_doctor_reports_redacted_structured_diagnostics_for_corrupt_evidence -v
```

RED: doctor emitted a generic stderr failure. GREEN: 1 passed in 0.50s.

### Final verification

```text
.venv/bin/ruff check src/intent_engineering/cli src/intent_engineering/core/policy src/intent_engineering/reconcile tests/e2e/test_cli_local.py tests/helpers/cli.py
.venv/bin/mypy --strict src/intent_engineering/cli src/intent_engineering/core/policy src/intent_engineering/reconcile
.venv/bin/python -m pytest
.venv/bin/intent --help
.venv/bin/intent reconcile --help
```

Results: Ruff clean; strict mypy clean across 12 modules; full pytest `213
passed in 10.14s`; both help commands exited 0.

Follow-up redaction boundary: `b5c534199b5f32e42427a97d53d65efd09fe85f5`.
Fresh final verification after that commit: focused E2E `28 passed in 8.94s`,
Ruff clean, strict mypy clean across 12 modules, full pytest `213 passed in
10.04s`, and both help commands exited 0.
