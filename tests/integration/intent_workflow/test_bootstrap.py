"""Integration proof for evidence-grounded PRD bootstrap and governed activation."""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from pydantic import ValidationError

from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    EvidenceRecord,
    Graph,
    Node,
    ProjectConfig,
    RelationType,
    SourceMode,
    SourceRole,
    SourceRoleAssignment,
)
from intent_engineering.intent_workflow.bootstrap import (
    BootstrapError,
    BootstrapService,
    BootstrapSubmission,
)
from intent_engineering.intent_workflow.models import ProposalDecision
from intent_engineering.intent_workflow.proposal_store import IntentProposalStore
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "intent_workflow"
    / "existing_project"
)


class CancellationSignal(BaseException):
    """Test-only cancellation whose exact identity must survive cleanup."""


@dataclass
class BootstrapHarness:
    root: Path
    service: BootstrapService
    graph_store: YamlGraphStore
    evidence_store: JsonlEvidenceStore
    proposal_store: IntentProposalStore
    changeset_executor: LocalChangeSetExecutor
    transactions: LocalTransactionCoordinator
    paths: dict[str, Path]
    evidence: EvidenceRecord
    now: datetime = NOW

    def graph(self) -> Graph:
        return self.graph_store.load()

    def state_bytes(self) -> dict[str, bytes | None]:
        return {
            name: path.read_bytes() if path.exists() else None
            for name, path in self.paths.items()
        }

    def agent_submission(self, **updates: object) -> BootstrapSubmission:
        evidence_id = self.evidence.id
        nodes = (
            _node("intent-local-export", "PRODUCT_INTENT", "Keep report export local", evidence_id),
            _node(
                "outcome-portable-report",
                "DESIRED_OUTCOME",
                "Analysts can open exported reports in spreadsheet software",
                evidence_id,
            ),
            _node("req-csv-export", "REQUIREMENT", "Provide CSV report export", evidence_id),
            _node(
                "constraint-no-cloud-export",
                "CONSTRAINT",
                "Do not send report data to a hosted service",
                evidence_id,
            ),
            _node(
                "acceptance-utf8",
                "ACCEPTANCE_CRITERION",
                "CSV output uses UTF-8 encoding",
                evidence_id,
                confidence=0.62,
            ),
        )
        edges = (
            _edge("edge-intent-outcome", "intent-local-export", RelationType.SEEKS_OUTCOME, "outcome-portable-report"),
            _edge("edge-intent-requirement", "intent-local-export", RelationType.REALIZED_BY, "req-csv-export"),
            _edge("edge-intent-constraint", "constraint-no-cloud-export", RelationType.CONSTRAINS, "req-csv-export"),
            _edge("edge-requirement-criterion", "req-csv-export", RelationType.HAS_ACCEPTANCE_CRITERION, "acceptance-utf8"),
        )
        values: dict[str, object] = {
            "baseline_graph_version": 0,
            "actor": "agent:codex",
            "timestamp": NOW,
            "evidence_refs": (evidence_id,),
            "source_roles": (_source_role(),),
            "candidate_nodes": nodes,
            "candidate_edges": edges,
            "core_node_ids": tuple(node.id for node in nodes[:4]),
            "provisional_node_ids": (nodes[4].id,),
            "assumptions": ("Spreadsheet software accepts RFC-style CSV",),
            "unanswered_questions": ("How should nested values be represented?",),
        }
        values.update(updates)
        return BootstrapSubmission.model_validate(values)

    def proposed_review(self, **updates: object):
        return self.service.propose(
            self.agent_submission(**updates),
            principals=frozenset({"local:owner"}),
        )

    def provisional_ids(self) -> tuple[str, ...]:
        return tuple(
            node.id
            for proposal in self.proposal_store.list()
            for node in proposal.changeset.nodes_added
            if node.id in proposal.provisional_node_ids
        )


def _source_role(**updates: object) -> SourceRoleAssignment:
    values: dict[str, object] = {
        "connector_id": "markdown",
        "scope": "docs/prd.md",
        "role": SourceRole.DECLARED_INTENT,
        "inherited": False,
    }
    values.update(updates)
    return SourceRoleAssignment.model_validate(values)


def _node(
    node_id: str,
    node_type: str,
    label: str,
    evidence_id: str,
    *,
    confidence: float = 0.82,
    **updates: object,
) -> Node:
    values: dict[str, object] = {
        "id": node_id,
        "type": node_type,
        "label": label,
        "status": "proposed",
        "created_by": "agent:codex",
        "created_at": NOW,
        "last_modified_by": "agent:codex",
        "last_modified_at": NOW,
        "source_mode": SourceMode.INFERRED,
        "intent_fidelity_confidence": confidence,
        "confidence_basis": "Inferred by the active agent from the captured PRD",
        "last_reassessed_at": NOW,
        "evidence_refs": (evidence_id,),
    }
    values.update(updates)
    return Node.model_validate(values)


def _edge(edge_id: str, from_id: str, relation: RelationType, to_id: str) -> Edge:
    return Edge(
        id=edge_id,
        **{"from": from_id, "to": to_id},
        relation=relation,
        status="proposed",
        created_by="agent:codex",
        created_at=NOW,
        last_modified_by="agent:codex",
        last_modified_at=NOW,
    )


def _empty_graph() -> Graph:
    return Graph(
        id="graph:bootstrap-test",
        version=0,
        name="Bootstrap test graph",
        nodes=(),
        edges=(),
    )


def _capture_prd(project: Path, config: ProjectConfig) -> EvidenceRecord:
    async def capture() -> EvidenceRecord:
        connector = MarkdownConnector(project, config)
        sources = await connector.discover(None)
        source = next(item for item in sources if item.locator == "docs/prd.md")
        return connector.normalize(await connector.fetch(source.external_object_id, source.external_version))

    return anyio.run(capture)


def _harness(
    tmp_path: Path,
    *,
    fault_hook: Callable[[str], None] | None = None,
    acl: tuple[str, ...] = (),
    source_roles: tuple[SourceRoleAssignment, ...] | None = None,
    existing_requirement: bool = False,
) -> BootstrapHarness:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    config = ProjectConfig(
        project_id="existing-project",
        local_actor="local:owner",
        source_roles=source_roles or (_source_role(),),
    )
    captured = _capture_prd(project, config)
    evidence = captured.model_copy(update={"acl": acl})
    workspace = project / ".intent"
    (workspace / "history").mkdir(parents=True)
    (workspace / "reconciliation").mkdir()
    (workspace / "evidence").mkdir()
    (workspace / "approvals").mkdir()
    raw_paths = {
        "graph": workspace / "graph.yaml",
        "history": workspace / "history/changesets.jsonl",
        "cases": workspace / "reconciliation/cases.jsonl",
        "evidence": workspace / "evidence/evidence.jsonl",
        "receipts": workspace / "approvals/receipts.jsonl",
        "intent_proposals": workspace / "history/intent-proposals.jsonl",
    }
    for name, path in raw_paths.items():
        if name != "graph":
            path.write_bytes(b"")
    directory = SecureDirectory.open(workspace)
    files = {
        "graph": directory.file("graph.yaml"),
        "history": directory.file("history/changesets.jsonl"),
        "cases": directory.file("reconciliation/cases.jsonl"),
        "evidence": directory.file("evidence/evidence.jsonl"),
        "receipts": directory.file("approvals/receipts.jsonl"),
        "intent_proposals": directory.file("history/intent-proposals.jsonl"),
    }
    transactions = LocalTransactionCoordinator(
        directory.file("history/.local-transaction.json"),
        files,
        fault_hook=fault_hook,
    )
    graph_store = YamlGraphStore(
        files["graph"],
        history_path=files["history"],
        transactions=transactions,
    )
    graph = _empty_graph()
    if existing_requirement:
        existing = _node(
            "req-existing",
            "REQUIREMENT",
            "Existing active requirement",
            evidence.id,
            source_mode=SourceMode.EXPLICIT,
            status="active",
            created_by="local:owner",
            last_modified_by="local:owner",
        )
        graph = graph.model_copy(update={"nodes": (existing,)})
    graph_store.initialize(graph)
    evidence_store = JsonlEvidenceStore(files["evidence"], transactions=transactions)
    evidence_store.associate("markdown", evidence)
    proposal_store = IntentProposalStore(files["intent_proposals"], transactions=transactions)
    case_store = JsonlCaseStore(files["cases"])
    executor = LocalChangeSetExecutor(graph_store, case_store, transactions)
    service = BootstrapService(
        graph_store=graph_store,
        evidence_store=evidence_store,
        proposal_store=proposal_store,
        changeset_executor=executor,
        transactions=transactions,
        config=config,
    )
    return BootstrapHarness(
        root=project,
        service=service,
        graph_store=graph_store,
        evidence_store=evidence_store,
        proposal_store=proposal_store,
        changeset_executor=executor,
        transactions=transactions,
        paths={
            **raw_paths,
            "journal": workspace / "history/.local-transaction.json",
        },
        evidence=evidence,
    )


@pytest.fixture
def bootstrap_harness(tmp_path: Path) -> BootstrapHarness:
    return _harness(tmp_path)


def test_ordinary_prd_becomes_reviewable_core_and_provisional_detail(
    bootstrap_harness: BootstrapHarness,
) -> None:
    submission = bootstrap_harness.agent_submission()
    review = bootstrap_harness.service.propose(
        submission,
        principals=frozenset({"local:owner"}),
    )

    assert review.status == "proposed"
    assert {item.type for item in review.core_nodes} >= {
        "PRODUCT_INTENT",
        "DESIRED_OUTCOME",
        "REQUIREMENT",
        "CONSTRAINT",
    }
    assert review.provisional_nodes
    assert all(node.evidence_refs == (bootstrap_harness.evidence.id,) for node in review.all_nodes)
    assert all(node.source_mode is SourceMode.INFERRED for node in review.all_nodes)
    assert bootstrap_harness.graph().version == 0
    assert bootstrap_harness.graph().nodes == ()
    assert bootstrap_harness.service.review(
        review.proposal_id,
        principals=frozenset({"local:owner"}),
    ) == review


def test_review_exposes_exact_candidate_edges_and_changeset(
    bootstrap_harness: BootstrapHarness,
) -> None:
    submission = bootstrap_harness.agent_submission()
    review = bootstrap_harness.service.propose(
        submission,
        principals=frozenset({"local:owner"}),
    )
    proposal = bootstrap_harness.proposal_store.get(review.proposal_id)

    assert review.candidate_edges == submission.candidate_edges
    assert review.candidate_changeset == proposal.changeset
    assert review.candidate_changeset.id == proposal.changeset.id


def test_activation_applies_only_confirmed_core_as_one_changeset(
    bootstrap_harness: BootstrapHarness,
) -> None:
    review = bootstrap_harness.proposed_review()
    result = bootstrap_harness.service.activate(
        review.proposal_id,
        confirmed_node_ids=tuple(node.id for node in review.core_nodes),
        actor="local:owner",
        at=bootstrap_harness.now,
    )

    assert result.version == 1
    assert {node.id for node in result.nodes} == {node.id for node in review.core_nodes}
    assert bootstrap_harness.provisional_ids() == tuple(
        node.id for node in review.provisional_nodes
    )
    assert set(bootstrap_harness.provisional_ids()).isdisjoint(
        node.id for node in result.nodes
    )
    history = bootstrap_harness.graph_store.history(review.core_nodes[0].id)
    assert len(history) == 1
    assert history[0].actor == "local:owner"
    assert bootstrap_harness.proposal_store.decision_for(review.proposal_id) is not None


def test_identical_proposal_and_activation_replay_are_exact_noops(
    bootstrap_harness: BootstrapHarness,
) -> None:
    submission = bootstrap_harness.agent_submission()
    first = bootstrap_harness.service.propose(submission, frozenset({"local:owner"}))
    proposed_bytes = bootstrap_harness.state_bytes()
    second = bootstrap_harness.service.propose(submission, frozenset({"local:owner"}))

    assert second.model_dump_json() == first.model_dump_json()
    assert bootstrap_harness.state_bytes() == proposed_bytes

    selected = tuple(node.id for node in first.core_nodes)
    activated = bootstrap_harness.service.activate(
        first.proposal_id,
        confirmed_node_ids=selected,
        actor="local:owner",
        at=NOW,
    )
    activated_bytes = bootstrap_harness.state_bytes()
    replayed = bootstrap_harness.service.activate(
        first.proposal_id,
        confirmed_node_ids=selected,
        actor="local:owner",
        at=NOW,
    )

    assert replayed == activated
    assert bootstrap_harness.state_bytes() == activated_bytes


def _run_concurrently(call: Callable[[], Any]) -> tuple[Any, Any]:
    return _run_two_concurrently(call, call)


def _run_two_concurrently(
    first: Callable[[], Any],
    second: Callable[[], Any],
) -> tuple[Any, Any]:
    start = threading.Barrier(2)

    def invoke(call: Callable[[], Any]) -> Any:
        start.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(invoke, first), executor.submit(invoke, second))
        return cast(Any, futures[0].result(timeout=5)), cast(
            Any, futures[1].result(timeout=5)
        )


def _capture_bootstrap_failure(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except BootstrapError as error:
        return error


def test_concurrent_identical_proposal_is_one_canonical_noop(
    bootstrap_harness: BootstrapHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submission = bootstrap_harness.agent_submission()
    original_bytes = bootstrap_harness.proposal_store.bytes
    both_read_preimage = threading.Barrier(2)

    def synchronized_bytes() -> bytes:
        content = original_bytes()
        both_read_preimage.wait(timeout=5)
        return content

    monkeypatch.setattr(bootstrap_harness.proposal_store, "bytes", synchronized_bytes)
    first, second = _run_concurrently(
        lambda: bootstrap_harness.service.propose(
            submission,
            frozenset({"local:owner"}),
        )
    )

    assert first == second
    assert bootstrap_harness.proposal_store.list() == (
        bootstrap_harness.proposal_store.get(first.proposal_id),
    )
    assert bootstrap_harness.graph().version == 0
    assert bootstrap_harness.graph_store.history(first.core_nodes[0].id) == ()


def test_concurrent_identical_activation_is_one_canonical_noop(
    bootstrap_harness: BootstrapHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = bootstrap_harness.proposed_review()
    selected = tuple(node.id for node in review.core_nodes)
    original_bytes = bootstrap_harness.proposal_store.bytes
    both_read_preimage = threading.Barrier(2)

    def synchronized_bytes() -> bytes:
        content = original_bytes()
        both_read_preimage.wait(timeout=5)
        return content

    monkeypatch.setattr(bootstrap_harness.proposal_store, "bytes", synchronized_bytes)
    first, second = _run_concurrently(
        lambda: bootstrap_harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=selected,
            actor="local:owner",
            at=NOW,
        )
    )

    assert first == second
    assert first.version == 1
    assert len(bootstrap_harness.graph_store.history(selected[0])) == 1
    assert bootstrap_harness.proposal_store.decision_for(review.proposal_id) is not None


def test_concurrent_foreign_proposal_change_fails_closed(
    bootstrap_harness: BootstrapHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_submission = bootstrap_harness.agent_submission()
    second_submission = bootstrap_harness.agent_submission(
        assumptions=("A different asserted assumption",)
    )
    original_bytes = bootstrap_harness.proposal_store.bytes
    both_read_preimage = threading.Barrier(2)

    def synchronized_bytes() -> bytes:
        content = original_bytes()
        both_read_preimage.wait(timeout=5)
        return content

    monkeypatch.setattr(bootstrap_harness.proposal_store, "bytes", synchronized_bytes)
    results = _run_two_concurrently(
        lambda: _capture_bootstrap_failure(
            lambda: bootstrap_harness.service.propose(
                first_submission, frozenset({"local:owner"})
            )
        ),
        lambda: _capture_bootstrap_failure(
            lambda: bootstrap_harness.service.propose(
                second_submission, frozenset({"local:owner"})
            )
        ),
    )

    assert sum(isinstance(result, BootstrapError) for result in results) == 1
    assert len(bootstrap_harness.proposal_store.list()) == 1
    assert bootstrap_harness.graph().version == 0


def test_concurrent_conflicting_activation_selection_fails_closed(
    bootstrap_harness: BootstrapHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = bootstrap_harness.proposed_review()
    first_selection = tuple(node.id for node in review.core_nodes[:2])
    second_selection = tuple(node.id for node in review.core_nodes[:3])
    original_bytes = bootstrap_harness.proposal_store.bytes
    both_read_preimage = threading.Barrier(2)

    def synchronized_bytes() -> bytes:
        content = original_bytes()
        both_read_preimage.wait(timeout=5)
        return content

    monkeypatch.setattr(bootstrap_harness.proposal_store, "bytes", synchronized_bytes)
    results = _run_two_concurrently(
        lambda: _capture_bootstrap_failure(
            lambda: bootstrap_harness.service.activate(
                review.proposal_id,
                confirmed_node_ids=first_selection,
                actor="local:owner",
                at=NOW,
            )
        ),
        lambda: _capture_bootstrap_failure(
            lambda: bootstrap_harness.service.activate(
                review.proposal_id,
                confirmed_node_ids=second_selection,
                actor="local:owner",
                at=NOW,
            )
        ),
    )

    assert sum(isinstance(result, BootstrapError) for result in results) == 1
    assert bootstrap_harness.graph().version == 1
    assert len(bootstrap_harness.graph_store.history(first_selection[0])) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "stale-baseline",
        "missing-evidence",
        "foreign-evidence",
        "role-mismatch",
        "duplicate-node",
        "classification-overlap",
        "classification-omission",
        "unregistered-type",
        "orphan-edge",
        "non-inferred",
        "active-candidate",
        "empty-identity",
        "invalid-provenance",
    ],
)
def test_invalid_submissions_are_fixed_failures_and_store_nothing(
    bootstrap_harness: BootstrapHarness,
    mutation: str,
) -> None:
    submission = bootstrap_harness.agent_submission()
    values = submission.model_dump(mode="python")
    nodes = list(submission.candidate_nodes)
    edges = list(submission.candidate_edges)
    if mutation == "stale-baseline":
        values["baseline_graph_version"] = 1
    elif mutation == "missing-evidence":
        values["evidence_refs"] = ()
    elif mutation == "foreign-evidence":
        values["evidence_refs"] = ("evidence:foreign",)
        nodes = [node.model_copy(update={"evidence_refs": ("evidence:foreign",)}) for node in nodes]
    elif mutation == "role-mismatch":
        values["source_roles"] = (_source_role(role=SourceRole.OPERATING_CONTEXT),)
    elif mutation == "duplicate-node":
        nodes.append(nodes[0])
    elif mutation == "classification-overlap":
        values["provisional_node_ids"] = (*submission.provisional_node_ids, submission.core_node_ids[0])
    elif mutation == "classification-omission":
        values["provisional_node_ids"] = ()
    elif mutation == "unregistered-type":
        nodes[0] = nodes[0].model_copy(update={"type": "vendor:UNREGISTERED"})
    elif mutation == "orphan-edge":
        edges.append(_edge("edge-orphan", "missing-node", RelationType.REFINES, nodes[0].id))
    elif mutation == "non-inferred":
        nodes[0] = nodes[0].model_copy(update={"source_mode": SourceMode.EXPLICIT})
    elif mutation == "active-candidate":
        nodes[0] = nodes[0].model_copy(update={"status": "active"})
    elif mutation == "empty-identity":
        original_id = nodes[0].id
        nodes[0] = nodes[0].model_copy(update={"id": ""})
        values["core_node_ids"] = tuple(
            "" if node_id == original_id else node_id for node_id in submission.core_node_ids
        )
        edges = [
            edge.model_copy(
                update={
                    "from_id": "" if edge.from_id == original_id else edge.from_id,
                    "to_id": "" if edge.to_id == original_id else edge.to_id,
                }
            )
            for edge in edges
        ]
    elif mutation == "invalid-provenance":
        nodes[0] = nodes[0].model_copy(update={"evidence_refs": ()})
    values["candidate_nodes"] = tuple(nodes)
    values["candidate_edges"] = tuple(edges)
    hostile = BootstrapSubmission.model_construct(**values)
    before = bootstrap_harness.state_bytes()

    with pytest.raises(BootstrapError) as caught:
        bootstrap_harness.service.propose(hostile, frozenset({"local:owner"}))

    assert caught.value.args == ("intent bootstrap unavailable",)
    assert caught.value.__context__ is None
    assert bootstrap_harness.state_bytes() == before


def test_acl_hidden_evidence_is_rejected_without_persistence(tmp_path: Path) -> None:
    harness = _harness(tmp_path, acl=("team:product",))
    before = harness.state_bytes()

    with pytest.raises(BootstrapError):
        harness.service.propose(harness.agent_submission(), frozenset({"local:owner"}))

    assert harness.state_bytes() == before


def test_inherited_source_role_is_supported_but_exact_override_wins(tmp_path: Path) -> None:
    inherited = _source_role(scope="docs", inherited=True)
    exact = _source_role(role=SourceRole.OPERATING_CONTEXT)
    inherited_harness = _harness(tmp_path / "inherited", source_roles=(inherited,))

    review = inherited_harness.service.propose(
        inherited_harness.agent_submission(source_roles=(inherited,)),
        frozenset({"local:owner"}),
    )
    assert review.core_nodes

    overridden = _harness(tmp_path / "overridden", source_roles=(inherited, exact))
    before = overridden.state_bytes()
    with pytest.raises(BootstrapError):
        overridden.service.propose(
            overridden.agent_submission(source_roles=(inherited,)),
            frozenset({"local:owner"}),
        )
    assert overridden.state_bytes() == before
    accepted = overridden.service.propose(
        overridden.agent_submission(source_roles=(exact,)),
        frozenset({"local:owner"}),
    )
    assert accepted.core_nodes


def test_submission_collections_and_utc_timestamp_are_bounded(
    bootstrap_harness: BootstrapHarness,
) -> None:
    values = bootstrap_harness.agent_submission().model_dump(mode="python")
    values["assumptions"] = tuple("x" for _ in range(257))
    with pytest.raises(ValidationError):
        BootstrapSubmission.model_validate(values)

    submission = bootstrap_harness.agent_submission()
    invalid_node = Node.model_construct(
        **{
            **submission.candidate_nodes[0].model_dump(mode="python"),
            "intent_fidelity_confidence": 1.5,
        }
    )
    hostile = BootstrapSubmission.model_construct(
        **{
            **submission.model_dump(mode="python"),
            "candidate_nodes": (invalid_node, *submission.candidate_nodes[1:]),
        }
    )
    before = bootstrap_harness.state_bytes()
    with pytest.raises(BootstrapError):
        bootstrap_harness.service.propose(hostile, frozenset({"local:owner"}))
    assert bootstrap_harness.state_bytes() == before
    values = bootstrap_harness.agent_submission().model_dump(mode="python")
    values["evidence_refs"] = tuple(f"evidence:{index}" for index in range(10_001))
    with pytest.raises(ValidationError):
        BootstrapSubmission.model_validate(values)
    values = bootstrap_harness.agent_submission().model_dump(mode="python")
    values["timestamp"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError):
        BootstrapSubmission.model_validate(values)


@pytest.mark.parametrize("reference_count", [2, 10_001])
def test_nested_node_evidence_refs_are_bounded_and_duplicate_free(
    bootstrap_harness: BootstrapHarness,
    reference_count: int,
) -> None:
    submission = bootstrap_harness.agent_submission()
    node = submission.candidate_nodes[0].model_copy(
        update={
            "evidence_refs": (bootstrap_harness.evidence.id,) * reference_count
        }
    )
    hostile = BootstrapSubmission.model_construct(
        **{
            **submission.model_dump(mode="python"),
            "candidate_nodes": (node, *submission.candidate_nodes[1:]),
        }
    )
    before = bootstrap_harness.state_bytes()

    with pytest.raises(BootstrapError):
        bootstrap_harness.service.propose(hostile, frozenset({"local:owner"}))

    assert bootstrap_harness.state_bytes() == before


@pytest.mark.parametrize(
    ("selection", "actor"),
    [
        ((), "local:owner"),
        (("acceptance-utf8",), "local:owner"),
        (("foreign-core",), "local:owner"),
        (("req-csv-export",), "local:stranger"),
    ],
)
def test_activation_rejects_invalid_selection_or_unauthorized_contributor(
    bootstrap_harness: BootstrapHarness,
    selection: tuple[str, ...],
    actor: str,
) -> None:
    review = bootstrap_harness.proposed_review()
    before = bootstrap_harness.state_bytes()

    with pytest.raises(BootstrapError):
        bootstrap_harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=selection,
            actor=actor,
            at=NOW,
        )

    assert bootstrap_harness.state_bytes() == before


@pytest.mark.parametrize(
    "proposal_flag",
    ["conflict", "destructive"],
)
def test_conflicting_or_destructive_bootstrap_cannot_be_self_activated(
    bootstrap_harness: BootstrapHarness,
    proposal_flag: str,
) -> None:
    updates = (
        {"conflicting_authors": ("local:owner", "local:reviewer")}
        if proposal_flag == "conflict"
        else {"destructive": True}
    )
    review = bootstrap_harness.proposed_review(**updates)
    before = bootstrap_harness.state_bytes()

    with pytest.raises(BootstrapError):
        bootstrap_harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor="local:owner",
            at=NOW,
        )

    assert bootstrap_harness.state_bytes() == before


@pytest.mark.parametrize("relation", [RelationType.CONTRADICTS, RelationType.SUPERSEDES])
def test_structural_conflict_marker_requires_independent_review(
    tmp_path: Path,
    relation: RelationType,
) -> None:
    harness = _harness(tmp_path, existing_requirement=True)
    submission = harness.agent_submission()
    conflict = _edge("edge-structural-conflict", submission.core_node_ids[0], relation, "req-existing")
    review = harness.service.propose(
        submission.model_copy(
            update={"candidate_edges": (*submission.candidate_edges, conflict)}
        ),
        frozenset({"local:owner"}),
    )
    before = harness.state_bytes()

    with pytest.raises(BootstrapError):
        harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor="local:owner",
            at=NOW,
        )

    assert harness.state_bytes() == before


def test_existing_to_existing_candidate_edge_cannot_activate_as_hidden_side_effect(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, existing_requirement=True)
    submission = harness.agent_submission()
    hidden = _edge(
        "edge-hidden-existing",
        "req-existing",
        RelationType.REFINES,
        "req-existing",
    )
    review = harness.service.propose(
        submission.model_copy(
            update={"candidate_edges": (*submission.candidate_edges, hidden)}
        ),
        frozenset({"local:owner"}),
    )

    result = harness.service.activate(
        review.proposal_id,
        confirmed_node_ids=tuple(node.id for node in review.core_nodes),
        actor="local:owner",
        at=NOW,
    )

    assert hidden.id not in {edge.id for edge in result.edges}


def test_different_confirmed_subsets_bind_distinct_changesets_and_decisions(
    tmp_path: Path,
) -> None:
    first = _harness(tmp_path / "first")
    second = _harness(tmp_path / "second")
    first_review = first.proposed_review()
    second_review = second.proposed_review()
    first_ids = tuple(node.id for node in first_review.core_nodes[:2])
    second_ids = tuple(node.id for node in second_review.core_nodes[:3])

    first.service.activate(
        first_review.proposal_id,
        confirmed_node_ids=first_ids,
        actor="local:owner",
        at=NOW,
    )
    second.service.activate(
        second_review.proposal_id,
        confirmed_node_ids=second_ids,
        actor="local:owner",
        at=NOW,
    )
    first_change = first.graph_store.history(first_ids[0])[0]
    second_change = second.graph_store.history(second_ids[0])[0]
    first_decision = first.proposal_store.decision_for(first_review.proposal_id)
    second_decision = second.proposal_store.decision_for(second_review.proposal_id)

    assert first_change.id != second_change.id
    assert first_decision is not None and second_decision is not None
    assert first_decision.id != second_decision.id
    assert first_decision.confirmed_node_ids == first_ids
    assert second_decision.confirmed_node_ids == second_ids
    assert first_decision.activation_changeset_id == first_change.id
    assert second_decision.activation_changeset_id == second_change.id

    replayed = first.service.activate(
        first_review.proposal_id,
        confirmed_node_ids=first_ids,
        actor="local:owner",
        at=NOW,
    )
    assert replayed.version == 1
    with pytest.raises(BootstrapError):
        first.service.activate(
            first_review.proposal_id,
            confirmed_node_ids=second_ids,
            actor="local:owner",
            at=NOW,
        )


def test_activation_rejects_stale_graph_and_a_distinct_second_decision(
    bootstrap_harness: BootstrapHarness,
) -> None:
    review = bootstrap_harness.proposed_review()
    drift = ChangeSet(
        id="changeset:graph-drift",
        actor="local:owner",
        timestamp=NOW,
        baseline_graph_version=0,
        evidence_refs=(bootstrap_harness.evidence.id,),
        nodes_added=(
            _node("context-drift", "CONTEXT", "Graph changed after review", bootstrap_harness.evidence.id),
        ),
        nodes_updated=(),
        nodes_superseded=(),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="validated",
    )
    bootstrap_harness.changeset_executor.apply(drift)
    before = bootstrap_harness.state_bytes()

    with pytest.raises(BootstrapError):
        bootstrap_harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor="local:owner",
            at=NOW,
        )

    assert bootstrap_harness.state_bytes() == before

    fresh = _harness(bootstrap_harness.root.parent / "fresh")
    fresh_review = fresh.proposed_review()
    chosen = tuple(node.id for node in fresh_review.core_nodes)
    fresh.service.activate(fresh_review.proposal_id, confirmed_node_ids=chosen, actor="local:owner", at=NOW)
    accepted = fresh.state_bytes()
    with pytest.raises(BootstrapError):
        fresh.service.activate(
            fresh_review.proposal_id,
            confirmed_node_ids=chosen,
            actor="local:owner",
            at=NOW + timedelta(seconds=1),
        )
    assert fresh.state_bytes() == accepted


def _rejection(proposal_id: str, proposal_digest: str) -> ProposalDecision:
    material: dict[str, object] = {
        "schema_version": 1,
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "actor": "local:owner",
        "actor_aliases": ["local:owner"],
        "decided_at": "2026-08-26T12:00:00Z",
        "action": "reject",
        "baseline_graph_version": 0,
    }
    encoded = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return ProposalDecision.model_validate_json(
        json.dumps(
            {
                "id": f"proposal-decision:sha256:{sha256(encoded).hexdigest()}",
                **material,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def test_rejected_proposal_cannot_activate(bootstrap_harness: BootstrapHarness) -> None:
    review = bootstrap_harness.proposed_review()
    bootstrap_harness.proposal_store.decide(
        _rejection(review.proposal_id, review.proposal_digest)
    )
    before = bootstrap_harness.state_bytes()

    with pytest.raises(BootstrapError):
        bootstrap_harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor="local:owner",
            at=NOW,
        )

    assert bootstrap_harness.state_bytes() == before


def test_activation_rechecks_contributor_access_to_proposal_evidence(tmp_path: Path) -> None:
    harness = _harness(tmp_path, acl=("team:product",))
    review = harness.service.propose(
        harness.agent_submission(),
        frozenset({"team:product"}),
    )
    before = harness.state_bytes()

    with pytest.raises(BootstrapError):
        harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor="local:owner",
            at=NOW,
        )

    assert harness.state_bytes() == before


@pytest.mark.parametrize("stage", ["journal_prepared", "target:intent_proposals"])
def test_proposal_transaction_failure_stores_nothing(tmp_path: Path, stage: str) -> None:
    armed = False

    def fault(current: str) -> None:
        if armed and current == stage:
            raise RuntimeError("PRIVATE-PROPOSAL-FAILURE")

    harness = _harness(tmp_path, fault_hook=fault)
    before = harness.state_bytes()
    armed = True

    with pytest.raises(BootstrapError):
        harness.service.propose(harness.agent_submission(), frozenset({"local:owner"}))

    assert harness.state_bytes() == before


@pytest.mark.parametrize(
    "stage",
    ["journal_prepared", "target:intent_proposals", "journal_committed"],
)
def test_proposal_cancellation_preserves_signal_and_exact_state(
    tmp_path: Path,
    stage: str,
) -> None:
    signal = CancellationSignal()
    armed = False

    def fault(current: str) -> None:
        if armed and current == stage:
            raise signal

    harness = _harness(tmp_path, fault_hook=fault)
    before = harness.state_bytes()
    armed = True

    with pytest.raises(CancellationSignal) as caught:
        harness.service.propose(harness.agent_submission(), frozenset({"local:owner"}))

    assert caught.value is signal
    assert harness.state_bytes() == before


@pytest.mark.parametrize("stage", ["journal_prepared", "target:intent_proposals", "target:graph", "target:history"])
def test_transaction_failure_rolls_back_decision_graph_and_history(
    tmp_path: Path,
    stage: str,
) -> None:
    armed = False

    def fault(current: str) -> None:
        if armed and current == stage:
            raise RuntimeError("PRIVATE-PRD-FAILURE")

    harness = _harness(tmp_path, fault_hook=fault)
    review = harness.proposed_review()
    before = harness.state_bytes()
    armed = True

    with pytest.raises(BootstrapError) as caught:
        harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor="local:owner",
            at=NOW,
        )

    assert caught.value.args == ("intent bootstrap unavailable",)
    assert caught.value.__context__ is None
    assert harness.state_bytes() == before


def _repository_traceback_locals(error: BaseException) -> str:
    frames: list[str] = []
    current = error.__traceback__
    while current is not None:
        filename = current.tb_frame.f_code.co_filename
        if "/src/intent_engineering/" in filename:
            frames.append(repr(current.tb_frame.f_locals))
        current = current.tb_next
    return "\n".join(frames)


@pytest.mark.parametrize(
    "stage",
    [
        "journal_prepared",
        "target:intent_proposals",
        "target:graph",
        "target:history",
        "journal_committed",
    ],
)
def test_cancellation_during_commit_preserves_identity_rolls_back_and_drops_sensitive_locals(
    tmp_path: Path,
    stage: str,
) -> None:
    signal = CancellationSignal()
    armed = False

    def fault(current: str) -> None:
        if armed and current == stage:
            raise signal

    harness = _harness(tmp_path, fault_hook=fault)
    review = harness.proposed_review()
    selected = tuple(node.id for node in review.core_nodes)
    before = harness.state_bytes()
    armed = True

    with pytest.raises(CancellationSignal) as caught:
        harness.service.activate(
            review.proposal_id,
            confirmed_node_ids=selected,
            actor="local:owner",
            at=NOW,
        )

    assert caught.value is signal
    assert harness.state_bytes() == before
    rendered = _repository_traceback_locals(caught.value)
    for private in (
        "Keep report export local",
        "agent:codex",
        "local:owner",
        review.proposal_id,
    ):
        assert private not in rendered


def test_public_validation_failure_is_context_free_and_drops_candidate_traceback_locals(
    bootstrap_harness: BootstrapHarness,
) -> None:
    submission = bootstrap_harness.agent_submission()
    private_label = "PRIVATE-CANDIDATE-LABEL"
    nodes = list(submission.candidate_nodes)
    nodes[0] = nodes[0].model_copy(update={"label": private_label, "source_mode": SourceMode.EXPLICIT})
    hostile = BootstrapSubmission.model_construct(
        **{
            **submission.model_dump(mode="python"),
            "candidate_nodes": tuple(nodes),
        }
    )

    with pytest.raises(BootstrapError) as caught:
        bootstrap_harness.service.propose(hostile, frozenset({"local:owner"}))

    assert caught.value.args == ("intent bootstrap unavailable",)
    assert caught.value.__context__ is None
    assert private_label not in _repository_traceback_locals(caught.value)
    assert "agent:codex" not in _repository_traceback_locals(caught.value)


def test_review_cancellation_preserves_identity_and_drops_proposal_traceback_locals(
    bootstrap_harness: BootstrapHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = bootstrap_harness.proposed_review()
    signal = CancellationSignal()
    original_snapshot = bootstrap_harness.transactions.snapshot

    def cancel_after_snapshot(*args: object, **kwargs: object):
        original_snapshot(*args, **kwargs)
        raise signal

    monkeypatch.setattr(bootstrap_harness.transactions, "snapshot", cancel_after_snapshot)

    with pytest.raises(CancellationSignal) as caught:
        bootstrap_harness.service.review(
            review.proposal_id,
            principals=frozenset({"local:owner"}),
        )

    assert caught.value is signal
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = _repository_traceback_locals(caught.value)
    for private in (
        "Keep report export local",
        "agent:codex",
        "local:owner",
        review.proposal_id,
    ):
        assert private not in rendered


def test_review_is_detached_from_hostile_model_protocols(bootstrap_harness: BootstrapHarness) -> None:
    review = bootstrap_harness.proposed_review()
    dumped = json.loads(review.model_dump_json())
    dumped["core_nodes"][0]["label"] = "tampered"

    reread = bootstrap_harness.service.review(
        review.proposal_id,
        principals=frozenset({"local:owner"}),
    )

    assert reread.core_nodes[0].label != "tampered"
    assert cast(Any, reread).model_config["frozen"] is True
