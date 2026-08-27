"""Integration proof for deterministic four-outcome task preflight and exact context."""

from __future__ import annotations

import hashlib
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.context.provider import ContextProvider
from intent_engineering.core.models import (
    ChangeSet,
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
    SourceRole,
    SourceRoleAssignment,
)
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    IntentProposal,
    ProposalKind,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.preflight import (
    AgentClassificationSubmission,
    PreflightError,
    PreflightService,
    classification_evidence_content,
)
from intent_engineering.intent_workflow.proposal_store import IntentProposalStore
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore, serialize_case
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore, serialize_graph

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _semantic_evidence(
    evidence_id: str,
    *,
    author: str = "product:priya",
    acl: tuple[str, ...] = ("local:asha",),
) -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence_id,
        connector_type="markdown",
        external_object_id="docs/prd.md",
        external_version=evidence_id,
        author=author,
        observed_at=NOW - timedelta(days=1),
        source_locator="docs/prd.md",
        content_hash=hashlib.sha256(evidence_id.encode()).hexdigest(),
        payload={"repository_scope": "demo"},
        acl=acl,
    )


def _node(
    node_id: str,
    node_type: NodeType,
    label: str,
    evidence_id: str,
    *,
    status: str = "active",
) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label=label,
        status=status,
        created_by="product:priya",
        created_at=NOW - timedelta(days=1),
        last_modified_by="product:priya",
        last_modified_at=NOW - timedelta(days=1),
        source_mode=SourceMode.EXPLICIT,
        intent_fidelity_confidence=0.95,
        confidence_basis="Approved PRD",
        last_reassessed_at=NOW - timedelta(days=1),
        evidence_refs=(evidence_id,),
    )


def _edge(edge_id: str, from_id: str, to_id: str) -> Edge:
    return Edge(
        id=edge_id,
        from_id=from_id,
        relation="REFINES",
        to_id=to_id,
        status="active",
        created_by="product:priya",
        created_at=NOW - timedelta(days=1),
        last_modified_by="product:priya",
        last_modified_at=NOW - timedelta(days=1),
    )


@dataclass
class PreflightHarness:
    service: PreflightService
    capture: ConversationCapture
    evidence_store: JsonlEvidenceStore
    case_store: JsonlCaseStore
    graph_store: YamlGraphStore
    proposal_store: IntentProposalStore
    transactions: LocalTransactionCoordinator
    paths: dict[str, Path]
    config: ProjectConfig

    def state_bytes(self) -> dict[str, bytes | None]:
        return {
            name: path.read_bytes() if path.exists() else None
            for name, path in self.paths.items()
        }

    def inputs(
        self,
        classification: TaskClassification,
        *,
        request: str = "Add CSV export",
        relevant_node_ids: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        semantic_effects: tuple[str, ...] = (),
        uncertainties: tuple[str, ...] = (),
        questions: tuple[str, ...] = (),
        conflict_claims: tuple[str, ...] = (),
        envelope_scope: tuple[str, ...] = ("src/export.py",),
        submission_scope: tuple[str, ...] | None = None,
        graph_version: int = 7,
    ) -> tuple[TaskEnvelope, AgentClassificationSubmission]:
        human = self.capture.record_turn(
            conversation_ref="codex:thread-1",
            role="human",
            author="local:asha",
            content=request,
            captured_at=NOW,
            acl=("local:asha",),
        )
        envelope = TaskEnvelope(
            repository_id="demo",
            actor="local:asha",
            conversation_ref="codex:thread-1",
            request=request,
            request_evidence_ref=human.id,
            graph_version=graph_version,
            created_at=NOW,
            requested_scope=envelope_scope,
        )
        material = {
            "task_id": envelope.id,
            "task_digest": envelope.digest,
            "graph_version": graph_version,
            "classification": classification,
            "basis": "Bounded classifier explanation",
            "relevant_node_ids": relevant_node_ids,
            "evidence_refs": evidence_refs,
            "semantic_effects": semantic_effects,
            "uncertainties": uncertainties,
            "questions": questions,
            "conflict_claims": conflict_claims,
            "requested_scope": envelope_scope if submission_scope is None else submission_scope,
        }
        agent = self.capture.record_turn(
            conversation_ref="codex:thread-1",
            role="agent",
            author="agent:codex",
            content=classification_evidence_content(**material),
            captured_at=NOW + timedelta(microseconds=1),
            acl=("local:asha",),
        )
        submission = AgentClassificationSubmission(
            **material,
            agent_evidence_ref=agent.id,
        )
        return envelope, submission

    def evaluate(self, classification: TaskClassification, **updates: object):
        envelope, submission = self.inputs(classification, **updates)
        return self.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


@pytest.fixture
def preflight_harness(tmp_path: Path) -> PreflightHarness:
    workspace = tmp_path / ".intent"
    for directory in ("history", "reconciliation", "evidence", "approvals"):
        (workspace / directory).mkdir(parents=True, exist_ok=True)
    paths = {
        "config": workspace / "config.yaml",
        "graph": workspace / "graph.yaml",
        "history": workspace / "history/changesets.jsonl",
        "cases": workspace / "reconciliation/cases.jsonl",
        "evidence": workspace / "evidence/evidence.jsonl",
        "receipts": workspace / "approvals/receipts.jsonl",
        "intent_proposals": workspace / "history/intent-proposals.jsonl",
    }
    for name, path in paths.items():
        if name not in {"graph", "config"}:
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
        directory.file("history/.local-transaction.json"), files
    )
    evidence_store = JsonlEvidenceStore(files["evidence"], transactions=transactions)
    evidence_store.associate("markdown", _semantic_evidence("ev-intent"))
    evidence_store.associate("markdown", _semantic_evidence("ev-requirement"))
    evidence_store.associate("markdown", _semantic_evidence("ev-test"))
    evidence_store.associate(
        "markdown",
        _semantic_evidence("ev-hidden", acl=("local:other",)),
    )
    graph = Graph(
        id="graph:demo",
        version=7,
        nodes=(
            _node("intent-export", NodeType.PRODUCT_INTENT, "Portable local reports", "ev-intent"),
            _node("req-export", NodeType.REQUIREMENT, "Provide CSV export", "ev-requirement"),
            _node("test-export", NodeType.TEST, "Verify CSV export", "ev-test"),
            _node("symbol-export", NodeType.SYMBOL, "CSV export implementation", "ev-test"),
            _node("req-hidden", NodeType.REQUIREMENT, "Hidden requirement", "ev-hidden"),
            _node("req-inactive", NodeType.REQUIREMENT, "Inactive requirement", "ev-requirement", status="superseded"),
            _node("req-unrelated", NodeType.REQUIREMENT, "Remote archive", "ev-requirement"),
        ),
        edges=(
            _edge("edge-intent-req", "intent-export", "req-export"),
            _edge("edge-req-test", "req-export", "test-export"),
            _edge("edge-test-symbol", "test-export", "symbol-export"),
            _edge("edge-symbol-unrelated", "symbol-export", "req-unrelated"),
        ),
    )
    graph_store = YamlGraphStore(
        files["graph"], history_path=files["history"], transactions=transactions
    )
    graph_store.initialize(graph)
    case_store = JsonlCaseStore(files["cases"])
    config = ProjectConfig(project_id="demo", local_actor="local:asha")
    paths["config"].write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    proposal_store = IntentProposalStore(files["intent_proposals"], transactions=transactions)
    config_file = directory.file("config.yaml")
    return PreflightHarness(
        service=PreflightService(
            transactions=transactions,
            config_file=config_file,
            agent_principal="agent:codex",
        ),
        capture=ConversationCapture(
            evidence_store,
            connector_id="conversation:codex",
        ),
        evidence_store=evidence_store,
        case_store=case_store,
        graph_store=graph_store,
        proposal_store=proposal_store,
        transactions=transactions,
        paths={**paths, "journal": workspace / "history/.local-transaction.json"},
        config=config,
    )


class CancellationSignal(BaseException):
    """Test-only control-flow signal whose exact identity must survive preflight."""


def _repository_traceback_values(error: BaseException) -> str:
    values: list[str] = []
    for frame, _ in traceback.walk_tb(error.__traceback__):
        if "/src/intent_engineering/" in frame.f_code.co_filename:
            values.extend(repr(value) for value in frame.f_locals.values())
    return " ".join(values)


@pytest.mark.parametrize(
    ("classification", "updates", "authorized", "has_questions", "has_case"),
    [
        (TaskClassification.NO_SEMANTIC_IMPACT, {}, True, False, False),
        (
            TaskClassification.ALIGNED,
            {"relevant_node_ids": ("req-export",), "evidence_refs": ("ev-requirement",)},
            True,
            False,
            False,
        ),
        (
            TaskClassification.NEW_OR_AMBIGUOUS,
            {"questions": ("Which reports need export?",)},
            False,
            True,
            False,
        ),
        (
            TaskClassification.CONFLICTING,
            {
                "relevant_node_ids": ("req-export",),
                "evidence_refs": ("ev-requirement",),
                "conflict_claims": ("The request contradicts the active export boundary.",),
            },
            False,
            False,
            True,
        ),
    ],
)
def test_preflight_returns_exactly_one_fixed_outcome(
    preflight_harness: PreflightHarness,
    classification: TaskClassification,
    updates: dict[str, object],
    authorized: bool,
    has_questions: bool,
    has_case: bool,
) -> None:
    """Fails if any classification authorizes the wrong branch or mixes result shapes."""
    result = preflight_harness.evaluate(classification, **updates)

    assert result.classification is classification
    assert result.authorized is authorized
    assert bool(result.questions) is has_questions
    assert (result.review_case_id is not None) is has_case
    assert "token" not in result.model_dump()


def test_aligned_preflight_returns_context_for_exact_active_ids_only(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if alignment can cite a foreign node or leak an unrelated third-hop node."""
    result = preflight_harness.evaluate(
        TaskClassification.ALIGNED,
        relevant_node_ids=("req-export",),
        evidence_refs=("ev-requirement",),
    )

    context = cast(dict[str, object], result.model_dump(mode="json")["context"])
    encoded = str(context)
    assert result.relevant_node_ids == ("req-export",)
    assert "req-export" in encoded
    assert "intent-export" in encoded
    assert "test-export" in encoded
    assert "req-unrelated" not in encoded
    assert result.permitted_scope == ("src/export.py",)


@pytest.mark.parametrize(
    ("classification", "updates"),
    [
        (TaskClassification.NO_SEMANTIC_IMPACT, {"uncertainties": ("May change behavior",)}),
        (TaskClassification.NO_SEMANTIC_IMPACT, {"semantic_effects": ("Changes behavior",)}),
        (TaskClassification.ALIGNED, {"relevant_node_ids": ()}),
        (TaskClassification.ALIGNED, {"relevant_node_ids": ("req-foreign",)}),
        (TaskClassification.ALIGNED, {"relevant_node_ids": ("req-hidden",)}),
        (TaskClassification.ALIGNED, {"relevant_node_ids": ("req-inactive",)}),
        (
            TaskClassification.ALIGNED,
            {"relevant_node_ids": ("req-export",), "submission_scope": ("src/other.py",)},
        ),
        (TaskClassification.NEW_OR_AMBIGUOUS, {"questions": ()}),
        (TaskClassification.CONFLICTING, {"relevant_node_ids": ("req-export",)}),
    ],
)
def test_invalid_classification_paths_fail_closed_without_mutating_cases(
    preflight_harness: PreflightHarness,
    classification: TaskClassification,
    updates: dict[str, object],
) -> None:
    """Fails if uncertainty, stale identity, missing questions, or scope drift can authorize."""
    envelope, submission = preflight_harness.inputs(classification, **updates)
    before = preflight_harness.state_bytes()

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )

    assert preflight_harness.state_bytes() == before


def test_stale_task_digest_and_graph_version_fail_closed(preflight_harness: PreflightHarness) -> None:
    """Fails if submitted task or graph bindings are trusted instead of recomputed."""
    envelope, submission = preflight_harness.inputs(TaskClassification.NO_SEMANTIC_IMPACT)
    stale_task = submission.model_copy(update={"task_digest": "sha256:" + "f" * 64})
    stale_graph = submission.model_copy(update={"graph_version": 6})

    for invalid in (stale_task, stale_graph):
        with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
            preflight_harness.service.evaluate(
                envelope,
                invalid,
                principals=frozenset({"local:asha"}),
            )


def test_conflicting_replay_reuses_one_stable_nonterminal_case(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if identical conflict classification manufactures duplicate review cases."""
    envelope, submission = preflight_harness.inputs(
        TaskClassification.CONFLICTING,
        relevant_node_ids=("req-export",),
        evidence_refs=("ev-requirement",),
        conflict_claims=("The request contradicts the active export boundary.",),
    )
    first = preflight_harness.service.evaluate(
        envelope, submission, principals=frozenset({"local:asha"})
    )
    before = preflight_harness.state_bytes()
    second = preflight_harness.service.evaluate(
        envelope, submission, principals=frozenset({"local:asha"})
    )

    assert second.review_case_id == first.review_case_id
    assert len(preflight_harness.case_store.list()) == 1
    case = preflight_harness.case_store.list()[0]
    assert set(case.all_evidence_refs) == {
        "ev-requirement",
        envelope.request_evidence_ref,
        submission.agent_evidence_ref,
    }
    assert case.status is ReconciliationStatus.OPEN
    assert case.resolution is None
    assert case.history == ()
    assert preflight_harness.state_bytes() == before


@pytest.mark.parametrize("collision", ["hidden", "unrelated"])
def test_conflict_reuse_requires_visible_exact_immutable_semantics(
    preflight_harness: PreflightHarness,
    collision: str,
) -> None:
    """Fails if a hidden or semantically unrelated same-fingerprint case is disclosed."""
    envelope, submission = preflight_harness.inputs(
        TaskClassification.CONFLICTING,
        relevant_node_ids=("req-export",),
        evidence_refs=("ev-requirement",),
        conflict_claims=("The request contradicts the active export boundary.",),
    )
    first = preflight_harness.service.evaluate(
        envelope,
        submission,
        principals=frozenset({"local:asha"}),
    )
    original = preflight_harness.case_store.get(cast(str, first.review_case_id))
    if collision == "hidden":
        hidden_side = original.evidence_sides[0].model_copy(
            update={
                "evidence_refs": ("ev-hidden",),
                "authors": ("product:other",),
            }
        )
        replacement = original.model_copy(
            update={"evidence_sides": (hidden_side, original.evidence_sides[1])}
        )
    else:
        replacement = original.model_copy(
            update={
                "subject_ref": "req-unrelated",
                "affected_refs": ("req-unrelated",),
            }
        )
    preflight_harness.paths["cases"].write_bytes(serialize_case(replacement))

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


def test_concurrent_identical_conflict_replay_returns_one_exact_durable_case(
    preflight_harness: PreflightHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if the loser of an identical conflict append race returns a fixed failure."""
    envelope, submission = preflight_harness.inputs(
        TaskClassification.CONFLICTING,
        relevant_node_ids=("req-export",),
        evidence_refs=("ev-requirement",),
        conflict_claims=("The request contradicts the active export boundary.",),
    )
    barrier = threading.Barrier(2)
    original = preflight_harness.transactions.transaction

    @contextmanager
    def synchronized_transaction(*args: object, **kwargs: object):
        barrier.wait(timeout=5)
        with original(*args, **kwargs) as transaction:
            yield transaction

    monkeypatch.setattr(
        preflight_harness.transactions,
        "transaction",
        synchronized_transaction,
    )

    def evaluate():
        return preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(evaluate), executor.submit(evaluate))
        results = tuple(future.result(timeout=10) for future in futures)

    assert results[0] == results[1]
    cases = preflight_harness.case_store.list()
    assert len(cases) == 1
    assert results[0].review_case_id == cases[0].id


def test_missing_persisted_agent_turn_prevents_evaluation(preflight_harness: PreflightHarness) -> None:
    """Fails if a typed submission can authorize without its immutable agent evidence turn."""
    envelope, submission = preflight_harness.inputs(TaskClassification.NO_SEMANTIC_IMPACT)
    missing = submission.model_copy(
        update={"agent_evidence_ref": "evidence:conversation:" + "0" * 64}
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            missing,
            principals=frozenset({"local:asha"}),
        )


def test_preflight_reads_current_config_in_the_same_snapshot(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if preflight trusts a cached config after authenticated project policy changes."""
    envelope, submission = preflight_harness.inputs(TaskClassification.NO_SEMANTIC_IMPACT)
    changed = preflight_harness.config.model_copy(update={"local_actor": "local:other"})
    preflight_harness.paths["config"].write_text(
        yaml.safe_dump(changed.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


@pytest.mark.parametrize("target", ["graph", "evidence", "cases", "intent_proposals"])
def test_invalid_snapshot_components_fail_closed_without_rewrite(
    preflight_harness: PreflightHarness,
    target: str,
) -> None:
    """Fails if corrupt graph, evidence, case, or proposal state is partially trusted."""
    envelope, submission = preflight_harness.inputs(TaskClassification.NO_SEMANTIC_IMPACT)
    preflight_harness.paths[target].write_bytes(b"invalid canonical state\n")
    before = preflight_harness.state_bytes()

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )

    assert preflight_harness.state_bytes() == before


def test_unauthenticated_actor_principals_fail_closed(preflight_harness: PreflightHarness) -> None:
    """Fails if caller-supplied principals can omit the configured authenticated actor."""
    envelope, submission = preflight_harness.inputs(TaskClassification.NO_SEMANTIC_IMPACT)

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:other"}),
        )


def test_caller_principal_superset_cannot_amplify_acl_visibility(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if caller data can add an ACL-only foreign principal to authorization."""
    envelope, submission = preflight_harness.inputs(
        TaskClassification.ALIGNED,
        relevant_node_ids=("req-hidden",),
        evidence_refs=("ev-hidden",),
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha", "local:other"}),
        )


def test_snapshot_bound_resolver_preserves_authenticated_alias_visibility(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if legitimate aliases cannot be derived from the held project snapshot."""
    directory = SecureDirectory.open(preflight_harness.paths["config"].parent)
    service = PreflightService(
        transactions=preflight_harness.transactions,
        config_file=directory.file("config.yaml"),
        agent_principal="agent:codex",
        principal_resolver=lambda config, _snapshot: frozenset(
            {config.local_actor, "local:other"}
        ),
    )
    envelope, submission = preflight_harness.inputs(
        TaskClassification.ALIGNED,
        relevant_node_ids=("req-hidden",),
        evidence_refs=("ev-hidden",),
    )

    result = service.evaluate(
        envelope,
        submission,
        principals=frozenset({"local:asha", "local:other"}),
    )

    assert result.authorized is True
    assert result.relevant_node_ids == ("req-hidden",)


@pytest.mark.parametrize(
    "caller_principals",
    [
        frozenset({"local:asha"}),
        frozenset({"local:asha", "local:other", "local:intruder"}),
    ],
)
def test_snapshot_bound_principals_require_exact_caller_equality(
    preflight_harness: PreflightHarness,
    caller_principals: frozenset[str],
) -> None:
    """Fails if caller omission or addition can alter the exact authenticated principal set."""
    directory = SecureDirectory.open(preflight_harness.paths["config"].parent)
    service = PreflightService(
        transactions=preflight_harness.transactions,
        config_file=directory.file("config.yaml"),
        agent_principal="agent:codex",
        principal_resolver=lambda config, _snapshot: frozenset(
            {config.local_actor, "local:other"}
        ),
    )
    envelope, submission = preflight_harness.inputs(TaskClassification.NO_SEMANTIC_IMPACT)

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        service.evaluate(
            envelope,
            submission,
            principals=caller_principals,
        )


def test_preflight_takes_one_transaction_snapshot(preflight_harness: PreflightHarness) -> None:
    """Fails if stores are independently reloaded into a torn classification view."""
    envelope, submission = preflight_harness.inputs(TaskClassification.NO_SEMANTIC_IMPACT)
    original = preflight_harness.transactions.snapshot
    calls = 0

    def counted(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    preflight_harness.transactions.snapshot = counted  # type: ignore[method-assign]
    result = preflight_harness.service.evaluate(
        envelope,
        submission,
        principals=frozenset({"local:asha"}),
    )

    assert result.authorized is True
    assert calls == 1


@pytest.mark.parametrize("target", ["config", "graph", "evidence", "cases", "intent_proposals"])
@pytest.mark.parametrize(
    ("classification", "updates"),
    [
        (TaskClassification.NO_SEMANTIC_IMPACT, {}),
        (
            TaskClassification.ALIGNED,
            {"relevant_node_ids": ("req-export",), "evidence_refs": ("ev-requirement",)},
        ),
    ],
)
def test_authorized_result_cannot_escape_after_snapshot_replacement(
    preflight_harness: PreflightHarness,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    classification: TaskClassification,
    updates: dict[str, object],
) -> None:
    """Fails if aligned or mechanical authorization can return after any held state drifts."""
    envelope, submission = preflight_harness.inputs(classification, **updates)
    original = preflight_harness.service._validate_references

    def replace_state() -> None:
        if target == "config":
            changed = preflight_harness.config.model_copy(update={"auto_apply_metadata": False})
            preflight_harness.paths[target].write_text(
                yaml.safe_dump(changed.model_dump(mode="json"), sort_keys=True),
                encoding="utf-8",
            )
        elif target == "graph":
            graph = preflight_harness.graph_store.load()
            changed_node = graph.nodes[0].model_copy(update={"label": "Replacement intent"})
            changed = graph.model_copy(update={"nodes": (changed_node, *graph.nodes[1:])})
            preflight_harness.paths[target].write_bytes(serialize_graph(changed))
        elif target == "evidence":
            preflight_harness.evidence_store.associate(
                "markdown", _semantic_evidence("ev-replacement")
            )
        elif target == "cases":
            preflight_harness.case_store.put(
                ReconciliationCase(
                    id="case:replacement",
                    subject_ref="req-unrelated",
                    case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
                    affected_refs=("req-unrelated",),
                    evidence_sides=(
                        EvidenceSide(
                            label="replacement",
                            claim="Replacement case",
                            evidence_refs=("ev-requirement",),
                            observed_at=NOW,
                            authors=("product:priya",),
                            confidence=0.8,
                        ),
                    ),
                    detector_id="test",
                    fingerprint=hashlib.sha256(b"replacement").hexdigest(),
                    created_at=NOW,
                    created_by="detector:test",
                )
            )
        else:
            _store_provisional_candidate(
                preflight_harness,
                candidate_id="req-replacement",
                provisional_id="req-replacement",
            )

    def mutating_validate(*args: object, **kwargs: object):
        result = original(*args, **kwargs)
        replace_state()
        return result

    monkeypatch.setattr(preflight_harness.service, "_validate_references", mutating_validate)

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


def test_relevant_nonterminal_case_blocks_aligned_authorization(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if aligned work can proceed through an unresolved relevant review case."""
    case = ReconciliationCase(
        id="case:block-export",
        subject_ref="req-export",
        case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
        affected_refs=("req-export",),
        evidence_sides=(
            EvidenceSide(
                label="requirement",
                claim="Export behavior remains under review",
                evidence_refs=("ev-requirement",),
                observed_at=NOW,
                authors=("product:priya",),
                confidence=0.8,
            ),
        ),
        detector_id="test",
        fingerprint=hashlib.sha256(b"block-export").hexdigest(),
        created_at=NOW,
        created_by="detector:test",
        status=ReconciliationStatus.OPEN,
    )
    preflight_harness.case_store.put(case)
    envelope, submission = preflight_harness.inputs(
        TaskClassification.ALIGNED,
        relevant_node_ids=("req-export",),
        evidence_refs=("ev-requirement",),
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


@pytest.mark.parametrize(
    ("latest_status", "authorized"),
    [
        (ReconciliationStatus.DEFERRED, True),
        (ReconciliationStatus.PROPOSED, False),
    ],
)
def test_only_latest_case_lifecycle_version_controls_alignment(
    preflight_harness: PreflightHarness,
    latest_status: ReconciliationStatus,
    authorized: bool,
) -> None:
    """Fails if historical OPEN rows block after a terminal latest lifecycle version."""
    opened = ReconciliationCase(
        id="case:lifecycle-export",
        subject_ref="req-export",
        case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
        affected_refs=("req-export",),
        evidence_sides=(
            EvidenceSide(
                label="requirement",
                claim="Export behavior lifecycle",
                evidence_refs=("ev-requirement",),
                observed_at=NOW,
                authors=("product:priya",),
                confidence=0.8,
            ),
        ),
        detector_id="test",
        fingerprint=hashlib.sha256(b"lifecycle-export").hexdigest(),
        created_at=NOW,
        created_by="detector:test",
    )
    latest = transition_case(opened, latest_status, "local:asha", NOW + timedelta(seconds=1))
    preflight_harness.case_store.put(opened)
    preflight_harness.case_store.put(latest)
    envelope, submission = preflight_harness.inputs(
        TaskClassification.ALIGNED,
        relevant_node_ids=("req-export",),
        evidence_refs=("ev-requirement",),
    )

    if authorized:
        result = preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )
        assert result.authorized is True
    else:
        with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
            preflight_harness.service.evaluate(
                envelope,
                submission,
                principals=frozenset({"local:asha"}),
            )


def test_aligned_requires_complete_citation_of_relevant_node_evidence(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if an agent can align to a node without citing its authorized provenance."""
    envelope, submission = preflight_harness.inputs(
        TaskClassification.ALIGNED,
        relevant_node_ids=("req-export",),
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


def _store_provisional_candidate(
    preflight_harness: PreflightHarness,
    *,
    candidate_id: str = "req-provisional",
    provisional_id: str = "req-provisional",
    candidate_evidence_id: str = "ev-requirement",
    candidate_updates: dict[str, object] | None = None,
) -> None:
    candidate = _node(
        candidate_id,
        NodeType.REQUIREMENT,
        "Provisional sharing requirement",
        candidate_evidence_id,
        status="proposed",
    ).model_copy(
        update={
            "created_by": "agent:codex",
            "created_at": NOW,
            "last_modified_by": "agent:codex",
            "last_modified_at": NOW,
            "last_reassessed_at": NOW,
            "source_mode": SourceMode.INFERRED,
            **(candidate_updates or {}),
        }
    )
    changeset = ChangeSet(
        id="changeset:provisional",
        actor="agent:codex",
        timestamp=NOW,
        baseline_graph_version=7,
        evidence_refs=("ev-requirement",),
        nodes_added=(candidate,),
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
    values = {
        "kind": ProposalKind.REQUIREMENT,
        "proposed_by": "agent:codex",
        "proposed_at": NOW,
        "baseline_graph_version": 7,
        "evidence_refs": ("ev-requirement",),
        "source_roles": (),
        "changeset": changeset,
        "core_node_ids": (),
        "provisional_node_ids": (provisional_id,),
    }
    draft = IntentProposal.model_construct(id="", **values)
    preflight_harness.proposal_store.put(
        IntentProposal(id=f"proposal:{draft.digest}", **values)
    )


def _store_bootstrap_candidate(
    preflight_harness: PreflightHarness,
    *,
    role_variant: str,
) -> None:
    exact = SourceRoleAssignment(
        connector_id="markdown",
        scope="docs/prd.md",
        role=SourceRole.DECLARED_INTENT,
        inherited=False,
    )
    inherited = SourceRoleAssignment(
        connector_id="markdown",
        scope="docs",
        role=SourceRole.PROPOSED_INTENT,
        inherited=True,
    )
    unused = SourceRoleAssignment(
        connector_id="markdown",
        scope="docs/unused.md",
        role=SourceRole.OPERATING_CONTEXT,
        inherited=False,
    )
    mismatched = SourceRoleAssignment(
        connector_id="jira",
        scope="docs/prd.md",
        role=SourceRole.DECISION,
        inherited=False,
    )
    if role_variant == "exact":
        configured_roles = (exact,)
        submitted_roles = (exact,)
    elif role_variant == "inherited":
        configured_roles = (inherited,)
        submitted_roles = (inherited,)
    elif role_variant == "missing":
        configured_roles = (exact,)
        submitted_roles = ()
    elif role_variant == "unused":
        configured_roles = (exact, unused)
        submitted_roles = (exact, unused)
    elif role_variant == "mismatched":
        configured_roles = (mismatched,)
        submitted_roles = (mismatched,)
    elif role_variant == "exact_override":
        configured_roles = (inherited, exact)
        submitted_roles = (inherited,)
    else:
        raise AssertionError("unknown role variant")
    changed_config = preflight_harness.config.model_copy(
        update={"source_roles": configured_roles}
    )
    preflight_harness.paths["config"].write_text(
        yaml.safe_dump(changed_config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )

    def candidate(node_id: str, node_type: NodeType, label: str) -> Node:
        return _node(
            node_id,
            node_type,
            label,
            "ev-requirement",
            status="proposed",
        ).model_copy(
            update={
                "created_by": "agent:codex",
                "created_at": NOW,
                "last_modified_by": "agent:codex",
                "last_modified_at": NOW,
                "last_reassessed_at": NOW,
                "source_mode": SourceMode.INFERRED,
            }
        )

    core = candidate("intent-bootstrap-core", NodeType.PRODUCT_INTENT, "Bootstrap intent")
    provisional = candidate(
        "req-bootstrap-provisional",
        NodeType.REQUIREMENT,
        "Bootstrap provisional requirement",
    )
    changeset = ChangeSet(
        id="changeset:bootstrap-preflight",
        actor="agent:codex",
        timestamp=NOW,
        baseline_graph_version=7,
        evidence_refs=("ev-requirement",),
        nodes_added=(core, provisional),
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
    values = {
        "kind": ProposalKind.BOOTSTRAP,
        "proposed_by": "agent:codex",
        "proposed_at": NOW,
        "baseline_graph_version": 7,
        "evidence_refs": ("ev-requirement",),
        "source_roles": submitted_roles,
        "changeset": changeset,
        "core_node_ids": (core.id,),
        "provisional_node_ids": (provisional.id,),
    }
    draft = IntentProposal.model_construct(id="", **values)
    preflight_harness.proposal_store.put(
        IntentProposal(id=f"proposal:{draft.digest}", **values)
    )


@pytest.mark.parametrize(
    ("role_variant", "accepted"),
    [
        ("exact", True),
        ("inherited", True),
        ("missing", False),
        ("unused", False),
        ("mismatched", False),
        ("exact_override", False),
    ],
)
def test_bootstrap_provisional_requires_complete_exact_source_role_association(
    preflight_harness: PreflightHarness,
    role_variant: str,
    accepted: bool,
) -> None:
    """Fails if bootstrap provisional evidence lacks exact Task 3 source-role coverage."""
    _store_bootstrap_candidate(preflight_harness, role_variant=role_variant)
    envelope, submission = preflight_harness.inputs(
        TaskClassification.NEW_OR_AMBIGUOUS,
        relevant_node_ids=("req-bootstrap-provisional",),
        evidence_refs=("ev-requirement",),
        questions=("Should the bootstrap candidate become active?",),
    )

    if accepted:
        result = preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )
        assert result.authorized is False
        assert result.relevant_node_ids == ("req-bootstrap-provisional",)
    else:
        with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
            preflight_harness.service.evaluate(
                envelope,
                submission,
                principals=frozenset({"local:asha"}),
            )


def test_provisional_candidate_cannot_satisfy_alignment(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if unresolved proposal material is treated as active authorization."""
    _store_provisional_candidate(preflight_harness)
    aligned_envelope, aligned_submission = preflight_harness.inputs(
        TaskClassification.ALIGNED,
        relevant_node_ids=("req-provisional",),
        evidence_refs=("ev-requirement",),
    )
    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            aligned_envelope,
            aligned_submission,
            principals=frozenset({"local:asha"}),
        )



def test_provisional_candidate_can_inform_ambiguous_questions(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if unresolved proposal evidence cannot be cited by a blocked question flow."""
    _store_provisional_candidate(preflight_harness)
    ambiguous = preflight_harness.evaluate(
        TaskClassification.NEW_OR_AMBIGUOUS,
        relevant_node_ids=("req-provisional",),
        evidence_refs=("ev-requirement",),
        questions=("Should the provisional sharing behavior become active?",),
    )
    assert ambiguous.authorized is False
    assert ambiguous.relevant_node_ids == ("req-provisional",)


def test_provisional_identity_must_resolve_to_the_exact_proposal_changeset(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if proposal metadata can invent a provisional identity absent from its ChangeSet."""
    _store_provisional_candidate(
        preflight_harness,
        candidate_id="req-other",
        provisional_id="req-provisional",
    )
    envelope, submission = preflight_harness.inputs(
        TaskClassification.NEW_OR_AMBIGUOUS,
        relevant_node_ids=("req-provisional",),
        evidence_refs=("ev-requirement",),
        questions=("Should this proposed behavior exist?",),
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


def test_preflight_rejects_typed_noncanonical_proposal_snapshot(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if Pydantic normalization can authenticate bytes the proposal store rejects."""
    _store_provisional_candidate(preflight_harness)
    proposal_path = preflight_harness.paths["intent_proposals"]
    content = proposal_path.read_bytes()
    normalized = content.replace(b"2026-08-26T12:00:00Z", b"2026-08-26T12:00:00+00:00")
    assert normalized != content
    proposal_path.write_bytes(normalized)
    envelope, submission = preflight_harness.inputs(
        TaskClassification.NEW_OR_AMBIGUOUS,
        relevant_node_ids=("req-provisional",),
        evidence_refs=("ev-requirement",),
        questions=("Should this proposed behavior exist?",),
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


@pytest.mark.parametrize(
    "candidate_updates",
    [
        {"evidence_refs": ("ev-hidden",)},
        {"evidence_refs": ("ev-intent",)},
        {"created_by": "agent:foreign"},
        {"source_mode": SourceMode.EXPLICIT},
        {"type": NodeType.SYMBOL},
    ],
)
def test_provisional_candidate_requires_exact_visible_provenance_association(
    preflight_harness: PreflightHarness,
    candidate_updates: dict[str, object],
) -> None:
    """Fails if nested hidden, foreign, unauthored, explicit, or wrong-type candidates inform."""
    _store_provisional_candidate(
        preflight_harness,
        candidate_updates=candidate_updates,
    )
    envelope, submission = preflight_harness.inputs(
        TaskClassification.NEW_OR_AMBIGUOUS,
        relevant_node_ids=("req-provisional",),
        evidence_refs=("ev-requirement",),
        questions=("Should this provisional behavior become active?",),
    )

    with pytest.raises(PreflightError, match="^intent preflight unavailable$"):
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )


def test_conflict_cancellation_rolls_back_case_and_preserves_signal(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if interruption after case append leaves a partial review case or masks control flow."""
    envelope, submission = preflight_harness.inputs(
        TaskClassification.CONFLICTING,
        relevant_node_ids=("req-export",),
        evidence_refs=("ev-requirement",),
        conflict_claims=("The request conflicts with the export requirement.",),
    )
    before = preflight_harness.state_bytes()
    signal = CancellationSignal()

    def interrupt(stage: str) -> None:
        if stage == "target:cases":
            raise signal

    preflight_harness.transactions._fault_hook = interrupt
    with pytest.raises(CancellationSignal) as caught:
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )

    assert caught.value is signal
    assert preflight_harness.state_bytes() == before


def test_conversation_persistence_failure_leaves_no_turn_and_cannot_authorize(
    preflight_harness: PreflightHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if a persistence error can be ignored before classification evaluation."""
    before = preflight_harness.state_bytes()

    def fail(*_args: object, **_kwargs: object) -> bool:
        raise OSError("disk unavailable")

    monkeypatch.setattr(preflight_harness.evidence_store, "associate", fail)
    with pytest.raises(ValueError, match="^conversation capture unavailable$"):
        preflight_harness.capture.record_turn(
            conversation_ref="codex:failed",
            role="human",
            author="local:asha",
            content="request",
            captured_at=NOW,
            acl=("local:asha",),
        )
    assert preflight_harness.state_bytes() == before


def test_fixed_failure_and_cancellation_tracebacks_drop_sensitive_inputs(
    preflight_harness: PreflightHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if rejected or interrupted preflight retains private request/classifier material."""
    sentinel = "SENTINEL-PREFLIGHT-PRIVATE"
    envelope, submission = preflight_harness.inputs(
        TaskClassification.NEW_OR_AMBIGUOUS,
        request=sentinel,
        questions=(sentinel,),
    )
    invalid = submission.model_copy(update={"task_digest": "sha256:" + "f" * 64})
    with pytest.raises(PreflightError) as failed:
        preflight_harness.service.evaluate(
            envelope,
            invalid,
            principals=frozenset({"local:asha"}),
        )
    assert sentinel not in _repository_traceback_values(failed.value)

    signal = CancellationSignal()

    def cancel(*_args: object, **_kwargs: object):
        raise signal

    monkeypatch.setattr(preflight_harness.transactions, "snapshot", cancel)
    with pytest.raises(CancellationSignal) as interrupted:
        preflight_harness.service.evaluate(
            envelope,
            submission,
            principals=frozenset({"local:asha"}),
        )
    assert interrupted.value is signal
    assert sentinel not in _repository_traceback_values(interrupted.value)


def test_context_for_refs_is_permutation_stable_and_bounded_to_two_hops(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if exact-reference context depends on input order or expands past two hops."""
    provider = ContextProvider(
        preflight_harness.graph_store.load(),
        preflight_harness.case_store.list(),
        preflight_harness.config,
        preflight_harness.evidence_store.list(),
    )
    first = provider.for_refs(("req-export", "intent-export"), actor={"local:asha"})
    second = provider.for_refs(("intent-export", "req-export"), actor={"local:asha"})

    assert first == second
    assert "req-unrelated" not in first.model_dump_json()
    assert {item.id for item in first.relevant_requirements} == {"req-export"}


def test_context_for_refs_hides_absent_and_unauthorized_identically(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if exact-reference lookup reveals whether a hidden node exists."""
    provider = ContextProvider(
        preflight_harness.graph_store.load(),
        (),
        preflight_harness.config,
        preflight_harness.evidence_store.list(),
    )
    messages: list[str] = []
    for reference in ("req-hidden", "req-absent"):
        with pytest.raises(ValueError) as caught:
            provider.for_refs((reference,), actor={"local:asha"})
        messages.append(str(caught.value))

    assert messages == ["intent context unavailable", "intent context unavailable"]


def test_context_for_refs_applies_fixed_caps_even_when_config_is_larger(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if project-configured limits can make an exact-reference agent packet unbounded."""
    graph = preflight_harness.graph_store.load()
    neighbours = tuple(
        _node(
            f"req-neighbour-{index:02d}",
            NodeType.REQUIREMENT,
            f"Neighbour requirement {index}",
            "ev-requirement",
        )
        for index in range(25)
    )
    edges = tuple(
        _edge(f"edge-neighbour-{index:02d}", "intent-export", node.id)
        for index, node in enumerate(neighbours)
    )
    expanded = graph.model_copy(
        update={"nodes": (*graph.nodes, *neighbours), "edges": (*graph.edges, *edges)}
    )
    config = preflight_harness.config.model_copy(
        update={"context_limits": {"relevant_intent": 1000, "relevant_requirements": 1000}}
    )
    provider = ContextProvider(
        expanded,
        (),
        config,
        preflight_harness.evidence_store.list(),
    )

    pack = provider.for_refs(("intent-export",), actor={"local:asha"})

    assert len(pack.relevant_requirements) == 10


def test_context_for_refs_freezes_principals_once(preflight_harness: PreflightHarness) -> None:
    """Fails if a mutable principal collection is re-read across evidence projection."""

    class OneShotPrincipals:
        def __init__(self) -> None:
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            yield "local:asha" if self.iterations == 1 else "local:other"

        def __len__(self) -> int:
            return 1

        def __contains__(self, value: object) -> bool:
            return value == "local:asha"

    actor = OneShotPrincipals()
    provider = ContextProvider(
        preflight_harness.graph_store.load(),
        (),
        preflight_harness.config,
        preflight_harness.evidence_store.list(),
    )

    pack = provider.for_refs(("req-export",), actor=actor)  # type: ignore[arg-type]

    assert [item.id for item in pack.relevant_requirements] == ["req-export"]
    assert actor.iterations == 1


def test_context_for_refs_fixed_failure_drops_built_pack_from_traceback(
    preflight_harness: PreflightHarness,
) -> None:
    """Fails if a cap rejection retains authorized graph labels in repository frame locals."""
    sentinel = "SENTINEL-CONTEXT-LABEL"
    graph = preflight_harness.graph_store.load()
    requested = tuple(f"req-cap-{index:02d}" for index in range(11))
    nodes = tuple(
        _node(node_id, NodeType.REQUIREMENT, f"{sentinel}-{index}", "ev-requirement")
        for index, node_id in enumerate(requested)
    )
    provider = ContextProvider(
        graph.model_copy(update={"nodes": (*graph.nodes, *nodes)}),
        (),
        preflight_harness.config,
        preflight_harness.evidence_store.list(),
    )

    with pytest.raises(ValueError, match="^intent context unavailable$") as caught:
        provider.for_refs(requested, actor={"local:asha"})

    assert sentinel not in _repository_traceback_values(caught.value)
