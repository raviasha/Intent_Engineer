# Task 9 report — post-task evidence linkage and scheduled assurance

## Status

`READY`, with the final same-reviewer finding-only verdict `0 Critical / 0 Important / 0 Minor`.
The reviewed precommit state was unstaged and uncommitted at exact base
`c0b7631c93c2e5ba5ea432bbc936b2a32c743639`. The implementation adds only the reusable
provider-neutral `after_task` path; it does not claim that Codex invokes the path automatically.
Task 8's unsupported mandatory-Codex ruling remains unchanged.

## Implemented outcome

- Strict, frozen, token-free `PostTaskSubmission` and `PostTaskResult` records with exact Python and
  JSON boundaries, bounded identities/collections, canonical UTC `Z`, canonical relative paths,
  exact revision bindings, and detached implementation claims.
- `PostTaskService` reauthenticates the process-local Task 7 capability against the current actor,
  repository, task, request digest, graph content/version, path subset, and expiry at the atomic
  commit boundary. The capability is consumed exactly once; concurrent completion, replay,
  revocation, expiry, failure, and cancellation cannot double-commit or preserve live authority.
- Git, requirement, code, test, and evidence claims are resolved from one descriptor-held authority,
  graph, and evidence snapshot. Current ACL-visible immutable Git evidence must cover the exact
  repository, final revision, predecessor, alias-authenticated author, and changed paths. A changed
  test file is never execution proof: current ACL-visible immutable passing test-result evidence
  bound to the final revision and exact test graph refs is required before `test_evidence`, a
  `VERIFIED_BY` edge, or `IMPLEMENTED_BASELINE`. Unknown, hidden, stale, duplicate, wrong-type,
  foreign, or ambiguous associations fixed-fail without Task 9 semantic mutation.
- Successful completion returns one deterministic `ImplementationClaim` and applies the smallest
  canonical ChangeSet: existing graph identities receive only supported `IMPLEMENTED_BY` /
  `VERIFIED_BY` edges and implementation-status evidence. No authorship, timestamp, confidence,
  acceptance satisfaction, code symbol, test, or graph identity is manufactured.
- `IntentAgentHostAdapter.after_task` uses the exact retained Task 8 host task and private token,
  delegates once, returns the exact detached bounded `PostTaskResult` (including withheld outcomes),
  and clears/revokes private state on recorded success, fixed withholding, malformed completion, or
  cancellation. Disabled mode remains a no-op.
- `AssuranceService.detect` emits stable evidence-backed observations for the eight approved checks
  using existing case types and established precedence. Hidden, incomplete, noncurrent, graph-only,
  duplicate, terminal/already-classified, and unsupported inputs fail closed. Before optional
  reasoning, the service creates a second detached ACL-visible view: hidden/incomplete nodes,
  hidden-endpoint edges, unauthorized evidence and its ingestion envelopes, and cases with hidden
  subjects/affected refs/evidence sides are removed. Optional reasoner output must be an exact
  bounded tuple grounded in unobscured visible graph identities, exact evidence-side provenance,
  and deterministic topology; it has no mutation or approval authority.
- Sync persists raw evidence first, reasons over successful connectors, then captures graph, all
  durable evidence/ingestions (including other and failing sources), cases, and config under one
  transaction snapshot before checkpoint advancement. Case commits bind exact graph/evidence/case/
  config preimages, closing same-version and revocation races. Hidden or incomplete unrelated nodes
  are filtered conservatively rather than erasing visible assurance; an edge to hidden provenance
  obscures only its visible endpoint. Legacy detector groups and checkpoint behavior remain exact.
  Retry and replay are deterministic no-ops after the first case commit, while raw evidence remains
  durable across semantic failure.
- Production CLI sync constructs the default no-model `AssuranceService`. No provider/model/network
  execution, external write, approval creation, conflict resolution, or silent intent/requirement
  mutation was added.

## Minimal supporting seams

The approved file list required narrow adjacent changes:

- Task 7 `AuthorizationIssuer` gained process-local request-digest binding and atomic one-use
  `consume`; no token, digest, grant, reason, or proof is persisted.
- `LocalChangeSetExecutor.apply` gained exact graph and explicit absent-or-present case preimage
  checks alongside its evidence and read-only authority preimages.
- The local Git connector accepts an optional project repository identity; production runtime binds
  it to `ProjectConfig.project_id`, so captured Git evidence can prove its repository.
- `intent_workflow.__init__` exports the new provider-neutral surfaces.
- CLI runtime injects deterministic assurance into the existing sync orchestrator.

## TDD and debugging evidence

- Required tests-only initial RED before any production edit: the exact mandated pair failed
  collection with two missing-module errors in `0.24s`. The expanded tests-only pair repeated the
  same two-error RED in `0.26s`.
- First post-task implementation run: `14 failed, 9 passed`; strict JSON tuple detachment was the
  common root cause. Focused post-task then reached `3 failed, 11 passed` before the remaining
  boundary expectations were corrected.
- Host tests-first RED: `2 failed, 14 passed` because completion evidence fields were absent.
- Sync tests-first RED: `2 failed, 9 passed` because assurance was not invoked.
- Request-binding regression RED: `1 failed, 16 passed`; a substituted request digest had been
  accepted before process-local grant binding was added.
- Current/provenance regression RED: `2 failed, 12 passed`; stale evidence could win a topology
  classification and a reasoner could manufacture authorship before current-side gating and exact
  provenance grounding were added.
- Independent review returned `0 Critical / 4 Important / 0 Minor`. The grouped finding-only RED was
  exact command `... pytest ... test_post_task.py test_scheduled_assurance.py -q -W error -k
  'review_fix'`: `15 failed, 1 error, 47 deselected in 0.59s`. It proved (I1) touched test nodes were
  treated as execution, (I2) Git authors were not alias-aware and authority preimages were unbound,
  (I3) withheld post-task results were swallowed, and (I4) assurance used only selected connectors,
  lacked a unified commit-bound snapshot, and globally aborted for one hidden node.
- The grouped finding selector reached `18 passed, 47 deselected in 0.38s`. A repository-binding
  microcycle first failed because foreign-repository Git evidence recorded successfully, then passed
  after local Git capture and post-task verification were bound to the project repository identity.
- The same reviewer's final disclosure finding received its own tests-only RED: a recording reasoner
  observed the unique ACL-hidden node/evidence/ingestion/case sentinel (`1 failed, 25 deselected in
  0.17s`). Filtering now occurs before `reasoner.detect`; the complete finding selector is `19 passed,
  47 deselected in 0.32s`, and no hidden sentinel reaches the detached reasoner snapshot.
- Cross-detector precedence RED proved two cases for one legacy/assurance subject; the final fix
  preserves arbitrary legacy detector groups while suppressing only overlapping assurance output.
- The first full run was genuinely diagnostic: `9 failed, 1499 passed in 50.32s`. It exposed
  lower-priority assurance replay after a legacy case and absence conclusions on pre-bootstrap
  fixture graphs. New focused regressions failed first, then terminal/subject suppression and the
  intent-baseline gate made the exact fixture/public-alpha selection `17 passed in 4.07s`.
- Complete Task 9 integration pair: `66 passed in 1.24s`.
- Expanded host/workflow/sync/executor/transaction/reconcile compatibility: `510 passed in 4.27s`.
- Scoped Ruff: clean across every changed production Python path and both required integration
  files.
- Full mypy: `Success: no issues found in 117 source files`.
- Authoritative post-review full offline warnings-as-errors suite: `1529 passed in 51.12s`.
- Final same-reviewer finding-only selector: `4 passed`; verdict `0C/0I/0M`, Ready.
- `git diff --check`: clean.

## Handoff state

The final reviewed precommit HEAD/base was `c0b7631c93c2e5ba5ea432bbc936b2a32c743639`
with an empty index. The five protected untracked artifacts remained untouched. The final review
authorized the exact allowlisted Task 9 commit; no automatic Codex invocation is claimed.
