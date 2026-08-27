# SDD ledger — plan: docs/superpowers/plans/2026-08-26-intent-aware-agent-workflow.md

## Setup

- Workspace: `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow`
- Linked worktree: `.worktrees/public-alpha`, branch `feat/public-alpha`
- Execution base before plan correction: `2afcbda`
- Task execution base after plan rulings: `e90c399`
- Baseline: fresh full offline rerun `1041 passed in 39.56s`.
- Baseline observation: the first full run stalled in
  `test_two_processes_can_create_only_one_durable_claim`; process inspection showed one fork child
  waiting at its barrier. The exact test then passed in 0.61s and the fresh full rerun passed. No
  product change was made.
- Spec authority: `docs/superpowers/specs/2026-08-26-intent-aware-agent-workflow-design.md`

## Preflight conflict scan

| Tasks | Producer / consumer or self-check | Finding / ruling |
|---|---|---|
| 1 | Own files, tests, interfaces | Ruling: source-role vocabulary lives in core and is re-exported by the workflow package; `ProjectConfig` must not import the application layer — avoids dependency inversion — cost if wrong: one additional public core module. |
| 2 | Own ledger/store/runtime steps | Internally consistent after Task 6 extension ruling below. |
| 3 | Own bootstrap tests and service | Tests match proposal-only then reviewed activation; no direct graph mutation during extraction. |
| 4 | Own CLI/MCP tests and registration | Ruling: CLI-only PRD capture returns `agent_submission_required` with evidence/context, never an invented proposal ID; typed MCP/agent submission creates the proposal — preserves deterministic authority — cost if wrong: CLI alone is a two-step rather than one-command flow. |
| 5 | Own conversation/preflight records and tests | Human and agent turns are evidence before typed classification; result rules match all four classifications. |
| 6 | Own clarification/governance tests | Ruling: clarification records are introduced here, not Task 1, and extend the Task 2 framed ledger with a third one-of event — preserves one durable conversational order — cost if wrong: schema evolution is required in the shared ledger. |
| 7 | Own grant lifecycle and MCP tests | Required verification bindings are explicit; CLI remains diagnostic and cannot mint grants. |
| 8 | Own host contract and Codex capability audit | The supported/unsupported branch is intentional and bound to the installed official contract; mandatory mode cannot be advisory. |
| 9 | Own post-task and assurance tests | Post-task evidence and scheduled observations are separated; background reasoner remains optional and cannot approve. |
| 10 | Own E2E/docs/workflow proof | Production-composition harness uses one project/transaction domain; docs and scheduled workflow match preceding tasks. |
| 1 → 3 | Workflow models → bootstrap submission/proposal | Compatible after core source-role re-export. |
| 1 → 4 | `ProjectConfig.source_roles` → CLI source assignment | Exact connector/scope pairs and deterministic serialization are shared. |
| 1 → 5 | task/classification/result models → preflight | Compatible; Task 5 adds the untrusted submission rather than widening Task 1 records. |
| 1 → 6 | proposal model → clarification/governance | Clarification types deliberately deferred to Task 6 per ruling. |
| 2 → 3 | proposal ledger/runtime → bootstrap propose/activate | Same held store and transaction domain. |
| 2 → 4 | runtime proposal store → CLI/MCP list/show/confirm | Exact held runtime service is reused. |
| 2 ↔ 6 | framed ledger ↔ clarification event order | Task 6 adds a backward-compatible third payload kind per ruling. |
| 3 → 4 | `BootstrapService` → CLI/MCP onboarding | CLI capture-only and MCP typed proposal paths are now noncontradictory. |
| 3 → 6 | ChangeSet activation → governed confirmation | Both use the existing deterministic executor/shared transaction. |
| 4 ↔ 7 | focused MCP registration module | Task 7 extends the same module with preflight/verify; it does not alter bootstrap tools. |
| 4 → 10 | onboarding CLI/MCP → release proof/docs | Task 10 must exercise both capture-only CLI and agent-submitted proposal. |
| 5 → 6 | ambiguous/conflicting preflight → clarification/case governance | Stable task/evidence/baseline IDs flow forward. |
| 5 → 7 | authorized result → capability issuer | Only mechanical/aligned results can issue. |
| 5 → 8 | preflight/context → host `before_task` | Host delegates to versioned MCP; no duplicate classifier. |
| 6 → 10 | proposal confirmation → E2E new/conflict flows | Contributor and independent-review paths remain distinct. |
| 7 → 8 | issuer/verify MCP → host mutation decision | Token stays outside serialized host models. |
| 7 → 9 | grant → post-task scope comparison | Current graph/task/actor/repository/path bindings are reverified. |
| 8 ↔ 9 | `agent_host/base.py` before/after task | Task 9 extends post-task delegation without weakening pre-mutation enforcement. |
| 8 → 10 | host adapter → production E2E and opt-out docs | Release proof must demonstrate real denial or fixed unsupported-host refusal. |
| 9 → 10 | assurance/sync integration → scheduled workflow | Capture and assurance stay separate, idempotent, and non-writing externally. |

## Task status

- Task 1: fix round 1/5 (1 addressed, 0 open — proposal source-role canonicalization; commits `611a6ae..217a33e`)
- Task 1: complete (commits `e90c399..217a33e`, review clean)
- Task 1: minor (deferred): `TaskEnvelope.conversation_ref` also enforces a 512-byte limit, so some valid 512-character non-ASCII references are rejected.
- Task 1: review evidence ruling: implementer report contains complete RED/GREEN/focused/static/full commands and outputs; reviewer did not need to rerun them, so its `⚠️ Cannot verify` note is resolved from the report.
- Task 2: complete (implementer `/root/intent_workflow_task2`, base `217a33e`, commit `66ad92a`); required RED witnessed (`ModuleNotFoundError`), focused ledger/recovery/runtime `30 passed`, Ruff clean, mypy 105 files clean, and fresh full suite `1073 passed in 40.59s`. The known receipt-store fork race occurred once, passed in isolation, and the subsequent full rerun was clean. Detailed evidence: `task-2-report.md`.
- Task 2: fix round 1/5 complete (commit `7e08c7e`, 3 addressed, 0 open); narrow RED `7 failed, 28 deselected`, narrow GREEN `7 passed`, focused Task 2 `37 passed`, shared-storage regressions `29 passed`, Ruff/mypy clean, full suite `1080 passed in 38.36s`. Fixes cover typed canonical replay, real low-level interruption secrecy, and FIFO fail-fast append/force-init boundaries.
- Task 3: complete after clean Fix Round 1 re-review (implementer `/root/intent_workflow_task3`, base
  `7e08c7e`; 4 addressed, 0 open). Original mandatory RED was witnessed (`ModuleNotFoundError`,
  `1 error in 0.15s`). Review-fix focused RED was `6 failed, 1 passed, 46 deselected`; focused GREEN
  was `7 passed, 46 deselected`; all four identical/conflicting concurrency cases pass. Final
  bootstrap GREEN is `56 passed`; required Task 3/executor/validation gate is `80 passed`; Task 1/2 decision compatibility plus
  framework/transaction/recovery gate is `54 passed`; Ruff is clean on every changed Python path;
  mypy is clean across 106 source files; fresh full offline suite is `1136 passed in 49.18s`; and
  `git diff --check` is clean. Fixes bind review/decision to exact mutations, make identical
  concurrent proposal/activation replay semantic no-ops, scrub review cancellation tracebacks, and
  enforce bounded duplicate-free nested evidence. Detailed evidence: `task-3-report.md`;
  controlling strengthened scope: `task-3-brief.md`. Independent final verdict: 0 Critical / 0
  Important / 0 Minor, Ready; reviewer evidence was 10 fix-focused tests, 6 schema-1/canonical
  compatibility tests, and a clean diff check.
- Task 4: review approved after clean Fix Round 1 re-review (implementer `/root/intent_workflow_task4`, exact
  base `ef1c2ead86c634f818b78fac8b93d2b5b722c53a`; no commit/staging; 6 Important addressed, 0
  Important open). Required missing-surface RED was `9 failed in 2.18s`; review-fix tests-only RED
  was `9 failed, 12 passed in 5.25s`. Final Task 4 pair is `21 passed in 4.48s`; focused
  CLI/MCP/legacy gate is `58 passed in 23.28s`; adjacent descriptor/evidence/config/MCP regressions
  are `119 passed` and `33 passed`; all three help commands exit 0; Ruff is clean; mypy is clean
  across 108 source files; fresh full offline suite is `1157 passed in 43.41s`; and `git diff
  --check` is clean. The implementation provides
  descriptor-safe capture-only CLI onboarding, atomic canonical source roles, ACL-safe complete
  proposal review/TTY confirmation, and optional provider-neutral official-SDK MCP proposal tools
  without approval creation, external writes, or Task 5 preflight. Detailed evidence:
  `task-4-report.md`; controlling strengthened scope: `task-4-brief.md`. Final reviewer verdict:
  Ready, 0 new Critical / 0 new Important / 0 new Minor; reviewer evidence was 21 fix-focused tests,
  20 shared regressions, a clean diff check, and the reported full result of 1157 passing tests.
- Task 4: Minor (deferred): full-config `yaml.safe_dump` preserves unrelated semantic fields but
  drops unrelated YAML comments and lexical formatting. A bespoke comment-preserving rewriter was
  intentionally not introduced in Fix Round 1.
- Task 5: READY (implementer `/root/intent_workflow_task5`, exact base
  `3cef53684b2b44e0324284e53f8f22852b92446f`; no commit/staging). Required missing-module RED was
  `2 errors in 0.26s`. First independent review was Not Ready with `0 Critical / 6 Important /
  0 Minor`; Fix Round 1 tests-only RED was `21 failed, 46 passed in 1.05s` and final focused GREEN is
  `67 passed in 0.90s`. Fix Round 1 re-review was Not Ready with `0 Critical / 2 Important / 0 Minor`;
  Fix Round 2 tests-only RED was `2 failed, 73 passed in 1.21s`, focused GREEN is `75 passed in
  0.86s`, and the prior 22 security regressions pass separately. Task 5 plus context is `95 passed`;
  transaction/evidence/case/proposal/bootstrap/reconciliation compatibility is `172 passed`; Ruff
  is clean on all changed Python paths; mypy is clean across 110 source files; clean fresh full
  offline suite is `1232 passed in 44.56s`; and `git diff --check` is clean. Fix Round 1 derives ACL
  principals only from the held policy or trusted exact-snapshot resolver, atomically
  reauthenticates authorization preimages, uses latest
  case lifecycle versions, requires visible exact-semantic conflict reuse with concurrent winner
  validation, and validates every unresolved proposal candidate's nested ACL/provenance/type
  association. Fix Round 2 requires exact caller/resolver principal equality and nonempty complete
  source-role association for BOOTSTRAP proposals while retaining requirement-kind compatibility.
  No capability issuance, model/provider call, external write, clarification session, or host hook
  was added. Final scoped reviewer verdict: Ready, `0 Critical / 0 Important / 0 Minor`; reviewer
  evidence was 11 Round 2 tests plus the prior 22 security regressions, with the reported full
  result of 1232 passing tests. Detailed evidence: `task-5-report.md`; controlling strengthened
  scope: `task-5-brief.md`.
- Task 6: PRECOMMIT_REVIEW_READY (implementer `/root/intent_workflow_task6`, exact base
  `f1ca752818b39c7d76a8657fdd769cdc1f33ec92`; no commit/staging). Required missing-coordinator RED
  was `2 errors in 0.27s`; first expanded integration RED was `6 failed, 6 errors`; final self-audit
  RED was `3 failed, 30 passed`. Final clarification/governance GREEN is `33 passed`; required
  governance/mutation gate is `55 passed`; final ledger/bootstrap/preflight/recovery compatibility
  is `241 passed`; Ruff is clean on required and all changed Python paths; mypy is clean across 111
  source files; fresh full offline suite is `1265 passed in 50.50s`; and `git diff --check` is clean.
  The implementation adds evidence-only raw clarification, exact event/proposal chronology, strict
  backward-compatible ledger frames, detached typed proposals, deterministic local risk review,
  live person-level alias independence, and atomic decision/graph/history/case confirmation. It adds
  no capability, host hook, provider/model call, or external write. Detailed evidence:
  `task-6-report.md`; controlling strengthened scope: `task-6-brief.md`.
- Task 6: Fix Round 1/5 PRECOMMIT_REVIEW_READY after independent review reported `0 Critical / 5
  Important / 0 Minor`, Not Ready. Tests-only focused RED was `7 failed, 31 passed in 1.86s`; an
  additional earlier-question revision chronology probe was RED in isolation. Final focused GREEN
  is `39 passed in 1.17s`; combined governance/mutation/ledger/bootstrap/preflight/recovery is `247
  passed in 3.79s`; Ruff and mypy (111 source files) are clean; fresh full offline warnings-as-errors
  is `1271 passed in 64.96s`; diff-check is clean. The fixes durably associate divergent human turns
  as blocking conflict events, preserve all authoritative existing-semantic provenance, reject
  stale risk before case creation, derive exact affected references for every supported mutation
  group, and atomically bind session closure to the exact proposal/decision/activation. All work
  remains unstaged pending scoped re-review. Detailed evidence: `task-6-report.md`.
- Task 6: Fix Round 2/5 PRECOMMIT_REVIEW_READY after scoped re-review reported `0 Critical / 3
  Important / 0 Minor`, Not Ready. The authoritative tests-only RED was `6 failed, 38 passed in
  2.55s` after correcting one test-fixture-only `-W error` warning; no production edit preceded that
  RED. Final focused GREEN is `44 passed in 2.34s`; combined governance/mutation/ledger/bootstrap/
  preflight/recovery/executor/validation compatibility is `260 passed in 3.83s`; Ruff and mypy (111
  source files) are clean; fresh full offline warnings-as-errors is `1276 passed in 52.26s`; and
  diff-check is clean. A final evidence-side flag self-audit was tests-first (`4 failed, 40 passed`)
  and restored the focused suite to `44 passed in 1.23s` before all gates were rerun. The fixes make
  exact divergent-conflict replay byte-idempotent while retaining new conflict chronology, create
  distinct attributable current/proposal evidence sides for every affected current assertion, and
  require each proposed clarification event's immediate exact typed proposal association without
  rewriting legacy or incomplete-session ledgers. All work remains unstaged pending final scoped
  re-review. Detailed evidence: `task-6-report.md`.
- Task 6: Fix Round 3/5 PRECOMMIT_REVIEW_READY after final narrow re-review reported `0 Critical / 1
  Important / 0 Minor`, Not Ready. Tests-only focused RED was `6 failed, 41 passed in 3.79s`; final
  epistemic GREEN was `47 passed in 1.15s`. A final current-evidence ACL probe was independently RED
  at `1 failed, 47 passed` and brought the final focused suite to `48 passed in 1.22s`. Combined
  governance/mutation/ledger/bootstrap/preflight/recovery/executor/validation compatibility is `264
  passed in 3.32s`; Ruff and mypy (111 source files) are clean; fresh full offline warnings-as-errors
  is `1280 passed in 48.16s`; and diff-check is clean. Current and proposed review evidence now
  carries exact per-assertion source mode and confidence, with stable separate sides for mixed
  epistemics and grouping only across fully identical metadata. Added inferred nodes, derived
  current nodes, confidence changes, edges, and implementation status no longer receive invented
  explicit/1.0 defaults; inaccessible current evidence fails before case mutation. All work remains
  unstaged pending final narrow re-review. Detailed evidence: `task-6-report.md`.
- Task 6: Fix Round 4/5 PRECOMMIT_REVIEW_READY after the next final narrow review reported `0
  Critical / 1 Important / 0 Minor`, Not Ready. Exact tests-only focused RED was `1 failed, 48
  passed in 1.78s`; the expanded conflicting actor/time/subset/policy/stale characterization remained
  `1 failed, 53 passed`; final focused GREEN is `54 passed in 1.11s`, with the prior Round 3
  epistemic/ACL selection independently `7 passed`. Combined governance/mutation/ledger/bootstrap/
  preflight/recovery/executor/validation compatibility is `270 passed in 2.77s`; Ruff and mypy (111
  source files) are clean; fresh full offline warnings-as-errors is `1286 passed in 44.92s`; and
  diff-check is clean. Exact applied high-risk replay now authenticates current live authority and
  every durable decision/closure/activation/case/graph/history binding before returning a detached
  byte-noop `APPLIED` result; all conflicting inputs and stale state remain fixed failures. All work
  remains unstaged pending final narrow re-review. Detailed evidence: `task-6-report.md`.
- Task 6: Fix Round 5/5 PRECOMMIT_REVIEW_READY after scoped review reported `0 Critical / 2
  Important / 0 Minor`, Not Ready. Exact tests-only focused RED was `8 failed, 54 passed in 2.66s`;
  final focused GREEN is `62 passed in 1.54s`, and the prior epistemic/ACL/authenticated-replay
  matrix is `13 passed, 24 deselected`. Combined governance/mutation/ledger/bootstrap/preflight/
  recovery/executor/validation compatibility is `278 passed in 3.89s`; Ruff and mypy (111 source
  files) are clean. The first fresh full run encountered the known unchanged fork lock race once
  (`1 failed, 1293 passed`), its isolated probe passed, and the authoritative fresh full offline
  warnings-as-errors rerun is `1294 passed in 67.78s`. Replay now authenticates a canonical complete
  post-activation digest for every affected graph object plus an independently derived exact review
  case preimage and transition; same-version semantic/provenance tampering, ACL-hidden evidence
  manipulation, and coordinated case-version forgery fixed-fail without durable mutation. All work
  remains unstaged pending the same scoped reviewer. Detailed evidence: `task-6-report.md`.
- Task 6: Fix Round 6/6 PRECOMMIT_REVIEW_READY after the final narrow review reported `0 Critical /
  1 Important / 0 Minor`, Not Ready. The authoritative tests-only behavioral RED was `1 failed in
  0.88s` before production edits; the exact race GREEN is `1 passed in 0.29s` and complete focused
  clarification/governance GREEN is `63 passed in 1.36s`. The mandated governance/mutation gate is
  `87 passed in 1.92s`; broadened ledger/bootstrap/preflight/recovery/executor/validation/host
  compatibility is `303 passed in 3.73s`; Ruff and mypy (111 source files) are clean; and the fresh
  full offline warnings-as-errors suite is `1295 passed in 46.05s`. A final immutable live-snapshot
  bundle now reauthenticates exact actor aliases, contributor/reviewer role and independence,
  proposal and canonical current/proposed case evidence ACLs, source-role association,
  project/policy/provider binding, ledger, and baseline before decision construction. Exact evidence
  bytes join config/policy/binding preimages at the atomic activation boundary. The deterministic
  alias revocation during case persistence fixed-fails without any later graph or decision mutation.
  All work remains unstaged pending the same scoped reviewer. Detailed evidence: `task-6-report.md`.
- Task 6: Fix Round 7/7 PRECOMMIT_REVIEW_READY after narrow review reported `0 Critical / 1
  Important / 0 Minor`, Not Ready. The authoritative tests-only RED was `1 failed in 0.79s` before
  production edits; exact torn-state GREEN is `1 passed in 0.25s`, full focused clarification/
  governance is `64 passed in 1.54s`, and the replay/concurrency/tamper selection is `17 passed`.
  Mandated governance/mutation is `88 passed in 2.12s`; broadened ledger/bootstrap/preflight/
  recovery/executor/validation/host compatibility is `304 passed in 3.84s`; Ruff and mypy (111
  source files) are clean. The first full run encountered the known unchanged fork lock race once,
  its isolated case passed, and the authoritative fresh full offline warnings-as-errors rerun is
  `1296 passed in 54.29s`. Every `APPLIED` path now uses one fresh durable-state authenticator that
  requires exact decision, closure, activation history tail, complete graph effect/version, resolved
  case transition/preimage, and live authority/ACL; an exact decision+closed ledger with missing
  activation fixed-fails byte-noop. All work remains unstaged pending the same scoped reviewer.
  Detailed evidence: `task-6-report.md`.
- Task 6: READY. Final scoped Round 7 review verdict is `0 Critical / 0 Important / 0 Minor`, Ready.
  The reviewer accepted the complete focused, replay/concurrency/tamper, compatibility, static, and
  fresh full-suite evidence recorded in `task-6-report.md`; no further implementation change was
  requested. Authorized for the exact Task 6 commit.
- Task 7: pending
- Task 8: pending
- Task 9: pending
- Task 10: pending
