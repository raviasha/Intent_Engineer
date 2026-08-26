# Intent-Aware Agent Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add PRD-to-graph bootstrap, mandatory task preflight, attributed clarification and proposal confirmation, a compatible Codex host adapter, and scheduled intent/code assurance on top of the existing local engine.

**Architecture:** New `intent_workflow` application services consume the existing immutable evidence, graph, ChangeSet, reconciliation, policy, MCP, and transaction ports. The active coding agent submits untrusted typed semantic proposals; deterministic services validate, store, authorize, and apply them. Mandatory mutation authorization is exposed only through a verified Codex pre-tool hook; CLI preflight remains diagnostic and cannot mint capabilities.

**Tech Stack:** Python 3.12, Pydantic v2 strict models, Typer, official MCP Python SDK v2, PyYAML, existing descriptor-safe storage/transaction adapters, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-08-26-intent-aware-agent-workflow-design.md`

## Global Constraints

- Preserve local-first operation, stable graph IDs, immutable/versioned evidence, authorship, ACLs, source locators, predecessor history, confidence history, and explicit reconciliation.
- Confidence is epistemic; it never means approval, priority, completeness, or source authority.
- Source roles weight interpretation but never silently select truth.
- Model-generated material is an untrusted typed proposal; only validated ChangeSets mutate canonical graph state.
- Enabled mandatory mode fails closed for repository mutation; disabling the plugin is the explicit opt-out.
- Hosts without a real pre-mutation hook must refuse mandatory-mode activation.
- Semantic task or graph changes invalidate authorization; CLI diagnostics never mint mutation capabilities.
- Non-conflicting additions may be confirmed by an authorized contributor; conflict, weakening, supersession, deletion, and destructive changes require an independent reviewer.
- Background capture never approves conflict or performs external writes.
- All tests are deterministic, offline, fixed-time, credential-free, and warnings-as-errors.
- Preserve the five authorized untracked artifacts without reading, staging, deleting, renaming, or rewriting them.

---

## File map

New focused package:

```text
src/intent_engineering/intent_workflow/
├── __init__.py          public workflow types and services
├── models.py            source roles, proposals, task envelopes/results, clarification records
├── proposal_store.py    descriptor-safe append-only proposal and decision ledger
├── bootstrap.py         evidence-grounded draft validation and baseline proposal creation
├── conversation.py      attributed immutable human/agent conversation evidence
├── preflight.py         task classification validation and context-bound preflight results
├── clarification.py     attributed answers, proposal completion, and confirmation governance
├── authorization.py     process-local opaque capability issue/verify/revoke
├── post_task.py         authorized-scope comparison and implementation/test evidence proposal
└── assurance.py         scheduled provisional-relevance and alignment observations
```

Integration files:

```text
src/intent_engineering/cli/intent_workflow.py
src/intent_engineering/integrations/mcp_server/intent_workflow.py
src/intent_engineering/integrations/agent_host/base.py
src/intent_engineering/integrations/agent_host/codex.py
```

Do not grow `cli/app.py`, `integrations/mcp_server/tools.py`, or
`integrations/mcp_server/mutations.py` with workflow internals; they only register the new focused
modules.

## Spec coverage map

| Approved design concern | Implemented and proven in |
|---|---|
| Source roles, confidence semantics, framework vocabulary | Tasks 1 and 4 |
| Existing-repo PRD/source capture, hybrid review, baseline activation | Tasks 3, 4, and 10 |
| Per-message lightweight/full classification and bounded context | Tasks 5, 7, 8, and 10 |
| Immutable human/agent turns, attributed clarification, proposal governance | Tasks 5, 6, and 10 |
| Contributor confirmation versus independent conflict review | Tasks 6 and 10 |
| Process-local task/actor/repository/graph/scope capabilities | Tasks 7, 8, and 10 |
| Honest mandatory-host enforcement and explicit plugin opt-out | Tasks 8 and 10 |
| Post-task implementation/test evidence | Tasks 8, 9, and 10 |
| Frequent capture plus separate scheduled intent/requirements/code/test assurance | Tasks 9 and 10 |
| Descriptor-safe storage, transactions, replay, no-op, cancellation, secrecy | Tasks 2–10 |
| CLI, MCP, host adapter, workflow, docs, release proof | Tasks 4, 7, 8, and 10 |

---

### Task 1: Add strict workflow vocabulary and project source roles

**Files:**
- Create: `src/intent_engineering/intent_workflow/__init__.py`
- Create: `src/intent_engineering/intent_workflow/models.py`
- Modify: `src/intent_engineering/core/models/project.py`
- Modify: `src/intent_engineering/core/models/__init__.py`
- Modify: `src/intent_engineering/core/policy/project.py`
- Modify: `schemas/intent-meta-model.yaml`
- Modify: `graph/framework-intent-graph.yaml`
- Test: `tests/unit/intent_workflow/test_models.py`
- Test: `tests/integration/test_framework_graph.py`

**Interfaces:**
- Consumes: existing `StrictModel`, `ChangeSet`, `JsonValue`, `ProjectConfig`, `Node`, and `Edge`.
- Produces: `SourceRole`, `SourceRoleAssignment`, `ProposalKind`, `IntentProposal`, `ProposalDecision`, `TaskClassification`, `TaskEnvelope`, `ClarificationAnswer`, and `PreflightResult`.

- [ ] **Step 1: Write strict-model and compatibility tests**

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import ProjectConfig
from intent_engineering.intent_workflow.models import (
    SourceRole,
    SourceRoleAssignment,
    TaskClassification,
    TaskEnvelope,
)


def test_source_role_assignment_is_granular_strict_and_frozen() -> None:
    assignment = SourceRoleAssignment(
        connector_id="mcp:slack-product",
        scope="channel:C123",
        role=SourceRole.PROPOSED_INTENT,
        inherited=False,
    )
    assert assignment.role is SourceRole.PROPOSED_INTENT
    with pytest.raises(ValidationError):
        SourceRoleAssignment.model_validate(
            {**assignment.model_dump(), "role": "truth", "unexpected": True}
        )


def test_project_config_round_trips_source_roles_without_changing_old_configs() -> None:
    legacy = ProjectConfig(project_id="demo", local_actor="local:asha")
    assert legacy.source_roles == ()
    configured = legacy.model_copy(
        update={
            "source_roles": (
                SourceRoleAssignment(
                    connector_id="markdown",
                    scope="docs/prd.md",
                    role=SourceRole.DECLARED_INTENT,
                    inherited=False,
                ),
            )
        }
    )
    assert ProjectConfig.model_validate_json(configured.model_dump_json()) == configured


def test_task_envelope_has_canonical_bounded_identity() -> None:
    envelope = TaskEnvelope(
        repository_id="demo",
        actor="local:asha",
        conversation_ref="codex:thread-1:message-1",
        request="Add CSV export",
        request_evidence_ref="evidence:sha256:request-1",
        graph_version=3,
        created_at=datetime(2026, 8, 26, tzinfo=UTC),
        requested_scope=("src/export.py",),
    )
    assert envelope.id.startswith("task:sha256:")
    assert TaskClassification.ALIGNED.value == "aligned"
```

- [ ] **Step 2: Run the model tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_models.py -q -W error
```

Expected: collection fails because `intent_engineering.intent_workflow` does not exist.

- [ ] **Step 3: Implement the exact workflow models**

In `models.py`, define string enums and strict frozen records. Use canonical JSON with
`allow_nan=False`, sorted keys, compact separators, and SHA-256 identities:

```python
class SourceRole(StrEnum):
    DECLARED_INTENT = "declared_intent"
    DECISION = "decision"
    PROPOSED_INTENT = "proposed_intent"
    IMPLEMENTATION_EVIDENCE = "implementation_evidence"
    OPERATING_CONTEXT = "operating_context"


class TaskClassification(StrEnum):
    NO_SEMANTIC_IMPACT = "no_semantic_impact"
    ALIGNED = "aligned"
    NEW_OR_AMBIGUOUS = "new_or_ambiguous"
    CONFLICTING = "conflicting"


class SourceRoleAssignment(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)
    connector_id: Annotated[str, Field(min_length=1, max_length=256)]
    scope: Annotated[str, Field(min_length=1, max_length=2048)]
    role: SourceRole
    inherited: bool
```

Define the remaining records with these exact public fields:

```python
class ProposalKind(StrEnum):
    BOOTSTRAP = "bootstrap"
    REQUIREMENT = "requirement"


class IntentProposal(StrictModel):
    schema_version: Literal[1] = 1
    id: str
    kind: ProposalKind
    proposed_by: str
    proposed_at: datetime
    baseline_graph_version: int
    evidence_refs: tuple[str, ...]
    source_roles: tuple[SourceRoleAssignment, ...]
    changeset: ChangeSet
    core_node_ids: tuple[str, ...] = ()
    provisional_node_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unanswered_questions: tuple[str, ...] = ()
    conflicting_authors: tuple[str, ...] = ()
    destructive: bool = False


class ProposalDecision(StrictModel):
    schema_version: Literal[1] = 1
    id: str
    proposal_id: str
    proposal_digest: str
    actor: str
    actor_aliases: tuple[str, ...]
    decided_at: datetime
    action: Literal["confirm", "reject"]
    baseline_graph_version: int


class TaskEnvelope(StrictModel):
    schema_version: Literal[1] = 1
    id: str = ""
    repository_id: str
    actor: str
    conversation_ref: str
    request: str
    request_evidence_ref: str
    graph_version: Annotated[int, Field(ge=0)]
    created_at: datetime
    requested_scope: tuple[str, ...] = ()


class PreflightResult(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str
    graph_version: int
    classification: TaskClassification
    authorized: bool
    basis: str
    relevant_node_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    review_case_id: str | None = None
    permitted_scope: tuple[str, ...] = ()
    context: Mapping[str, JsonValue] = Field(default_factory=dict)
```

`PreflightResult.context` is detached strict JSON: exact `dict[str, JsonValue]` input is copied into
frozen mappings/tuples, arbitrary mapping/list subclasses are rejected, and serialization returns
plain JSON containers without exposing internal mutable references.

`TaskEnvelope.id` defaults empty, is computed from canonical material excluding `id`, and rejects
any caller-supplied nonempty mismatch.
Bound request to 16 KiB, each scope entry to 2 KiB, scope count to 256, conversation reference to
512 characters, and reject control characters. `IntentProposal` must contain proposal kind, actor,
timestamp, baseline graph version, evidence refs, source-role assignments, exact `ChangeSet`,
assumptions, unanswered questions, and a content-addressed ID. `ProposalDecision` is a separate
immutable append record and never mutates the proposal.

Add `source_roles: tuple[SourceRoleAssignment, ...] = ()` to `ProjectConfig`; reject duplicate
`(connector_id, scope)` pairs and sort serialization deterministically. Update initialization so old
projects retain identical defaults.

- [ ] **Step 4: Dogfood the approved design vocabulary**

Add framework graph nodes for mandatory preflight, PRD bootstrap, and agentic-proposal/deterministic-
authority separation, plus edges to `intent-preserve-fidelity`, `cap-manage`, `cap-sync`, and
`decision-agent-agnostic`. Preserve existing IDs and graph version semantics. Extend the meta-model
principles with “agent task mutation requires intent preflight when the project policy enables it.”

- [ ] **Step 5: Run focused and framework gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_models.py tests/integration/test_framework_graph.py -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow src/intent_engineering/core/models/project.py tests/unit/intent_workflow/test_models.py
.venv/bin/mypy src
```

Expected: all tests and static checks pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/intent_engineering/intent_workflow src/intent_engineering/core/models/project.py src/intent_engineering/core/models/__init__.py src/intent_engineering/core/policy/project.py schemas/intent-meta-model.yaml graph/framework-intent-graph.yaml tests/unit/intent_workflow/test_models.py tests/integration/test_framework_graph.py
git commit -m "feat: define intent workflow contracts"
```

---

### Task 2: Add the descriptor-safe proposal and decision ledger

**Files:**
- Create: `src/intent_engineering/intent_workflow/proposal_store.py`
- Modify: `src/intent_engineering/core/policy/project.py`
- Modify: `src/intent_engineering/cli/runtime.py`
- Test: `tests/unit/intent_workflow/test_proposal_store.py`
- Test: `tests/integration/test_startup_transaction_recovery.py`

**Interfaces:**
- Consumes: Task 1 `IntentProposal`, `ProposalDecision`; existing `SecureFile`, `same_path_lock`, `append_durable_line`, `loads_strict_object`, and `LocalTransactionCoordinator`.
- Produces: `IntentProposalStore.put`, `.get`, `.list`, `.decide`, `.decision_for`; `Runtime.intent_proposals`.

- [ ] **Step 1: Write append-only ledger tests**

```python
def test_proposal_store_is_canonical_idempotent_and_append_only(proposal_store, proposal) -> None:
    assert proposal_store.put(proposal) is True
    before = proposal_store.bytes()
    assert proposal_store.put(proposal) is False
    assert proposal_store.bytes() == before
    assert proposal_store.get(proposal.id) == proposal


def test_decision_binds_exact_proposal_actor_and_graph_version(
    proposal_store, proposal, contributor_decision
) -> None:
    proposal_store.put(proposal)
    assert proposal_store.decide(contributor_decision) is True
    assert proposal_store.decision_for(proposal.id) == contributor_decision


def test_fifo_symlink_hardlink_duplicate_key_and_unterminated_ledgers_fail_without_blocking(
    proposal_store_attack_matrix,
) -> None:
    assert proposal_store_attack_matrix.run_with_watchdog(seconds=1.0) == "fixed-unavailable"
```

- [ ] **Step 2: Run the ledger tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_proposal_store.py -q -W error
```

Expected: collection fails because `proposal_store.py` does not exist.

- [ ] **Step 3: Implement one framed canonical ledger**

Use one record envelope so proposal and decision ordering is explicit:

```python
class IntentLedgerRecord(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)
    schema_version: Literal[1] = 1
    sequence: Annotated[int, Field(ge=0)]
    proposal: IntentProposal | None = None
    decision: ProposalDecision | None = None

    @model_validator(mode="after")
    def require_exactly_one_payload(self) -> Self:
        if (self.proposal is None) == (self.decision is None):
            raise ValueError("invalid intent proposal ledger")
        return self
```

Read with `SecureFile.read_bytes_nonblocking`, reject nonempty files without a trailing newline,
strict-decode every line, require byte-for-byte canonical JSON, contiguous sequence numbers,
content-addressed identities, one proposal body per ID, and at most one identical decision per
proposal. Append under `same_path_lock` with `append_durable_line`. Clear arguments and internal
buffers on public error/cancellation paths.

- [ ] **Step 4: Wire the exact held file into project initialization and Runtime**

Create `.intent/history/intent-proposals.jsonl` as an empty regular file during initialization.
Add it to the transaction target map as `intent_proposals`; bind `IntentProposalStore` to that exact
held `SecureFile` and expose it as `Runtime.intent_proposals`. Add a legacy target-set entry so prior
transaction journals remain recoverable.

- [ ] **Step 5: Run ledger, recovery, and static gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_proposal_store.py tests/integration/test_startup_transaction_recovery.py tests/unit/cli/test_runtime_paths.py -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/proposal_store.py tests/unit/intent_workflow/test_proposal_store.py
.venv/bin/mypy src
```

Expected: all pass; an old initialized fixture still loads.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/intent_engineering/intent_workflow/proposal_store.py src/intent_engineering/core/policy/project.py src/intent_engineering/cli/runtime.py tests/unit/intent_workflow/test_proposal_store.py tests/integration/test_startup_transaction_recovery.py tests/unit/cli/test_runtime_paths.py
git commit -m "feat: persist intent proposals safely"
```

---

### Task 3: Build PRD bootstrap and hybrid baseline activation

**Files:**
- Create: `src/intent_engineering/intent_workflow/bootstrap.py`
- Modify: `src/intent_engineering/intent_workflow/__init__.py`
- Modify: `src/intent_engineering/storage/executor.py`
- Test: `tests/integration/intent_workflow/test_bootstrap.py`
- Test fixture: `tests/fixtures/intent_workflow/existing_project/docs/prd.md`

**Interfaces:**
- Consumes: Task 1 models, Task 2 store, existing `EvidenceRecord`, `Graph`, `ChangeSet`, `LocalChangeSetExecutor`, `refs_allowed`.
- Produces: `BootstrapSubmission`, `BootstrapReview`, `BootstrapService.propose`, `.review`, `.activate`.

- [ ] **Step 1: Write the ordinary-PRD bootstrap RED**

```python
def test_ordinary_prd_becomes_reviewable_core_and_provisional_detail(bootstrap_harness) -> None:
    evidence = bootstrap_harness.capture_prd("docs/prd.md")
    submission = bootstrap_harness.agent_submission(evidence)
    review = bootstrap_harness.service.propose(submission)

    assert review.status == "proposed"
    assert {item.type for item in review.core_nodes} >= {
        "PRODUCT_INTENT", "DESIRED_OUTCOME", "REQUIREMENT", "CONSTRAINT"
    }
    assert review.provisional_nodes
    assert all(node.evidence_refs == (evidence.id,) for node in review.all_nodes)
    assert all(node.source_mode.value == "inferred" for node in review.all_nodes)
    assert bootstrap_harness.graph().version == 0


def test_activation_applies_only_confirmed_core_as_one_changeset(bootstrap_harness) -> None:
    review = bootstrap_harness.proposed_review()
    result = bootstrap_harness.service.activate(
        review.proposal_id,
        confirmed_node_ids=tuple(node.id for node in review.core_nodes),
        actor="local:owner",
        at=bootstrap_harness.now,
    )
    assert result.version == 1
    assert {node.id for node in result.nodes} == {
        node.id for node in review.core_nodes
    }
    assert bootstrap_harness.provisional_ids() == tuple(
        node.id for node in review.provisional_nodes
    )
```

- [ ] **Step 2: Run the bootstrap test and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_bootstrap.py -q -W error
```

Expected: collection fails because `BootstrapService` is missing.

- [ ] **Step 3: Implement typed submission validation**

Define the exact strict/frozen bootstrap boundary in `bootstrap.py`:

```python
class BootstrapSubmission(StrictModel):
    schema_version: Literal[1] = 1
    baseline_graph_version: Annotated[int, Field(ge=0)]
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    timestamp: datetime
    evidence_refs: tuple[str, ...]
    source_roles: tuple[SourceRoleAssignment, ...]
    candidate_nodes: tuple[Node, ...]
    candidate_edges: tuple[Edge, ...]
    core_node_ids: tuple[str, ...]
    provisional_node_ids: tuple[str, ...]
    assumptions: tuple[str, ...] = ()
    unanswered_questions: tuple[str, ...] = ()
    conflicting_authors: tuple[str, ...] = ()
    destructive: bool = False


class BootstrapReview(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["proposed"] = "proposed"
    proposal_id: str
    proposal_digest: str
    baseline_graph_version: int
    core_nodes: tuple[Node, ...]
    provisional_nodes: tuple[Node, ...]
    all_nodes: tuple[Node, ...]
    assumptions: tuple[str, ...]
    unanswered_questions: tuple[str, ...]
```

Bound every collection (candidate nodes/edges 10,000, source/evidence references 10,000,
questions/assumptions 256), reject duplicate IDs, and require UTC timestamps. The submission
contains the exact baseline graph version, actor, timestamp, captured evidence IDs, source-role
assignments, candidate nodes/edges, core node IDs, provisional node IDs, assumptions, and unanswered
questions. `BootstrapService.propose` must:

```python
def propose(self, submission: BootstrapSubmission, principals: frozenset[str]) -> BootstrapReview:
    graph, evidence = self._snapshot()
    validated = BootstrapSubmission.model_validate_json(submission.model_dump_json())
    self._validate_baseline(validated, graph)
    self._validate_evidence(validated, evidence, principals)
    self._validate_roles(validated)
    changeset = self._changeset(validated, graph)
    apply_changeset(graph, changeset)  # validation only; discard returned graph
    proposal = IntentProposal(
        id=intent_proposal_id(validated, changeset),
        kind=ProposalKind.BOOTSTRAP,
        proposed_by=validated.actor,
        proposed_at=validated.timestamp,
        baseline_graph_version=validated.baseline_graph_version,
        evidence_refs=validated.evidence_refs,
        source_roles=validated.source_roles,
        changeset=changeset,
        core_node_ids=validated.core_node_ids,
        provisional_node_ids=validated.provisional_node_ids,
        assumptions=validated.assumptions,
        unanswered_questions=validated.unanswered_questions,
        conflicting_authors=validated.conflicting_authors,
        destructive=validated.destructive,
    )
    self._store.put(proposal)
    return BootstrapReview.from_proposal(proposal)
```

Reject evidence outside the submission, nodes without evidence, non-inferred bootstrap nodes,
unregistered types, duplicate semantic IDs, edges outside candidate node scope, core/provisional
overlap, and candidate graphs that fail public graph invariants. Do not invoke arbitrary mapping or
model protocols.

- [ ] **Step 4: Implement hybrid activation**

`activate` requires an authorized contributor, exact proposal ID, exact confirmed subset of the
proposal's core IDs, no unresolved conflict marker, current baseline graph version, and a fresh
decision record. Rebuild a subset ChangeSet with the confirming actor/time while preserving original
`created_by`, `created_at`, evidence, source mode, and confidence history. Apply it through
`LocalChangeSetExecutor` and append the decision and graph/history mutation within the shared
transaction boundary. Provisional candidates remain only in the proposal ledger.

- [ ] **Step 5: Run bootstrap and graph gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_bootstrap.py tests/integration/test_changeset_executor.py tests/unit/validation/test_service.py -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/bootstrap.py tests/integration/intent_workflow/test_bootstrap.py
.venv/bin/mypy src
```

Expected: all pass; identical proposal replay creates no new ledger or graph row.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/intent_engineering/intent_workflow/bootstrap.py src/intent_engineering/intent_workflow/__init__.py src/intent_engineering/storage/executor.py tests/integration/intent_workflow/test_bootstrap.py tests/fixtures/intent_workflow/existing_project/docs/prd.md
git commit -m "feat: bootstrap intent from reviewed PRD proposals"
```

---

### Task 4: Expose bootstrap and source-role onboarding through CLI and MCP

**Files:**
- Create: `src/intent_engineering/cli/intent_workflow.py`
- Create: `src/intent_engineering/integrations/mcp_server/intent_workflow.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/integrations/mcp_server/server.py`
- Modify: `src/intent_engineering/cli/runtime.py`
- Test: `tests/e2e/test_cli_intent_bootstrap.py`
- Test: `tests/contract/mcp/test_intent_workflow_tools.py`

**Interfaces:**
- Consumes: Task 3 `BootstrapService`; existing `Runtime`, `McpReadServices`, `_IntentMCPServer`.
- Produces: `intent bootstrap`, `intent sources add`, `intent proposals list|show|confirm`; MCP tools `intent_bootstrap_propose`, `intent_proposal_show`, `intent_proposal_confirm`.

- [ ] **Step 1: Write CLI and official-MCP contract tests**

```python
def test_cli_bootstrap_captures_prd_but_requires_review(clean_project) -> None:
    result = run_intent(clean_project, "bootstrap", "--prd", "docs/prd.md", "--format", "json")
    assert result.returncode == 4
    payload = result.json()
    assert payload["status"] == "review_required"
    assert payload["proposal_id"].startswith("intent-proposal:sha256:")
    assert payload["graph_version"] == 0


@pytest.mark.anyio
async def test_mcp_agent_submits_typed_bootstrap_without_direct_graph_write(server, submission) -> None:
    result = await server.call_tool(
        "intent_bootstrap_propose", {"submission": submission.model_dump(mode="json")}
    )
    assert result.structured_content["status"] == "proposed"
    assert server.services.runtime.graph_store.load().version == 0
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py -q -W error
```

Expected: CLI command and MCP tools are absent.

- [ ] **Step 3: Implement CLI onboarding commands**

`intent bootstrap --prd` must resolve one descriptor-rooted, nonblocking regular file; sync it
through the Markdown connector before asking the active agent for a typed submission. In CLI-only
mode with no agent submission, emit a bounded evidence/context packet and `review_required` status,
not an invented graph. `sources add` atomically updates `ProjectConfig.source_roles` only after
validating the connector/scope pair and preserving every other field. `proposals confirm` uses the
interactive terminal for an exact proposal digest and refuses non-TTY input.

- [ ] **Step 4: Implement narrow MCP registration**

Define an `IntentWorkflowPort` protocol and register only bounded Pydantic inputs. Handlers call the
port, delete raw arguments before returning/raising, and return fixed `invalid intent workflow
arguments` on pre-handler/validation errors. Do not register an approval-creation tool. Extend
`build_server` with `intent_workflow_services: IntentWorkflowPort | None = None` and keep the
existing read-only surface unchanged when omitted.

- [ ] **Step 5: Run CLI/MCP, help, and regression gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_cli_local.py tests/e2e/test_mcp_server.py -q -W error
.venv/bin/intent bootstrap --help
.venv/bin/intent sources --help
.venv/bin/intent proposals --help
.venv/bin/ruff check src/intent_engineering/cli/intent_workflow.py src/intent_engineering/integrations/mcp_server/intent_workflow.py tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py
.venv/bin/mypy src
```

Expected: all pass and each help command exits 0.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/intent_engineering/cli/intent_workflow.py src/intent_engineering/integrations/mcp_server/intent_workflow.py src/intent_engineering/cli/app.py src/intent_engineering/integrations/mcp_server/server.py src/intent_engineering/cli/runtime.py tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py
git commit -m "feat: expose reviewed intent onboarding"
```

---

### Task 5: Implement deterministic task preflight validation

**Files:**
- Create: `src/intent_engineering/intent_workflow/conversation.py`
- Create: `src/intent_engineering/intent_workflow/preflight.py`
- Modify: `src/intent_engineering/intent_workflow/__init__.py`
- Modify: `src/intent_engineering/context/provider.py`
- Test: `tests/unit/intent_workflow/test_preflight.py`
- Test: `tests/integration/intent_workflow/test_preflight_context.py`

**Interfaces:**
- Consumes: Task 1 `TaskEnvelope`, `TaskClassification`, `PreflightResult`; existing `ContextProvider`, graph/evidence/case snapshot and access policies.
- Produces: `ConversationCapture.record_turn`, `AgentClassificationSubmission`, `PreflightService.evaluate`.

- [ ] **Step 1: Write the four-classification RED**

```python
@pytest.mark.parametrize(
    ("classification", "authorized", "requires_questions", "requires_review"),
    [
        ("no_semantic_impact", True, False, False),
        ("aligned", True, False, False),
        ("new_or_ambiguous", False, True, False),
        ("conflicting", False, False, True),
    ],
)
def test_preflight_returns_one_fixed_outcome(
    preflight_harness, classification, authorized, requires_questions, requires_review
) -> None:
    result = preflight_harness.evaluate(classification)
    assert result.classification.value == classification
    assert result.authorized is authorized
    assert bool(result.questions) is requires_questions
    assert result.review_case_id is not None is requires_review


def test_human_request_and_agent_classification_are_attributed_evidence(preflight_harness) -> None:
    result = preflight_harness.evaluate("aligned")
    turns = preflight_harness.conversation_versions("codex:thread-1")
    assert [turn.author for turn in turns] == ["local:asha", "agent:codex"]
    assert turns[1].predecessor_id == turns[0].id
    assert result.evidence_refs == tuple(turn.id for turn in turns)
```

Add explicit tests proving: mechanical uncertainty cannot authorize; aligned IDs must exist and be
ACL-visible; new/ambiguous questions are bounded and nonempty; conflicting evidence produces or
reuses a stable case; invalid graph/config blocks; raw request content is absent from fixed error
traceback locals.

- [ ] **Step 2: Run preflight tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py -q -W error
```

Expected: collection fails because `PreflightService` is absent.

- [ ] **Step 3: Implement the untrusted classification boundary**

Capture every human or agent turn before semantic use as an immutable `EvidenceRecord` with stable
conversation locator, exact author principal, ACL, captured timestamp, content hash, and predecessor.
`TaskEnvelope` carries the human request evidence ID; the agent-classification turn supplies
`agent_evidence_ref`. If evidence persistence fails, preflight fails closed and does not authorize.
The core receives the bounded typed submission rather than arbitrary raw conversation objects.

Define the exact untrusted classifier boundary:

```python
class AgentClassificationSubmission(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str
    task_digest: str
    graph_version: Annotated[int, Field(ge=0)]
    classification: TaskClassification
    basis: Annotated[str, Field(min_length=1, max_length=4096)]
    relevant_node_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    agent_evidence_ref: str
    semantic_effects: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    conflict_claims: tuple[str, ...] = ()
    requested_scope: tuple[str, ...] = ()
```

Bound IDs/scope to 256 entries, questions/effects/uncertainties/conflicts to 64 entries, and each
text item to 4 KiB. `PreflightService.evaluate` reloads one transaction snapshot, authenticates actor
principals, validates both conversation evidence turns and every cited object against that snapshot,
and applies these rules:

```python
match submission.classification:
    case TaskClassification.NO_SEMANTIC_IMPACT:
        authorize = submission.semantic_effects == () and submission.uncertainties == ()
    case TaskClassification.ALIGNED:
        authorize = bool(submission.relevant_node_ids) and not blocking_cases
    case TaskClassification.NEW_OR_AMBIGUOUS:
        authorize = False
        require_nonempty_bounded_questions(submission.questions)
    case TaskClassification.CONFLICTING:
        authorize = False
        review_case = create_or_reuse_case(submission, snapshot)
```

The service never trusts the submitted graph version, IDs, ACL, or case identity. It returns a
detached `PreflightResult` containing bounded context material, not the entire graph.

The same snapshot includes unresolved `IntentProposal` records. If the agent cites a provisional
node ID, validate it against the proposal ledger and force `NEW_OR_AMBIGUOUS`; provisional material
can inform questions but cannot satisfy `ALIGNED` or authorize mutation.

- [ ] **Step 4: Extend context selection for exact referenced IDs**

Add `ContextProvider.for_refs(node_ids, *, actor)` that returns the same `ContextPack` contract while
requiring every requested node to be visible and expanding only the current two-hop bounded
neighborhood. Reuse it for aligned preflight rather than adding another context serializer.

- [ ] **Step 5: Run focused, context, and static gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py tests/unit/context -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/preflight.py src/intent_engineering/context/provider.py tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py
.venv/bin/mypy src
```

Expected: all pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add src/intent_engineering/intent_workflow/conversation.py src/intent_engineering/intent_workflow/preflight.py src/intent_engineering/intent_workflow/__init__.py src/intent_engineering/context/provider.py tests/unit/intent_workflow/test_preflight.py tests/integration/intent_workflow/test_preflight_context.py
git commit -m "feat: classify coding tasks against intent"
```

---

### Task 6: Add attributed clarification and governed proposal confirmation

**Files:**
- Create: `src/intent_engineering/intent_workflow/clarification.py`
- Modify: `src/intent_engineering/intent_workflow/models.py`
- Modify: `src/intent_engineering/intent_workflow/proposal_store.py`
- Modify: `src/intent_engineering/mutations/authorization.py`
- Test: `tests/integration/intent_workflow/test_clarification.py`
- Test: `tests/integration/intent_workflow/test_proposal_governance.py`

**Interfaces:**
- Consumes: Task 1 clarification/proposal records, Task 2 store, existing `MutationPolicy`, identity alias resolver, and `LocalChangeSetExecutor`.
- Produces: `ClarificationCoordinator.open`, `.answer`, `.propose`; `ProposalConfirmationService.confirm`.

- [ ] **Step 1: Write clarification and governance REDs**

```python
def test_clarification_preserves_each_answer_author_and_evidence(clarification_harness) -> None:
    session = clarification_harness.open("Add team sharing")
    session = clarification_harness.answer(
        session.id,
        actor="local:asha",
        question_id="audience",
        answer="Workspace admins may share read-only reports",
    )
    proposal = clarification_harness.propose(session.id)
    assert proposal.evidence_refs == session.answer_evidence_refs
    assert proposal.proposed_by == "local:asha"


def test_contributor_confirms_addition_but_cannot_self_approve_conflict(governance_harness) -> None:
    assert governance_harness.confirm_new(actor="local:asha").status == "applied"
    rejected = governance_harness.confirm_conflict(actor="local:asha")
    assert rejected.status == "review_required"
    assert rejected.case_id.startswith("case:sha256:")
    assert governance_harness.graph_bytes_unchanged_after_conflict()
```

- [ ] **Step 2: Run the clarification tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py -q -W error
```

Expected: collection fails because `ClarificationCoordinator` is absent.

- [ ] **Step 3: Implement bounded attributed sessions**

Define exact strict/frozen records before the coordinator:

```python
class ClarificationQuestion(StrictModel):
    id: str
    prompt: Annotated[str, Field(min_length=1, max_length=2048)]
    evidence_ref: str
    required: bool = True


class ClarificationAnswer(StrictModel):
    question_id: str
    actor: str
    answered_at: datetime
    evidence_ref: str
    answer_digest: str


class ClarificationSession(StrictModel):
    schema_version: Literal[1] = 1
    id: str
    task_id: str
    opened_by: str
    opened_at: datetime
    baseline_graph_version: int
    questions: tuple[ClarificationQuestion, ...]
    answers: tuple[ClarificationAnswer, ...] = ()
    status: Literal["open", "proposed", "closed"] = "open"
```

Raw question/answer text lives in immutable conversation `EvidenceRecord` versions, not the session
ledger. The session stores each digest/reference, preventing a second mutable copy while preserving
exact agent/human author and chronology.

Require 1–16 questions, each with stable ID and 1–2,048 characters; 1–16 answers, each 1–16 KiB;
one answer per question; exact actor, timestamp, and evidence reference on each answer. Session records
are immutable append events in the proposal ledger. `propose` requires answers for every required
question and a typed agent submission with exact evidence scope. It emits an `IntentProposal`, never
a graph write.

- [ ] **Step 4: Implement semantic-risk and person-level approval checks**

Classify proposal risk deterministically from ChangeSet groups and current graph:

```python
requires_independent_review = any(
    (
        proposal.changeset.nodes_updated,
        proposal.changeset.nodes_superseded,
        proposal.changeset.edges_updated,
        proposal.changeset.edges_superseded,
        proposal.conflicting_authors,
        proposal.destructive,
    )
)
```

New node/edge additions are contributor-confirmable only when they do not contradict an active node
or constraint and the actor is an authorized contributor. Re-resolve authoritative aliases from the
live policy/config on every confirmation. Independent review rejects any intersection between
proposer aliases, conflicting-author aliases, and reviewer aliases. Apply accepted proposals through
the existing transaction executor and append the exact decision in the same recovery domain.

- [ ] **Step 5: Run governance and mutation regressions**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py tests/unit/mutations/test_approval.py tests/integration/mcp/test_write_conflict.py -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/clarification.py tests/integration/intent_workflow
.venv/bin/mypy src
```

Expected: all pass.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/intent_engineering/intent_workflow/clarification.py src/intent_engineering/intent_workflow/models.py src/intent_engineering/intent_workflow/proposal_store.py src/intent_engineering/mutations/authorization.py tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py
git commit -m "feat: govern conversational intent proposals"
```

---

### Task 7: Issue process-local scoped mutation capabilities

**Files:**
- Create: `src/intent_engineering/intent_workflow/authorization.py`
- Modify: `src/intent_engineering/intent_workflow/preflight.py`
- Modify: `src/intent_engineering/integrations/mcp_server/intent_workflow.py`
- Test: `tests/unit/intent_workflow/test_authorization.py`
- Test: `tests/contract/mcp/test_intent_workflow_tools.py`

**Interfaces:**
- Consumes: Task 5 validated preflight result.
- Produces: `AuthorizationIssuer.issue`, `.verify`, `.revoke_all`; MCP `intent_preflight` and `intent_authorization_verify`.

- [ ] **Step 1: Write capability binding and invalidation REDs**

```python
def test_capability_binds_actor_repo_task_graph_scope_and_expiry(issuer, aligned_preflight) -> None:
    token = issuer.issue(aligned_preflight, now=NOW)
    assert issuer.verify(
        token,
        actor="local:asha",
        repository_id="demo",
        task_id=aligned_preflight.task_id,
        graph_version=3,
        requested_paths=("src/export.py",),
        now=NOW,
    ).authorized
    assert not issuer.verify(
        token,
        actor="local:asha",
        repository_id="demo",
        task_id=aligned_preflight.task_id,
        graph_version=4,
        requested_paths=("src/export.py",),
        now=NOW,
    ).authorized
    assert not issuer.verify(
        token,
        actor="local:other",
        repository_id="demo",
        task_id=aligned_preflight.task_id,
        graph_version=3,
        requested_paths=("src/export.py",),
        now=NOW,
    ).authorized
    assert not issuer.verify(
        token,
        actor="local:asha",
        repository_id="demo",
        task_id=aligned_preflight.task_id,
        graph_version=3,
        requested_paths=("src/unrelated.py",),
        now=NOW,
    ).authorized


def test_restart_and_expiry_invalidate_without_persisting_token(issuer, aligned_preflight, project) -> None:
    token = issuer.issue(aligned_preflight, now=NOW)
    assert token not in scan_regular_project_text(project)
    verification = {
        "actor": "local:asha",
        "repository_id": "demo",
        "task_id": aligned_preflight.task_id,
        "graph_version": 3,
        "requested_paths": ("src/export.py",),
    }
    assert not AuthorizationIssuer().verify(token, now=NOW, **verification).authorized
    assert not issuer.verify(
        token, now=NOW + timedelta(minutes=6), **verification
    ).authorized
```

- [ ] **Step 2: Run authorization tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_authorization.py -q -W error
```

Expected: collection fails because `AuthorizationIssuer` is missing.

- [ ] **Step 3: Implement the process-local issuer**

Define exact strict/frozen server-side records:

```python
class AuthorizationGrant(StrictModel):
    schema_version: Literal[1] = 1
    digest: str
    actor: str
    repository_id: str
    task_id: str
    classification: Literal[
        TaskClassification.NO_SEMANTIC_IMPACT,
        TaskClassification.ALIGNED,
    ]
    graph_version: Annotated[int, Field(ge=0)]
    permitted_paths: tuple[str, ...]
    relevant_node_ids: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime


class AuthorizationVerification(StrictModel):
    schema_version: Literal[1] = 1
    authorized: bool
    classification: TaskClassification | None = None
    relevant_node_ids: tuple[str, ...] = ()
    expires_at: datetime | None = None
    reason: Literal[
        "authorized",
        "unknown",
        "expired",
        "actor_mismatch",
        "repository_mismatch",
        "task_mismatch",
        "graph_mismatch",
        "scope_mismatch",
    ]
```

Generate tokens with `secrets.token_urlsafe(32)`. Retain only a SHA-256 digest key and an immutable
`AuthorizationGrant` in a bounded in-memory ordered mapping. Default TTL is five minutes, maximum
live grants is 1,024, and issue evicts expired grants before rejecting capacity. `verify` uses
`hmac.compare_digest` on digests, never returns the stored token, and requires exact actor,
repository, task ID, graph version, and a requested path subset. `revoke_all` clears the map. No
serialization method may expose token material.

- [ ] **Step 4: Wire MCP preflight and verification**

The long-lived MCP workflow services own one issuer. `intent_preflight` accepts a task envelope and
agent classification submission, invokes Task 5, and issues a token only for valid mechanical or
aligned results. `intent_authorization_verify` accepts token plus exact operation scope and returns
only `{schema_version, authorized, classification, relevant_ids, expires_at}`. CLI `intent
preflight` invokes classification validation but never receives the issuer and therefore never
returns a token.

- [ ] **Step 5: Run capability and MCP secrecy gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_authorization.py tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_mcp_server.py -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/authorization.py src/intent_engineering/integrations/mcp_server/intent_workflow.py tests/unit/intent_workflow/test_authorization.py
.venv/bin/mypy src
```

Expected: all pass; token sentinel is absent from files, logs, errors, and captured repository
traceback locals.

- [ ] **Step 6: Commit Task 7**

```bash
git add src/intent_engineering/intent_workflow/authorization.py src/intent_engineering/intent_workflow/preflight.py src/intent_engineering/integrations/mcp_server/intent_workflow.py tests/unit/intent_workflow/test_authorization.py tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_mcp_server.py
git commit -m "feat: issue scoped intent preflight capabilities"
```

---

### Task 8: Add the agent-host contract and Codex capability gate

**Files:**
- Create: `src/intent_engineering/integrations/agent_host/__init__.py`
- Create: `src/intent_engineering/integrations/agent_host/base.py`
- Create: `src/intent_engineering/integrations/agent_host/codex.py`
- Create: `tests/contract/agent_host/test_host_contract.py`
- Create: `tests/contract/agent_host/test_codex_adapter.py`
- Create when the official contract supports pre-tool hooks: `plugins/intent-preflight/.codex-plugin/plugin.json`
- Create when supported: `plugins/intent-preflight/skills/intent-preflight/SKILL.md`
- Create when supported: `plugins/intent-preflight/.mcp.json`

**Interfaces:**
- Consumes: Task 7 MCP preflight/verify contracts.
- Produces: `AgentHostAdapter.before_task`, `.before_mutation`, `.after_task`, `.enabled`; `CodexIntentAdapter` or fixed `MandatoryHookUnavailable`.

- [ ] **Step 1: Write the host-neutral mandatory contract RED**

```python
@pytest.mark.anyio
async def test_enabled_adapter_blocks_mutation_without_matching_grant(adapter) -> None:
    task = await adapter.before_task(request="Add export", actor="local:asha")
    denied = await adapter.before_mutation(
        task=task,
        operation="write_file",
        paths=("src/export.py",),
        token=None,
    )
    assert denied.allowed is False
    assert denied.reason == "intent_preflight_required"


def test_host_without_real_pretool_hook_refuses_mandatory_mode() -> None:
    with pytest.raises(MandatoryHookUnavailable):
        CodexIntentAdapter.from_contract(FakeCodexContract(pre_mutation_hook=None), mandatory=True)
```

- [ ] **Step 2: Run the host contract tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/agent_host -q -W error
```

Expected: collection fails because the agent-host package does not exist.

- [ ] **Step 3: Implement the provider-neutral adapter protocol**

Define the host-neutral records and fixed activation error first:

```python
class HostTask(StrictModel):
    schema_version: Literal[1] = 1
    id: str
    actor: str
    repository_id: str
    conversation_ref: str
    request_digest: str
    graph_version: int
    created_at: datetime
    preflight: PreflightResult


class HostTaskResult(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str
    status: Literal["completed", "blocked", "failed", "cancelled"]
    response_evidence_ref: str
    changed_paths: tuple[str, ...] = ()
    commit_sha: str | None = None
    test_refs: tuple[str, ...] = ()
    completed_at: datetime


class MutationDecision(StrictModel):
    schema_version: Literal[1] = 1
    allowed: bool
    reason: Literal[
        "authorized",
        "plugin_disabled",
        "intent_preflight_required",
        "scope_mismatch",
        "graph_changed",
        "task_changed",
    ]
    task_id: str
    graph_version: int
    paths: tuple[str, ...]


class MandatoryHookUnavailable(RuntimeError):
    """Fixed public failure when the host cannot deny before mutation."""
```

Bound all identifiers/paths and require UTC timestamps. Token material is never a model field; the
long-lived adapter/MCP process retains it separately and supplies it only to verification.

```python
class AgentHostAdapter(Protocol):
    @property
    def enabled(self) -> bool: ...
    async def before_task(self, request: str, actor: str) -> HostTask: ...
    async def before_mutation(
        self,
        *,
        task: HostTask,
        operation: str,
        paths: tuple[str, ...],
        token: str | None,
    ) -> MutationDecision: ...
    async def after_task(self, task: HostTask, result: HostTaskResult) -> None: ...
```

The base implementation calls only the versioned MCP workflow surface. `before_task` records the
human turn before preflight; clarification records each agent question and human answer; and
`after_task` requires an already-captured agent completion evidence reference before it delegates
post-task processing. It normalizes paths relative
to the held repository root, rejects path escapes/control characters/oversize input, never caches
graph context across graph versions, and makes disabled mode a transparent no-op.

- [ ] **Step 4: Audit the current official Codex plugin contract before adapter code**

At execution time, invoke the `openai-docs` skill, inspect the installed local Codex plugin schemas
first, and use official OpenAI documentation only as fallback. Record the exact installed contract
version and whether it exposes a synchronous pre-mutation hook capable of denying native file and
shell mutation tools.

The decision is binary:

```python
if contract.can_deny_native_mutation_before_effect:
    adapter = CodexIntentAdapter(contract, mandatory=True)
else:
    raise MandatoryHookUnavailable("Codex mandatory mutation hook is unavailable")
```

Skills, instructions, prompts, post-tool notifications, or MCP-only guidance do not satisfy this
condition.

- [ ] **Step 5: Implement only the supported Codex outcome**

If the hook exists, scaffold the plugin using the current official manifest schema, register the
local Intent MCP server, bind task and pre-mutation events to `CodexIntentAdapter`, and add an E2E
test proving a native write tool is denied before effect without a token and allowed with a matching
token.

If the hook does not exist, ship `CodexIntentAdapter.from_installed_contract()` that raises the fixed
`MandatoryHookUnavailable` before activation, persist no plugin bundle, and make the test assert
that no `plugins/intent-preflight` directory exists. In both branches, the core host-neutral adapter
contract remains complete and testable.

- [ ] **Step 6: Run host, MCP, and static gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/agent_host tests/contract/mcp/test_intent_workflow_tools.py -q -W error
.venv/bin/ruff check src/intent_engineering/integrations/agent_host tests/contract/agent_host
.venv/bin/mypy src
git diff --check
```

Expected: host-neutral tests pass; Codex test proves either real denial-before-effect or fixed refusal
to activate mandatory mode.

- [ ] **Step 7: Commit Task 8**

If the hook exists:

```bash
git add src/intent_engineering/integrations/agent_host tests/contract/agent_host plugins/intent-preflight
git commit -m "feat: enforce intent preflight in Codex"
```

If the hook does not exist:

```bash
git add src/intent_engineering/integrations/agent_host tests/contract/agent_host
git commit -m "feat: gate unsupported mandatory agent hosts"
```

---

### Task 9: Link post-task evidence and expand scheduled assurance

**Files:**
- Create: `src/intent_engineering/intent_workflow/post_task.py`
- Create: `src/intent_engineering/intent_workflow/assurance.py`
- Modify: `src/intent_engineering/reconcile/evidence_detection.py`
- Modify: `src/intent_engineering/sync/orchestrator.py`
- Modify: `src/intent_engineering/integrations/agent_host/base.py`
- Test: `tests/integration/intent_workflow/test_post_task.py`
- Test: `tests/integration/intent_workflow/test_scheduled_assurance.py`

**Interfaces:**
- Consumes: Task 7 authorization grants, existing Git/Markdown evidence, `ImplementationClaim`, `DetectionInput`, `DriftObservation`, sync/case transaction services.
- Produces: `PostTaskService.evaluate`, `AssuranceService.detect`; post-task host delegation.

- [ ] **Step 1: Write scope and scheduled-drift REDs**

```python
def test_post_task_links_code_and_tests_to_authorized_requirements(post_task_harness) -> None:
    result = post_task_harness.complete(
        changed_paths=("src/export.py", "tests/test_export.py"),
        requirement_ids=("requirement:csv-export",),
    )
    assert result.status == "recorded"
    claim = post_task_harness.latest_claim()
    assert claim.requirement_refs == ("requirement:csv-export",)
    assert claim.code_evidence and claim.test_evidence


def test_scope_expansion_withholds_completion_and_requires_preflight(post_task_harness) -> None:
    result = post_task_harness.complete(changed_paths=("src/unrelated.py",))
    assert result.status == "preflight_required"
    assert post_task_harness.graph_and_history_unchanged()


def test_assurance_finds_intent_requirement_code_and_test_gaps(assurance_harness) -> None:
    case_types = {case.case_type.value for case in assurance_harness.run()}
    assert case_types >= {"INTENT_LAG", "ORPHAN_REQUIREMENT", "UNDOCUMENTED_CODE", "TEST_LAG"}
```

- [ ] **Step 2: Run post-task/assurance tests and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_post_task.py tests/integration/intent_workflow/test_scheduled_assurance.py -q -W error
```

Expected: collection fails because post-task and assurance services are absent.

- [ ] **Step 3: Implement post-task scope comparison**

`PostTaskSubmission` contains task ID, authorization token, base/final repository revision, changed
paths, requirement IDs, code refs, test refs, and captured Git evidence refs. Verify the token again
against current actor/repository/graph/task/scope. Reject missing evidence, paths beyond the grant,
unknown or ACL-hidden requirements, and stale graph versions. Build implementation claims and graph
links only as a typed proposal; apply deterministic already-authorized evidence linkage through one
ChangeSet. Scope expansion returns `preflight_required` with no mutation.

- [ ] **Step 4: Implement scheduled assurance observations**

Use current graph/evidence/cases plus deterministic indexes to create `DriftObservation` records for:

```python
checks = (
    detect_intent_without_requirement,
    detect_requirement_without_intent,
    detect_code_lag,
    detect_undocumented_code,
    detect_test_lag,
    detect_conflicting_sources,
    detect_relevant_provisional_intent,
    detect_stale_source_evidence,
)
```

Extend enums/meta-model only where a required case type is not already present. Preserve existing
detector precedence and stable fingerprints. The optional background reasoner is a protocol injected
into `AssuranceService`; the default is `None`, which performs capture and deterministic detection
without semantic inference.

- [ ] **Step 5: Wire scheduled operation without approving changes**

Call `AssuranceService.detect` after successful evidence persistence and graph reasoning but before
checkpoint finalization. Append cases through the existing executor/transaction boundary. A
reasoner failure returns a partial source result, leaves graph/case/checkpoint bytes exact for that
semantic phase, and retains durable raw evidence according to the existing replay contract.

- [ ] **Step 6: Run sync, detector, and full workflow gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_post_task.py tests/integration/intent_workflow/test_scheduled_assurance.py tests/integration/sync tests/unit/reconcile -q -W error
.venv/bin/ruff check src/intent_engineering/intent_workflow/post_task.py src/intent_engineering/intent_workflow/assurance.py src/intent_engineering/reconcile/evidence_detection.py tests/integration/intent_workflow
.venv/bin/mypy src
```

Expected: all pass; repeated identical assurance run adds no case or graph/history row.

- [ ] **Step 7: Commit Task 9**

```bash
git add src/intent_engineering/intent_workflow/post_task.py src/intent_engineering/intent_workflow/assurance.py src/intent_engineering/reconcile/evidence_detection.py src/intent_engineering/sync/orchestrator.py src/intent_engineering/integrations/agent_host/base.py tests/integration/intent_workflow/test_post_task.py tests/integration/intent_workflow/test_scheduled_assurance.py
git commit -m "feat: reconcile completed tasks with intent"
```

---

### Task 10: Prove the complete operating model and document adoption

**Files:**
- Create: `tests/e2e/test_intent_aware_agent_workflow.py`
- Create: `tests/e2e/intent_aware_agent_harness.py`
- Modify: `tests/e2e/test_public_alpha.py`
- Modify: `README.md`
- Modify: `docs/mcp.md`
- Create: `docs/intent-aware-agent.md`
- Modify: `.github/workflows/intent-sync.yml`
- Modify: `CONTRIBUTING.md`

**Interfaces:**
- Consumes: Tasks 1–9 and all existing production CLI/MCP/sync/write services.
- Produces: one offline release proof and truthful user/operator documentation.

- [ ] **Step 1: Write the complete E2E RED**

```python
def test_existing_repo_prd_to_agent_to_scheduled_assurance(intent_agent_harness) -> None:
    bootstrap = intent_agent_harness.bootstrap("docs/prd.md")
    assert bootstrap.core_confirmed
    assert bootstrap.provisional_ids
    assert bootstrap.graph_version == 1

    aligned = intent_agent_harness.preflight("Implement CSV export")
    assert aligned.classification == "aligned"
    assert aligned.token
    assert intent_agent_harness.mutate_with_token(aligned.token).allowed
    assert intent_agent_harness.post_task(aligned.token).status == "recorded"

    new = intent_agent_harness.preflight("Add team sharing")
    assert new.classification == "new_or_ambiguous"
    proposal = intent_agent_harness.answer_and_propose(new.questions)
    assert intent_agent_harness.confirm_as_contributor(proposal.id).status == "applied"

    conflict = intent_agent_harness.preflight("Upload all raw conversations")
    assert conflict.classification == "conflicting"
    assert conflict.review_case_id
    assert not intent_agent_harness.mutate_without_token().allowed

    first = intent_agent_harness.scheduled_assurance()
    second = intent_agent_harness.scheduled_assurance()
    assert first.reported_cases
    assert second.semantic_changes == 0
```

Also assert author/source/version/predecessor provenance, exact graph/history/case consistency, one
shared project state across CLI/MCP/plugin/scheduler, no credential sentinel in regular files,
stdout, stderr, logs, errors, or repository traceback locals, and unchanged behavior with the plugin
disabled.

- [ ] **Step 2: Run the E2E test and confirm RED**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_intent_aware_agent_workflow.py -q -W error
```

Expected: failure at the first missing integrated composition boundary.

- [ ] **Step 3: Build the production-composition harness**

The harness may fake only external HTTP/MCP/model/agent-host boundaries and the interactive terminal.
It must use `initialize_project`, `load_runtime`, real connectors, real evidence/graph/history/case/
proposal stores, `SyncOrchestrator`, bootstrap/preflight/clarification/authorization/post-task/
assurance services, official in-memory MCP calls, and the actual host-neutral adapter. All phases use
one project and one transaction domain.

- [ ] **Step 4: Document the enabled and disabled user journeys**

`docs/intent-aware-agent.md` must include executable commands for install/init, PRD bootstrap,
source-role assignment, proposal review, plugin activation or fixed unsupported-host result, manual
preflight diagnostics, frequent capture, scheduled assurance, conflict review, and disabling the
plugin. README retains the concise happy path. Documentation must state that active-agent reasoning
submits proposals, background model inference is optional, and unsupported hosts are advisory only
when explicitly configured as such.

- [ ] **Step 5: Update the scheduled workflow contract**

Keep capture and assurance as separate steps. The workflow runs validation, combined source sync,
then drift/assurance reporting. It never runs proposal confirmation, approval creation, or external
write execution. Add an offline structural test asserting exact trigger, permissions, order, and
absence of mutation commands.

- [ ] **Step 6: Run focused, broad, full, coverage, static, and help gates**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow tests/integration/intent_workflow tests/contract/agent_host tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_intent_aware_agent_workflow.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract tests/integration tests/e2e/test_public_alpha.py tests/e2e/test_intent_aware_agent_workflow.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -p pytest_cov.plugin --cov=src/intent_engineering --cov-report=term -q -W error
.venv/bin/ruff check $(git ls-files '*.py')
.venv/bin/ruff format --check $(git ls-files '*.py')
.venv/bin/mypy src
.venv/bin/intent bootstrap --help
.venv/bin/intent preflight --help
.venv/bin/intent proposals --help
.venv/bin/intent mcp --help
git diff --check
```

Expected: every command exits 0. Record exact test counts, duration, statement/miss coverage, source
file count, and any absent configured coverage threshold.

- [ ] **Step 7: Request independent adversarial review before commits**

Use `superpowers:requesting-code-review`. Reviewer scope must include the complete feature range,
spec and plan, all new untracked product/test/docs files, preflight bypasses, source-role authority,
proposal/approval aliases, token lifecycle, host hook non-vacuity, graph/evidence transactions,
scheduled no-op/partial failure, cancellation, secret absence, and test non-vacuity. Resolve every
Critical or Important finding with a new focused RED before production changes; repeat review until
clean.

- [ ] **Step 8: Commit release proof and report separately**

```bash
git add README.md CONTRIBUTING.md docs/intent-aware-agent.md docs/mcp.md .github/workflows/intent-sync.yml tests/e2e/test_intent_aware_agent_workflow.py tests/e2e/intent_aware_agent_harness.py tests/e2e/test_public_alpha.py
git commit -m "feat: complete intent-aware agent workflow"
```

Then write the task report and progress ledger with exact commits/gates/review findings and commit
only those metadata files separately.

---

## Final completion checklist

- [ ] Every spec requirement maps to a task above.
- [ ] Every production behavior began with a focused failing test.
- [ ] All canonical graph mutations use validated ChangeSets and shared transactions.
- [ ] All source-derived and conversational statements retain provenance and authorship.
- [ ] Mechanical classification cannot authorize uncertain semantic work.
- [ ] Aligned context is bounded and ACL-filtered.
- [ ] Non-conflicting contributor confirmation and independent conflict review are both proven.
- [ ] Tokens are process-local, short-lived, graph/task/actor/repository/scope-bound, and absent from persistence/logs/errors.
- [ ] Mandatory Codex mode is either genuinely enforced before native mutation or explicitly refused.
- [ ] Post-task evidence remains inside authorized scope.
- [ ] Scheduled capture/assurance is idempotent and cannot approve or write externally.
- [ ] Plugin-disabled behavior is unchanged.
- [ ] Full offline, coverage, Ruff, format, mypy, help, diff, and independent review gates are clean.
- [ ] Only the five protected untracked artifacts remain outside tracked work.
