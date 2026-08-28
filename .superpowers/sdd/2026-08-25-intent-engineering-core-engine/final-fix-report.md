# Intent Engineering core — final fix-wave report

Date: 2026-08-25

Base: `ca550edba89d43160fe42916771f7550fdedbe54`

Verified product head: `d78dfc8fb3b6085085ff197391548af433a89a67`

## Outcome

All seven Important findings from `final-review-1.md` are addressed in one
cross-cutting fix wave. The final product gate passed 330 tests with 86% total
coverage, repository-wide Ruff, strict mypy, a fresh-project `intent validate`,
and both requested help commands. No network, model, cloud service, GUI, or raw
Git-object access was used.

Product commits, in application order:

1. `1a37259d9fd5f84bc333526a2a4bac035596632b` — enforce strict models and case provenance
2. `c6113c6537caeec5b1771d2c78376598dadd57ca` — root canonical I/O in held descriptors
3. `b4f17dcf77b349fa9892c3cf0d970901cc1372ad` — coordinate complete local ChangeSets
4. `f9f77ef5f114f550d6f92d4bf108a9370e70a27a` — persist partial connector fetches
5. `e4b085f317e89c309bace1bdc247c2c913c8c135` — derive drift from causal evidence
6. `601ec1d0da192ed02e30d97b5a1fc2b761f83ed0` — validate recovered canonical state deeply
7. `d78dfc8fb3b6085085ff197391548af433a89a67` — type starter graph creation date

The branch delta is 64 files, 5,057 insertions, and 846 deletions.

## Finding 1 — strict public models and case authorship

### RED

Focused regressions demonstrated that Pydantic silently accepted misspelled
canonical fields, `ReconciliationCase` could be created without authorship, and
legacy case JSONL could not satisfy a strict required field. The new strict/model,
case-store migration, and schema regressions were run before production changes;
12 focused tests were RED.

### GREEN

- Added the shared frozen `StrictModel` with `extra="forbid"` and migrated all
  public persisted/domain records to it.
- Made `ReconciliationCase.created_by` required. Detector-built cases now use
  `detector:<detector_id>` and actor-built transitions retain real actor provenance.
- Kept legacy compatibility solely at the JSONL case-store boundary: missing
  `created_by` comes from the earliest history actor, otherwise
  `detector:<detector_id>`.
- Canonical rewrites preserve known graph metadata. Starter graph `created_at`
  and `source_spec` are explicit typed Graph fields; unknown/misspelled fields
  still fail closed.
- Regenerated evidence, graph, and reconciliation schemas. All public schemas
  set `additionalProperties: false` where applicable.

Focused result: 12 new regressions GREEN; expanded strict/schema/store group:
116 passed. The later framework/schema compatibility gate passed 7 tests.

Primary files: `core/models/_base.py`, public model modules,
`storage/jsonl/case_store.py`, all three generated public schemas, and strict
model/store tests.

## Finding 2 — descriptor-rooted I/O and configured graph path

### RED

New deterministic attacks showed ordinary pathname stores and Markdown reads
could follow a final symlink/hardlink or observe a swapped parent, and that the
runtime always selected `.intent/graph.yaml` even when a contained `graph_path`
was configured. Eleven new attack/path regressions were RED before the shared
descriptor layer was wired.

### GREEN

- Added `SecureDirectory` and `SecureFile`: absolute lexical roots are traversed
  once with `O_DIRECTORY|O_NOFOLLOW`; descendants use held descriptors and
  `dir_fd`; reads verify regular kind and link count.
- Locks, reads, appends, atomic replace, rename, unlink, and parent-directory
  fsync all operate on the same held parent descriptors.
- YAML, JSONL, checkpoint, transaction, render, runtime, and Markdown discovery/
  fetch paths use the shared layer. Markdown snapshots pin the opened ancestor
  and final-file identities between discovery and fetch.
- `configured_graph_relative()` accepts only a contained relative path beneath
  held `.intent`, including the documented `.intent/...` spelling, and rejects
  absolute paths, `..`, symlinked components, hardlinks, missing/wrong kinds, and
  unsafe final components.
- Runtime, validate/doctor, status, render, and resolution share that canonical
  graph target through runtime or the validation snapshot service.

Focused result: 11 attacks GREEN; expanded store/Markdown/runtime group: 98
passed.

Primary files: `storage/secure.py`, `storage/_atomic.py`, all local stores,
`capture/markdown/connector.py`, `cli/runtime.py`, renderer/runtime path tests,
and `tests/contract/storage/test_secure_paths.py`.

## Findings 3 and 4 — crash consistency and complete ChangeSet semantics

### RED

Transaction regressions reproduced torn graph/history/case combinations, replay
after each durable stage, malformed/untrusted journals, and startup parsing before
raw recovery. ChangeSet regressions showed confidence/status and reconciliation
groups could increment a graph version without their declared effect, and a
graph-only applier accepted case groups. Resolution tests also exposed its legacy
parallel journal path and incomplete cross-file rollback.

### GREEN

- Added strict/versioned `intent.local_transaction` v1 journals with a fixed
  symbolic target set, canonical base64, SHA-256 preimage digests, duplicate-key
  rejection, exact existence restoration, and redacted recovery failures.
- Journal prepare, target writes, committed marker, and unlink all use durable
  file/parent fsync. Prepared journals restore exact bytes/existence;
  committed-stale journals retain committed effects and are only removed.
- Recovery is idempotent and occurs before graph/history/case store construction
  in runtime startup. Crash hooks prove `journal_prepared`, every target stage,
  and `journal_committed` behavior.
- Added `LocalChangeSetExecutor`, which validates complete graph and case effects,
  then commits graph/history/case bytes through one coordinator transaction.
- Confidence changes update confidence/basis/reassessment/provenance; implementation
  status changes update the typed claim status/provenance. All node and edge add,
  update, and supersede groups are materialized with stable identities and graph
  invariants revalidated.
- Contradictory groups and prior-state mismatches fail before mutation. The
  graph-only applier rejects any reconciliation group; the executor requires exact
  created/resolved case payloads. Local resolution and sync route case effects
  through the executor, and the old resolution journal is gone.
- Every successful semantic ChangeSet is preserved whole in append-only history;
  no nonempty group can be silently ignored.

Focused transaction/ChangeSet/resolution aggregate: 109 passed. The final
transaction snapshot/storage/validation group remained GREEN at 22 passed, and
the complete 330-test gate exercises all public groups.

Primary files: `storage/transaction.py`, `storage/executor.py`,
`core/graph/applier.py`, `core/models/changeset.py`, graph/case/history stores,
`reconcile/local_resolution.py`, `sync/orchestrator.py`, startup/crash tests, and
all-group executor/applier tests.

## Finding 5 — durable partial connector fetch

### RED

`test_multi_object_fetch_failure_keeps_prior_records_and_retries_full_pending_delta`
showed that a failure on object N left zero earlier records durable and a retry
could omit semantic work that was already written before the committed checkpoint.

### GREEN

- Each fetched/normalized record is put immediately and counted only when newly
  appended.
- A later fetch/normalize failure returns a structured connector failure/partial
  run, leaves records 1..N-1 durable, and never advances that connector checkpoint.
- Retry reconstructs the semantic delta from durable evidence beyond the last
  committed checkpoint, including rows appended by the failed attempt. Prior
  version links remain deterministic.
- Connector failures remain isolated; graph/case transactions stay atomic and
  checkpoint bytes remain exact until all connector semantics succeed.

Focused sync suite: 19 passed. The multi-object proof records failure counts
`1/0/0`, retry counts `2/1/0`, all three uncheckpointed records in the retry
delta, and byte-identical pre-retry checkpoint state.

Primary files: `sync/orchestrator.py`, `sync/models.py`, and sync recovery/partial
failure tests.

## Finding 6 — evidence/graph-derived causal drift

### RED

Combined-run regressions initially produced no cross-connector case, while the
old metadata detector accepted fabricated side authors/timestamps/identities and
disconnected graph references. Fixture-removal checks showed classifications did
not require concrete Git causal evidence.

### GREEN

- Added a strict v1 internal detection declaration that accepts semantic claims,
  compatibility, and version labels but treats provenance metadata as untrusted.
- `$self`, exact evidence IDs, `source:<connector>:<external-object-id>`, and
  `git-path:<path>` resolve only when a unique durable record exists in the
  successful combined run. Missing, ambiguous, overlapping, or ACL-denied refs
  fail closed.
- `EvidenceSide.evidence_refs`, `authors`, and `observed_at` are derived from the
  resolved durable records. Synthetic metadata cannot override them.
- Subject and every affected ref must exist in the final graph. Drift detection
  runs once over the combined evidence of all successful connectors after their
  graph reasoning, so Markdown requirements and Git code/test/decision commits
  participate causally.
- Case creation is one case-only ChangeSet through the transaction executor before
  checkpoints. Shared detector failure prevents all involved checkpoints.
- Fixtures now create independent deterministic Git commits for implementation,
  test, decision, and Markdown revision with independent authors/dates. The matrix
  asserts actual durable evidence IDs, connected refs, independent versions, and
  cross-author provenance.

Focused causal/sync/fixture/e2e aggregate: 93 passed. Fixture matrix: 14 passed,
including six per-classification assertions that removing Git causal evidence
produces no case.

Primary files: `reconcile/evidence_detection.py`, `cli/runtime.py`,
`sync/orchestrator.py`, fixture materializer/descriptors, combined detection and
evidence-resolution tests.

## Finding 7 — shared deep validation

### RED

The first coordinator RED was:

```text
tests/unit/storage/test_transaction.py::test_snapshot_recovers_before_returning_one_locked_cross_store_view
AttributeError: LocalTransactionCoordinator has no attribute 'snapshot'
```

The 10-test service matrix then failed collection with
`ModuleNotFoundError: intent_engineering.validation`. Three CLI REDs showed old
validate/doctor behavior: validate emitted a generic stderr runtime failure,
doctor returned string labels, and corrupt transaction content had no structured
validation path.

### GREEN

- `LocalTransactionCoordinator.snapshot()` takes a deterministic union lock over
  journal, graph, history, cases, config, evidence, and checkpoints; recovers raw
  preimages first; then returns immutable bytes plus recovery status.
- `WorkspaceValidationService` securely loads strict config, resolves the same
  contained configured graph, compares the locked config bytes against the
  resolution pre-read, and validates one stable byte snapshot after locks release.
- Strict duplicate-key YAML/JSONL parsing validates graph invariants, normalized
  evidence IDs/content hashes/source versions/parents, every graph/case/history
  evidence ref, case graph refs and record-derived side provenance, lifecycle
  ordering, created/resolved case effects, every ChangeSet group/ref/baseline/ID,
  replayed graph state/version, and local checkpoint cursor-to-evidence consistency.
- Valid prepared/stale journals are recovered and reported as a v1 notice; corrupt
  journals remain in place and return only `transaction.corrupt`.
- `intent validate` and doctor call the same service. Diagnostics are frozen v1
  records containing only `code`, `scope`, and `severity`; parser text, content,
  IDs, and filesystem paths never enter output. Validation failure uses exit 1
  with structured stdout and empty stderr.
- README/CONTRIBUTING now explain that a clean checkout must run `intent init`
  before validating its ignored local `.intent/` workspace, and distinguish that
  workspace from the tracked framework graph.

Focused unit/storage result: 22 passed. Focused unit/e2e result: 27 passed.
Validation/e2e/fixture/startup aggregate: 59 passed. Validation/framework/e2e
aggregate after starter compatibility: 76 passed.

Primary files: `validation/service.py`, `validation/__init__.py`,
`core/policy/doctor.py`, `cli/app.py`, transaction/case pure parsers, validation
tests, CLI E2E tests, README, CONTRIBUTING, and `.gitignore`.

## Migration and compatibility notes

- **Case authorship:** only the JSONL case-store boundary accepts a legacy row
  without `created_by`; canonical models and generated schemas keep it required.
  Every new write contains the field.
- **Starter graph:** its two known graph-level metadata fields are explicitly typed
  and round-trip through canonical serialization. No generic extra-field bag was
  introduced, and arbitrary canonical fields remain rejected.
- **Markdown cursor:** legacy content-hash cursors still trigger the ruled one-time
  safe rescan and migrate to the canonical v1 manifest on successful checkpoint.
  Deep validation identifies a legacy cursor as a non-failing notice.
- **Transactions:** journal schema is v1 and only symbolic expected targets are
  accepted. There is no permissive fallback for malformed or foreign journals.
- **Filesystem:** no pathname fallback exists after a descriptor root is held.
  Existing Path-based constructors are compatibility adapters that immediately
  coerce to held secure descriptors.

## Exact verification evidence

Focused work used `.venv/bin/python -m pytest` (and
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 -p anyio.pytest_plugin` where appropriate)
because a pre-existing untracked duplicate source file in this Desktop-backed
worktree interferes with direct plugin/module discovery. The final requested
coverage command itself was run exactly after temporarily moving that one
untracked file aside and restoring it with a verified SHA-256.

```text
.venv/bin/ruff check .
All checks passed!

.venv/bin/mypy src/intent_engineering
Success: no issues found in 64 source files

.venv/bin/pytest --cov=intent_engineering --cov-report=term-missing
collected 330 items
330 passed in 20.50s
TOTAL 3894 statements, 562 missed, 86%
```

Fresh installed-CLI project gate:

```text
.venv/bin/intent init --project /tmp/intent-validation-final-core --format json
{"project_id":"intent-validation-final-core","version":"1","workspace":".intent"}

(cd /tmp/intent-validation-final-core && .../.venv/bin/intent validate --format json)
{"diagnostics":[],"graph_id":"graph:intent-validation-final-core","graph_version":0,"schema_version":"1","valid":true,"version":"1"}

.venv/bin/intent --help
exit 0

.venv/bin/intent reconcile --help
exit 0
```

Additional broad deterministic run:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  -p anyio.pytest_plugin tests/unit tests/contract tests/integration tests/e2e -q
330 passed in 17.10s
```

`git diff --check` is clean. `git status --short --untracked-files=no` is empty.

## Self-review and concerns

- Reviewed the full `ca550ed..d78dfc8` diff for fail-open parsing, descriptor/path
  fallback, lock-order overlap, recovery timing, ignored ChangeSet groups,
  checkpoint advancement on partial work, synthetic case provenance, and output
  leakage. No remaining product blocker was found.
- Normal sync never holds evidence/checkpoint locks while entering the graph/case
  transaction, so the validator's sorted union lock does not introduce a reverse
  production lock order.
- Prepared recovery restores exact target bytes/existence; committed-stale recovery
  never replays preimages. Corrupt journals and unsafe files remain untouched.
- The intentional deferred minors from the controller ledger were not expanded
  into this wave unless an adjacent edit was required.
- Worktree-only concern: five pre-existing unrelated untracked Desktop artifacts
  remain preserved: `.coverage 2`, `.coverage 3`, `.coverage 4`, `README 2.md`,
  and `src/intent_engineering/core/policy/dogfood 2.py`. The last has an invalid
  Python module filename and is why the literal whole-tree gate required temporary
  isolation. It was restored byte-for-byte; tracked branch state is clean, and a
  clean checkout has no such artifact.

Status: **DONE_WITH_CONCERNS** only for the preserved untracked environment
artifact; the product implementation and tracked branch are complete.
