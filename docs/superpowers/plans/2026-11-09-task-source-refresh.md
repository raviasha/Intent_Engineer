# Task Source Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

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

- [ ] Write real-store tests for initial fetch, metadata reuse, changes, failures, races, isolation and conflict/user-decision readiness.
- [ ] Run the tests and observe the missing behavior fail.
- [ ] Implement strict models, actor-scoped source selection, task-bound checks and signal invalidation through transactions.
- [ ] Verify all tests in this task pass; commit the service and tests.

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

- [ ] Add failing public-boundary tests, including exact task replay and complete source-to-conflict-to-user-resolution flow.
- [ ] Add public routes, bounded hook guidance and the explicit source refresh/authority workflow in the skill.
- [ ] Run task 1 and CLI/MCP/hook tests; commit the integration.

Run: `PYTHONPATH=src /Users/rampetaravishankar/.local/share/intent-engineering/worktrees/onboarding-release/.venv/bin/python -m pytest tests/integration/intent_workflow/test_source_refresh.py tests/e2e/test_cli_source_refresh.py tests/contract/mcp/test_background_tools.py tests/contract/agent_host/test_advisory_plugin.py -q`
Expected: all tests pass; no source reads or graph mutations happen inside the prompt hook.

### Task 3: Documentation, review and installed verification

**Files:**
- Modify: `docs/quick-start.md`, `docs/superpowers/specs/2026-11-09-background-intent-workflow.md`, `plugins/intent-advisor/.codex-plugin/plugin.json`

**Interfaces:**
- Consumes: complete public workflow from tasks 1–2.
- Produces: documented current capability, reviewed branch and updated installed runtime/plugin.

- [ ] Document metadata fallback, signal adapter boundary and failure/conflict behavior.
- [ ] Run full pytest, ruff and relevant type checks; commit.
- [ ] Request a fresh whole-branch review; address important findings with regression tests.
- [ ] Update the local installed runtime and plugin from the verified checkout, preserving unrelated configuration.
- [ ] Exercise the installed CLI/MCP with an isolated fixture and verify unchanged-source fast path and source-conflict blocking.

Run: `PYTHONPATH=src /Users/rampetaravishankar/.local/share/intent-engineering/worktrees/onboarding-release/.venv/bin/python -m pytest -q`
Expected: full suite passes; any platform-only skips reported.
