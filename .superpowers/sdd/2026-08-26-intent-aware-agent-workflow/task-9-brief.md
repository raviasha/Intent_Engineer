### Task 9: Link post-task evidence and expand scheduled assurance

**Base:** `c0b7631c93c2e5ba5ea432bbc936b2a32c743639`

**Files from the approved plan:**
- Create: `src/intent_engineering/intent_workflow/post_task.py`
- Create: `src/intent_engineering/intent_workflow/assurance.py`
- Modify: `src/intent_engineering/reconcile/evidence_detection.py`
- Modify: `src/intent_engineering/sync/orchestrator.py`
- Modify: `src/intent_engineering/integrations/agent_host/base.py`
- Test: `tests/integration/intent_workflow/test_post_task.py`
- Test: `tests/integration/intent_workflow/test_scheduled_assurance.py`
- Modify other Task 7 authorization/runtime or deterministic detector/model files only when an exact
  atomic authority or vocabulary seam cannot be implemented within the listed files; keep such
  changes minimal and regression-tested.

**Task 8 ruling:** Codex mandatory mode is unsupported and no plugin exists. Task 9 still completes
the reusable provider-neutral `AgentHostAdapter.after_task` path for manual, test, and future
supported hosts. It must not imply that Codex invokes it automatically.

## Required behavior

1. Author both required integration files first and witness the approved missing-module RED before
   any production edit. Add every later behavior tests-first.
2. Define strict, frozen, deeply detached, bounded post-task submission/result records. Require
   exact JSON containers/scalars, canonical UTC `Z`, sorted unique relative paths/refs, exact
   repository/task/actor/graph/base/final revision bindings, and no unknown/coerced/subclassed
   input. The authorization token is **not** a model field or durable submission member; the
   adapter passes it as a separate private argument to the in-process service.
3. Enabled `after_task` must use the exact issued `HostTask` and its privately retained Task 7 token,
   delegate once to `PostTaskService`, and revoke task/token state on success, fixed failure, or
   cancellation according to the exact lifecycle contract. Disabled mode remains a transparent
   no-op. A malformed/substituted result cannot preserve a live capability.
4. Reauthenticate current actor, repository identity, task ID, request binding, graph version, and
   exact changed-path subset through the same live Task 7 issuer. Close revocation/expiry/graph/
   scope races at the local commit boundary: a token that becomes invalid before durable commit
   cannot authorize graph/history/evidence association. Do not persist the token, digest, grant,
   authorization reason, or a replayable proof. Concurrent post-task calls cannot double-commit,
   consume the wrong task, or resurrect/reuse a revoked grant.
5. Treat changed paths, requirement IDs, code refs, test refs, commit SHA, and Git evidence refs as
   untrusted claims until resolved against the exact held repository/evidence/graph snapshot.
   Require current ACL-visible immutable Git evidence covering every changed path and revision;
   reject hidden/missing/duplicate/stale/wrong-repository/wrong-author/wrong-predecessor evidence,
   unknown or non-requirement semantic IDs, code/test refs of the wrong graph type, and test refs
   not backed by test evidence. Caller text alone never becomes implementation provenance.
6. Build one deterministic `ImplementationClaim` and the smallest canonical ChangeSet that preserves
   all pre-existing node/edge provenance while linking exact requirements to existing code/test
   graph identities and updating implementation status only where supported by the evidence. Never
   manufacture authorship, timestamps, confidence, tests, code symbols, acceptance satisfaction, or
   graph identities. If the existing meta-model cannot represent a requested link, return a bounded
   proposal/review result rather than widening truth silently.
7. Scope expansion, task/graph/repository/actor drift, unauthorized requirements/evidence, ambiguous
   code/test association, and changes beyond the grant return exact `preflight_required` or fixed
   rejection with graph/history/cases/evidence association bytes unchanged. Successful commit uses
   the existing shared transaction/executor, exact evidence/config/graph preimages, and one history
   row. Identical authorized inputs are deterministic; no duplicate evidence association, graph
   edge, case, or history row is created.
8. `AssuranceService.detect` reads one immutable, descriptor-held graph/evidence/case/config/code/
   test snapshot and emits detached deterministic observations for all eight approved checks:
   intent without requirement, requirement without intent, code lag, undocumented code, test lag,
   conflicting sources, implementation-relevant provisional intent, and stale source evidence.
   Use existing `ReconciliationCaseType` values (`INTENT_LAG`, `ORPHAN_REQUIREMENT`, `CODE_LAG`,
   `UNDOCUMENTED_CODE`, `TEST_LAG`, `CONFLICTING_SOURCES`, `POSSIBLE_INTENT_CHANGE`, and the existing
   best-fit stale/ambiguous type) rather than adding synonyms unless no truthful type exists.
9. Preserve established detector precedence, evidence chronology, stable fingerprints, exact
   current-version semantics, terminal-case suppression, and one case per subject/condition.
   Observations require complete current ACL-visible evidence sides with real authors and source
   modes. Graph topology alone is insufficient provenance. Do not expose hidden graph IDs through
   affected refs or report rows.
10. The optional scheduled `SemanticReasoner` is one injected protocol. `None` is the default and
    still performs deterministic assurance. A reasoner may return only bounded detached candidate
    detection inputs/proposals grounded in the supplied snapshot; it cannot create approval,
    resolution, external writes, provider mutations, checkpoints, or canonical graph changes.
    Malformed/ungrounded/unauthorized/subclassed output fails the semantic phase unchanged.
11. Integrate assurance after successful raw evidence persistence and graph reasoning, before source
    checkpoint finalization. Build every new assurance case first, then append cases through one
    complete ChangeSet/executor transaction. A detector/reasoner/case-commit failure leaves graph,
    history, cases, and checkpoint exact for the assurance phase while preserving already durable
    raw evidence under the existing replay contract. Retry creates the same cases exactly once and
    advances only successful checkpoints.
12. Combined multi-connector runs use one stable snapshot and stable sorted observation set;
    connector order, evidence order, and repeated identical runs cannot change fingerprints or
    output. A partial connector failure cannot erase another connector's evidence or authorize an
    incomplete cross-source conclusion. Existing reasoner and detector behavior remains compatible.
13. Preserve exact cancellation/interrupt identity and close owned connector/runtime resources.
    Clear raw post-task submissions, token/proof, diff/path/ref collections, evidence payloads,
    graph/case snapshots, reasoner outputs, errors, and encoded forms from every repository traceback
    frame. Public errors/results/logs are fixed and bounded; scan persisted files, captured output,
    logs, and rendered tracebacks for unique sentinels.
14. No background or post-task path creates human approval, confirms a proposal, resolves a
    conflict, performs an external write, or changes declared intent/requirements based only on
    confidence. Scheduled output is evidence-backed cases/report data; CI blocking policy remains a
    later operator/documentation concern.

## Required tests

- Real Task 7 issuer + real stores/executor successful post-task path from exact changed Git/code/test
  evidence to one implementation claim/ChangeSet/history update; exact requirement/code/test refs,
  author/source/predecessor provenance, and deep detachment asserted.
- Rejection matrix for missing/expired/revoked/substituted token; actor/repository/task/request/graph/
  base/final revision mismatch; changed-path expansion; unknown/hidden/wrong-type requirement/code/
  test refs; missing/stale/foreign/duplicate evidence; and malformed strict inputs. Every rejection
  is byte-noop.
- Concurrency and crash/cancellation matrix at authorization, snapshot, evidence resolution,
  ChangeSet, transaction write, and postcommit result boundaries; no double commit, token reuse,
  torn state, secret retention, or wrong-task revocation.
- Host adapter enabled delegation and disabled no-op; exact completion result binding; malformed and
  cancelled completion revokes private state without leaking it.
- Each of the eight assurance checks independently creates the exact expected deterministic
  observation/case from real graph/evidence topology, plus negative controls proving graph-only,
  ACL-hidden, stale/noncurrent, terminal, unsupported, and incomplete inputs do not create cases.
- Stable detector precedence/fingerprint/order under input permutations and multi-source histories;
  repeated identical assurance is a semantic no-op with unchanged graph/history/case/checkpoint.
- Real sync integration success, no-reasoner default, valid optional reasoner, reasoner failure,
  malformed/unauthorized output, one connector failure, combined-source case, checkpoint failure,
  cancellation, retry, and replay. Raw evidence durability and semantic/checkpoint rollback are
  asserted byte-for-byte.
- Existing sync, evidence detector, preflight, authorization, host-neutral, validation, rendering,
  case lifecycle, and transaction recovery suites remain compatible.

## Gates and handoff

Use the approved fast-but-safe cadence: focused RED/GREEN and static checks while developing, one
fresh full offline warnings-as-errors run before commit, and one final scoped independent review.
Fix grouped Critical/Important findings tests-first, rerun affected gates, and do not repeatedly
expand adjacent scope after the final focused fix.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_post_task.py tests/integration/intent_workflow/test_scheduled_assurance.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_post_task.py tests/integration/intent_workflow/test_scheduled_assurance.py tests/integration/sync tests/unit/reconcile tests/contract/agent_host -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/post_task.py src/intent_engineering/intent_workflow/assurance.py src/intent_engineering/reconcile/evidence_detection.py src/intent_engineering/sync/orchestrator.py src/intent_engineering/integrations/agent_host/base.py tests/integration/intent_workflow
.venv/bin/mypy src
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
git diff --check
```

Write `task-9-report.md`, update `progress.md`, and pause unstaged at
`PRECOMMIT_REVIEW_READY`. Commit only after a clean scoped review with
`feat: reconcile completed tasks with intent`. Never stage protected artifacts.
