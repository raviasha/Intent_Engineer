"""Scheduled assurance emits evidence-backed deterministic review observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from intent_engineering.capture.base import RawSourceObject, SourceObject
from intent_engineering.core.models import (
    ClassificationEvent,
    DriftObservation,
    Edge,
    EvidenceIngestion,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    Node,
    NodeType,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    RelationType,
    SourceMode,
)
from intent_engineering.intent_workflow.assurance import (
    AssuranceService,
    AssuranceSnapshot,
)
from intent_engineering.reconcile.detectors import DetectionInput
from intent_engineering.storage.yaml.graph_store import serialize_graph
from intent_engineering.sync.models import SyncRunStatus
from tests.integration.sync.conftest import SyncHarness

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
EARLIER = NOW - timedelta(days=1)
LATER = NOW + timedelta(days=1)


def _record(
    evidence_id: str,
    *,
    author: str,
    observed_at: datetime = NOW,
    external_object_id: str | None = None,
    acl: tuple[str, ...] = (),
) -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence_id,
        connector_type="markdown",
        external_object_id=external_object_id or evidence_id,
        external_version=evidence_id,
        author=author,
        observed_at=observed_at,
        source_locator=f"docs/{evidence_id}.md",
        content_hash=f"sha256:{evidence_id}",
        payload={"content": evidence_id},
        acl=acl,
    )


def _node(
    node_id: str,
    node_type: NodeType,
    evidence_id: str,
    *,
    author: str = "local:asha",
    at: datetime = NOW,
    status: str = "active",
    source_mode: SourceMode = SourceMode.EXPLICIT,
) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label=node_id,
        status=status,
        created_by=author,
        created_at=at,
        last_modified_by=author,
        last_modified_at=at,
        source_mode=source_mode,
        intent_fidelity_confidence=0.8,
        confidence_basis="fixture evidence",
        evidence_refs=(evidence_id,),
    )


def _edge(
    edge_id: str,
    from_id: str,
    relation: RelationType,
    to_id: str,
    *,
    author: str = "local:asha",
) -> Edge:
    return Edge(
        id=edge_id,
        from_id=from_id,
        relation=relation,
        to_id=to_id,
        status="active",
        created_by=author,
        created_at=NOW,
        last_modified_by=author,
        last_modified_at=NOW,
    )


def _ingestions(records: tuple[EvidenceRecord, ...]) -> tuple[EvidenceIngestion, ...]:
    previous: dict[str, str] = {}
    result: list[EvidenceIngestion] = []
    for sequence, record in enumerate(records, start=1):
        result.append(
            EvidenceIngestion(
                connector_id="markdown",
                sequence=sequence,
                predecessor_id=previous.get(record.external_object_id),
                evidence=record,
            )
        )
        previous[record.external_object_id] = record.id
    return tuple(result)


@pytest.mark.parametrize(
    ("nodes", "edges", "expected"),
    [
        (
            (_node("intent:lonely", NodeType.PRODUCT_INTENT, "ev-intent"),),
            (),
            ReconciliationCaseType.INTENT_LAG,
        ),
        (
            (_node("requirement:orphan", NodeType.REQUIREMENT, "ev-requirement"),),
            (),
            ReconciliationCaseType.ORPHAN_REQUIREMENT,
        ),
        (
            (
                _node("requirement:code", NodeType.REQUIREMENT, "ev-requirement", at=LATER),
                _node("file:old", NodeType.FILE, "ev-code", at=EARLIER),
                _node("intent:code", NodeType.PRODUCT_INTENT, "ev-intent"),
            ),
            (
                _edge("e-intent-code", "intent:code", RelationType.REFINES, "requirement:code"),
                _edge("e-code", "requirement:code", RelationType.IMPLEMENTED_BY, "file:old"),
            ),
            ReconciliationCaseType.CODE_LAG,
        ),
        (
            (_node("file:undocumented", NodeType.FILE, "ev-code"),),
            (),
            ReconciliationCaseType.UNDOCUMENTED_CODE,
        ),
        (
            (
                _node("requirement:test", NodeType.REQUIREMENT, "ev-requirement"),
                _node("intent:test", NodeType.PRODUCT_INTENT, "ev-intent"),
                _node("file:new", NodeType.FILE, "ev-code", at=LATER),
                _node("test:old", NodeType.TEST, "ev-test", at=EARLIER),
            ),
            (
                _edge("e-intent-test", "intent:test", RelationType.REFINES, "requirement:test"),
                _edge("e-impl", "requirement:test", RelationType.IMPLEMENTED_BY, "file:new"),
                _edge("e-test", "requirement:test", RelationType.VERIFIED_BY, "test:old"),
            ),
            ReconciliationCaseType.TEST_LAG,
        ),
        (
            (
                _node("requirement:a", NodeType.REQUIREMENT, "ev-a", author="local:asha"),
                _node("decision:b", NodeType.DECISION, "ev-b", author="local:ben"),
            ),
            (
                _edge(
                    "e-conflict",
                    "requirement:a",
                    RelationType.CONTRADICTS,
                    "decision:b",
                ),
            ),
            ReconciliationCaseType.CONFLICTING_SOURCES,
        ),
        (
            (
                _node(
                    "intent:provisional",
                    NodeType.PRODUCT_INTENT,
                    "ev-intent",
                    status="provisional",
                    source_mode=SourceMode.INFERRED,
                ),
                _node("requirement:provisional", NodeType.REQUIREMENT, "ev-requirement"),
                _node("file:relevant", NodeType.FILE, "ev-code"),
            ),
            (
                _edge(
                    "e-provisional-requirement",
                    "intent:provisional",
                    RelationType.REFINES,
                    "requirement:provisional",
                ),
                _edge(
                    "e-relevant",
                    "requirement:provisional",
                    RelationType.IMPLEMENTED_BY,
                    "file:relevant",
                ),
            ),
            ReconciliationCaseType.POSSIBLE_INTENT_CHANGE,
        ),
    ],
)
def test_each_topology_assurance_check_emits_exact_evidence_backed_observation(
    nodes: tuple[Node, ...],
    edges: tuple[Edge, ...],
    expected: ReconciliationCaseType,
) -> None:
    refs = tuple(sorted({ref for node in nodes for ref in node.evidence_refs}))
    records = tuple(
        _record(ref, author=next(node.created_by for node in nodes if ref in node.evidence_refs))
        for ref in refs
    )
    observations = AssuranceService(actor="local:asha").detect(
        graph=Graph(id="graph", version=1, nodes=nodes, edges=edges),
        records=records,
        ingestions=_ingestions(records),
        existing_cases=(),
    )

    assert {item.case_type for item in observations} == {expected}
    assert all(
        side.evidence_refs and side.authors
        for item in observations
        for side in item.evidence_sides
    )


def test_stale_source_evidence_uses_complete_visible_version_chain() -> None:
    old = _record(
        "ev-old", author="local:asha", observed_at=EARLIER, external_object_id="prd"
    )
    new = _record(
        "ev-new", author="local:asha", observed_at=NOW, external_object_id="prd"
    )
    intent = _record("ev-intent", author="local:asha")
    graph = Graph(
        id="graph",
        version=1,
        nodes=(
            _node("intent:stale", NodeType.PRODUCT_INTENT, "ev-intent"),
            _node("requirement:stale", NodeType.REQUIREMENT, "ev-old"),
        ),
        edges=(
            _edge("e-stale", "intent:stale", RelationType.REFINES, "requirement:stale"),
        ),
    )
    records = (old, new, intent)
    observations = AssuranceService(actor="local:asha").detect(
        graph=graph,
        records=records,
        ingestions=_ingestions(records),
        existing_cases=(),
    )
    assert len(observations) == 1
    assert observations[0].case_type is ReconciliationCaseType.AMBIGUOUS_DIVERGENCE
    assert observations[0].detector_id == "stale_source_evidence"
    assert observations[0].evidence_sides[0].current is False

    hidden = new.model_copy(update={"acl": ("local:ben",)})
    hidden_records = (old, hidden, intent)
    assert AssuranceService(actor="local:asha").detect(
        graph=graph,
        records=hidden_records,
        ingestions=_ingestions(hidden_records),
        existing_cases=(),
    ) == ()


def test_noncurrent_evidence_cannot_authorize_an_unrelated_topology_case() -> None:
    old = _record(
        "ev-old", author="local:asha", observed_at=EARLIER, external_object_id="prd"
    )
    new = _record(
        "ev-new", author="local:asha", observed_at=NOW, external_object_id="prd"
    )
    graph = Graph(
        id="graph",
        version=1,
        nodes=(_node("requirement:stale-orphan", NodeType.REQUIREMENT, "ev-old"),),
        edges=(),
    )

    observations = AssuranceService(actor="local:asha").detect(
        graph=graph,
        records=(old, new),
        ingestions=_ingestions((old, new)),
        existing_cases=(),
    )

    assert [item.case_type for item in observations] == [
        ReconciliationCaseType.AMBIGUOUS_DIVERGENCE
    ]


def test_terminal_case_fingerprint_suppresses_repeats() -> None:
    record = _record("ev-intent", author="local:asha")
    graph = Graph(
        id="graph",
        version=1,
        nodes=(_node("intent:lonely", NodeType.PRODUCT_INTENT, record.id),),
        edges=(),
    )
    service = AssuranceService(actor="local:asha")
    first = service.detect(
        graph=graph,
        records=(record,),
        ingestions=_ingestions((record,)),
        existing_cases=(),
    )[0]
    existing = ReconciliationCase(
        id="case:sha256:" + "f" * 64,
        subject_ref=first.subject_ref,
        case_type=first.case_type,
        affected_refs=first.affected_refs,
        evidence_sides=first.evidence_sides,
        detector_id=first.detector_id,
        fingerprint="f" * 64,
        created_at=NOW,
        created_by="detector:test",
        status=ReconciliationStatus.DEFERRED,
        history=(
            ClassificationEvent(
                actor="local:reviewer",
                at=NOW,
                prior=ReconciliationStatus.OPEN,
                new=ReconciliationStatus.DEFERRED,
            ),
        ),
    )

    assert service.detect(
        graph=graph,
        records=(record,),
        ingestions=_ingestions((record,)),
        existing_cases=(existing,),
    ) == ()


class _InventedProvenanceReasoner:
    def detect(self, snapshot: AssuranceSnapshot) -> tuple[DetectionInput, ...]:
        record = snapshot.records[0]
        side = snapshot.graph.nodes[0]
        from intent_engineering.core.models import EvidenceSide

        return (
            DetectionInput(
                subject_ref=side.id,
                affected_refs=(side.id,),
                implementation=EvidenceSide(
                    label=side.type.value,
                    claim=side.label,
                    evidence_refs=(record.id,),
                    observed_at=record.observed_at,
                    authors=("local:invented",),
                    confidence=side.intent_fidelity_confidence or 0.0,
                    source_mode=side.source_mode or SourceMode.EXPLICIT,
                    current=True,
                ),
                compatibility="aligns",
                has_mapped_semantics=False,
                material_code_change=True,
            ),
        )


class _InventedTopologyReasoner:
    def detect(self, snapshot: AssuranceSnapshot) -> tuple[DetectionInput, ...]:
        node = next(item for item in snapshot.graph.nodes if item.id == "file:linked")
        record = next(item for item in snapshot.records if item.id == "ev-code")
        return (
            DetectionInput(
                subject_ref=node.id,
                affected_refs=(node.id,),
                implementation=EvidenceSide(
                    label=node.type.value,
                    claim=node.label,
                    evidence_refs=(record.id,),
                    observed_at=record.observed_at,
                    authors=(record.author,),
                    confidence=node.intent_fidelity_confidence or 0.0,
                    source_mode=node.source_mode or SourceMode.EXPLICIT,
                    current=True,
                ),
                compatibility="aligns",
                has_mapped_semantics=False,
                material_code_change=True,
            ),
        )


def test_reasoner_cannot_manufacture_snapshot_provenance() -> None:
    record = _record("ev-code", author="local:asha")
    graph = Graph(
        id="graph",
        version=1,
        nodes=(_node("file:export", NodeType.FILE, record.id),),
        edges=(),
    )

    with pytest.raises(ValueError, match="ungrounded assurance reasoner output"):
        AssuranceService(
            actor="local:asha", reasoner=_InventedProvenanceReasoner()
        ).detect(
            graph=graph,
            records=(record,),
            ingestions=_ingestions((record,)),
            existing_cases=(),
        )


def test_reasoner_cannot_override_deterministic_graph_topology() -> None:
    records = (
        _record("ev-intent", author="local:asha"),
        _record("ev-requirement", author="local:asha"),
        _record("ev-code", author="local:asha"),
    )
    graph = Graph(
        id="graph",
        version=1,
        nodes=(
            _node("intent:linked", NodeType.PRODUCT_INTENT, "ev-intent"),
            _node("requirement:linked", NodeType.REQUIREMENT, "ev-requirement"),
            _node("file:linked", NodeType.FILE, "ev-code"),
        ),
        edges=(
            _edge(
                "edge:intent",
                "intent:linked",
                RelationType.REFINES,
                "requirement:linked",
            ),
            _edge(
                "edge:code",
                "requirement:linked",
                RelationType.IMPLEMENTED_BY,
                "file:linked",
            ),
        ),
    )

    with pytest.raises(ValueError, match="ungrounded assurance reasoner output"):
        AssuranceService(
            actor="local:asha", reasoner=_InventedTopologyReasoner()
        ).detect(
            graph=graph,
            records=records,
            ingestions=_ingestions(records),
            existing_cases=(),
        )


def test_assurance_is_stable_under_permutation_and_graph_only_is_insufficient() -> None:
    nodes = (
        _node("intent:lonely", NodeType.PRODUCT_INTENT, "ev-intent"),
        _node("requirement:orphan", NodeType.REQUIREMENT, "ev-requirement"),
    )
    records = (
        _record("ev-intent", author="local:asha"),
        _record("ev-requirement", author="local:asha"),
    )
    graph = Graph(id="graph", version=1, nodes=nodes, edges=())
    service = AssuranceService(actor="local:asha")
    first = service.detect(
        graph=graph,
        records=records,
        ingestions=_ingestions(records),
        existing_cases=(),
    )
    second = service.detect(
        graph=graph.model_copy(update={"nodes": tuple(reversed(nodes))}),
        records=tuple(reversed(records)),
        ingestions=_ingestions(records),
        existing_cases=(),
    )
    assert second == first
    assert service.detect(graph=graph, records=(), ingestions=(), existing_cases=()) == ()


def test_review_fix_hidden_unrelated_node_does_not_erase_visible_assurance() -> None:
    visible = _record("ev-visible", author="local:asha")
    hidden = _record("ev-hidden", author="local:ben", acl=("local:ben",))
    graph = Graph(
        id="graph",
        version=1,
        nodes=(
            _node("intent:visible", NodeType.PRODUCT_INTENT, visible.id),
            _node(
                "requirement:hidden",
                NodeType.REQUIREMENT,
                hidden.id,
                author="local:ben",
            ),
        ),
        edges=(),
    )

    observations = AssuranceService(actor="local:asha").detect(
        graph=graph,
        records=(visible, hidden),
        ingestions=_ingestions((visible, hidden)),
        existing_cases=(),
    )

    assert len(observations) == 1
    assert observations[0].subject_ref == "intent:visible"
    assert "requirement:hidden" not in observations[0].affected_refs


class _AssuranceConnector:
    connector_id = "markdown"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        if cursor == "v1":
            return ()
        return (
            SourceObject(
                external_object_id="intent:scheduled",
                external_version="v1",
                locator="intent.md",
            ),
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        return RawSourceObject(
            connector_type="markdown",
            external_object_id=object_id,
            external_version=version,
            author="local:asha",
            observed_at=NOW,
            source_locator="intent.md",
            content_hash="sha256:scheduled",
            payload={"content": "Scheduled intent"},
        )

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        del raw
        return _record("ev-scheduled", author="local:asha")

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str:
        return "v1"


class _FailingArchiveConnector:
    connector_id = "archive"
    connector_type = "markdown"

    async def discover(self, cursor: str | None) -> tuple[SourceObject, ...]:
        del cursor
        raise ValueError("private archive failure")

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        raise AssertionError((object_id, version))

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        raise AssertionError(raw)

    def next_checkpoint(self, discovered: tuple[SourceObject, ...]) -> str:
        raise AssertionError(discovered)


def _scheduled_graph() -> Graph:
    return Graph(
        id="scheduled",
        version=0,
        nodes=(
            _node("intent:scheduled", NodeType.PRODUCT_INTENT, "ev-scheduled"),
        ),
        edges=(),
    )


@pytest.mark.anyio
async def test_sync_runs_assurance_before_checkpoint_and_replay_is_case_noop(
    tmp_path: Path,
) -> None:
    harness = SyncHarness(tmp_path, (_AssuranceConnector(),))
    harness.graph_store.initialize(_scheduled_graph())
    harness.orchestrator._assurance_service = AssuranceService(  # type: ignore[attr-defined]
        actor="local:asha"
    )

    first = await harness.run()

    assert first.status is SyncRunStatus.SUCCESS
    assert first.cases_created == 1
    assert harness.checkpoint_store.get("markdown") is not None
    cases = harness.case_store.list()
    assert len(cases) == 1
    assert cases[0].case_type is ReconciliationCaseType.INTENT_LAG
    assert len(harness.graph_store.history(cases[0].id)) == 1
    case_before = harness.case_path.read_bytes()

    second = await harness.run()

    assert second.status is SyncRunStatus.SUCCESS
    assert second.cases_created == 0
    assert harness.case_path.read_bytes() == case_before


@pytest.mark.anyio
async def test_review_fix_sync_uses_all_durable_sources_when_one_connector_fails(
    tmp_path: Path,
) -> None:
    harness = SyncHarness(
        tmp_path,
        (_AssuranceConnector(), _FailingArchiveConnector()),
    )
    archive = _record("ev-archive", author="local:ben")
    harness.evidence_store.associate("archive", archive)
    harness.graph_store.initialize(
        Graph(
            id="scheduled",
            version=0,
            nodes=(
                _node("intent:scheduled", NodeType.PRODUCT_INTENT, "ev-scheduled"),
                _node("intent:archive", NodeType.PRODUCT_INTENT, archive.id),
            ),
            edges=(),
        )
    )
    harness.orchestrator._assurance_service = AssuranceService(  # type: ignore[attr-defined]
        actor="local:asha"
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.PARTIAL
    assert result.connectors["archive"].redacted_error == "connector failed"
    assert {case.subject_ref for case in harness.case_store.list()} == {
        "intent:archive",
        "intent:scheduled",
    }
    assert harness.checkpoint_store.get("markdown") is not None


class _DriftingAssuranceService:
    def __init__(self, harness: SyncHarness, target: str) -> None:
        self._harness = harness
        self._target = target
        self._delegate = AssuranceService(actor="local:asha")

    def detect(self, **kwargs: object) -> tuple[DriftObservation, ...]:
        observations = self._delegate.detect(**kwargs)  # type: ignore[arg-type]
        if self._target == "graph":
            graph = self._harness.graph_store.load()
            changed = graph.model_copy(
                update={
                    "nodes": tuple(
                        node.model_copy(update={"label": f"{node.label} external"})
                        if node.id == "intent:scheduled"
                        else node
                        for node in graph.nodes
                    )
                }
            )
            self._harness.graph_store._file.atomic_write(  # type: ignore[attr-defined]
                serialize_graph(changed)
            )
        elif self._target == "config":
            self._harness.config_file.atomic_write(b"project_id: externally-changed\n")
        else:
            observation = observations[0]
            self._harness.case_store.put(
                ReconciliationCase(
                    id="case:sha256:" + "f" * 64,
                    subject_ref=observation.subject_ref,
                    case_type=ReconciliationCaseType.POSSIBLE_INTENT_CHANGE,
                    affected_refs=observation.affected_refs,
                    evidence_sides=observation.evidence_sides,
                    detector_id="external_detector",
                    fingerprint="f" * 64,
                    created_at=NOW,
                    created_by="detector:external_detector",
                )
            )
        return observations


@pytest.mark.anyio
@pytest.mark.parametrize("target", ("graph", "config", "cases"))
async def test_review_fix_snapshot_drift_rejects_case_commit_then_retry_is_exact_noop(
    tmp_path: Path,
    target: str,
) -> None:
    harness = SyncHarness(tmp_path, (_AssuranceConnector(),))
    harness.graph_store.initialize(_scheduled_graph())
    history_path = tmp_path / "history.jsonl"
    history_before = history_path.read_bytes() if history_path.exists() else None
    harness.orchestrator._assurance_service = _DriftingAssuranceService(  # type: ignore[attr-defined]
        harness,
        target,
    )

    failed = await harness.run()

    assert failed.status is SyncRunStatus.FAILED
    assert harness.checkpoint_store.get("markdown") is None
    assert (history_path.read_bytes() if history_path.exists() else None) == history_before
    assert all(
        case.detector_id != "intent_without_requirement"
        for case in harness.case_store.list()
    )

    harness.orchestrator._assurance_service = AssuranceService(  # type: ignore[attr-defined]
        actor="local:asha"
    )
    retry = await harness.run()
    case_bytes = harness.case_path.read_bytes()
    history_bytes = history_path.read_bytes() if history_path.exists() else None
    replay = await harness.run()

    assert retry.status is SyncRunStatus.SUCCESS
    assert retry.cases_created == (0 if target == "cases" else 1)
    assert replay.status is SyncRunStatus.SUCCESS
    assert replay.cases_created == 0
    assert harness.case_path.read_bytes() == case_bytes
    assert (history_path.read_bytes() if history_path.exists() else None) == history_bytes


@pytest.mark.anyio
async def test_sync_skips_absence_checks_until_an_intent_baseline_exists(
    tmp_path: Path,
) -> None:
    harness = SyncHarness(tmp_path, (_AssuranceConnector(),))
    harness.graph_store.initialize(
        Graph(
            id="scheduled",
            version=0,
            nodes=(
                _node(
                    "requirement:pre-bootstrap",
                    NodeType.REQUIREMENT,
                    "ev-scheduled",
                ),
            ),
            edges=(),
        )
    )
    harness.orchestrator._assurance_service = AssuranceService(  # type: ignore[attr-defined]
        actor="local:asha"
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.SUCCESS
    assert result.cases_created == 0
    assert harness.case_store.list() == ()


class _FailingAssuranceReasoner:
    def detect(self, snapshot: AssuranceSnapshot) -> tuple[DetectionInput, ...]:
        del snapshot
        raise ValueError("UNREDACTED-REASONER-DETAIL")


class _MalformedAssuranceReasoner:
    def detect(self, snapshot: AssuranceSnapshot) -> tuple[DetectionInput, ...]:
        del snapshot
        return []  # type: ignore[return-value]


class _GroundedAssuranceReasoner:
    def detect(self, snapshot: AssuranceSnapshot) -> tuple[DetectionInput, ...]:
        records = {record.id: record for record in snapshot.records}
        nodes = {node.id: node for node in snapshot.graph.nodes}

        def side(node_id: str) -> EvidenceSide:
            node = nodes[node_id]
            resolved = tuple(records[ref] for ref in node.evidence_refs)
            return EvidenceSide(
                label=node.type.value,
                claim=node.label,
                evidence_refs=node.evidence_refs,
                observed_at=max(record.observed_at for record in resolved),
                authors=tuple(sorted({record.author for record in resolved})),
                confidence=node.intent_fidelity_confidence or 0.0,
                source_mode=node.source_mode or SourceMode.EXPLICIT,
                current=True,
            )

        return (
            DetectionInput(
                subject_ref="requirement:reasoned",
                affected_refs=("decision:reasoned", "requirement:reasoned"),
                requirement=side("requirement:reasoned"),
                decision=side("decision:reasoned"),
                compatibility="unknown",
            ),
        )


class _RecordingAssuranceReasoner:
    def __init__(self) -> None:
        self.snapshot: AssuranceSnapshot | None = None

    def detect(self, snapshot: AssuranceSnapshot) -> tuple[DetectionInput, ...]:
        self.snapshot = AssuranceSnapshot.model_validate_json(snapshot.model_dump_json())
        return ()


def test_review_fix_reasoner_receives_only_acl_visible_snapshot_content() -> None:
    sentinel = "PRIVATE-HIDDEN-REASONER-SENTINEL"
    visible_intent = _record("ev-visible-intent", author="local:asha")
    hidden = _record(sentinel, author="local:ben", acl=("local:ben",))
    visible_requirement = _record("ev-visible-requirement", author="local:asha")
    records = (visible_intent, hidden, visible_requirement)
    graph = Graph(
        id="graph",
        version=1,
        nodes=(
            _node(
                "intent:visible",
                NodeType.PRODUCT_INTENT,
                visible_intent.id,
            ),
            _node(
                "requirement:visible",
                NodeType.REQUIREMENT,
                visible_requirement.id,
            ),
            _node(
                f"file:{sentinel}",
                NodeType.FILE,
                hidden.id,
                author="local:ben",
            ),
        ),
        edges=(
            _edge(
                "edge:visible",
                "intent:visible",
                RelationType.REFINES,
                "requirement:visible",
            ),
            _edge(
                f"edge:{sentinel}",
                "requirement:visible",
                RelationType.IMPLEMENTED_BY,
                f"file:{sentinel}",
            ),
        ),
    )
    visible_side = EvidenceSide(
        label="PRODUCT_INTENT",
        claim="visible case",
        evidence_refs=(visible_intent.id,),
        observed_at=NOW,
        authors=("local:asha",),
        confidence=0.8,
        source_mode=SourceMode.EXPLICIT,
        current=True,
    )
    hidden_side = visible_side.model_copy(
        update={
            "claim": sentinel,
            "evidence_refs": (hidden.id,),
            "authors": ("local:ben",),
        }
    )
    visible_case = ReconciliationCase(
        id="case:sha256:" + "a" * 64,
        subject_ref="intent:visible",
        case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
        affected_refs=("intent:visible", "requirement:visible"),
        evidence_sides=(visible_side,),
        detector_id="visible_detector",
        fingerprint="a" * 64,
        created_at=NOW,
        created_by="detector:visible_detector",
    )
    hidden_case = ReconciliationCase(
        id="case:sha256:" + "b" * 64,
        subject_ref=f"file:{sentinel}",
        case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
        affected_refs=(f"file:{sentinel}",),
        evidence_sides=(hidden_side,),
        detector_id=sentinel,
        fingerprint="b" * 64,
        created_at=NOW,
        created_by=f"detector:{sentinel}",
    )
    reasoner = _RecordingAssuranceReasoner()

    AssuranceService(actor="local:asha", reasoner=reasoner).detect(
        graph=graph,
        records=records,
        ingestions=_ingestions(records),
        existing_cases=(visible_case, hidden_case),
    )

    assert reasoner.snapshot is not None
    assert {node.id for node in reasoner.snapshot.graph.nodes} == {
        "intent:visible",
        "requirement:visible",
    }
    assert {edge.id for edge in reasoner.snapshot.graph.edges} == {"edge:visible"}
    assert {record.id for record in reasoner.snapshot.records} == {
        visible_intent.id,
        visible_requirement.id,
    }
    assert {
        ingestion.evidence.id for ingestion in reasoner.snapshot.ingestions
    } == {record.id for record in reasoner.snapshot.records}
    assert reasoner.snapshot.existing_cases == (visible_case,)
    assert sentinel not in reasoner.snapshot.model_dump_json()


@pytest.mark.anyio
async def test_sync_accepts_grounded_optional_reasoner_output(tmp_path: Path) -> None:
    harness = SyncHarness(tmp_path, (_AssuranceConnector(),))
    harness.graph_store.initialize(
        Graph(
            id="scheduled",
            version=0,
            nodes=(
                _node("intent:reasoned", NodeType.PRODUCT_INTENT, "ev-scheduled"),
                _node("requirement:reasoned", NodeType.REQUIREMENT, "ev-scheduled"),
                _node("decision:reasoned", NodeType.DECISION, "ev-scheduled"),
            ),
            edges=(
                _edge(
                    "edge:intent-reasoned",
                    "intent:reasoned",
                    RelationType.REFINES,
                    "requirement:reasoned",
                ),
                _edge(
                    "edge:decision-reasoned",
                    "requirement:reasoned",
                    RelationType.DECIDED_BY,
                    "decision:reasoned",
                ),
            ),
        )
    )
    harness.orchestrator._assurance_service = AssuranceService(  # type: ignore[attr-defined]
        actor="local:asha",
        reasoner=_GroundedAssuranceReasoner(),
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.SUCCESS
    cases = harness.case_store.list()
    assert len(cases) == 1
    assert cases[0].case_type is ReconciliationCaseType.AMBIGUOUS_DIVERGENCE


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reasoner",
    (
        _FailingAssuranceReasoner(),
        _MalformedAssuranceReasoner(),
        _InventedProvenanceReasoner(),
    ),
    ids=("failure", "malformed", "ungrounded"),
)
async def test_assurance_failure_retains_raw_evidence_and_does_not_advance_checkpoint(
    tmp_path: Path,
    reasoner: object,
) -> None:
    harness = SyncHarness(tmp_path, (_AssuranceConnector(),))
    harness.graph_store.initialize(_scheduled_graph())
    harness.orchestrator._assurance_service = AssuranceService(  # type: ignore[attr-defined]
        actor="local:asha",
        reasoner=reasoner,  # type: ignore[arg-type]
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.FAILED
    assert result.connectors["markdown"].redacted_error == "connector failed"
    assert harness.evidence_store.get("ev-scheduled").author == "local:asha"
    assert harness.checkpoint_store.get("markdown") is None
    assert harness.case_store.list() == ()
    assert harness.graph_store.load().version == 0


@pytest.mark.anyio
async def test_sync_applies_established_precedence_across_legacy_and_assurance_cases(
    tmp_path: Path,
) -> None:
    def legacy_detector(*_args: object) -> tuple[DriftObservation, ...]:
        return (
            DriftObservation(
                subject_ref="requirement:scheduled",
                case_type=ReconciliationCaseType.CODE_LAG,
                affected_refs=("requirement:scheduled",),
                evidence_sides=(
                    EvidenceSide(
                        label="REQUIREMENT",
                        claim="requirement:scheduled",
                        evidence_refs=("ev-scheduled",),
                        observed_at=NOW,
                        authors=("local:asha",),
                        confidence=0.8,
                        source_mode=SourceMode.EXPLICIT,
                        current=True,
                    ),
                ),
                detector_id="code_lag",
                fingerprint="a" * 64,
            ),
        )

    harness = SyncHarness(
        tmp_path,
        (_AssuranceConnector(),),
        case_detector=legacy_detector,
    )
    harness.graph_store.initialize(
        Graph(
            id="scheduled",
            version=0,
            nodes=(
                _node(
                    "requirement:scheduled",
                    NodeType.REQUIREMENT,
                    "ev-scheduled",
                ),
            ),
            edges=(),
        )
    )
    harness.orchestrator._assurance_service = AssuranceService(  # type: ignore[attr-defined]
        actor="local:asha"
    )

    result = await harness.run()

    assert result.status is SyncRunStatus.SUCCESS
    cases = harness.case_store.list()
    assert len(cases) == 1
    assert cases[0].case_type is ReconciliationCaseType.CODE_LAG
