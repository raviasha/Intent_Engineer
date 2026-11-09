# Background intent workflow implementation plan

> Execute inline using superpowers:executing-plans and test-driven-development.

**Goal:** Background intent capture and maintenance with consequential chat questions only.
**Architecture:** Bounded conversational service on the existing local stores and atomic
ChangeSet executor; advisory hook selects that path without global readiness gates.
**Tech stack:** Python, Pydantic, Typer, MCP, YAML, JSONL; no new dependencies.
**Spec:** ../specs/2026-11-09-background-intent-workflow.md

## Global constraints

Preserve stable IDs, provenance, immutable evidence, typed validation and history.
Never fabricate approvals, activate old proposals, overwrite disputes or require exports.
Keep user statements separate from agent inferences; retain intent/requirements scope.

## Review focus

- Duplicate turns and divergent replay must not duplicate or rewrite graph state.
- Stale versions, unavailable evidence and invalid edges must leave all stores unchanged.
- Unrelated tasks must proceed despite pending proposals or other conversations' questions.
- Answer reuse must be scoped to the right conversation and cited question.
- Existing disagreements and independently authenticated approval contracts must survive.

## Task 1: Conversational persistence

- [x] Add failing integration tests for silent classifications, bootstrap, source distinction,
  atomic validation, replay, clarification answers, revisions and conflict preservation.
- [x] Implement `BackgroundIntentService.context` and `.submit` in
  `src/intent_engineering/intent_workflow/background.py`, with bounded models in
  `background_models.py`; use existing evidence, graph and transaction services.
- [x] Run the new tests and existing workflow/storage regressions.

## Task 2: Host and public interfaces

- [x] Add failing CLI/MCP/hook tests for missing graph, aligned work, pending proposals,
  conversational replies and optional reports.
- [x] Add `intent background` context/submission/schema bridge and MCP tools; route the
  default advisory hook without global onboarding/readiness review or dashboard startup.
- [x] Update readiness/onboarding projections to distinguish pending data from task gating.
- [x] Add first-use source-connected setup in a separate host chat, durable handoff state,
  reminder after three completed turns, and versioned source capture/update through CLI/MCP.
- [x] Run host, CLI and MCP regressions.

## Task 3: Instructions and verification

- [x] Update plugin instructions, README, superseded design notes and dogfood graph via a
  public ChangeSet with source evidence and history.
- [x] Run full suite with importlib collection, lint, typing and graph validation.
- [x] Obtain independent whole-change review, address consequential findings with tests,
  and report implementation versus installed-runtime status accurately.

## Execution record

- User authorized implementation in the handoff; no repeated design approval requested.
- Work branch: `feat/background-intent`, clean baseline `26e7430` in the specified checkout.
- Bare pytest has six pre-existing duplicate-module collection errors; use importlib mode.
- Independent reviewer found three issues: existing-side conflict provenance, local evidence
  ACL widening, and inability to decide after preserving a disagreement. Each was reproduced
  in a failing test, fixed and verified. No human approvals were synthesized.
- User's follow-up added the separate-chat first-use offer, connections to selected sources,
  and deferred reminders. The host owns real chat creation and connection authentication;
  the Python handoff packet does not impersonate either capability.
- Source capture preserves exact provider content and revision metadata. Tests cover unchanged
  rereads, new versions, revision collisions, content tampering, graph updates from the same
  source/author, and rejection of silent changed-author revisions.
- The dogfood graph was updated through two public ChangeSets (versions 3 to 5). Replaying
  those changes against the original graph reproduces the shipped graph exactly.
- The first resource-constrained broad runs encountered disk exhaustion; no user files were
  removed. With space available, the first complete regression attempt stopped at 1,043
  passes and three old-default assertions. Those compatibility checks were updated for the
  explicit governed preset/new background tool surface; all 17 affected tests then passed.
- Installed CLI/plugin activation and live provider authentication are separate from this
  source checkout. Existing disabled hook/skill preferences are preserved.
- Final full suite: 3,543 passed, 9 skipped, with two obsolete documentation assertions.
  After updating those assertions and labeling the retained guides as explicit governed
  workflows, all five public-alpha tests passed. The final `pytest --import-mode=importlib
  --lf --last-failed-no-failures=none` rerun passed both previously failing tests. Thus all
  3,545 executed tests pass across the full run and focused rerun; the nine opt-in Docker/manual
  checks remain skipped. Ruff passes; mypy reports no issues in 194 source files.
