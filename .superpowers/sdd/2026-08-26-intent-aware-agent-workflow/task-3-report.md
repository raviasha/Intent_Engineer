# Task 3 report — PRD bootstrap and hybrid baseline activation

## Status

REVIEW APPROVED after independent-review Fix Round 1. Implementation and all required gates are
complete, and the final independent verdict is 0 Critical / 0 Important / 0 Minor: Ready.

Execution base: `7e08c7e`.

Task authority retained with this report:
`.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-3-brief.md`.

## Implementation

- Added strict/frozen `BootstrapSubmission` and detached `BootstrapReview` records with UTC,
  collection-bound, duplicate-free typed inputs. Review now exposes the exact candidate edges and
  content-addressed candidate ChangeSet alongside the core/provisional node projection.
- Added `BootstrapService.propose`, `.review`, and `.activate`. The service accepts only typed
  agent candidates over already captured evidence; it contains no prose parser or semantic
  extractor and performs no provider/external writes.
- Proposal validation binds the exact graph version, evidence ledger association, ACL principals,
  configured source role (including inherited scope and exact override precedence), candidate
  authorship/time/source mode/confidence provenance, registered types, stable identities,
  exhaustive core/provisional classification, endpoint scope, and public ChangeSet/graph
  invariants.
- Proposal records are content-addressed and stored only in the proposal ledger. Creation is an
  atomic transaction over the exact graph/evidence/proposal preimages; sequential and concurrent
  identical replay is byte-identical and canonical graph/history state remains unchanged. A
  concurrent foreign preimage fails closed.
- Activation accepts a non-empty exact subset of core IDs from an exact proposal, rechecks the
  configured contributor and evidence access, blocks provisional/foreign selections, conflicts,
  destruction, rejection, stale graph state, and conflicting second decisions, and activates only
  selected nodes and their in-scope relationships.
- Proposal decision, graph version, and ChangeSet history commit in one shared transaction.
  Schema-2 activation decisions bind the canonical confirmed-node IDs and exact activation
  ChangeSet ID; schema-1 Task 1/2 decisions retain their original model and canonical ledger bytes.
  Concurrent identical activation returns the one durable result, while a differing selection or
  mutation cannot replay. Existing-to-existing candidate edges cannot activate as an unconfirmed
  side effect. Provisional nodes remain solely in the held proposal ledger.
- Extended the local executor with a narrowly scoped, preimage-checked proposal-ledger append and
  opt-in cancellation rollback. Extended the transaction coordinator with opt-in BaseException
  rollback while retaining the established default crash-journal behavior for existing callers.
- Public failures are fixed/context-free; cancellation retains exact signal identity, and sensitive
  candidate/proposal/actor material is cleared from repository traceback locals.
- Every nested node evidence tuple is independently bounded to 10,000 entries, duplicate-free,
  authorized, and restricted to the submission evidence scope. `Edge` has no nested evidence field.

## TDD evidence

### Required RED

Before any production edit, the ordinary PRD fixture and complete bootstrap integration test were
created, then this exact command ran:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_bootstrap.py -q -W error
```

Exact result:

```text
ModuleNotFoundError: No module named 'intent_engineering.intent_workflow.bootstrap'
1 error in 0.15s
```

Additional test-first hardening witnessed focused RED/GREEN cycles for:

- atomic proposal failure/cancellation: `4 failed, 31 deselected`, then `4 passed`;
- inherited source-role scope and exact override precedence: `1 failed`, then `1 passed`;
- contributor evidence access at activation: `1 failed`, then passing with the invalid-submission
  matrix;
- cancellation after the durable committed-journal stage: `2 failed`, then `2 passed`;
- structural `CONTRADICTS`/`SUPERSEDES` markers without a declared author-conflict flag:
  `2 failed`, then `2 passed`;
- existing crash recovery compatibility after the first rollback integration: `3 failed`, then
  `4 passed` for the three original crash cases plus Task 3 cancellation.

Independent-review Fix Round 1 added all regressions before its production edits. The exact focused
command was:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_bootstrap.py -k 'review_exposes_exact or concurrent_identical or nested_node_evidence or existing_to_existing or different_confirmed_subsets or review_cancellation' -q -W error
```

Exact RED: `6 failed, 1 passed, 46 deselected in 0.97s`. Failures proved the review omitted exact
edges, concurrent proposal/activation returned the fixed failure, an existing-to-existing edge
silently activated, distinct subsets shared one decision identity, and review cancellation exposed
private proposal material. The nested 10,001-reference atomic rejection already returned the fixed
error; the service-level duplicate/bound distinction was then retained as separate 2-reference and
10,001-reference cases.

Exact focused GREEN after the cohesive fix: `7 passed, 46 deselected in 0.43s`.
Deterministic conflicting-concurrency coverage was then added; both foreign proposal and differing
activation-selection races fail closed while leaving one canonical winner (`4 passed, 52
deselected in 0.62s` for all concurrent cases).

### Focused GREEN

Final bootstrap file after Fix Round 1:

```text
56 passed in 0.67s
```

Final required Task 3/executor/validation command:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_bootstrap.py tests/integration/test_changeset_executor.py tests/unit/validation/test_service.py -q -W error
80 passed in 2.15s
```

Task 1/2 canonical-decision compatibility plus framework/startup/local-resolution recovery gate:

```text
54 passed in 0.42s
```

## Final gates

Required Ruff command:

```text
.venv/bin/ruff check src/intent_engineering/intent_workflow/bootstrap.py src/intent_engineering/intent_workflow/__init__.py src/intent_engineering/intent_workflow/models.py src/intent_engineering/intent_workflow/proposal_store.py src/intent_engineering/storage/executor.py src/intent_engineering/storage/transaction.py tests/integration/intent_workflow/test_bootstrap.py
All checks passed!
```

All changed Python paths also returned `All checks passed!`.

Type gate:

```text
.venv/bin/mypy src
Success: no issues found in 106 source files
```

Fresh full offline warnings-as-errors gate:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
1136 passed in 49.18s
```

`git diff --check` completed with no output. Framework graph and validation integration tests are
included in the expanded focused gate; no schema or dogfood graph change was required because Task
1 already introduced the approved bootstrap capability and deterministic-authority vocabulary.

## Changed files

- `src/intent_engineering/intent_workflow/bootstrap.py`
- `src/intent_engineering/intent_workflow/__init__.py`
- `src/intent_engineering/intent_workflow/models.py`
- `src/intent_engineering/intent_workflow/proposal_store.py`
- `src/intent_engineering/storage/executor.py`
- `src/intent_engineering/storage/transaction.py`
- `tests/integration/intent_workflow/test_bootstrap.py`
- `tests/fixtures/intent_workflow/existing_project/docs/prd.md`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-3-brief.md`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-3-report.md`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/progress.md`

## Self-review

- The service never interprets PRD prose; the active agent supplies all semantic candidates.
- Evidence is captured through the real Markdown connector and persisted through the real evidence
  store before proposal validation.
- Proposal creation and activation compare exact durable preimages inside the shared coordinator;
  failures, cancellations, stale versions, replays, and conflicting decisions do not leave torn
  canonical state.
- Human review and the durable schema-2 decision expose/bind the exact proposal and activation
  mutation respectively; legacy schema-1 decisions still decode, canonicalize, and replay through
  the Task 2 store unchanged.
- Activation changes node/edge lifecycle metadata while preserving original creator/time, evidence,
  source mode, confidence, confidence basis, and reassessment provenance.
- Source-role inheritance uses component boundaries and exact document assignments override parent
  defaults, preventing authority laundering through a broader inherited role.
- The generic executor default still leaves BaseException crash journals for startup recovery;
  eager rollback is explicitly enabled only by Task 3 interactive transactions.
- The five protected untracked artifacts remain unstaged and were not read, modified, renamed, or
  deleted.

## Concerns

None open.

## Independent review

Fix Round 1 re-review verdict: **Ready** with 0 Critical, 0 Important, and 0 Minor findings. The
reviewer ran 10 fix-focused tests and 6 schema-1/canonical ledger compatibility tests; both groups
passed, and the reviewer reported a clean diff check. This independently confirms the exact
review/decision mutation bindings and preserves existing schema-1 canonical ledgers.
