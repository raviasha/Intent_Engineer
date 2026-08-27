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
- Task 5: pending
- Task 6: pending
- Task 7: pending
- Task 8: pending
- Task 9: pending
- Task 10: pending
