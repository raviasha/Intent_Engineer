# Initial Reconciliation Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add resumable, nonblocking, evidence-backed reconciliation of intent, requirements and code, with source priority and no assessed-code writes.

**Architecture:** A host agent does source reads and semantic comparisons in the existing separate setup chat. A local assessment service snapshots explicit file scope and selected source manifests, checkpoints comparison records, validates completeness/freshness and atomically publishes code mappings and reconciliation cases. CLI/MCP and onboarding expose the same service; no model dependency or fake background daemon is introduced.

**Tech Stack:** Existing Python 3.12, Pydantic, Typer, MCP, immutable evidence ledger and ChangeSet executor.

**Spec:** docs/superpowers/specs/2026-11-09-prd-consistency-check-design.md

## Global Constraints

- Source intent has highest priority; conflicting source positions remain explicit.
- Assessment must not modify assessed code or source documents.
- User work continues; background host jobs checkpoint and resume without short semantic deadlines.
- Requirements-to-code traceability is in scope; test-case-to-code traceability is deferred.
- Completion means checked within the reported scope, never universal semantic proof.
- Use existing transactions, access checks, provenance and public model validation.

## Review Focus

- Changed graph, source membership, or file contents must invalidate completed/replayed assessments (Task 1).
- Missing/unknown/deferred coverage must remain distinguishable; incomplete comparisons cannot become ready (Task 1).
- Existing graph/code nodes and cases must not be overwritten or duplicated by retries (Task 1).
- Excluded files, traversal, symlinks and private evidence must not be read or disclosed (Task 1).
- Both setup entry paths must expose pending assessment; normal work and host resume must remain unblocked (Task 2).

### Task 1: Durable initial assessment and code traceability

**Files:** Create intent_workflow/initial_reconciliation_models.py, initial_reconciliation.py and tests/integration/intent_workflow/test_initial_reconciliation.py.

**Interfaces:** InitialReconciliationRequest(action, session_id, turn_id, run_id, files, comparisons); InitialReconciliationService.run(request) -> dict. Files have relative path and role; comparisons have axis, subject, related references, status, evidence, explanation, recommendation and confidence. Status returns required comparison slots and snapshot evidence refs.

- [ ] Write real-store tests for resumable/idempotent start/checkpoint, all five comparison axes, incomplete completion, source authority evidence, code mappings, durable cases and unchanged target files.
- [ ] Run tests to see the missing-service failure.
- [ ] Implement typed bounded requests; snapshot explicitly scoped files and selected source manifests; validate five-axis coverage; record progress in immutable conversation evidence.
- [ ] Publish FILE nodes and supported IMPLEMENTED_BY edges plus nonaligned findings as cases through one validated ChangeSet. Preserve deferrals and unknowns in the result, with recommendations. Never mutate inspected artifacts.
- [ ] Add stale-input, authorization, traversal/symlink, conflicting-source, exact-retry and no-automatic-resolution tests. Run the focused suite and lint/type checks.
- [ ] Commit the service and tests.

### Task 2: Public workflow and host background routing

**Files:** CLI initial_reconciliation.py and app.py; MCP background.py; background_onboarding.py; onboarding_assistance.py; agent_host/advisory.py; plugin skills/references; CLI/MCP/onboarding tests.

**Interfaces:** intent initial-reconciliation --submission/--submission-schema; intent_initial_reconciliation(draft). background-onboarding --finished consumes a fresh ready assessment; governed bootstrap exposes assessment as pending without claiming reconciled completion.

- [ ] Write failing CLI/MCP tests for the same persisted lifecycle and onboarding completion checks.
- [ ] Add public tools with fixed error boundaries and no provider reads hidden in the service.
- [ ] Teach setup instructions to collect sources, build semantic intent, start/checkpoint/finish scoped comparisons, resume in the real host chat, and present consolidated findings only at readiness.
- [ ] Keep normal task execution nonblocking while setup is in progress; preserve readiness limits and source-conflict decisions.
- [ ] Run contract/e2e tests and commit workflow changes.

### Task 3: Documentation, review and publication

**Files:** README, quick start, foundational current-scope docs, plugin release metadata and contract expectations.

- [ ] Update stale code-tracing deferral guidance and document the full host-operated workflow, scope limits and test tracing deferral.
- [ ] Run repository CI commands: ruff check ., mypy --strict src, and full pytest with plugin autoload disabled and anyio plugin enabled.
- [ ] Obtain an independent whole-change review; fix material findings with regression tests.
- [ ] Fetch origin/main, preserve remote changes, verify the integrated tree and push a normal fast-forward to main. Verify the remote commit and report CI status accurately.
