# Task 6 — At-most-once approved execution and auditable receipts

Status: DONE. Task 7 CLI wiring has not started.

Base: `54e1c6c`.

Product/tests commit: `7b9309b` (`feat: execute approved writes at most once`).

## Outcome

Added a provider-neutral `WriteExecutor` that strictly reloads and revalidates one immutable write plan and one independent approval before it can call an external mutation capability. The executor reauthenticates contributor, approver, and executor policies; person-level aliases; profile and binding identity; operation/object compatibility; complete binding and write-contract hashes; target/version guards; bound arguments; and the active plan/approval window.

Execution durably claims the exact `(plan_id, approval_id)` pair before refetching or mutating. A target must still match the approved connector, profile, object type, object ID, version, and complete semantic content. A changed target produces a terminal rejected receipt and zero provider writes. Permission and provider failures produce only fixed redacted codes. A provider mutation is attempted once, never retried automatically. An incomplete durable claim is a manual-recovery state; a completed retry returns the immutable existing receipt, including after the original approval expires.

Added a canonical append-only receipt ledger with strict duplicate-key JSON, exact byte serialization, claim-before-completion ordering, actor binding, conflict detection, cross-instance locking, and descriptor-safe symlink/hardlink rejection.

Successful writes produce authorship-preserving evidence that binds the proposer and aliases, approver and aliases, executor, conflicting authors, source evidence, provider/profile/binding/write-contract identity, approved resolution action, exact semantic result, and prior/result versions. `LocalWriteCommitter` independently revalidates the receipt/plan/approval/result/case-authorship bundle, then atomically commits receipt completion, evidence, case resolution, graph version, and ChangeSet history through `LocalTransactionCoordinator`.

Crash tests cover every prepared journal stage and the committed-journal boundary. Before the transaction commit point, recovery restores graph/history/evidence/case/receipt completion exactly while retaining the durable execution claim, preventing a second provider mutation. After the commit point, recovery preserves the complete successful state and retry returns its receipt without another write. Cancellation and interrupt signals retain their exact control-flow identity while provider arguments and hostile values are cleared from repository traceback locals.

## TDD evidence

The initial tests-only RED established missing executor, receipt store, and execution semantics. Focused GREEN implemented strict claims, exact refetch, one provider call, fixed failure receipts, and successful local commit.

Subsequent RED/GREEN slices added cross-store crash recovery, concurrent execution ownership, descriptor-path attacks, canonical JSON corruption, cancellation at fetch/write/commit boundaries, unexpected local-commit failure, result-version validation, immutable completed-receipt replay after expiry, and independent committer validation. The committer regression was non-vacuous: it first persisted a valid durable claim, then proved the baseline wrongly accepted approval-plan, executor, and live-case-author mismatches. The committer now rejects all three with no graph/history/evidence/case/receipt-completion effects.

The first independent review found three Critical and two Important defects: executor/committer separation-of-duties could be forged, provider targets were not tied to the current case, a first-created claim lacked parent-directory fsync, provider result payloads were trusted without post-write semantic verification, and receipt-store failures bypassed the sanitized boundary. Focused REDs reproduced each issue. The fix shares current person-level approval reauthentication across approval/executor/committer, hashes a canonical case target reference, fsyncs file then directory, verifies the exact approved post-write target through a second read, persists only a fixed locally generated result summary, and wraps all receipt reads/claims/completions in fixed-error/cancellation-safe boundaries. Real two-process claim contention and get/claim/complete cancellation regressions close the review's coverage gaps.

The first re-review confirmed those fixes but found two remaining Important direct-boundary gaps: the independently callable committer still accepted arbitrary result payloads, and cancellation during approval reload could retain the already-loaded private plan. RED/GREEN fixes require the committer's exact locally normalized result and detach validation-time interrupts before returning any plan. A five-case contributor/approver/executor/profile/binding drift matrix also proves failure before claim or provider access. Final independent review: 0 Critical / 0 Important / 0 Minor; Ready.

## Files

- `src/intent_engineering/mutations/{models,planner,committer,executor}.py`
- `src/intent_engineering/storage/jsonl/receipt_store.py`
- `tests/unit/mutations/{test_planner,test_approval}.py`
- `tests/unit/storage/test_receipt_store.py`
- `tests/integration/mcp/{test_write_execution,test_write_conflict,test_write_permission_denied}.py`

## Verification

| Gate | Result |
| --- | --- |
| Task 6 focused selection | 93 passed |
| broadened MCP/mutation/storage selection | 265 passed |
| full offline suite with warnings as errors | 954 passed in 28.21s |
| tracked plus Task 6 Ruff | clean |
| mypy tracked source plus Task 6 | 91 source files, clean |
| `git diff --check` | clean |
| independent review | 0 Critical / 0 Important / 0 Minor; Ready |

The five protected untracked artifacts remain untouched. Tests use no live provider, network credential, GUI, or raw Git object access.
