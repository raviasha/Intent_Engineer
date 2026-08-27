### Task 3: Build PRD bootstrap and hybrid baseline activation

**Base:** `7e08c7e`

**Files:**
- Create: `src/intent_engineering/intent_workflow/bootstrap.py`
- Modify: `src/intent_engineering/intent_workflow/__init__.py`
- Modify only if the existing interface truly requires it: `src/intent_engineering/storage/executor.py`
- Test: `tests/integration/intent_workflow/test_bootstrap.py`
- Test fixture: `tests/fixtures/intent_workflow/existing_project/docs/prd.md`

**Interfaces:**
- Consumes: Task 1 workflow models and source roles; Task 2 exact held proposal store; existing
  `EvidenceRecord`, `Graph`, `Node`, `Edge`, `ChangeSet`, `LocalChangeSetExecutor`, ACL projection,
  and the shared transaction coordinator.
- Produces: strict/frozen `BootstrapSubmission`, detached `BootstrapReview`, and
  `BootstrapService.propose`, `.review`, and `.activate`.

## Required behavior

1. Write the ordinary-PRD integration tests first and witness collection/functionality RED before
   production edits.
2. `propose` consumes only an agent-supplied typed candidate submission over already captured,
   immutable evidence. It must never parse prose semantically or invent graph assertions itself.
3. Validate an exact current baseline graph version; UTC actor/timestamp; configured source-role
   assignments; authorized, in-scope evidence; registered node/edge types; unique semantic IDs;
   inferred source mode; evidence on every candidate; edge endpoints within the candidate/current
   graph scope; disjoint and exhaustive core/provisional classifications; and public graph/
   ChangeSet invariants. All collections must use explicit bounds from the plan.
4. The candidate ChangeSet is validation-only during proposal. Store one content-addressed proposal
   outside canonical graph state and return a detached review. Replaying the identical submission is
   byte-identical and creates no graph/history change. Invalid or unauthorized submissions store
   nothing.
5. The hybrid review exposes a compact core foundation separately from lower-confidence provisional
   detail. Every candidate retains exact evidence/authorship/source-role/source-mode/confidence
   provenance. Proposal creation leaves graph version 0 unchanged.
6. `activate` requires an authorized configured contributor, the exact proposal/digest/baseline,
   an exact confirmed subset of core IDs only, no unresolved conflict/destructive marker, and a fresh
   proposal decision. It rebuilds a subset ChangeSet while preserving original creator/time,
   evidence, source mode, and confidence history.
7. Decision append and graph/history mutation must share the existing recovery/transaction domain;
   no torn decision/activation may be observable. Stale graph, replay, rejection, cancellation, and
   transaction failure must preserve exact prior ledger/graph/history bytes.
8. Provisional candidates remain only in the proposal ledger until a later governed confirmation.
   The bootstrap service never writes external systems and never manufactures human approval.
9. Public failures are fixed/context-free and retain no PRD content, candidate text, evidence body,
   actor, or proposal data in repository traceback locals. Avoid arbitrary mapping/model protocol
   execution; round-trip through strict detached models at public boundaries.

## Required test matrix

- Ordinary Markdown evidence becomes a review containing at least product intent, desired outcome,
  requirement, and constraint core types plus provisional detail; graph remains version 0.
- Activating confirmed core creates exactly one graph version and leaves provisional IDs inactive.
- Identical propose and activation replays are semantic no-ops with exact bytes.
- Reject stale baseline, missing/unauthorized/foreign evidence, role mismatch, duplicate IDs,
  core/provisional overlap or omission, unregistered types, orphan/out-of-scope edges, non-inferred
  nodes, candidate graph invariant failure, invalid confidence/provenance, and oversize inputs.
- Reject unauthorized contributor, stale activation, provisional confirmation, empty/foreign core
  selection, unresolved conflict/destructive proposal, second/conflicting decision, and activation
  after graph drift.
- Inject failure/cancellation before and during the shared commit and prove exact rollback/recovery,
  signal identity, fixed error context, and traceback-local secrecy.
- Use real stores/executor/runtime composition where boundaries matter; do not satisfy the contract
  solely with mocks or helper-only tests.

## Gates

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_bootstrap.py tests/integration/test_changeset_executor.py tests/unit/validation/test_service.py -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/bootstrap.py tests/integration/intent_workflow/test_bootstrap.py
.venv/bin/mypy src
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
```

Write `task-3-report.md`, update `progress.md`, and commit only after all gates and self-review pass.
Commit message: `feat: bootstrap intent from reviewed PRD proposals`.
