# Task 5 report — deterministic task preflight validation

## Status

READY. Final scoped review reported `0 Critical / 0 Important / 0 Minor`. The implementation and
verification evidence are approved for the Task 5 commit.

Execution base: `3cef53684b2b44e0324284e53f8f22852b92446f`.

Task authority retained with this report:
`.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-5-brief.md`.

## Implementation

- Added `ConversationCapture.record_turn`, which records bounded human and agent JSON turns as
  immutable `EvidenceRecord` versions through the production evidence association ledger. Turns
  retain exact author, role, ACL, UTC capture time, stable conversation locator, canonical
  content/version hashes, and the ledger's exact predecessor chain. Exact replay is byte-identical;
  concurrent identical and different appends remain one unforked chain.
- Added strict/frozen `AgentClassificationSubmission` with exact task/digest/graph/evidence/scope
  bindings. Identifier/scope collections are duplicate-free, canonical, and capped at 256;
  semantic text collections are duplicate-free, canonical, capped at 64, and each item is capped
  at 4 KiB. Agent evidence captures the exact detached typed submission material before evaluation.
- Added `PreflightService.evaluate`, which authenticates one descriptor-held transaction snapshot
  containing current config, graph, evidence/ingestions, case versions, and unresolved proposal
  ledger state. Submitted identities, ACLs, graph version, task digest, scope, proposal association,
  and classification semantics are all revalidated rather than trusted.
- Authorization principals now default to the held config actor and may be extended only by a
  trusted resolver receiving that exact snapshot. Caller principal sets must equal the exact
  canonical resolver result, so omission and addition cannot alter ACL visibility. Before any aligned or mechanical
  result escapes, every config/graph/evidence/case/proposal preimage is reauthenticated atomically
  under the complete read lock set.
- Implemented exactly four deterministic outcomes: mechanical work authorizes only with no semantic
  effect/uncertainty/relevant semantic ID/conflict; aligned work requires complete authorized active
  citations, exact scope, no provisional material, no relevant nonterminal case, and exact bounded
  context; new/ambiguous work is non-authorizing and requires questions; conflicting work is
  non-authorizing and atomically creates or reuses one stable open evidence-backed case.
- Conflict cases include active semantic evidence plus exact human-request and agent-classification
  turns, remain unresolved/unapproved, and use deterministic fingerprints. Cancellation after the
  case append rolls back exact prior bytes and preserves the original signal.
- Durable case lifecycle rows are collapsed to the latest exact version per stable ID before
  terminal/blocking decisions. Conflict reuse requires an ACL-visible exact immutable semantic
  match by stable ID/fingerprint; hidden or unrelated collisions fail closed. Concurrent identical
  appends reload and validate the winner after a case-only preimage race, returning two equivalent
  non-authorizing results over one durable case.
- Every candidate in every unresolved proposal is validated, including exhaustive ChangeSet node
  scope, nested evidence ACL/subset association, configured source-role precedence, agent
  authorship, timestamps, inferred provenance, confidence, registered and kind-compatible types,
  and candidate-edge provenance. Hidden or foreign nested candidates cannot inform questions.
- Added `ContextProvider.for_refs`, using a single frozen principal set, exact active ACL-visible
  seeds, the existing two-hop expansion and `ContextPack` serializer, stable permutation-independent
  order, and fixed category caps even when project limits are larger. Hidden and absent IDs share
  one fixed error, and fixed-error frames clear already-built context.
- Extended `LocalTransaction.transaction` with descriptor-held read-only extras and added a
  no-journal `read_transaction` view. Preflight uses these to lock and compare the exact config
  snapshot during conflict append and final authorization; extras cannot be written and are not
  added to recovery journal target semantics.
- Public failures are fixed and context-free. Cancellation/interrupt paths preserve signal identity,
  recover owned transaction state, and clear request/classifier/context data from repository
  traceback locals. No model/provider/network call, capability issuance, clarification session,
  external write, or host hook was added.

## TDD evidence

### Required RED

Before any production edit, both required Task 5 test files were created and the exact command ran:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py -q -W error
```

Exact result: both files failed collection with
`ModuleNotFoundError: No module named 'intent_engineering.intent_workflow.conversation'`;
`2 errors in 0.26s`.

### Focused GREEN and hardening cycles

- Initial required two-file GREEN: `24 passed in 0.43s`.
- Current-config snapshot plus provisional/blocking/persistence/secrecy hardening RED:
  `2 failed, 28 passed`; GREEN: `31 passed in 0.51s`.
- Complete citations, exact proposal association, and conflict agent-evidence RED:
  `3 failed, 31 passed`; GREEN: `34 passed in 0.46s`.
- Typed noncanonical proposal-ledger RED: `1 failed, 34 passed`; GREEN:
  `35 passed in 0.42s` after byte-for-byte comparison with the validated ledger record.
- Fixed exact-context caps RED: `1 failed, 35 passed`; then passed in the expanded context gate.
- Fixed-error context traceback secrecy RED: `1 failed, 37 deselected`; GREEN:
  `1 passed, 37 deselected`.
- Initial final required focused/context command: `65 passed in 0.51s`.

### Independent review Fix Round 1

The first independent review reported `0 Critical / 6 Important / 0 Minor` and Not Ready. Before
any Fix Round production edit, regression probes for all six findings were added and the exact
required two-file command above produced `21 failed, 46 passed in 1.05s`.

The probes cover caller principal amplification plus snapshot-bound legitimate aliases; ten
config/graph/evidence/case/proposal replacement races across aligned and mechanical evaluation;
latest terminal/nonterminal lifecycle selection; hidden and unrelated same-fingerprint case
collisions; a deterministic two-caller identical-conflict append race; and five nested provisional
candidate ACL/provenance/authorship/type failures. Final Fix Round focused GREEN:
`67 passed in 0.90s`.

### Independent review Fix Round 2

The Fix Round 1 re-review reported `0 Critical / 2 Important / 0 Minor` and Not Ready. Before any
Round 2 production edit, exact-principal and bootstrap-role matrices were added; the required
two-file command produced `2 failed, 73 passed in 1.21s`. The failures were exactly caller omission
from a resolver-authenticated alias set and a canonical BOOTSTRAP proposal with empty source roles.

Round 2 requires exact equality between submitted and snapshot-resolved principals. BOOTSTRAP
proposals now require nonempty configured source roles before the existing every-evidence coverage,
no-unused-role, connector/scope, inherited scope, and exact-override-precedence checks. Requirement-
kind proposals retain their deliberate empty-role compatibility. Final focused GREEN:
`75 passed in 0.86s`; the prior 22 Fix Round 1 security regressions separately passed in `0.47s`.

Final scoped re-review verdict: Ready, `0 Critical / 0 Important / 0 Minor`. Reviewer evidence was
11 Round 2 focused tests plus the prior 22 security regressions, with the clean full-suite result of
1232 passing tests reported for integration.

## Final gates

Changed-path Ruff:

```text
.venv/bin/ruff check src/intent_engineering/intent_workflow/conversation.py src/intent_engineering/intent_workflow/preflight.py src/intent_engineering/intent_workflow/__init__.py src/intent_engineering/context/provider.py src/intent_engineering/storage/transaction.py tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py
All checks passed!
```

Type gate:

```text
.venv/bin/mypy src
Success: no issues found in 110 source files
```

Relevant transaction/storage/reconciliation/proposal/bootstrap compatibility:

```text
172 passed in 0.85s
```

Task 5 plus context compatibility:

```text
95 passed in 0.85s
```

Fresh full offline warnings-as-errors gate after the final production edit:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
1232 passed in 44.56s
```

The first full Round 2 attempt reported one unrelated receipt-store fork race after 1231 passes and
left its forked pytest child alive. The unchanged isolated receipt test passed (`1 passed in 0.22s`).
A subsequent full process blocked behind that orphan and was interrupted; after terminating only the
exact verification-process PIDs, the clean fresh full command above passed all 1232 tests.

`git diff --check` completed with no output.

## Files changed

- `src/intent_engineering/intent_workflow/conversation.py`
- `src/intent_engineering/intent_workflow/preflight.py`
- `src/intent_engineering/intent_workflow/__init__.py`
- `src/intent_engineering/context/provider.py`
- `src/intent_engineering/storage/transaction.py`
- `tests/unit/intent_workflow/test_preflight.py`
- `tests/integration/intent_workflow/test_preflight_context.py`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-5-report.md`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/progress.md`

## Self-review

- Every successful semantic use is preceded by real persisted human and agent evidence with the
  exact predecessor relationship. Rejected capture/evaluation paths do not authorize or mutate a
  case.
- One authenticated snapshot supplies config, graph, evidence, cases, and unresolved proposals;
  conflict mutation compares all exact preimages while config remains descriptor-locked, and every
  authorization performs a final atomic exact-preimage authentication.
- Principal visibility is never changed by caller data: supplied principals must exactly equal the
  identities derived by current project policy or its trusted exact-snapshot resolver.
- Historical case versions cannot block after a terminal latest version, and neither fingerprint
  collision nor an append race can disclose or reuse a hidden/different case.
- Proposal validation traverses every candidate and edge, including material not selected by the
  classification, before provisional content may inform a non-authorizing question outcome.
- Bootstrap proposals additionally require a nonempty exact source-role association; inherited
  scopes remain valid, exact scopes override inherited scopes, and missing, unused, or mismatched
  roles fail closed. Requirement-kind proposal behavior remains intentionally compatible.
- Aligned context cannot use inactive, hidden, absent, foreign, stale, provisional, incompletely
  cited, or blocking-case state. Requested exact IDs survive caps or the request fails closed.
- A conflict creates only an open case. It creates no resolution, approval, decision, ChangeSet,
  capability, provider mutation, or graph change.
- Context selection reuses the existing two-hop traversal and `ContextPack`; no second serializer or
  unbounded graph dump was added.
- The transaction extra is read-only, lock-order deterministic, excluded from journal preimages,
  and cleared when the transaction handle finishes. Existing recovery and mutation callers retain
  their prior behavior, as proven by the compatibility and full gates.
- The five protected untracked artifacts remain unstaged and were not read, modified, renamed, or
  deleted.

## Concerns

None open. Independent review is required before staging or committing.
