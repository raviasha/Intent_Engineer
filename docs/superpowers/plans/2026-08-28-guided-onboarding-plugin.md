# Guided Onboarding and Prompt-Time Intent UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a guided `intent onboard` journey and an advisory Codex plugin that checks repository readiness after each human prompt, routes initialized repositories through existing intent preflight tools, and leaves scheduled assurance CLI-driven.

**Architecture:** A new read-only onboarding-state service reports whether an approved baseline exists. The CLI composes initialization, PRD capture, source-role assignment, proposal review, and confirmation without duplicating bootstrap logic. A small stdin/stdout hook adapter emits advisory prompt instructions only; the plugin delegates all semantic work to existing MCP tools, while canonical state remains owned by current deterministic services.

**Tech Stack:** Python 3.12, Typer, Pydantic v2, official MCP SDK, Codex plugin manifest/hooks/skills, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-28-guided-onboarding-plugin-design.md`

## Global Constraints

- The plugin is advisory and must never claim complete mutation interception.
- `MandatoryHookUnavailable` remains the result for mandatory Codex enforcement.
- The plugin never receives, prints, stores, or transports authorization tokens.
- Canonical graph, evidence, proposal, decision, case, and history writes remain in existing deterministic services.
- Onboarding requires explicit human confirmation before source capture and before baseline activation.
- Scheduled assurance remains executable through the CLI with no plugin installed.
- All file reads and writes must preserve the repository's descriptor-safe, bounded, nonblocking, fail-closed storage conventions.
- The five protected untracked workspace artifacts must not be read, edited, staged, or committed.

---

### Task 1: Define Read-Only Onboarding State

**Files:**
- Create: `src/intent_engineering/intent_workflow/onboarding.py`
- Modify: `src/intent_engineering/intent_workflow/__init__.py`
- Create: `tests/unit/intent_workflow/test_onboarding.py`

**Interfaces:**
- Consumes: an `OnboardingRuntime` protocol exposing `graph_store` and `intent_proposals`, plus existing graph validation conventions; it must not import the CLI `Runtime` into the application layer.
- Produces: `OnboardingStatus`, `OnboardingState`, and `inspect_onboarding(runtime: OnboardingRuntime) -> OnboardingStatus`.

- [ ] **Step 1: Write strict state-model and baseline-detection tests**

```python
def test_empty_initialized_repository_requires_onboarding(runtime) -> None:
    status = inspect_onboarding(runtime)
    assert status == OnboardingStatus(
        state=OnboardingState.REQUIRED,
        graph_version=0,
        active_node_count=0,
        pending_proposal_ids=(),
    )


def test_active_baseline_is_ready(runtime_with_confirmed_baseline) -> None:
    status = inspect_onboarding(runtime_with_confirmed_baseline)
    assert status.state is OnboardingState.READY
    assert status.graph_version == 1
    assert status.active_node_count > 0


def test_pending_proposal_is_reported_without_becoming_a_baseline(runtime_with_proposal) -> None:
    status = inspect_onboarding(runtime_with_proposal)
    assert status.state is OnboardingState.REVIEW_REQUIRED
    assert status.graph_version == 0
    assert status.pending_proposal_ids == (runtime_with_proposal.proposal_id,)
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_onboarding.py -q -W error
```

Expected: collection fails because `intent_engineering.intent_workflow.onboarding` does not exist.

- [ ] **Step 3: Implement the frozen onboarding models and inspector**

```python
class OnboardingState(StrEnum):
    REQUIRED = "required"
    REVIEW_REQUIRED = "review_required"
    READY = "ready"


class OnboardingStatus(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    state: OnboardingState
    graph_version: int
    active_node_count: int
    pending_proposal_ids: tuple[str, ...]


class OnboardingRuntime(Protocol):
    graph_store: GraphStore
    intent_proposals: IntentProposalStore


def inspect_onboarding(runtime: OnboardingRuntime) -> OnboardingStatus:
    graph = runtime.graph_store.load()
    pending = tuple(
        proposal.id
        for proposal in runtime.intent_proposals.list()
        if runtime.intent_proposals.decision_for(proposal.id) is None
    )
    state = (
        OnboardingState.READY
        if graph.version > 0 and graph.nodes
        else OnboardingState.REVIEW_REQUIRED
        if pending
        else OnboardingState.REQUIRED
    )
    return OnboardingStatus(
        state=state,
        graph_version=graph.version,
        active_node_count=len(graph.nodes),
        pending_proposal_ids=pending,
    )
```

Validate graph invariants before returning `READY`, bound pending IDs using the proposal-store limit, return detached data, and map malformed or inconsistent durable state to one fixed onboarding failure.

- [ ] **Step 4: Run focused and compatibility tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_onboarding.py tests/integration/intent_workflow/test_bootstrap.py tests/unit/intent_workflow/test_proposal_store.py -q -W error
```

Expected: all pass.

- [ ] **Step 5: Commit the onboarding-state slice**

```bash
git add src/intent_engineering/intent_workflow/onboarding.py src/intent_engineering/intent_workflow/__init__.py tests/unit/intent_workflow/test_onboarding.py
git commit -m "feat: inspect intent onboarding state"
```

---

### Task 2: Add the Guided `intent onboard` CLI

**Files:**
- Modify: `src/intent_engineering/cli/intent_workflow.py`
- Modify: `src/intent_engineering/cli/app.py`
- Create: `tests/e2e/test_cli_intent_onboard.py`
- Modify: `tests/e2e/test_cli_intent_bootstrap.py`

**Interfaces:**
- Consumes: `inspect_onboarding`, `_bootstrap_result`, `_source_role_result`, `proposal_payload`, existing proposal confirmation, `BootstrapService`, and `ProposalTerminal`.
- Produces: `onboard_command(prd, project, output_format, yes)` registered as `intent onboard` and a stable `OnboardingCommandResult` JSON projection.

- [ ] **Step 1: Write tests for offer, decline, capture, review, and replay**

```python
def test_onboard_requires_confirmation_before_capture(clean_project, monkeypatch) -> None:
    before = durable_bytes(clean_project)
    result = runner.invoke(app, ["onboard", "--project", str(clean_project), "--prd", "docs/prd.md"])
    assert result.exit_code == 1
    assert "Start guided onboarding now?" in result.output
    assert durable_bytes(clean_project) == before


def test_onboard_yes_captures_prd_and_returns_agent_proposal_next_action(clean_project) -> None:
    result = runner.invoke(
        app,
        ["onboard", "--project", str(clean_project), "--prd", "docs/prd.md", "--yes", "--format", "json"],
    )
    payload = json.loads(result.stdout)
    assert payload["state"] == "proposal_required"
    assert payload["source_role"]["role"] == "declared_intent"
    assert payload["next_action"] == "intent_bootstrap_propose"
    assert payload["authorization_issued"] is False


def test_onboard_existing_proposal_displays_exact_review_without_recapture(project_with_proposal) -> None:
    before = durable_bytes(project_with_proposal.path)
    result = runner.invoke(app, ["onboard", "--project", str(project_with_proposal.path), "--prd", "docs/prd.md", "--yes", "--format", "json"])
    payload = json.loads(result.stdout)
    assert payload["state"] == "review_required"
    assert payload["proposal"]["proposal_id"] == project_with_proposal.proposal_id
    assert durable_bytes(project_with_proposal.path) == before


def test_onboard_ready_repository_is_a_semantic_noop(project_with_baseline) -> None:
    before = durable_bytes(project_with_baseline.path)
    result = runner.invoke(app, ["onboard", "--project", str(project_with_baseline.path), "--prd", "docs/prd.md", "--yes", "--format", "json"])
    assert json.loads(result.stdout)["state"] == "ready"
    assert durable_bytes(project_with_baseline.path) == before
```

Also test unsafe/symlink/FIFO/oversize PRD paths, non-interactive confirmation refusal, cancellation identity, fixed errors, traceback-local secrecy, config replacement, and exact proposal-digest confirmation.

- [ ] **Step 2: Run the CLI tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_intent_onboard.py -q -W error
```

Expected: fail because `intent onboard` is not registered.

- [ ] **Step 3: Implement the command as a state-machine composition**

Implement these stable states:

```python
type OnboardState = Literal[
    "confirmation_required",
    "proposal_required",
    "review_required",
    "ready",
]
```

Command behavior:

- inspect before every mutation;
- when `REQUIRED` and `--yes` is absent, show the offer and exit without loading PRD bytes;
- when `REQUIRED` and `--yes` is present, reuse the exact bootstrap capture and source-role code, then return `proposal_required` with the public MCP tool name;
- when `REVIEW_REQUIRED`, return the existing bounded proposal preview and require the existing exact digest confirmation path for activation;
- when `READY`, return a byte-noop summary;
- never generate an agent proposal inside the CLI and never mint authorization.

- [ ] **Step 4: Run onboarding, legacy bootstrap, help, and static gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_intent_onboard.py tests/e2e/test_cli_intent_bootstrap.py tests/integration/intent_workflow/test_bootstrap.py -q -W error
.venv/bin/intent onboard --help
.venv/bin/ruff check src/intent_engineering/cli/intent_workflow.py src/intent_engineering/cli/app.py tests/e2e/test_cli_intent_onboard.py
.venv/bin/mypy src
```

Expected: all tests and static checks pass; help exits 0.

- [ ] **Step 5: Commit the guided CLI slice**

```bash
git add src/intent_engineering/cli/intent_workflow.py src/intent_engineering/cli/app.py tests/e2e/test_cli_intent_onboard.py tests/e2e/test_cli_intent_bootstrap.py
git commit -m "feat: guide initial intent onboarding"
```

---

### Task 3: Add Token-Free Prompt Routing and Clarification MCP Ports

**Files:**
- Create: `src/intent_engineering/integrations/agent_host/advisory.py`
- Modify: `src/intent_engineering/integrations/agent_host/__init__.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/integrations/mcp_server/intent_workflow.py`
- Modify: `src/intent_engineering/integrations/mcp_server/server.py`
- Create: `tests/contract/agent_host/test_advisory_prompt.py`
- Modify: `tests/contract/mcp/test_intent_workflow_tools.py`

**Interfaces:**
- Consumes: `inspect_onboarding(runtime)`, exact hook JSON from stdin, current project resolution, and existing public MCP tool names.
- Produces: `AdvisoryPromptRouter.route(event: PromptEvent) -> PromptRoute`, hidden CLI entry point `intent agent-prompt-hook` using stdin/stdout only, and public MCP tools `intent_clarification_open`, `intent_clarification_answer`, `intent_clarification_propose`, and `intent_clarification_confirm`.

- [ ] **Step 1: Write strict prompt-router contract tests**

```python
def test_uninitialized_repository_returns_onboarding_offer_without_writes(router, prompt_event) -> None:
    before = router.durable_bytes()
    route = router.route(prompt_event)
    assert route.action == "offer_onboarding"
    assert route.message == "This repository has not been onboarded into Intent Engineering. Start guided onboarding now?"
    assert route.mcp_tool is None
    assert router.durable_bytes() == before


def test_initialized_repository_routes_once_to_public_preflight(router_with_baseline, prompt_event) -> None:
    route = router_with_baseline.route(prompt_event)
    assert route.action == "classify"
    assert route.mcp_tool == "intent_preflight"
    assert route.arguments == {"task": prompt_event.prompt}
    assert "token" not in json.dumps(route.model_dump(mode="json")).lower()


def test_active_clarification_answer_routes_to_session_without_reclassification(router_with_session, answer_event) -> None:
    route = router_with_session.route(answer_event)
    assert route.action == "answer_clarification"
    assert route.mcp_tool == "intent_clarification_answer"
    assert route.arguments["session_id"] == router_with_session.session_id
```

Add exact-container/subclass, cyclic JSON, depth/node/UTF-8 bounds, canonical timestamp, cross-repository, symlink, cancellation, fixed-error, and traceback-secrecy tests modeled on `tests/contract/agent_host/test_host_contract.py`.

Add official MCP API tests proving the four clarification tools strictly parse frozen request
models, delegate to the real `ClarificationCoordinator` and `ProposalConfirmationService`, preserve
question/answer authorship and chronology, require exact proposal confirmation, expose no raw
evidence body or authorization capability, and return fixed context-free errors.

- [ ] **Step 2: Run the prompt-router tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/agent_host/test_advisory_prompt.py -q -W error
```

Expected: collection fails because `agent_host.advisory` does not exist.

- [ ] **Step 3: Implement strict frozen request/response models and router**

```python
class PromptEvent(_HostModel):
    schema_version: Literal[1] = 1
    session_id: str
    turn_id: str
    repository: str
    actor: str
    prompt: str
    created_at: datetime


class PromptRoute(_HostModel):
    schema_version: Literal[1] = 1
    action: Literal["offer_onboarding", "classify", "answer_clarification", "continue"]
    message: str
    mcp_tool: str | None
    arguments: dict[str, object]
    advisory: Literal[True] = True
    authorization_issued: Literal[False] = False
```

The router must read current onboarding/session state once from held runtime descriptors, return a
detached result, perform no canonical mutation, and scrub prompt bytes from exception and
cancellation traceback frames. The CLI wrapper must parse one bounded JSON object from stdin and
emit one bounded JSON object or one fixed denial; raw prompts must never appear in logs or errors.

Extend `McpIntentWorkflowServices` with typed methods whose production implementation constructs the
existing clarification services from the same held `Runtime` used by the MCP server. Register all
four tools as non-idempotent, graph-affecting operations where appropriate. `open` consumes the
exact `TaskEnvelope`, validated questions, actor, aliases, timestamp, and ACL; `answer` consumes an
exact session/question/answer tuple; `propose` consumes `ClarificationProposalSubmission`; and
`confirm` consumes proposal ID, actor, timestamp, and sorted selected node IDs. No handler may
accept a caller-supplied principal superset; principals and aliases are resolved from live held
configuration.

- [ ] **Step 4: Run host, MCP, CLI, and static compatibility gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/agent_host/test_advisory_prompt.py tests/contract/agent_host tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_mcp_server.py tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py -q -W error
.venv/bin/ruff check src/intent_engineering/integrations/agent_host src/intent_engineering/integrations/mcp_server/intent_workflow.py src/intent_engineering/integrations/mcp_server/server.py src/intent_engineering/cli/app.py tests/contract/agent_host tests/contract/mcp/test_intent_workflow_tools.py
.venv/bin/mypy src
```

Expected: all pass; existing mandatory-host refusal tests remain unchanged.

- [ ] **Step 5: Commit the advisory routing slice**

```bash
git add src/intent_engineering/integrations/agent_host/advisory.py src/intent_engineering/integrations/agent_host/__init__.py src/intent_engineering/cli/app.py src/intent_engineering/integrations/mcp_server/intent_workflow.py src/intent_engineering/integrations/mcp_server/server.py tests/contract/agent_host/test_advisory_prompt.py tests/contract/mcp/test_intent_workflow_tools.py
git commit -m "feat: route prompts through advisory intent checks"
```

---

### Task 4: Package the Advisory Codex Plugin

**Files:**
- Create: `plugins/intent-advisor/.codex-plugin/plugin.json`
- Create: `plugins/intent-advisor/hooks/hooks.json`
- Create: `plugins/intent-advisor/scripts/prompt-hook`
- Create: `plugins/intent-advisor/.mcp.json`
- Create: `plugins/intent-advisor/skills/intent-advisor/SKILL.md`
- Create: `tests/contract/agent_host/test_advisory_plugin.py`

**Interfaces:**
- Consumes: `intent agent-prompt-hook` stdin/stdout contract and existing `intent mcp --project .` server.
- Produces: a discoverable `intent-advisor` plugin that invokes the prompt router after human prompts and teaches the agent to call only public MCP workflow tools.

- [ ] **Step 1: Load the required plugin-authoring skill and scaffold the bundle**

Before any plugin file is created, read and follow `plugin-creator/SKILL.md`. Scaffold from the skill root with:

```bash
python3 scripts/create_basic_plugin.py intent-advisor --path '/Users/rampetaravishankar/Desktop/Intent Engineering/.worktrees/public-alpha/plugins' --with-skills --with-hooks --with-scripts --with-mcp
```

Configure no marketplace entry unless explicitly requested by the user.

- [ ] **Step 2: Write plugin artifact and offline hook tests**

```python
def test_advisory_plugin_declares_prompt_hook_and_project_mcp(plugin_root) -> None:
    manifest = load_plugin_manifest(plugin_root)
    hooks = load_hooks(plugin_root)
    assert manifest["name"] == "intent-advisor"
    assert hooks.has_user_prompt_submit_command("scripts/prompt-hook")
    assert load_mcp(plugin_root).command == "intent"
    assert load_mcp(plugin_root).args == ["mcp", "--project", "."]


def test_real_hook_offers_onboarding_before_baseline(plugin_hook, uninitialized_project) -> None:
    result = plugin_hook.run(official_prompt_event(uninitialized_project))
    assert result.updated_input_contains("Start guided onboarding now?")
    assert result.stderr == ""
    assert durable_bytes(uninitialized_project) == durable_bytes_before


def test_real_hook_routes_initialized_prompt_to_preflight_without_secret(plugin_hook, initialized_project) -> None:
    result = plugin_hook.run(official_prompt_event(initialized_project, prompt="Add team sharing"))
    assert result.updated_input_contains("intent_preflight")
    assert "authorization" not in result.full_output.casefold()
```

Also test missing CLI/MCP, malformed input, timeout, cancellation, cross-project session reuse, special files, output bounds, and fixed advisory fallback.

- [ ] **Step 3: Run plugin tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/agent_host/test_advisory_plugin.py -q -W error
```

Expected: fail because `plugins/intent-advisor` is absent.

- [ ] **Step 4: Implement the smallest advisory bundle**

The hook script must only forward bounded stdin to `intent agent-prompt-hook` and return its exact
host wrapper. The skill must instruct the agent to:

- offer `intent onboard` when `action=offer_onboarding`;
- call the named public MCP tool when `action=classify` or `answer_clarification`;
- ask returned questions before implementation;
- show exact graph proposals for human confirmation;
- never request, display, or infer a capability token; and
- state that advisory plugin guidance is not complete mutation enforcement.

Do not add `PreToolUse` denial hooks or weaken `CodexIntentAdapter`'s mandatory refusal.

- [ ] **Step 5: Validate plugin and compatibility behavior**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/agent_host/test_advisory_plugin.py tests/contract/agent_host/test_advisory_prompt.py tests/contract/agent_host/test_codex_adapter.py -q -W error
python3 '/Users/rampetaravishankar/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py' plugins/intent-advisor
```

Expected: plugin tests pass, the official validator passes, and mandatory Codex mode still raises `MandatoryHookUnavailable`.

- [ ] **Step 6: Commit the plugin slice**

```bash
git add plugins/intent-advisor tests/contract/agent_host/test_advisory_plugin.py
git commit -m "feat: package advisory intent prompt plugin"
```

---

### Task 5: Complete Adoption Documentation and Release Proof

**Files:**
- Modify: `README.md`
- Modify: `docs/intent-aware-agent.md`
- Modify: `docs/mcp.md`
- Modify: `.github/workflows/intent-engineering.yml`
- Modify: `tests/e2e/test_intent_aware_agent_workflow.py`
- Modify: `tests/e2e/test_public_alpha.py`

**Interfaces:**
- Consumes: `intent onboard`, advisory plugin artifacts, public MCP tools, and existing scheduled sync/assurance CLI commands.
- Produces: an executable documented journey and one end-to-end proof from uninitialized repository through onboarding, prompt classification, clarification, implementation evidence, and plugin-independent assurance.

- [ ] **Step 1: Write the release-journey assertions before editing docs**

```python
def test_guided_onboarding_plugin_and_assurance_share_one_project(intent_agent_harness) -> None:
    offer = intent_agent_harness.prompt("Implement CSV export")
    assert offer.action == "offer_onboarding"
    assert intent_agent_harness.graph().version == 0

    onboarded = intent_agent_harness.onboard("docs/prd.md", confirm=True)
    assert onboarded.state == "ready"
    assert onboarded.graph_version == 1

    aligned = intent_agent_harness.prompt("Implement CSV export")
    assert aligned.classification == "aligned"

    ambiguous = intent_agent_harness.prompt("Add team sharing")
    assert ambiguous.classification == "new_or_ambiguous"
    assert ambiguous.questions

    assurance = intent_agent_harness.run_scheduled_assurance()
    assert assurance.graph_version == intent_agent_harness.graph().version
    assert assurance.authorization_issued is False
```

Add structural tests requiring docs to order install → onboard → approve → ordinary prompts → scheduled assurance, and requiring the workflow to invoke CLI assurance without the plugin.

- [ ] **Step 2: Run the E2E/documentation tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_intent_aware_agent_workflow.py tests/e2e/test_public_alpha.py -q -W error
```

Expected: fail because documentation and harness do not expose the new journey.

- [ ] **Step 3: Update documentation and workflow copy**

Document these exact first-run commands:

```bash
python -m pip install intent-engineering
intent onboard --project . --prd docs/PRD.md
intent mcp --project .
```

Document plugin installation using the validated bundle path, its advisory limitation, the
automatic onboarding offer, prompt classifications, clarification/review flow, explicit opt-out,
and scheduled CLI assurance. Keep the GitHub workflow's executable command structure compatible
with existing clean-checkout tests.

- [ ] **Step 4: Run focused, broad, static, and full verification**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow tests/integration/intent_workflow tests/contract/agent_host tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_cli_intent_onboard.py tests/e2e/test_intent_aware_agent_workflow.py tests/e2e/test_public_alpha.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
.venv/bin/ruff check $(git ls-files '*.py')
.venv/bin/ruff format --check src/intent_engineering/intent_workflow/onboarding.py src/intent_engineering/integrations/agent_host/advisory.py tests/unit/intent_workflow/test_onboarding.py tests/e2e/test_cli_intent_onboard.py tests/contract/agent_host/test_advisory_prompt.py tests/contract/agent_host/test_advisory_plugin.py
.venv/bin/mypy src
.venv/bin/intent onboard --help
.venv/bin/intent preflight --help
.venv/bin/intent mcp --help
git diff --check
```

Expected: every command passes. If the repository-wide formatter still reports the documented
pre-existing baseline, format only files changed by this plan and record the exact baseline without
rewriting unrelated files.

- [ ] **Step 5: Request independent review and fix grouped findings tests-first**

Request one adversarial review covering onboarding consent/no-op behavior, plugin raw boundaries,
token secrecy, recursive clarification prevention, MCP delegation, advisory wording, and assurance
independence. For each grouped finding round, add failing regressions before production fixes and
rerun the focused and full gates required by the affected scope.

- [ ] **Step 6: Commit the release slice**

```bash
git add README.md docs/intent-aware-agent.md docs/mcp.md .github/workflows/intent-engineering.yml tests/e2e/test_intent_aware_agent_workflow.py tests/e2e/test_public_alpha.py
git commit -m "docs: publish guided intent developer journey"
```

- [ ] **Step 7: Perform final branch verification**

```bash
git status --short
git diff --check f3600e9..HEAD
git log --oneline --decorate -8
```

Expected: tracked worktree and index are clean; only the known protected untracked artifacts remain; every commit contains only its task allowlist.
