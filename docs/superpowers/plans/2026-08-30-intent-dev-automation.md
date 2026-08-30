# Intent Developer Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `intent ensure`, consolidated `intent check`, and a safe plugin preset so aligned development normally requires no manual Intent command.

**Architecture:** Readiness and assurance are separate deterministic services composed by thin CLI commands. The plugin invokes only idempotent readiness; test and Git observation create evidence but never assert human intent or task completion.

**Tech Stack:** Python 3.12, Typer, existing Runtime/sync/assurance services, Codex plugin hooks, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-intent-dev-control-plane-design.md`

## Global Constraints

- Milestone 1 is complete and `intent dev`/ControlPlaneService interfaces are stable.
- Automatic operations grant no human authority and perform no graph, conflict, publication, or provider approval.
- Every command has stable strict JSON output and bounded fixed failures.
- Test commands come only from reviewed project configuration; agent input never becomes a subprocess command.
- Existing granular commands remain compatible.

---

### Task 1: Readiness service and `intent ensure`

**Files:**
- Create: `src/intent_engineering/intent_workflow/readiness.py`
- Modify: `src/intent_engineering/cli/dev.py`
- Test: `tests/unit/intent_workflow/test_readiness.py`
- Test: `tests/e2e/test_cli_intent_ensure.py`

**Interfaces:**
- Produces `ReadinessService.ensure(EnsureRequest) -> EnsureResult` and CLI `intent ensure --preset developer --format json`.

```python
class ReadinessService:
    def ensure(self, request: EnsureRequest) -> EnsureResult: ...
```

- [ ] Write RED tests for `ready`, onboarding required, human attention, offline stale, unavailable/invalid state, upgrade required, exact replay, concurrent invocation, config/graph/state drift, cancellation, and secret-free output.
- [ ] Run the two focused test files and witness missing interfaces.
- [ ] Implement strict frozen models and one-snapshot readiness using existing validation/onboarding/case projections; call the control-plane lifecycle only after validation.
- [ ] Run focused plus onboarding/context/agent-host compatibility.
- [ ] Commit with `feat: ensure intent developer readiness`.

### Task 2: Consolidated check orchestrator

**Files:**
- Create: `src/intent_engineering/intent_workflow/check.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/cli/runtime.py`
- Test: `tests/integration/intent_workflow/test_check.py`
- Test: `tests/e2e/test_cli_intent_check.py`

**Interfaces:**
- Produces `CheckService.run(CheckRequest) -> CheckResult` and CLI `intent check [--ci] [--require-review] [--sources ...] [--test-results PATH]`.

```python
class CheckService:
    async def run(self, request: CheckRequest) -> CheckResult: ...
```

- [ ] Write RED tests proving ordered restore/readiness → source capture → validation → assurance → drift render and stable exit codes.
- [ ] Add strict test-result input tests: descriptor-safe regular file, canonical schema, repository/commit binding, fixed size, timestamp, passing status, no duplicate IDs, ACL, cancellation, and byte-noop replay.
- [ ] Implement orchestration by composing existing services; do not duplicate detector or validator logic.
- [ ] Prove partial connector failure cannot corrupt state and `--ci` never initializes or approves.
- [ ] Run sync/assurance/validation/CLI compatibility and commit `feat: consolidate intent assurance checks`.

### Task 3: Reviewed test configuration and Git observation

**Files:**
- Modify: `src/intent_engineering/core/models/project.py`
- Create: `src/intent_engineering/intent_workflow/dev_observer.py`
- Modify: `src/intent_engineering/control_plane/service.py`
- Test: `tests/unit/intent_workflow/test_dev_observer.py`
- Test: `tests/unit/core/models/test_project.py`

**Interfaces:**
- Adds bounded `ProjectConfig.test_commands: tuple[tuple[str, ...], ...]` and `test_result_paths: tuple[str, ...]`.
- Produces `DevObserver.poll() -> ObservationResult` containing evidence candidates only.

```python
class DevObserver:
    def poll(self, *, at: datetime) -> ObservationResult: ...
    async def run_reviewed_tests(self, command_id: str, *, at: datetime) -> TestRunResult: ...
```

- [ ] Write RED tests rejecting shell strings, metacharacter expansion, absolute/parent/device paths, duplicates, oversized commands, unreviewed executable changes, and foreign Git repositories.
- [ ] Implement argv-only reviewed commands; default observer records Git HEAD/path changes and ingests existing result artifacts but does not run commands in background.
- [ ] Add explicit UI/`intent check` action to run configured argv with bounded time/output and immutable result capture.
- [ ] Prove observation cannot create implementation-complete claims.
- [ ] Commit `feat: observe bounded development evidence`.

### Task 4: Developer plugin preset

**Files:**
- Modify: `plugins/intent-advisor/hooks/hooks.json`
- Modify: `plugins/intent-advisor/scripts/prompt-hook`
- Modify: `plugins/intent-advisor/skills/intent-advisor/SKILL.md`
- Modify: `src/intent_engineering/integrations/agent_host/advisory.py`
- Test: `tests/contract/agent_host/test_advisory_plugin.py`
- Test: `tests/contract/agent_host/test_advisory_prompt.py`

**Interfaces:**
- UserPromptSubmit runs exact readiness before advisory routing; statuses map to fixed additionalContext instructions.

```python
def readiness_context(result: EnsureResult) -> str: ...
```

- [ ] Write RED tests for first-prompt automatic readiness, process reuse, offline stale, invalid state, onboarding offer, human-attention UI route, disabled preset, hostile hook input, cancellation, and zero authority/token leakage.
- [ ] Implement the bounded `intent ensure --preset developer` call with timeout and fixed fail-closed guidance; never recursively invoke the plugin or MCP.
- [ ] Preserve official hook schema and plugin validator compatibility.
- [ ] Run all agent-host/MCP/plugin tests and commit `feat: enable automatic intent developer preset`.

### Task 5: Required-check workflow and release proof

**Files:**
- Modify: `.github/workflows/intent-sync.yml`
- Create: `.github/workflows/intent-check.yml`
- Modify: `README.md`
- Modify: `docs/intent-aware-agent.md`
- Test: `tests/e2e/test_github_action_workflow.py`
- Test: `tests/e2e/test_intent_dev_automation.py`

**Interfaces:**
- Workflow runs reviewed repository tests, writes canonical test-result evidence, then `intent check --ci --require-review`.

```yaml
- name: Intent Engineering check
  run: intent check --ci --require-review --test-results .intent-ci/test-results.json
```

- [ ] Write structural RED tests for checkout/install/test/result/check ordering, permissions, no plugin dependency, no masked failures, concurrency cancellation, artifact retention, and exact required-check name `Intent Engineering / check`.
- [ ] Implement workflow and docs; keep scheduled capture separate from PR required check.
- [ ] E2E a fresh onboarded clone, zero-command first prompt, aligned feature/test commit, required check pass, ambiguous/conflicting check failure, and plugin-disabled GitHub backstop.
- [ ] Run focused, broad, full offline, Ruff/format/mypy, plugin validator, help, workflow, and diff gates.
- [ ] Commit `feat: automate intent readiness and checks`.
