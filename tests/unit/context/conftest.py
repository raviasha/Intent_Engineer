"""Shared deterministic fixtures for context-provider tests."""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

import pytest

from intent_engineering.context.provider import ContextProvider
from intent_engineering.core.models import (
    Edge,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    Node,
    NodeType,
    ProjectConfig,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    SourceMode,
)

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _node(
    node_id: str,
    node_type: NodeType,
    label: str,
    *,
    confidence: float | None = 0.9,
    evidence_refs: tuple[str, ...] = ("ev-export",),
) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label=label,
        status="active",
        created_by="fixture@example.test",
        created_at=NOW,
        last_modified_by="fixture@example.test",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT if evidence_refs else None,
        intent_fidelity_confidence=confidence,
        evidence_refs=evidence_refs,
    )


def _edge(edge_id: str, from_id: str, to_id: str, *, status: str = "active") -> Edge:
    return Edge(
        id=edge_id,
        from_id=from_id,
        relation="REFINES",
        to_id=to_id,
        status=status,
        created_by="fixture@example.test",
        created_at=NOW,
        last_modified_by="fixture@example.test",
        last_modified_at=NOW,
    )


def _evidence(
    evidence_id: str, *, repository_scope: str | None, acl: tuple[str, ...]
) -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence_id,
        connector_type="fixture",
        external_object_id=evidence_id,
        external_version="1",
        author="fixture@example.test",
        observed_at=NOW,
        source_locator=f"fixture://{evidence_id}",
        content_hash=sha256(evidence_id.encode()).hexdigest(),
        payload={} if repository_scope is None else {"repository_scope": repository_scope},
        acl=acl,
    )


@dataclass(frozen=True)
class ContextFixture:
    graph: Graph
    cases: tuple[ReconciliationCase, ...]
    config: ProjectConfig
    evidence: tuple[EvidenceRecord, ...]
    provider: ContextProvider


@pytest.fixture
def context_fixture() -> ContextFixture:
    graph = Graph(
        id="context-graph",
        version=1,
        nodes=(
            _node("intent-local-export", NodeType.PRODUCT_INTENT, "Support local export"),
            _node("req-local-export", NodeType.REQUIREMENT, "Add local export café"),
            _node("test-local-export", NodeType.TEST, "Test local export"),
            _node(
                "req-unrelated",
                NodeType.REQUIREMENT,
                "Synchronize remote archive",
                evidence_refs=("ev-unrelated",),
            ),
            _node(
                "constraint-low-confidence",
                NodeType.CONSTRAINT,
                "Local export migration",
                confidence=0.5,
            ),
        ),
        edges=(
            _edge("edge-intent-requirement", "intent-local-export", "req-local-export"),
            _edge("edge-requirement-test", "req-local-export", "test-local-export"),
            _edge("edge-requirement-low", "req-local-export", "constraint-low-confidence"),
            _edge("edge-unrelated", "req-local-export", "req-unrelated", status="inactive"),
        ),
    )
    case = ReconciliationCase(
        id="case-export-tests",
        subject_ref="req-local-export",
        case_type=ReconciliationCaseType.TEST_LAG,
        affected_refs=("req-local-export", "test-local-export"),
        evidence_sides=(
            EvidenceSide(
                label="tests",
                claim="Tests lag local export",
                evidence_refs=("ev-case",),
                observed_at=NOW,
                authors=("fixture@example.test",),
                confidence=0.8,
            ),
        ),
        detector_id="fixture",
        fingerprint=sha256(b"case-export-tests").hexdigest(),
        created_at=NOW,
        status=ReconciliationStatus.OPEN,
    )
    config = ProjectConfig(
        project_id="fixture",
        local_actor="alice@example.test",
        context_limits={
            "relevant_intent": 10,
            "relevant_requirements": 1,
            "decisions": 10,
            "constraints": 10,
            "acceptance_criteria": 10,
            "code_refs": 20,
            "test_refs": 20,
            "open_reconciliation_cases": 10,
            "evidence_refs": 2,
        },
    )
    evidence = (
        _evidence("ev-export", repository_scope="repo-a", acl=("alice@example.test",)),
        _evidence("ev-case", repository_scope="repo-a", acl=("alice@example.test",)),
        _evidence("ev-unrelated", repository_scope="repo-b", acl=("bob@example.test",)),
    )
    provider = ContextProvider(
        graph,
        (case,),
        config,
        evidence,
    )
    return ContextFixture(
        graph=graph, cases=(case,), config=config, evidence=evidence, provider=provider
    )
