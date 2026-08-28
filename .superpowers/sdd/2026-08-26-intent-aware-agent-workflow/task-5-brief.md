### Task 5: Implement deterministic task preflight validation

**Base:** `3cef536`

**Files:**
- Create: `src/intent_engineering/intent_workflow/conversation.py`
- Create: `src/intent_engineering/intent_workflow/preflight.py`
- Modify: `src/intent_engineering/intent_workflow/__init__.py`
- Modify: `src/intent_engineering/context/provider.py`
- Test: `tests/unit/intent_workflow/test_preflight.py`
- Test: `tests/integration/intent_workflow/test_preflight_context.py`

**Interfaces:**
- Consumes Task 1 `TaskEnvelope`, `TaskClassification`, `PreflightResult`; Task 2/3 proposal
  ledger; existing exact held graph/evidence/case/config snapshot, ACL projection, context provider,
  and transaction coordinator.
- Produces `ConversationCapture.record_turn`, strict/frozen `AgentClassificationSubmission`, and
  `PreflightService.evaluate`; adds `ContextProvider.for_refs` without another serializer.

## Required behavior

1. Write the four-classification unit/integration tests first and witness the missing-service RED
   before production edits.
2. Capture the human request as immutable evidence before semantic use. Capture the agent's typed
   classification explanation as a second immutable evidence version before evaluating it. Both
   records use one stable conversation locator, exact human/agent principals, ACL, UTC capture time,
   canonical content hash/version, and an exact predecessor chain. Replays are idempotent; concurrent
   same/different turns cannot fork or overwrite the chain.
3. `TaskEnvelope` is built from the captured human evidence. `AgentClassificationSubmission` is a
   strict/frozen detached boundary bound to the exact task ID/digest, current graph version, agent
   evidence, cited evidence, relevant IDs, requested scope, basis, effects, uncertainties,
   questions, and conflict claims. IDs/scopes are limited to 256; text collections to 64; every text
   item to 4 KiB; all collections are duplicate-free and canonical.
4. Evaluate from one authenticated held transaction snapshot of graph, evidence, nonterminal cases,
   project policy/principals, and unresolved proposal ledger. Never trust submitted graph version,
   ACL, node/proposal/case identity, classification semantics, or provider objects.
5. Apply exactly four deterministic results:
   - `no_semantic_impact`: authorize only when semantic effects, uncertainties, relevant semantic
     IDs, and conflict claims are empty; mechanical uncertainty can never authorize.
   - `aligned`: require at least one current active ACL-visible relevant node, exact requested-scope
     consistency, no provisional citation, no relevant blocking case/conflict, and a bounded context
     pack from those exact IDs.
   - `new_or_ambiguous`: never authorize; require focused nonempty bounded questions and evidence;
     provisional candidates may inform questions but never satisfy alignment.
   - `conflicting`: never authorize; require grounded conflict claims/sides and atomically create or
     reuse one stable nonterminal reconciliation case without manufacturing resolution/approval.
6. Invalid/missing graph/config/evidence, stale graph/task digest, unauthorized/foreign IDs,
   terminal/hidden objects, missing turn persistence, or source/proposal association drift fails
   closed through one fixed public error. No partial authorization or case mutation.
7. `ContextProvider.for_refs` requires every requested node to be active and ACL-visible, uses one
   immutable principal/snapshot set, expands only the existing bounded two-hop neighborhood, and
   returns the existing detached `ContextPack`. Unauthorized and absent are indistinguishable.
8. Fixed errors, cancellation, and interrupt preserve the original signal where applicable and
   retain no raw human request, agent basis/questions/conflict claims, evidence body, actor, task ID,
   graph/case/proposal data, or credentials in outputs/logs/repository traceback locals.
9. This task classifies and returns `authorized`; it does not issue a mutation capability (Task 7),
   ask clarification sessions (Task 6), call a model, mutate external providers, or add host hooks.

## Required tests

- All four classification outcomes and invariant matrix, including mechanical uncertainty denial,
  aligned exact active IDs, new/ambiguous nonempty questions, and stable conflict case reuse.
- Real human→agent conversation evidence chain with exact authors/predecessor; identical replay
  no-op; concurrent append race; persistence failure prevents evaluation/authorization.
- Strict/bounded/detached submission, stale task/digest/graph, duplicate/foreign/hidden evidence,
  unauthorized or terminal nodes/cases, requested-scope mismatch, provisional aligned denial.
- Real graph/evidence/case/proposal/runtime snapshots and transaction recovery where relevant; no
  mock-only proof for authorization or case persistence.
- `for_refs` exact authorized IDs, bounded two-hop expansion, one principal snapshot, permutation
  stability, hidden/absent indistinguishability, and no unrelated evidence/graph leakage.
- Cancellation/interrupt/fixed-failure traceback and log secrecy with sentinel request and agent
  content; exact bytes/graph/cases unchanged on rejected paths.

## Gates

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py tests/unit/context -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/preflight.py src/intent_engineering/context/provider.py tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py
.venv/bin/mypy src
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
```

Write `task-5-report.md`, update `progress.md`, and pause unstaged at PRECOMMIT_REVIEW_READY. Commit
only after independent review, using `feat: classify coding tasks against intent`.
