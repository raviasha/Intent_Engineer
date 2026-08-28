# Intent Engineering Core Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local-first, deterministic Intent Engineering engine that ingests Markdown and Git evidence, maintains a validated YAML intent graph, detects drift, creates reconciliation cases, produces context packs, and exposes the complete local CLI.

**Architecture:** Implement a modular Python monolith with a provider-neutral semantic core, ports for persistence and capture, YAML/JSONL local adapters, and application services for sync, reconciliation, and context. Every semantic graph change is a validated ChangeSet, evidence is immutable, and repeated sync is idempotent.

**Tech Stack:** Python 3.12+, Pydantic 2, Typer, PyYAML, jsonschema, structlog, anyio, pytest, pytest-cov, Ruff, mypy, Git subprocesses

**Spec:** `docs/superpowers/specs/2026-08-25-intent-engineering-public-alpha-design.md`

## Global Constraints

- Python 3.12 or newer; one installable distribution exposing the `intent` command.
- Local operation must not require a model provider or cloud account.
- YAML is canonical graph state; JSONL is append-only evidence/history; derived caches are non-canonical.
- Evidence is immutable or version-addressed; secrets never enter `.intent/`, evidence, history, or logs.
- Domain code must not import connector, MCP, GitHub, or cloud SDK types.
- Every semantic graph mutation must be represented by a validated ChangeSet with a baseline graph version.
- Sync must be idempotent; identical input produces no evidence version, graph mutation, or duplicate case.
- Confidence is epistemic and independent of implementation status, priority, and completion.
- CODE_LAG and REQUIREMENT_LAG must be distinguishable from different evidence patterns.
- Material semantic changes and cross-author conflict resolutions require human review.
- Generated Markdown and Mermaid are views and never mutate canonical graph state.
- Use Apache-2.0 for the repository license.

---

## File Structure

Create these focused units before adding integrations:

```text
pyproject.toml                         Packaging, dependencies, quality-tool configuration
src/intent_engineering/__init__.py    Package version
src/intent_engineering/cli/app.py     Typer root application
src/intent_engineering/core/models/   Pydantic semantic records only
src/intent_engineering/core/graph/    Graph validation, identity, and ChangeSet application
src/intent_engineering/core/policy/   Auto-apply and review policy
src/intent_engineering/storage/       Store protocols and YAML/JSONL adapters
src/intent_engineering/capture/       Connector protocol and local adapters
src/intent_engineering/sync/          Idempotent orchestration and run summaries
src/intent_engineering/reconcile/     Drift detectors and case lifecycle
src/intent_engineering/context/       Task/symbol context selection
src/intent_engineering/render/        Non-canonical Markdown/Mermaid rendering
schemas/                              Copied meta-model plus generated JSON Schemas
graph/                                Framework dogfood graph
tests/unit/                           Isolated model and service tests
tests/contract/                       Reusable port/adapter contracts
tests/integration/                    Multi-component fixture tests
tests/e2e/                            CLI tests against temporary repositories
tests/fixtures/                       Evidence-pattern fixture projects
```

### Task 1: Bootstrap the repository and package

**Files:**
- Create: `INTENT_ENGINEERING.md`
- Create: `CODEX_IMPLEMENTATION.md`
- Create: `ROADMAP.md`
- Create: `schemas/intent-meta-model.yaml`
- Create: `graph/framework-intent-graph.yaml`
- Create: `LICENSE`
- Create: `README.md`
- Create: `pyproject.toml`
- Create: `src/intent_engineering/__init__.py`
- Create: `src/intent_engineering/cli/app.py`
- Create: `tests/unit/test_package.py`

**Interfaces:**
- Consumes: the attached starter archive at `/Users/rampetaravishankar/Desktop/intent-engineering-codex-starter-v0.1.0.zip`.
- Produces: `intent_engineering.__version__: str` and Typer application `intent_engineering.cli.app:app`.

- [ ] **Step 1: Import the five starter artifacts without editing their meaning**

Run a bulk extraction into a temporary directory, then copy only the named artifacts into the repository:

```bash
starter_dir=$(mktemp -d)
unzip -q /Users/rampetaravishankar/Desktop/intent-engineering-codex-starter-v0.1.0.zip -d "$starter_dir"
cp "$starter_dir"/intent-engineering-codex-starter-v0.1.0/INTENT_ENGINEERING.md INTENT_ENGINEERING.md
cp "$starter_dir"/intent-engineering-codex-starter-v0.1.0/CODEX_IMPLEMENTATION.md CODEX_IMPLEMENTATION.md
cp "$starter_dir"/intent-engineering-codex-starter-v0.1.0/ROADMAP.md ROADMAP.md
mkdir -p schemas graph
cp "$starter_dir"/intent-engineering-codex-starter-v0.1.0/schemas/intent-meta-model.yaml schemas/intent-meta-model.yaml
cp "$starter_dir"/intent-engineering-codex-starter-v0.1.0/graph/framework-intent-graph.yaml graph/framework-intent-graph.yaml
```

Expected: all five files exist and remain readable UTF-8 text.

- [ ] **Step 2: Write the failing packaging test**

```python
from typer.testing import CliRunner

from intent_engineering import __version__
from intent_engineering.cli.app import app


runner = CliRunner()


def test_package_version_and_cli_help() -> None:
    assert __version__ == "0.1.0"
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Intent Engineering" in result.stdout
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/unit/test_package.py -v`
Expected: FAIL because the package and CLI application do not exist.

- [ ] **Step 4: Add packaging, the initial CLI, and the license**

Create `pyproject.toml` with this minimum configuration:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "intent-engineering"
version = "0.1.0"
description = "Local-first intent graph and reconciliation engine for software repositories"
readme = "README.md"
requires-python = ">=3.12"
license = { text = "Apache-2.0" }
dependencies = [
  "pydantic>=2,<3",
  "typer>=0.12,<1",
  "PyYAML>=6,<7",
  "jsonschema>=4,<5",
  "structlog>=24,<27",
  "anyio>=4,<5",
]

[project.optional-dependencies]
dev = [
  "pytest>=8,<10",
  "pytest-cov>=5,<8",
  "ruff>=0.9,<1",
  "mypy>=1.13,<2",
]

[project.scripts]
intent = "intent_engineering.cli.app:app"

[tool.hatch.build.targets.wheel]
packages = ["src/intent_engineering"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["intent_engineering"]
```

Create `src/intent_engineering/__init__.py`:

```python
__version__ = "0.1.0"
```

Create `src/intent_engineering/cli/app.py`:

```python
import typer

app = typer.Typer(
    name="intent",
    help="Intent Engineering: evidence-backed intent, drift, and reconciliation.",
    no_args_is_help=True,
)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
```

Use the standard Apache-2.0 license text in `LICENSE` with copyright line `Copyright 2026 Intent Engineering contributors`.

Create `README.md` with the heading `# Intent Engineering`, the one-sentence package description from `pyproject.toml`, and a note that the executable quick start is completed in Task 10.

- [ ] **Step 5: Install and verify the bootstrap**

Run: `python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
Run: `.venv/bin/pytest tests/unit/test_package.py -v`
Run: `.venv/bin/intent --help`
Expected: test PASS and CLI help contains `Intent Engineering`.

- [ ] **Step 6: Commit**

```bash
git add INTENT_ENGINEERING.md CODEX_IMPLEMENTATION.md ROADMAP.md schemas graph LICENSE README.md pyproject.toml src tests/unit/test_package.py
git commit -m "chore: bootstrap intent engineering package"
```

### Task 2: Define graph, epistemic, and implementation models

**Files:**
- Create: `src/intent_engineering/core/models/enums.py`
- Create: `src/intent_engineering/core/models/graph.py`
- Create: `src/intent_engineering/core/models/epistemic.py`
- Create: `src/intent_engineering/core/models/implementation.py`
- Create: `src/intent_engineering/core/models/__init__.py`
- Create: `tests/unit/core/models/test_graph.py`
- Create: `tests/unit/core/models/test_epistemic.py`

**Interfaces:**
- Consumes: node and relation vocabulary from `schemas/intent-meta-model.yaml`.
- Produces: `Node`, `Edge`, `Graph`, `EpistemicState`, `ConfidenceChange`, and `ImplementationClaim` Pydantic models; `Graph.assert_invariants() -> None`.

- [ ] **Step 1: Write failing graph invariant tests**

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import Edge, Graph, Node, NodeType, SourceMode


NOW = datetime(2026, 8, 25, tzinfo=UTC)


def node(node_id: str) -> Node:
    return Node(
        id=node_id,
        type=NodeType.REQUIREMENT,
        label="Export is local-first",
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT,
        intent_fidelity_confidence=0.9,
        evidence_refs=["ev-1"],
    )


def test_confidence_range_is_validated() -> None:
    payload = node("req-1").model_dump()
    payload["intent_fidelity_confidence"] = 1.1
    with pytest.raises(ValidationError):
        Node.model_validate(payload)


def test_duplicate_node_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate node id: req-1"):
        Graph(id="g", version=0, nodes=[node("req-1"), node("req-1")], edges=[])


def test_edge_to_missing_node_is_rejected() -> None:
    edge = Edge(
        id="e-1",
        from_id="req-1",
        relation="VERIFIED_BY",
        to_id="test-missing",
        status="active",
        created_by="tester",
        created_at=NOW,
        last_modified_by="tester",
        last_modified_at=NOW,
    )
    with pytest.raises(ValueError, match="missing node: test-missing"):
        Graph(id="g", version=0, nodes=[node("req-1")], edges=[edge])
```

- [ ] **Step 2: Run graph tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/core/models/test_graph.py -v`
Expected: FAIL because model modules are missing.

- [ ] **Step 3: Implement the graph vocabulary and invariants**

Use `StrEnum` for the supplied node types, `SourceMode`, `ImplementationStatus`, reconciliation values, and change kinds. Define `NodeType` with every value in the starter meta-model. Add a `TypeRegistry` model containing `extensions: set[str]`; accept an unknown node type only when it is namespaced as `namespace:name` and present in that registry.

Implement these public shapes:

```python
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class Node(BaseModel, frozen=True):
    id: str
    type: NodeType | str
    label: str
    status: str
    created_by: str
    created_at: datetime
    last_modified_by: str
    last_modified_at: datetime
    source_mode: SourceMode | None = None
    intent_fidelity_confidence: Confidence | None = None
    confidence_basis: str | None = None
    last_reassessed_at: datetime | None = None
    evidence_refs: Sequence[str] = ()


class Edge(BaseModel, frozen=True):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_id: str = Field(alias="from")
    relation: str
    to_id: str = Field(alias="to")
    status: str
    created_by: str
    created_at: datetime
    last_modified_by: str
    last_modified_at: datetime
    external: bool = False


class Graph(BaseModel, frozen=True):
    id: str
    version: int = Field(ge=0)
    schema_version: str = "0.1.0"
    name: str | None = None
    purpose: str | None = None
    nodes: Sequence[Node]
    edges: Sequence[Edge]
    type_registry: TypeRegistry = Field(default_factory=TypeRegistry)

    @model_validator(mode="after")
    def assert_invariants(self) -> "Graph":
        node_ids = [item.id for item in self.nodes]
        edge_ids = [item.id for item in self.edges]
        duplicate_nodes = sorted({item for item in node_ids if node_ids.count(item) > 1})
        duplicate_edges = sorted({item for item in edge_ids if edge_ids.count(item) > 1})
        if duplicate_nodes:
            raise ValueError(f"duplicate node id: {duplicate_nodes[0]}")
        if duplicate_edges:
            raise ValueError(f"duplicate edge id: {duplicate_edges[0]}")
        known = set(node_ids)
        for node in self.nodes:
            self.type_registry.assert_registered(node.type)
            if node.source_mode is not None and not node.evidence_refs:
                raise ValueError(f"provenance-backed node requires evidence: {node.id}")
        for edge in self.edges:
            if edge.from_id not in known:
                raise ValueError(f"missing node: {edge.from_id}")
            if not edge.external and edge.to_id not in known:
                raise ValueError(f"missing node: {edge.to_id}")
        return self
```

- [ ] **Step 4: Write and implement epistemic/implementation tests**

Add tests proving that `ConfidenceChange` requires an evidence ref and distinct prior/new values, and `ImplementationClaim(status=IMPLEMENTED_BASELINE)` requires `requirement_refs`, `code_evidence`, `verified_commit`, `verified_at`, and test evidence when `test_evidence_required=True`.

Implement immutable models with these signatures:

```python
class EpistemicState(BaseModel, frozen=True):
    confidence: Confidence
    basis: str
    evidence_refs: Sequence[str]
    last_reassessed_at: datetime


class ConfidenceChange(BaseModel, frozen=True):
    change_id: str
    timestamp: datetime
    actor: str
    subject_ref: str
    change_kind: ChangeKind
    prior_confidence: Confidence
    new_confidence: Confidence
    evidence_refs: Sequence[str]
    reason: str


class ImplementationClaim(BaseModel, frozen=True):
    id: str
    status: ImplementationStatus
    requirement_refs: Sequence[str]
    current_behavior: str
    code_evidence: Sequence[str]
    test_evidence: Sequence[str] = ()
    verified_commit: str | None = None
    verified_at: datetime | None = None
    test_evidence_required: bool = True
```

Use model validators to enforce the stated conditions.

- [ ] **Step 5: Run model tests and quality checks**

Run: `.venv/bin/pytest tests/unit/core/models -v`
Run: `.venv/bin/ruff check src/intent_engineering/core tests/unit/core`
Run: `.venv/bin/mypy src/intent_engineering/core`
Expected: all commands PASS.

- [ ] **Step 6: Commit**

```bash
git add src/intent_engineering/core tests/unit/core
git commit -m "feat: add graph and epistemic domain models"
```

### Task 3: Add immutable evidence and ChangeSet models

**Files:**
- Create: `src/intent_engineering/core/models/evidence.py`
- Create: `src/intent_engineering/core/models/changeset.py`
- Create: `src/intent_engineering/core/models/project.py`
- Create: `schemas/graph.schema.json`
- Create: `schemas/evidence.schema.json`
- Create: `schemas/reconciliation.schema.json`
- Modify: `src/intent_engineering/core/models/__init__.py`
- Create: `tests/unit/core/models/test_evidence.py`
- Create: `tests/unit/core/models/test_changeset.py`

**Interfaces:**
- Consumes: `Node`, `Edge`, `ConfidenceChange`, `ImplementationClaim`.
- Produces: `EvidenceRecord.identity_key`, `EvidenceDelta`, `CandidateAssertion`, `NodeUpdate`, `ChangeSet`, `ProjectConfig`, `SyncCheckpoint`, and deterministic JSON Schemas.

- [ ] **Step 1: Write failing evidence identity tests**

```python
from datetime import UTC, datetime

from intent_engineering.core.models import EvidenceRecord


def test_evidence_identity_includes_version_and_hash() -> None:
    record = EvidenceRecord(
        id="ev-git-1",
        connector_type="git",
        external_object_id="commit:abc",
        external_version="abc",
        author="developer@example.com",
        observed_at=datetime(2026, 8, 25, tzinfo=UTC),
        source_locator="git:abc",
        content_hash="sha256:1234",
        payload={"message": "Add local export"},
    )
    assert record.identity_key == "git|commit:abc|abc|sha256:1234"
    assert record.model_copy(update={"payload": {"message": "changed"}}).id == "ev-git-1"
```

- [ ] **Step 2: Run the evidence test to verify it fails**

Run: `.venv/bin/pytest tests/unit/core/models/test_evidence.py -v`
Expected: FAIL because `EvidenceRecord` is not defined.

- [ ] **Step 3: Implement evidence and checkpoint models**

```python
class EvidenceRef(BaseModel, frozen=True):
    evidence_id: str
    locator: str | None = None


class EvidenceRecord(BaseModel, frozen=True):
    id: str
    connector_type: str
    external_object_id: str
    external_version: str
    author: str | None
    observed_at: datetime
    source_locator: str
    content_hash: str
    payload: dict[str, JsonValue]
    parent_ref: str | None = None
    acl: Sequence[str] = ()

    @property
    def identity_key(self) -> str:
        return "|".join(
            (self.connector_type, self.external_object_id, self.external_version, self.content_hash)
        )


class EvidenceDelta(BaseModel, frozen=True):
    added: Sequence[EvidenceRecord]
    prior_versions: dict[str, str]


class SyncCheckpoint(BaseModel, frozen=True):
    connector_id: str
    cursor: str | None
    committed_at: datetime


class ProjectConfig(BaseModel, frozen=True):
    project_id: str
    graph_path: str = ".intent/graph.yaml"
    local_actor: str
    source_exclusions: Sequence[str] = ()
    auto_apply_metadata: bool = True
    auto_apply_semantic: bool = False
    context_limits: dict[str, int] = Field(
        default_factory=lambda: {
            "relevant_intent": 10,
            "relevant_requirements": 10,
            "decisions": 10,
            "constraints": 10,
            "acceptance_criteria": 10,
            "code_refs": 20,
            "test_refs": 20,
            "open_reconciliation_cases": 10,
        }
    )
```

Define `JsonValue` recursively from JSON scalars, lists, and dictionaries so evidence payloads remain serializable.

- [ ] **Step 4: Write failing ChangeSet tests**

```python
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import ChangeSet


def test_semantic_changeset_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="semantic ChangeSet requires evidence"):
        ChangeSet(
            id="cs-1",
            actor="tester",
            timestamp=datetime(2026, 8, 25, tzinfo=UTC),
            baseline_graph_version=0,
            evidence_refs=(),
            nodes_added=(),
            nodes_updated=(),
            nodes_superseded=("req-1",),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="pending",
        )
```

- [ ] **Step 5: Implement ChangeSet records and validation**

Define immutable `NodeUpdate(node_id: str, replacement: Node)`, `EdgeUpdate(edge_id: str, replacement: Edge)`, and `ImplementationStatusChange(claim_id: str, prior: ImplementationStatus, new: ImplementationStatus, evidence_refs: Sequence[str])`.

Define the deterministic reasoner boundary record here so later tasks share one type:

```python
class CandidateAssertion(BaseModel, frozen=True):
    id: str
    subject_id: str
    change_kind: ChangeKind
    node_type: NodeType | str
    label: str
    source_mode: SourceMode
    evidence_refs: Sequence[str]
    confidence: Confidence
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
```

Define `ChangeSet` with every mutation group from the starter meta-model. Add properties `is_empty: bool` and `is_semantic: bool`. A model validator must reject semantic changes with no evidence, duplicate subjects within a mutation group, and a subject appearing in both update and supersede groups.

Generate `schemas/graph.schema.json`, `schemas/evidence.schema.json`, and `schemas/reconciliation.schema.json` deterministically from their Pydantic models. A unit test must regenerate each schema in memory and compare it to the checked-in canonical JSON bytes.

- [ ] **Step 6: Run tests and commit**

Run: `.venv/bin/pytest tests/unit/core/models/test_evidence.py tests/unit/core/models/test_changeset.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/core/models tests/unit/core/models
git commit -m "feat: model immutable evidence and changesets"
```

### Task 4: Implement YAML graph and append-only evidence stores

**Files:**
- Create: `src/intent_engineering/storage/interfaces.py`
- Create: `src/intent_engineering/storage/yaml/graph_store.py`
- Create: `src/intent_engineering/storage/jsonl/evidence_store.py`
- Create: `src/intent_engineering/storage/jsonl/history_store.py`
- Create: `src/intent_engineering/storage/yaml/checkpoint_store.py`
- Create: `src/intent_engineering/core/graph/applier.py`
- Create: `tests/contract/storage/test_graph_store_contract.py`
- Create: `tests/contract/storage/test_evidence_store_contract.py`
- Create: `tests/contract/storage/test_checkpoint_store_contract.py`
- Create: `tests/unit/core/graph/test_applier.py`

**Interfaces:**
- Consumes: `Graph`, `EvidenceRecord`, `ChangeSet`.
- Produces: graph, evidence, history, and checkpoint stores plus pure `apply_changeset()`.

- [ ] **Step 1: Write reusable failing store contracts**

```python
def assert_graph_store_round_trip(store: GraphStore, graph: Graph) -> None:
    store.initialize(graph)
    assert store.load() == graph


def assert_evidence_store_is_idempotent(store: EvidenceStore, record: EvidenceRecord) -> None:
    assert store.put(record) is True
    assert store.put(record) is False
    assert store.get(record.id) == record
    assert store.versions(record.external_object_id) == (record,)
```

Add concrete pytest fixtures that run these contracts against `YamlGraphStore` and `JsonlEvidenceStore` in `tmp_path`.

- [ ] **Step 2: Run contracts to verify they fail**

Run: `.venv/bin/pytest tests/contract/storage -v`
Expected: FAIL because store interfaces and adapters are missing.

- [ ] **Step 3: Implement store protocols and durable adapters**

Define protocols with exact signatures:

```python
class GraphStore(Protocol):
    def initialize(self, graph: Graph) -> None:
        raise NotImplementedError
    def load(self) -> Graph:
        raise NotImplementedError
    def apply(self, changeset: ChangeSet) -> Graph:
        raise NotImplementedError
    def history(self, subject_id: str) -> Sequence[ChangeSet]:
        raise NotImplementedError


class EvidenceStore(Protocol):
    def put(self, record: EvidenceRecord) -> bool:
        raise NotImplementedError
    def get(self, evidence_id: str) -> EvidenceRecord:
        raise NotImplementedError
    def versions(self, external_object_id: str) -> Sequence[EvidenceRecord]:
        raise NotImplementedError
```

`YamlGraphStore` must write to a sibling temporary file, flush and `fsync`, then atomically replace the canonical file. Serialize datetimes as ISO-8601 strings, serialize edge endpoints with their YAML aliases, and use stable key ordering. `JsonlEvidenceStore` must maintain an in-memory index rebuilt from disk at construction, reject an existing ID whose content differs, and return `False` for an exact duplicate. `YamlCheckpointStore` atomically stores one typed checkpoint per connector and exposes compare-and-set updates.

Define the checkpoint port exactly:

```python
class CheckpointStore(Protocol):
    def get(self, connector_id: str) -> SyncCheckpoint | None:
        raise NotImplementedError

    def compare_and_set(
        self,
        connector_id: str,
        expected: SyncCheckpoint | None,
        cursor: str | None,
        committed_at: datetime,
    ) -> SyncCheckpoint:
        raise NotImplementedError
```

- [ ] **Step 4: Write failing ChangeSet application tests**

Test that applying a valid node addition increments graph version exactly once, a stale baseline raises `StaleGraphVersion`, and a failed invariant leaves the original YAML bytes unchanged.

- [ ] **Step 5: Implement deterministic ChangeSet application**

Create a pure function:

```python
def apply_changeset(graph: Graph, changeset: ChangeSet) -> Graph:
    if changeset.baseline_graph_version != graph.version:
        raise StaleGraphVersion(changeset.baseline_graph_version, graph.version)
    nodes = {item.id: item for item in graph.nodes}
    edges = {item.id: item for item in graph.edges}
    for item in changeset.nodes_added:
        if item.id in nodes:
            raise DuplicateIdentity(item.id)
        nodes[item.id] = item
    for update in changeset.nodes_updated:
        if update.node_id not in nodes:
            raise UnknownIdentity(update.node_id)
        nodes[update.node_id] = update.replacement
    for node_id in changeset.nodes_superseded:
        current = nodes[node_id]
        nodes[node_id] = current.model_copy(update={"status": "superseded"})
    for item in changeset.edges_added:
        if item.id in edges:
            raise DuplicateIdentity(item.id)
        edges[item.id] = item
    for update in changeset.edges_updated:
        if update.edge_id not in edges:
            raise UnknownIdentity(update.edge_id)
        edges[update.edge_id] = update.replacement
    for edge_id in changeset.edges_superseded:
        current = edges[edge_id]
        edges[edge_id] = current.model_copy(update={"status": "superseded"})
    return graph.model_copy(
        update={
            "version": graph.version + 1,
            "nodes": tuple(nodes[key] for key in sorted(nodes)),
            "edges": tuple(edges[key] for key in sorted(edges)),
        }
    )
```

Validate the returned `Graph` before the store replaces the canonical file. Persist the ChangeSet to history after the graph replacement succeeds.

- [ ] **Step 6: Verify stores and commit**

Run: `.venv/bin/pytest tests/contract/storage tests/unit/core/graph -v`
Expected: PASS.

```bash
git add src/intent_engineering/storage src/intent_engineering/core/graph tests/contract/storage tests/unit/core/graph
git commit -m "feat: add durable graph and evidence stores"
```

### Task 5: Implement reconciliation cases and deterministic drift detectors

**Files:**
- Create: `src/intent_engineering/core/models/reconciliation.py`
- Create: `src/intent_engineering/reconcile/detectors.py`
- Create: `src/intent_engineering/reconcile/service.py`
- Create: `src/intent_engineering/storage/jsonl/case_store.py`
- Create: `tests/unit/reconcile/test_detectors.py`
- Create: `tests/unit/reconcile/test_case_lifecycle.py`
- Create: `tests/unit/reconcile/builders.py`
- Create: `tests/contract/storage/test_case_store_contract.py`

**Interfaces:**
- Consumes: `Graph`, `EvidenceRecord`, `ImplementationClaim`.
- Produces: `DriftObservation`, `ReconciliationCase`, `DetectionInput`, `detect_drift()`, and `transition_case()`.

- [ ] **Step 1: Write failing classification tests**

```python
def test_newer_requirement_without_new_code_is_code_lag() -> None:
    result = detect_drift(code_lag_input())
    assert [item.case_type for item in result] == [ReconciliationCaseType.CODE_LAG]


def test_newer_decision_and_code_against_old_requirement_is_requirement_lag() -> None:
    result = detect_drift(requirement_lag_input())
    assert [item.case_type for item in result] == [ReconciliationCaseType.REQUIREMENT_LAG]


def test_insufficient_evidence_is_ambiguous() -> None:
    result = detect_drift(ambiguous_input())
    assert [item.case_type for item in result] == [
        ReconciliationCaseType.AMBIGUOUS_DIVERGENCE
    ]


def test_cross_author_incompatibility_preserves_both_positions() -> None:
    result = detect_drift(cross_author_conflict_input())
    assert [item.case_type for item in result] == [
        ReconciliationCaseType.CONFLICTING_SOURCES
    ]
    assert len(result[0].evidence_sides) == 2
```

Fixture builders must use fixed timestamps and explicit semantic labels; no model call is allowed.

Create `tests/unit/reconcile/builders.py` with `code_lag_input()`, `requirement_lag_input()`, `ambiguous_input()`, and `cross_author_conflict_input()`. Each returns a `DetectionInput` with fixed UTC timestamps, complete evidence refs, author IDs, semantic compatibility labels, requirement version, implementation version, and test version. The cross-author builder uses two current explicit assertions from different authors with compatibility value `contradicts`.

- [ ] **Step 2: Run detector tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/reconcile/test_detectors.py -v`
Expected: FAIL because detector modules are missing.

- [ ] **Step 3: Implement case records and six detectors**

Define `EvidenceSide(label, claim, evidence_refs, observed_at, authors, confidence)`, `DriftObservation(subject_ref, case_type, affected_refs, evidence_sides, detector_id, fingerprint)`, and `ReconciliationCase` with the required lifecycle/audit fields.

`ReconciliationCase.all_evidence_refs` returns the stable, sorted union of refs from every evidence side. This property is the write planner's evidence input and prevents provider integrations from reimplementing evidence traversal.

Define `CaseStore.put(case) -> bool`, `CaseStore.get(case_id) -> ReconciliationCase`, `CaseStore.find_by_fingerprint(fingerprint) -> ReconciliationCase | None`, and `CaseStore.list(status=None) -> Sequence[ReconciliationCase]`. `JsonlCaseStore` is append-only, returns `False` for an exact duplicate, and appends a new version when a case with the same ID changes lifecycle state.

`detect_drift(input: DetectionInput) -> Sequence[DriftObservation]` must run detectors in this precedence order: CONFLICTING_SOURCES, REQUIREMENT_LAG, CODE_LAG, TEST_LAG, UNDOCUMENTED_CODE, AMBIGUOUS_DIVERGENCE. A deterministic SHA-256 fingerprint over detector ID, subject, affected refs, and evidence refs prevents duplicate cases.

```python
DETECTORS: Sequence[Callable[[DetectionInput], DriftObservation | None]] = (
    detect_conflicting_sources,
    detect_requirement_lag,
    detect_code_lag,
    detect_test_lag,
    detect_undocumented_code,
    detect_ambiguous_divergence,
)


def detect_drift(input: DetectionInput) -> Sequence[DriftObservation]:
    observations: list[DriftObservation] = []
    classified_subjects: set[str] = set()
    for detector in DETECTORS:
        observation = detector(input)
        if observation is not None and observation.subject_ref not in classified_subjects:
            observations.append(observation)
            classified_subjects.add(observation.subject_ref)
    return tuple(sorted(observations, key=lambda item: (item.case_type.value, item.fingerprint)))
```

- [ ] **Step 4: Write and implement lifecycle tests**

Test allowed transitions `open → proposed → needs_human → resolved`, `open → deferred`, and `open → false_positive`. Test that `resolved` requires a resolution action, actor, timestamp, and ChangeSet ID; disallow reopening in the alpha.

Implement:

```python
def transition_case(
    case: ReconciliationCase,
    target: ReconciliationStatus,
    actor: str,
    at: datetime,
    resolution: ResolutionAction | None = None,
    changeset_id: str | None = None,
) -> ReconciliationCase:
    allowed = ALLOWED_TRANSITIONS[case.status]
    if target not in allowed:
        raise InvalidCaseTransition(case.status, target)
    if target is ReconciliationStatus.RESOLVED and (resolution is None or changeset_id is None):
        raise MissingResolutionEvidence(case.id)
    event = ClassificationEvent(actor=actor, at=at, prior=case.status, new=target)
    return case.model_copy(
        update={
            "status": target,
            "resolution": resolution,
            "resolved_by_changeset": changeset_id,
            "history": case.history + (event,),
        }
    )
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/unit/reconcile tests/contract/storage/test_case_store_contract.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/core/models/reconciliation.py src/intent_engineering/reconcile src/intent_engineering/storage/jsonl/case_store.py tests/unit/reconcile tests/contract/storage/test_case_store_contract.py
git commit -m "feat: detect and track evidence-backed drift"
```

### Task 6: Implement Markdown and Git connectors

**Files:**
- Create: `src/intent_engineering/capture/base.py`
- Create: `src/intent_engineering/capture/markdown/connector.py`
- Create: `src/intent_engineering/capture/git/connector.py`
- Create: `src/intent_engineering/capture/checkpoints.py`
- Create: `tests/contract/capture/test_connector_contract.py`
- Create: `tests/integration/capture/test_markdown_connector.py`
- Create: `tests/integration/capture/test_git_connector.py`

**Interfaces:**
- Consumes: `EvidenceRecord`, `SyncCheckpoint`, project paths.
- Produces: `SourceObject`, `Connector.discover()`, `Connector.fetch()`, `Connector.normalize()`, and durable checkpoints.

- [ ] **Step 1: Write the connector contract and failing local tests**

```python
@pytest.mark.anyio
async def assert_connector_is_stable(connector: Connector) -> None:
    first = await connector.discover(cursor=None)
    second = await connector.discover(cursor=None)
    assert first == second
    for source in first:
        raw = await connector.fetch(source.external_object_id, source.external_version)
        evidence = connector.normalize(raw)
        assert evidence.external_object_id == source.external_object_id
        assert evidence.external_version == source.external_version
        assert evidence.content_hash.startswith("sha256:")
```

Markdown integration tests create two files, exclude one by glob, edit the included file, and assert two version-addressed EvidenceRecords. Git tests create a temporary repository with two commits and assert stable `commit:<sha>` external IDs.

- [ ] **Step 2: Run connector tests to verify they fail**

Run: `.venv/bin/pytest tests/contract/capture tests/integration/capture -v`
Expected: FAIL because connector implementations are missing.

- [ ] **Step 3: Implement the connector protocol and Markdown adapter**

```python
class SourceObject(BaseModel, frozen=True):
    external_object_id: str
    external_version: str
    locator: str


class Connector(Protocol):
    connector_id: str
    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        raise NotImplementedError
    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        raise NotImplementedError
    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        raise NotImplementedError
    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        raise NotImplementedError
```

The Markdown connector uses sorted POSIX-relative paths, exclusion globs from `ProjectConfig`, file bytes for SHA-256, and `path:<relative-path>` external IDs. The external version is the content hash so timestamp-only changes are no-ops. Its async methods move filesystem reads to `anyio.to_thread.run_sync`.

- [ ] **Step 4: Implement the Git adapter through a subprocess wrapper**

Create a private `run_git(repo: Path, args: Sequence[str]) -> str` that invokes `git -C <repo>`, sets `check=True`, captures UTF-8 output, and never uses a shell. Async connector methods call it through `anyio.to_thread.run_sync`. Discover commits with `git rev-list --reverse <cursor>..HEAD` or all commits when the cursor is absent. Fetch author, timestamp, parents, subject, body, and changed paths with machine-delimited formats. Normalize commits without storing diffs by default.

```python
def run_git(repo: Path, args: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
    revision = "HEAD" if cursor is None else f"{cursor}..HEAD"
    output = await anyio.to_thread.run_sync(run_git, self.repo, ["rev-list", "--reverse", revision])
    return tuple(self._source_object(sha) for sha in output.splitlines() if sha)
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/contract/capture tests/integration/capture -v`
Expected: PASS.

```bash
git add src/intent_engineering/capture tests/contract/capture tests/integration/capture
git commit -m "feat: ingest markdown and git evidence"
```

### Task 7: Build idempotent sync orchestration

**Files:**
- Create: `src/intent_engineering/extract/base.py`
- Create: `src/intent_engineering/extract/deterministic.py`
- Create: `src/intent_engineering/sync/models.py`
- Create: `src/intent_engineering/sync/orchestrator.py`
- Create: `tests/integration/sync/test_idempotency.py`
- Create: `tests/integration/sync/test_partial_failure.py`
- Create: `tests/integration/sync/conftest.py`

**Interfaces:**
- Consumes: connectors, stores, graph applier, detector service.
- Produces: `SemanticReasoner`, `DeterministicReasoner`, `SyncOrchestrator.run() -> SyncRunResult`.

- [ ] **Step 1: Write failing idempotency and partial-failure tests**

```python
@pytest.mark.anyio
async def test_second_identical_sync_has_no_semantic_change(sync_harness: SyncHarness) -> None:
    first = await sync_harness.run()
    second = await sync_harness.run()
    assert first.evidence_added > 0
    assert second.evidence_added == 0
    assert second.changes_applied == 0
    assert second.cases_created == 0
    assert second.status is SyncRunStatus.SUCCESS


@pytest.mark.anyio
async def test_failed_connector_does_not_advance_its_checkpoint(
    partial_failure_harness: SyncHarness,
) -> None:
    result = await partial_failure_harness.run()
    assert result.status is SyncRunStatus.PARTIAL
    assert result.connectors["markdown"].checkpoint_advanced is True
    assert result.connectors["broken"].checkpoint_advanced is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/sync -v`
Expected: FAIL because sync orchestration is missing.

- [ ] **Step 3: Implement the reasoning boundary and deterministic fixture reasoner**

```python
class SemanticReasoner(Protocol):
    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        raise NotImplementedError
    def map_to_graph(
        self, assertions: Sequence[CandidateAssertion], graph: Graph
    ) -> ChangeSet:
        raise NotImplementedError


class DeterministicReasoner:
    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        return tuple(
            CandidateAssertion.model_validate(item.payload["intent_assertion"])
            for item in delta.added
            if "intent_assertion" in item.payload
        )
```

Mapping uses an explicit `subject_id` from fixture assertions and produces an empty ChangeSet when no assertion changes semantic content.

Define sync result types before the orchestrator:

```python
class SyncRunStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ConnectorRunResult(BaseModel, frozen=True):
    status: SyncRunStatus
    evidence_added: int
    changes_applied: int
    cases_created: int
    checkpoint_advanced: bool
    redacted_error: str | None = None


class SyncRunResult(BaseModel, frozen=True):
    run_id: str
    status: SyncRunStatus
    connectors: dict[str, ConnectorRunResult]
    evidence_added: int
    changes_applied: int
    cases_created: int
    duration_ms: int
```

`tests/integration/sync/conftest.py` defines `SyncHarness.run()` as an async wrapper around one configured `SyncOrchestrator`, plus `sync_harness` and `partial_failure_harness` pytest fixtures. The failing connector raises `ConnectorError("fixture failure")` from `discover()`.

- [ ] **Step 4: Implement the orchestrator transaction order**

`await SyncOrchestrator.run(run_id: str, connectors: Sequence[Connector]) -> SyncRunResult` must, per connector, discover, fetch, normalize, write evidence, compute deltas, reason, validate/apply ChangeSets, detect cases, persist cases, then commit the checkpoint. Catch connector exceptions at the connector boundary, redact error messages, and continue with remaining connectors. Emit one structured summary with counts and durations.

```python
async def run(self, run_id: str, connectors: Sequence[Connector]) -> SyncRunResult:
    results: dict[str, ConnectorRunResult] = {}
    for connector in connectors:
        prior = self.checkpoints.get(connector.connector_id)
        try:
            discovered = await connector.discover(prior.cursor if prior is not None else None)
            records = tuple(
                connector.normalize(
                    await connector.fetch(item.external_object_id, item.external_version)
                )
                for item in discovered
            )
            added = tuple(record for record in records if self.evidence.put(record))
            change_count, case_count = self._apply_delta(EvidenceDelta(added=added, prior_versions={}))
            self.checkpoints.compare_and_set(
                connector.connector_id,
                expected=prior,
                cursor=connector.next_checkpoint(discovered),
                committed_at=utc_now(),
            )
            results[connector.connector_id] = ConnectorRunResult.succeeded(
                len(added), change_count, case_count
            )
        except ConnectorError as error:
            results[connector.connector_id] = ConnectorRunResult.failed(redact_error(error))
    return SyncRunResult.from_connector_results(run_id, results)
```

- [ ] **Step 5: Verify idempotency and commit**

Run: `.venv/bin/pytest tests/integration/sync -v`
Run: `.venv/bin/pytest tests/unit tests/contract tests/integration -v`
Expected: PASS; the second-sync test reports zero semantic changes.

```bash
git add src/intent_engineering/extract src/intent_engineering/sync tests/integration/sync
git commit -m "feat: orchestrate idempotent local sync"
```

### Task 8: Generate concise context packs and non-canonical views

**Files:**
- Create: `src/intent_engineering/core/models/context.py`
- Create: `src/intent_engineering/context/provider.py`
- Create: `src/intent_engineering/render/markdown.py`
- Create: `src/intent_engineering/render/mermaid.py`
- Create: `tests/unit/context/test_provider.py`
- Create: `tests/unit/render/test_renderers.py`
- Create: `tests/unit/context/conftest.py`
- Create: `tests/unit/render/conftest.py`

**Interfaces:**
- Consumes: graph, evidence refs, reconciliation cases, task or symbol query.
- Produces: `ContextPack`, `ContextProvider.for_task()`, `ContextProvider.for_symbol()`, `render_markdown()`, and `render_mermaid()`.

- [ ] **Step 1: Write failing relevance and immutability tests**

```python
def test_task_context_returns_only_connected_active_items(context_fixture: ContextFixture) -> None:
    pack = context_fixture.provider.for_task("add local export")
    assert [item.id for item in pack.relevant_requirements] == ["req-local-export"]
    assert [item.id for item in pack.open_reconciliation_cases] == ["case-export-tests"]
    assert "req-unrelated" not in pack.model_dump_json()


def test_render_does_not_change_graph_file(render_fixture: RenderFixture) -> None:
    before = render_fixture.graph_path.read_bytes()
    render_fixture.renderer.render_all(render_fixture.output_dir)
    assert render_fixture.graph_path.read_bytes() == before
```

`tests/unit/context/conftest.py` defines `ContextFixture(graph, cases, provider)` with one connected requirement and one unrelated requirement. `tests/unit/render/conftest.py` defines `RenderFixture(graph_path, output_dir, renderer)` using the production YAML store and a temporary generated-view directory.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/context tests/unit/render -v`
Expected: FAIL because context and renderer modules are missing.

- [ ] **Step 3: Implement context selection**

Define these exact context records, then normalize task tokens with lowercase Unicode word extraction, score labels by token overlap, expand through active edges up to two hops, include linked open cases and evidence refs, sort by descending score then stable ID, and cap every category using `ProjectConfig.context_limits`. Add a warning when a selected node has confidence below `0.65` or missing evidence.

```python
class ContextItem(BaseModel, frozen=True):
    id: str
    type: str
    label: str
    confidence: Confidence | None
    evidence_refs: Sequence[str]


class ContextPack(BaseModel, frozen=True):
    schema_version: Literal["1"] = "1"
    task: str
    relevant_intent: Sequence[ContextItem]
    relevant_requirements: Sequence[ContextItem]
    decisions: Sequence[ContextItem]
    constraints: Sequence[ContextItem]
    acceptance_criteria: Sequence[ContextItem]
    code_refs: Sequence[ContextItem]
    test_refs: Sequence[ContextItem]
    open_reconciliation_cases: Sequence[ContextItem]
    evidence_refs: Sequence[str]
    warnings: Sequence[str]


class ContextProvider:
    def for_task(
        self,
        task: str,
        repository_scope: str | None = None,
        actor: str | None = None,
    ) -> ContextPack:
        return self._build(query=task, repository_scope=repository_scope, actor=actor)

    def for_symbol(self, symbol_ref: str, actor: str | None = None) -> ContextPack:
        return self._build(query=symbol_ref, repository_scope=None, actor=actor)
```

- [ ] **Step 4: Implement deterministic renderers**

Markdown output contains purpose, selected nodes grouped by semantic layer, evidence references, and open cases. Mermaid output sorts nodes and edges by stable ID, escapes labels, and emits `flowchart LR`. Both take immutable model inputs and write only to the supplied output directory.

```python
def render_markdown(graph: Graph, cases: Sequence[ReconciliationCase]) -> str:
    sections = [f"# {graph.name or graph.id}", graph.purpose or ""]
    sections.extend(render_node_group(group, graph.nodes) for group in SEMANTIC_GROUPS)
    sections.append(render_cases(cases))
    return "\n\n".join(section for section in sections if section)


def render_mermaid(graph: Graph) -> str:
    lines = ["flowchart LR"]
    lines.extend(render_node(node) for node in sorted(graph.nodes, key=lambda item: item.id))
    lines.extend(render_edge(edge) for edge in sorted(graph.edges, key=lambda item: item.id))
    return "\n".join(lines) + "\n"
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/unit/context tests/unit/render -v`
Expected: PASS.

```bash
git add src/intent_engineering/core/models/context.py src/intent_engineering/context src/intent_engineering/render tests/unit/context tests/unit/render
git commit -m "feat: generate task context and graph views"
```

### Task 9: Complete the local CLI and `.intent/` workspace

**Files:**
- Modify: `src/intent_engineering/cli/app.py`
- Create: `src/intent_engineering/cli/runtime.py`
- Create: `src/intent_engineering/cli/output.py`
- Create: `src/intent_engineering/core/policy/project.py`
- Create: `tests/e2e/test_cli_local.py`
- Create: `tests/helpers/cli.py`

**Interfaces:**
- Consumes: all core application services.
- Produces: commands `init`, `validate`, `ingest`, `sync`, `drift`, `status`, `explain`, `context`, `reconcile list/show/resolve`, `render`, and `doctor`.

- [ ] **Step 1: Write a failing end-to-end CLI test**

```python
def test_local_quick_start(tmp_path: Path) -> None:
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    assert (repo / ".intent/config.yaml").exists()
    assert (repo / ".intent/graph.yaml").exists()
    assert run_intent(repo, "validate", "--format", "json").json()["valid"] is True
    assert run_intent(repo, "sync", "--sources", "markdown,git").returncode == 0
    status = run_intent(repo, "status", "--format", "json").json()
    assert status["project_id"] == repo.name
    assert run_intent(repo, "context", "--task", "local export").returncode == 0
```

`tests/helpers/cli.py` defines `init_git_repo(path) -> Path`, `run_intent(repo, *args) -> CliResult`, and `CliResult.json() -> dict[str, JsonValue]`. It invokes the installed CLI as a subprocess with `cwd=repo`, captures UTF-8 output, and supplies a scrubbed environment.

- [ ] **Step 2: Run the E2E test to verify it fails**

Run: `.venv/bin/pytest tests/e2e/test_cli_local.py -v`
Expected: FAIL because commands are not registered.

- [ ] **Step 3: Implement runtime loading and `intent init`**

`load_runtime(root: Path) -> Runtime` locates `.intent/config.yaml`, builds stores/services, and raises a typed `ProjectNotInitialized` error. `intent init` creates `config.yaml`, `graph.yaml`, `evidence/`, `reconciliation/`, `history/`, `approvals/`, and `cache/` without overwriting existing files unless `--force` is supplied. It writes no secrets. Every command accepts `--project <path>` and defaults to the current directory. Async application services are invoked from Typer through one `anyio.run` boundary per command.

```python
@dataclass(frozen=True)
class Runtime:
    config: ProjectConfig
    graph_store: GraphStore
    evidence_store: EvidenceStore
    case_store: CaseStore
    checkpoint_store: CheckpointStore
    sync: SyncOrchestrator
    context: ContextProvider


def load_runtime(root: Path) -> Runtime:
    workspace = root.resolve() / ".intent"
    if not (workspace / "config.yaml").is_file():
        raise ProjectNotInitialized(root)
    return RuntimeFactory(workspace).build()
```

- [ ] **Step 4: Register commands with stable output and exit codes**

Use shared output helpers for `text`, `json`, and `markdown`. Return exit code `0` for success, `1` for validation/runtime failure, `2` for usage error, `3` for partial sync, and `4` when review is required. `reconcile resolve` must create a ChangeSet and must not directly edit graph YAML.

```python
@app.command("sync")
def sync_command(
    project: Path = typer.Option(Path.cwd(), "--project"),
    sources: str = typer.Option("markdown,git", "--sources"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    runtime = load_runtime(project)
    result = anyio.run(runtime.sync.run, new_run_id(), resolve_connectors(runtime, sources))
    emit(result, output_format)
    if result.status is SyncRunStatus.PARTIAL:
        raise typer.Exit(3)
```

- [ ] **Step 5: Verify all command help and quick start**

Run: `.venv/bin/pytest tests/e2e/test_cli_local.py -v`
Run: `.venv/bin/intent --help`
Run: `.venv/bin/intent reconcile --help`
Expected: PASS and every documented command appears.

- [ ] **Step 6: Commit**

```bash
git add src/intent_engineering/cli src/intent_engineering/core/policy tests/e2e/test_cli_local.py
git commit -m "feat: expose local intent workflows through CLI"
```

### Task 10: Add fixtures, dogfood validation, CI, and documentation

**Files:**
- Create: `tests/fixtures/aligned_project/`
- Create: `tests/fixtures/code_lag/`
- Create: `tests/fixtures/requirement_lag/`
- Create: `tests/fixtures/test_lag/`
- Create: `tests/fixtures/undocumented_code/`
- Create: `tests/fixtures/ambiguous_divergence/`
- Create: `tests/fixtures/cross_author_conflict/`
- Create: `tests/fixtures/idempotent_sync/`
- Create: `tests/integration/test_fixture_matrix.py`
- Create: `tests/integration/test_framework_graph.py`
- Create: `tests/helpers/fixtures.py`
- Create: `.github/workflows/ci.yml`
- Create: `README.md`
- Create: `CONTRIBUTING.md`
- Create: `AGENTS.md`

**Interfaces:**
- Consumes: the completed local engine and starter framework graph.
- Produces: release-gate proof for the core slice and contributor-facing workflows.

- [ ] **Step 1: Write the failing fixture matrix test**

```python
@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("aligned_project", set()),
        ("code_lag", {"CODE_LAG"}),
        ("requirement_lag", {"REQUIREMENT_LAG"}),
        ("test_lag", {"TEST_LAG"}),
        ("undocumented_code", {"UNDOCUMENTED_CODE"}),
        ("ambiguous_divergence", {"AMBIGUOUS_DIVERGENCE"}),
        ("cross_author_conflict", {"CONFLICTING_SOURCES"}),
    ],
)
def test_fixture_classification(fixture_name: str, expected: set[str]) -> None:
    result = run_fixture(FIXTURES / fixture_name)
    assert {item.case_type.value for item in result.cases} == expected
```

- [ ] **Step 2: Add minimal deterministic fixture repositories**

Each fixture contains `.intent/config.yaml`, `.intent/graph.yaml`, evidence JSONL, and a tiny Git repository built by a fixture setup function. Use fixed timestamps and the explicit `intent_assertion` payload supported by `DeterministicReasoner`. The cross-author fixture must preserve both nodes and create a `CONFLICTING_SOURCES` case.

Create `tests/helpers/fixtures.py` with this public helper used by the matrix:

```python
FIXTURES = Path(__file__).parents[1] / "fixtures"


def run_fixture(path: Path) -> SyncRunResult:
    project = materialize_fixture_repository(path)
    runtime = RuntimeFactory(project / ".intent").build()
    return anyio.run(runtime.sync.run, "fixture-run", resolve_all_connectors(runtime))
```

- [ ] **Step 3: Test the framework graph with the production loader**

```python
def test_framework_graph_is_valid_and_provenance_backed() -> None:
    graph = YamlGraphStore(Path("graph/framework-intent-graph.yaml")).load()
    assert graph.id == "intent-engineering-framework"
    assert all(node.evidence_refs for node in graph.nodes if node.source_mode is not None)
    graph.assert_invariants()
```

Adapt the loader to accept the starter's top-level `graph`, `nodes`, and `edges` representation. Normalize the starter's semantic `graph.version: "0.1.0"` to `Graph.schema_version`, initialize the monotonic mutation `Graph.version` to `0`, and serialize subsequent canonical files with both values. Import the foundational spec as versioned evidence for `spec:<section>` refs during dogfood initialization.

- [ ] **Step 4: Add CI and contributor documentation**

CI runs install, Ruff, mypy, and the full test suite on Python 3.12. README must include the problem, Capture → Manage → Sync → Reconcile loop, local quick start, a CODE_LAG example, a REQUIREMENT_LAG example, context usage, and the local/cloud boundary. CONTRIBUTING documents environment setup, TDD, fixture conventions, and semantic ChangeSet requirements. AGENTS tells coding agents to read the foundational spec, meta-model, framework graph, roadmap, and implementation instructions in that order.

The CI job body is:

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.12"
  - run: python -m pip install -e '.[dev]'
  - run: ruff check .
  - run: mypy src/intent_engineering
  - run: pytest --cov=intent_engineering --cov-report=term-missing
```

- [ ] **Step 5: Run the core release gate**

Run: `.venv/bin/ruff check .`
Run: `.venv/bin/mypy src/intent_engineering`
Run: `.venv/bin/pytest --cov=intent_engineering --cov-report=term-missing`
Run: `.venv/bin/intent validate --project .`
Expected: all commands PASS; the fixture matrix distinguishes CODE_LAG from REQUIREMENT_LAG; the second-sync fixture records zero mutations.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures tests/integration .github/workflows/ci.yml README.md CONTRIBUTING.md AGENTS.md
git commit -m "test: prove the local intent reconciliation loop"
```

## Core Slice Completion Check

Before starting the GitHub integration plan, run:

```bash
.venv/bin/ruff check .
.venv/bin/mypy src/intent_engineering
.venv/bin/pytest
.venv/bin/intent --help
```

Expected: all commands succeed, the worktree is clean, and the core engine works without network access, a model provider, or a cloud account.
