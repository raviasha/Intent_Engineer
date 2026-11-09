# Task Source Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Refresh selected authoritative sources before development with unchanged-content reuse and user-decided source conflicts.

**Architecture:** A local service maintains source selections and task-bound read receipts behind the existing transaction coordinator. Host providers perform actual reads; source signals only invalidate cached state. CLI/MCP expose the service and the existing prompt hook/skill route the host agent through it.

**Tech Stack:** Python 3.12, Pydantic, existing descriptor-rooted stores, Typer, MCP, pytest.

**Spec:** `docs/superpowers/specs/2026-11-09-task-source-refresh.md`

## Global Constraints

- Preserve immutable evidence, stable identities, ChangeSet history and existing proposals.
- Never claim provider authentication or subscriptions from local receipts.
- No full content fetch for a verified unchanged scope-wide revision marker.
- No source-is-fresh conclusion from silence, failed reads or partial manifests.
- User decides authoritative-source conflicts; source content is data, not tool instructions.
- Current semantic scope is intent and requirements; no routine code/test matching.

## Review Focus

- A provider signal arriving during a read must not be erased by the old receipt.
- Collection validators must cover edits/deletions/new objects, not just a newest timestamp.
- A changed source plus unresolved disagreement must not return ready.
- Cached selections/receipts must not leak across actors or reuse a different task's check.
- Cached readiness must be re-evaluated after source configuration, graph or signal changes.

### Task 1: Source registry and refresh protocol

**Files:**
- Create: `src/intent_engineering/intent_workflow/source_refresh.py`
- Create: `tests/integration/intent_workflow/test_source_refresh.py`

**Interfaces:**
- Produces: `SourceSelection`, `SourceSignal`, `RefreshRequest`, and `SourceRefreshService` with `sources`, `signal`, and `refresh` methods.
- Consumes: `Runtime`, `BackgroundIntentService`, immutable source evidence and existing conversation submissions.

- [x] Write real-store tests for initial fetch, metadata reuse, changes, failures, races, isolation and conflict/user-decision readiness.
- [x] Run the tests and observe the missing behavior fail.
- [x] Implement strict models, actor-scoped source selection, task-bound checks and signal invalidation through transactions.
- [x] Verify all tests in this task pass; commit the service and tests.

Run: `PYTHONPATH=src /Users/rampetaravishankar/.local/share/intent-engineering/worktrees/onboarding-release/.venv/bin/python -m pytest tests/integration/intent_workflow/test_source_refresh.py -q`
Expected: all tests pass.

### Task 2: Agent, CLI and MCP workflow

**Files:**
- Modify: `src/intent_engineering/integrations/mcp_server/background.py`
- Create: `src/intent_engineering/cli/source_refresh.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/integrations/agent_host/advisory.py`
- Modify: `plugins/intent-advisor/skills/intent-advisor/SKILL.md`
- Create: `tests/e2e/test_cli_source_refresh.py`
- Modify: `tests/contract/mcp/test_background_tools.py`

**Interfaces:**
- Consumes: task 1 models/service.
- Produces: `intent_background_sources`, `intent_background_source_signal`, `intent_background_refresh`; equivalent CLI commands with live submission schemas.

- [x] Add failing public-boundary tests, including exact task replay and complete source-to-conflict-to-user-resolution flow.
- [x] Add public routes, bounded hook guidance and the explicit source refresh/authority workflow in the skill.
- [x] Run task 1 and CLI/MCP/hook tests; commit the integration.

Run: `PYTHONPATH=src /Users/rampetaravishankar/.local/share/intent-engineering/worktrees/onboarding-release/.venv/bin/python -m pytest tests/integration/intent_workflow/test_source_refresh.py tests/e2e/test_cli_source_refresh.py tests/contract/mcp/test_background_tools.py tests/contract/agent_host/test_advisory_plugin.py -q`
Expected: all tests pass; no source reads or graph mutations happen inside the prompt hook.

### Task 3: Documentation, review and installed verification

**Files:**
- Modify: `docs/quick-start.md`, `docs/superpowers/specs/2026-11-09-background-intent-workflow.md`, `plugins/intent-advisor/.codex-plugin/plugin.json`

**Interfaces:**
- Consumes: complete public workflow from tasks 1–2.
- Produces: documented current capability, reviewed branch and updated installed runtime/plugin.

- [x] Document metadata fallback, signal adapter boundary and failure/conflict behavior.
- [x] Run full pytest, ruff and relevant type checks; commit.
- [x] Request a fresh whole-branch review; address important findings with regression tests.
- [x] Update the local installed runtime and plugin from the verified checkout, preserving unrelated configuration.
- [x] Exercise the installed CLI/MCP with an isolated fixture and verify unchanged-source fast path and source-conflict blocking.

Run: `PYTHONPATH=src .venv/bin/python -m pytest --import-mode=importlib -q`
Expected: full suite passes; any platform-only skips reported.

## Completion evidence

- Full suite: 3,570 passed, 9 opt-in Docker/manual checks skipped. Ruff passed; mypy passed for the five changed service/integration modules.
- Targeted source refresh, CLI, MCP and real hook suite: 56 passed.
- One independent whole-branch review found three important defects; each was reproduced with a failing test and fixed: concurrent first captures, source questions across chats, and graph changes between tasks.
- Installed and enabled local plugin0.1.4 and installed the verified wheel into the existing CLI environment. Marketplace now points to this retained worktree. No remote publication.
- Actual installed CLI/stdio MCP smoke passed unchanged-source zero-content-fetch reuse, conflict blocking, exact user decision, signal invalidation/idempotency and canonical validation. Installed assets match the reviewed plugin source.
- The current project graph was separately validated after correcting two supported product-intent links: 15 requirements connect to3 product intents, with no structural orphan in either direction. This is not proof of semantic completeness.
- Provider subscriptions remain a separate integration; receipts are host-reported and the hook is advisory. Restart the desktop app to load the updated local plugin.
