"""Descriptor-held, ACL-safe acquisition for detached graph assessment."""

from __future__ import annotations

import hashlib
import json
import traceback
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel

from intent_engineering.assessment.models import AssessmentSnapshot
from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.writes import MutationPolicy
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    ProjectConfig,
    ReconciliationCase,
)
from intent_engineering.core.policy.access import evidence_allowed
from intent_engineering.intent_workflow import (
    ClarificationEvent,
    IntentProposal,
    ProposalDecisionRecord,
    ProposalDecisionV2,
    ProposalDecisionV3,
)
from intent_engineering.intent_workflow.proposal_store import (
    parse_intent_ledger,
)
from intent_engineering.storage.jsonl.case_store import parse_case_versions
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import (
    LocalTransaction,
    LocalTransactionExtraReadPolicy,
    LocalTransactionSnapshot,
)
from intent_engineering.storage.yaml.graph_store import parse_graph

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import AssessmentRuntimeProtocol

_ASSESSMENT_TARGETS = frozenset({"graph", "evidence", "cases", "history", "intent_proposals"})
_CONFIG_LIMIT = 1024 * 1024
_CONFIG_POLICY = LocalTransactionExtraReadPolicy(
    max_bytes=_CONFIG_LIMIT,
    nonblocking_regular=True,
)
_AUTHORITY_POLICIES = {
    "acl_policy": _CONFIG_POLICY,
    "config": _CONFIG_POLICY,
}


class AssessmentUnavailable(ValueError):
    """One fixed public failure for rejected or unavailable assessment input."""

    def __init__(self) -> None:
        super().__init__("assessment unavailable")


@dataclass(frozen=True)
class _ParsedSnapshot:
    config: ProjectConfig
    graph: Graph
    evidence: tuple[EvidenceRecord, ...]
    ingestions: tuple[EvidenceIngestion, ...]
    cases: tuple[ReconciliationCase, ...]
    proposals: tuple[IntentProposal, ...]
    decisions: tuple[ProposalDecisionRecord, ...]
    clarifications: tuple[ClarificationEvent, ...]
    history: tuple[ChangeSet, ...]


@dataclass(frozen=True)
class _VisibleSnapshot:
    graph: Graph
    evidence: tuple[EvidenceRecord, ...]
    ingestions: tuple[EvidenceIngestion, ...]
    cases: tuple[ReconciliationCase, ...]
    clarifications: tuple[ClarificationEvent, ...]
    history: tuple[ChangeSet, ...]
    principals: frozenset[str]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _model_material(value: BaseModel) -> object:
    return value.model_dump(mode="json", by_alias=True)


def _parse_config(content: bytes | None) -> ProjectConfig:
    if content is None:
        raise ValueError("missing assessment config")
    return ProjectConfig.model_validate(load_strict_yaml_mapping_bytes(content))


def _parse_history(content: bytes | None) -> tuple[ChangeSet, ...]:
    if content is None:
        return ()
    history: list[ChangeSet] = []
    for line in content.decode("utf-8").splitlines():
        if not line.strip():
            continue
        history.append(ChangeSet.model_validate(loads_strict_object(line)))
    if len({item.id for item in history}) != len(history):
        raise ValueError("duplicate assessment history identity")
    return tuple(history)


def _parse_held(held: LocalTransactionSnapshot) -> _ParsedSnapshot:
    graph_content = held.content.get("graph")
    if graph_content is None:
        raise ValueError("missing assessment graph")
    load_strict_yaml_mapping_bytes(graph_content)
    config = _parse_config(held.content.get("config"))
    graph = parse_graph(graph_content)
    evidence, ingestions, _legacy = parse_evidence_lines(held.content.get("evidence"))
    case_versions = parse_case_versions(held.content.get("cases"))
    latest_cases = {case.id: case for case in case_versions}
    ledger = parse_intent_ledger(held.content.get("intent_proposals") or b"")
    if ledger is None:
        raise ValueError("invalid assessment intent ledger")
    history = _parse_history(held.content.get("history"))
    return _ParsedSnapshot(
        config=config,
        graph=graph,
        evidence=evidence,
        ingestions=ingestions,
        cases=tuple(latest_cases[item] for item in sorted(latest_cases)),
        proposals=tuple(ledger.proposals.values()),
        decisions=tuple(ledger.decisions.values()),
        clarifications=ledger.clarification_events,
        history=history,
    )


def _visible_ingestions(
    ingestions: Sequence[EvidenceIngestion],
    visible_ids: frozenset[str],
) -> tuple[EvidenceIngestion, ...]:
    sequence_by_connector: defaultdict[str, int] = defaultdict(int)
    predecessor_by_object: dict[tuple[str, str, str], str] = {}
    visible: list[EvidenceIngestion] = []
    for ingestion in ingestions:
        record = ingestion.evidence
        if record.id not in visible_ids:
            continue
        sequence_by_connector[ingestion.connector_id] += 1
        object_key = (
            ingestion.connector_id,
            record.connector_type,
            record.external_object_id,
        )
        visible.append(
            EvidenceIngestion(
                connector_id=ingestion.connector_id,
                sequence=sequence_by_connector[ingestion.connector_id],
                predecessor_id=predecessor_by_object.get(object_key),
                evidence=record,
            )
        )
        predecessor_by_object[object_key] = record.id
    return tuple(sorted(visible, key=lambda item: (item.connector_id, item.sequence)))


def _clarification_evidence_refs(event: ClarificationEvent) -> frozenset[str]:
    session = event.session
    references = {
        session.request_evidence_ref,
        session.classification_evidence_ref,
        *(question.evidence_ref for question in session.questions),
        *(answer.evidence_ref for answer in session.answers),
        *(conflict.original_evidence_ref for conflict in session.conflicts),
        *(conflict.conflicting_evidence_ref for conflict in session.conflicts),
    }
    references.update(
        question.predecessor_evidence_ref
        for question in session.questions
        if question.predecessor_evidence_ref is not None
    )
    references.update(
        answer.predecessor_evidence_ref
        for answer in session.answers
        if answer.predecessor_evidence_ref is not None
    )
    references.update(conflict.predecessor_evidence_ref for conflict in session.conflicts)
    return frozenset(references)


def _changeset_subjects(changeset: ChangeSet) -> frozenset[str]:
    return frozenset(
        {
            *(node.id for node in changeset.nodes_added),
            *(update.node_id for update in changeset.nodes_updated),
            *changeset.nodes_superseded,
            *(edge.id for edge in changeset.edges_added),
            *(update.edge_id for update in changeset.edges_updated),
            *changeset.edges_superseded,
            *(change.subject_ref for change in changeset.confidence_changes),
            *(change.claim_id for change in changeset.implementation_status_changes),
            *changeset.reconciliation_cases_created,
            *changeset.reconciliation_cases_resolved,
        }
    )


def _changeset_evidence_refs(changeset: ChangeSet) -> frozenset[str]:
    return frozenset(
        {
            *changeset.evidence_refs,
            *(ref for change in changeset.confidence_changes for ref in change.evidence_refs),
            *(
                ref
                for change in changeset.implementation_status_changes
                for ref in change.evidence_refs
            ),
            *(
                ref
                for node in (
                    *changeset.nodes_added,
                    *(update.replacement for update in changeset.nodes_updated),
                )
                for ref in node.evidence_refs
            ),
        }
    )


def _history_visible(
    changeset: ChangeSet,
    *,
    visible_evidence_ids: frozenset[str],
    visible_references: frozenset[str],
) -> bool:
    if not _changeset_evidence_refs(changeset).issubset(visible_evidence_ids):
        return False
    if not _changeset_subjects(changeset).issubset(visible_references):
        return False
    embedded_edges = (
        *changeset.edges_added,
        *(update.replacement for update in changeset.edges_updated),
    )
    visible_graph_ids = set(visible_references)
    return all(
        edge.from_id in visible_graph_ids and edge.to_id in visible_graph_ids
        for edge in embedded_edges
    )


def _proposal_visible(
    proposal: IntentProposal,
    *,
    visible_evidence_ids: frozenset[str],
    visible_references: frozenset[str],
) -> bool:
    changeset = proposal.changeset
    embedded_references = frozenset(
        {
            *(node.id for node in changeset.nodes_added),
            *(edge.id for edge in changeset.edges_added),
            *changeset.reconciliation_cases_created,
        }
    )
    if not set(proposal.evidence_refs).issubset(visible_evidence_ids):
        return False
    if not {*proposal.core_node_ids, *proposal.provisional_node_ids}.issubset(
        {*visible_references, *(node.id for node in changeset.nodes_added)}
    ):
        return False
    return _history_visible(
        changeset,
        visible_evidence_ids=visible_evidence_ids,
        visible_references=frozenset({*visible_references, *embedded_references}),
    )


def _decision_visible(
    decision: ProposalDecisionRecord,
    *,
    visible_proposal_ids: frozenset[str],
    visible_graph_ids: frozenset[str],
    visible_case_ids: frozenset[str],
    visible_history_ids: frozenset[str],
) -> bool:
    if decision.proposal_id not in visible_proposal_ids:
        return False
    if isinstance(decision, ProposalDecisionV2):
        return (
            set(decision.confirmed_node_ids).issubset(visible_graph_ids)
            and decision.activation_changeset_id in visible_history_ids
        )
    if isinstance(decision, ProposalDecisionV3):
        return (
            set(decision.selected_node_ids).issubset(visible_graph_ids)
            and decision.activation_changeset_id in visible_history_ids
            and (decision.review_case_id is None or decision.review_case_id in visible_case_ids)
        )
    return True


def _clarification_visible(
    event: ClarificationEvent,
    *,
    visible_clarification_ids: frozenset[str],
    visible_proposal_ids: frozenset[str],
    visible_decision_ids: frozenset[str],
    visible_history_ids: frozenset[str],
) -> bool:
    return (
        (
            event.predecessor_event_id is None
            or event.predecessor_event_id in visible_clarification_ids
        )
        and (event.proposal_id is None or event.proposal_id in visible_proposal_ids)
        and (event.decision_id is None or event.decision_id in visible_decision_ids)
        and (
            event.activation_changeset_id is None
            or event.activation_changeset_id in visible_history_ids
        )
    )


def _visible_projection(
    *,
    config: ProjectConfig,
    graph: Graph,
    evidence: Sequence[EvidenceRecord],
    ingestions: Sequence[EvidenceIngestion],
    cases: Sequence[ReconciliationCase],
    proposals: Sequence[IntentProposal],
    decisions: Sequence[ProposalDecisionRecord],
    clarifications: Sequence[ClarificationEvent],
    history: Sequence[ChangeSet],
    actor: str,
    principals: frozenset[str],
) -> _VisibleSnapshot:
    if type(actor) is not str or not actor or actor != config.local_actor:
        raise ValueError("invalid assessment actor")
    visible_evidence = tuple(
        sorted(
            (record for record in evidence if evidence_allowed(record, principals)),
            key=lambda record: record.id,
        )
    )
    visible_evidence_ids = frozenset(record.id for record in visible_evidence)
    nodes = tuple(
        sorted(
            (
                node
                for node in graph.nodes
                if set(node.evidence_refs).issubset(visible_evidence_ids)
            ),
            key=lambda node: node.id,
        )
    )
    node_ids = frozenset(node.id for node in nodes)
    edges = tuple(
        sorted(
            (edge for edge in graph.edges if edge.from_id in node_ids and edge.to_id in node_ids),
            key=lambda edge: edge.id,
        )
    )
    edge_ids = frozenset(edge.id for edge in edges)
    graph_projection = graph.model_copy(update={"nodes": nodes, "edges": edges})
    base_references = frozenset({*visible_evidence_ids, *node_ids, *edge_ids})
    case_candidates = tuple(
        sorted(
            (
                case
                for case in cases
                if set(case.all_evidence_refs).issubset(visible_evidence_ids)
                and case.subject_ref in base_references
                and set(case.affected_refs).issubset(base_references)
            ),
            key=lambda case: case.id,
        )
    )
    clarification_candidates = tuple(
        sorted(
            (
                event
                for event in clarifications
                if _clarification_evidence_refs(event).issubset(visible_evidence_ids)
            ),
            key=lambda event: event.id,
        )
    )
    visible_cases = case_candidates
    visible_proposals = tuple(proposals)
    visible_decisions = tuple(decisions)
    visible_clarifications = clarification_candidates
    visible_history = tuple(history)
    while True:
        visible_history_ids = frozenset(changeset.id for changeset in visible_history)
        closed_cases = tuple(
            case
            for case in case_candidates
            if case.resolved_by_changeset is None
            or case.resolved_by_changeset in visible_history_ids
        )
        visible_case_ids = frozenset(case.id for case in closed_cases)
        visible_clarification_ids = frozenset(event.id for event in visible_clarifications)
        visible_references = frozenset(
            {
                *base_references,
                *visible_case_ids,
                *visible_clarification_ids,
            }
        )
        closed_proposals = tuple(
            sorted(
                (
                    proposal
                    for proposal in proposals
                    if _proposal_visible(
                        proposal,
                        visible_evidence_ids=visible_evidence_ids,
                        visible_references=visible_references,
                    )
                ),
                key=lambda proposal: proposal.id,
            )
        )
        visible_proposal_ids = frozenset(proposal.id for proposal in closed_proposals)
        closed_history = tuple(
            sorted(
                (
                    changeset
                    for changeset in history
                    if _history_visible(
                        changeset,
                        visible_evidence_ids=visible_evidence_ids,
                        visible_references=visible_references,
                    )
                ),
                key=lambda changeset: changeset.id,
            )
        )
        closed_history_ids = frozenset(changeset.id for changeset in closed_history)
        closed_decisions = tuple(
            sorted(
                (
                    decision
                    for decision in decisions
                    if _decision_visible(
                        decision,
                        visible_proposal_ids=visible_proposal_ids,
                        visible_graph_ids=node_ids,
                        visible_case_ids=visible_case_ids,
                        visible_history_ids=closed_history_ids,
                    )
                ),
                key=lambda decision: decision.id,
            )
        )
        visible_decision_ids = frozenset(decision.id for decision in closed_decisions)
        closed_clarifications = tuple(
            event
            for event in clarification_candidates
            if _clarification_visible(
                event,
                visible_clarification_ids=visible_clarification_ids,
                visible_proposal_ids=visible_proposal_ids,
                visible_decision_ids=visible_decision_ids,
                visible_history_ids=closed_history_ids,
            )
        )
        if (
            closed_cases == visible_cases
            and closed_proposals == visible_proposals
            and closed_decisions == visible_decisions
            and closed_clarifications == visible_clarifications
            and closed_history == visible_history
        ):
            break
        visible_cases = closed_cases
        visible_proposals = closed_proposals
        visible_decisions = closed_decisions
        visible_clarifications = closed_clarifications
        visible_history = closed_history
    return _VisibleSnapshot(
        graph=graph_projection,
        evidence=visible_evidence,
        ingestions=_visible_ingestions(ingestions, visible_evidence_ids),
        cases=visible_cases,
        clarifications=visible_clarifications,
        history=visible_history,
        principals=principals,
    )


def _snapshot_model(
    *,
    config: ProjectConfig,
    graph: Graph,
    evidence: tuple[EvidenceRecord, ...],
    ingestions: tuple[EvidenceIngestion, ...],
    cases: tuple[ReconciliationCase, ...],
    clarifications: tuple[ClarificationEvent, ...],
    history: tuple[ChangeSet, ...],
    principals: frozenset[str],
) -> AssessmentSnapshot:
    graph_digest = _digest(_model_material(graph))
    evidence_digest = _digest([_model_material(item) for item in evidence])
    ingestion_digest = _digest([_model_material(item) for item in ingestions])
    case_digest = _digest([_model_material(item) for item in cases])
    clarification_digest = _digest([_model_material(item) for item in clarifications])
    history_digest = _digest([_model_material(item) for item in history])
    config_digest = _digest(_model_material(config))
    principal_projection_digest = _digest(sorted(principals))
    components = {
        "case": case_digest,
        "clarification": clarification_digest,
        "config": config_digest,
        "evidence": evidence_digest,
        "graph": graph_digest,
        "history": history_digest,
        "ingestion": ingestion_digest,
        "principal_projection": principal_projection_digest,
    }
    return AssessmentSnapshot(
        project_id=config.project_id,
        graph=graph,
        evidence=evidence,
        ingestions=ingestions,
        cases=cases,
        clarifications=clarifications,
        history=history,
        graph_digest=graph_digest,
        evidence_digest=evidence_digest,
        ingestion_digest=ingestion_digest,
        case_digest=case_digest,
        clarification_digest=clarification_digest,
        history_digest=history_digest,
        config_digest=config_digest,
        principal_projection_digest=principal_projection_digest,
        aggregate_digest=_digest(components),
        omitted_count=None,
    )


def _authenticate_preimages(
    runtime: AssessmentRuntimeProtocol,
    extras: Mapping[str, SecureFile],
    held: LocalTransactionSnapshot,
) -> None:
    with runtime.transactions.read_transaction_without_recovery(
        extras,
        extra_read_policies=_AUTHORITY_POLICIES,
    ) as transaction:
        for name in (*sorted(_ASSESSMENT_TARGETS), *sorted(extras)):
            if transaction.read_optional(name) != held.content.get(name):
                raise ValueError("assessment snapshot changed")


def _actor_principals(
    actor: str,
    config: ProjectConfig,
    policy_content: bytes | None,
) -> frozenset[str]:
    if type(actor) is not str or not actor or actor != config.local_actor:
        raise ValueError("invalid assessment actor")
    if policy_content is None:
        return frozenset({actor})
    policy = MutationPolicy.model_validate(load_strict_yaml_mapping_bytes(policy_content))
    aliases = policy.identities.get(actor)
    if aliases is None or actor not in aliases:
        raise ValueError("assessment actor aliases unavailable")
    return frozenset(aliases)


def _build_assessment_snapshot(
    runtime: AssessmentRuntimeProtocol,
    actor: str,
) -> AssessmentSnapshot:
    if not _ASSESSMENT_TARGETS.issubset(runtime.transactions.target_names):
        raise ValueError("assessment transaction targets unavailable")
    config_file = runtime.workspace_directory.file("config.yaml")
    acl_policy_file = runtime.workspace_directory.file("approvals/policy.yaml")
    extras = {"acl_policy": acl_policy_file, "config": config_file}
    held: LocalTransactionSnapshot | None = None
    parsed: _ParsedSnapshot | None = None
    projection: _VisibleSnapshot | None = None
    try:
        held = runtime.transactions.snapshot_without_recovery(
            extras,
            extra_read_policies=_AUTHORITY_POLICIES,
            target_names=_ASSESSMENT_TARGETS,
        )
        parsed = _parse_held(held)
        config = parsed.config
        if config != runtime.config:
            raise ValueError("assessment runtime config changed")
        principals = _actor_principals(actor, config, held.content.get("acl_policy"))
        projection = _visible_projection(
            config=config,
            graph=parsed.graph,
            evidence=parsed.evidence,
            ingestions=parsed.ingestions,
            cases=parsed.cases,
            proposals=parsed.proposals,
            decisions=parsed.decisions,
            clarifications=parsed.clarifications,
            history=parsed.history,
            actor=actor,
            principals=principals,
        )
        _authenticate_preimages(runtime, extras, held)
        return _snapshot_model(
            config=config,
            graph=projection.graph,
            evidence=projection.evidence,
            ingestions=projection.ingestions,
            cases=projection.cases,
            clarifications=projection.clarifications,
            history=projection.history,
            principals=projection.principals,
        )
    finally:
        held = None
        parsed = None
        projection = None
        actor = ""
        acl_policy_file.close()
        config_file.close()


def _canonical_authority_state(
    runtime: AssessmentRuntimeProtocol,
) -> tuple[dict[str, object], BaseException | None, tuple[BaseException, ...]]:
    canonical_files: list[SecureFile] = []
    primary: BaseException | None = None
    cleanups: list[BaseException] = []
    canonical_keys: dict[str, object] = {}
    try:
        canonical_files = [
            runtime.workspace_directory.file("config.yaml"),
            runtime.workspace_directory.file("approvals/policy.yaml"),
        ]
        canonical_keys = {
            "config": canonical_files[0].lock_key,
            "acl_policy": canonical_files[1].lock_key,
        }
    except BaseException as caught:  # noqa: BLE001 - defer through complete cleanup
        primary = caught
    finally:
        for canonical_file in reversed(canonical_files):
            try:
                canonical_file.close()
            except BaseException as caught:  # noqa: BLE001 - close every descriptor
                cleanups.append(caught)
        canonical_files.clear()
    return canonical_keys, primary, tuple(cleanups)


def _build_assessment_snapshot_from_transaction(
    runtime: AssessmentRuntimeProtocol,
    actor: str,
    transaction: LocalTransaction,
) -> AssessmentSnapshot:
    canonical_keys, primary, cleanups = _canonical_authority_state(runtime)
    cleanup_cancellation = next(
        (item for item in cleanups if not isinstance(item, Exception)), None
    )
    selected = (
        primary
        if primary is not None and not isinstance(primary, Exception)
        else cleanup_cancellation
        if cleanup_cancellation is not None
        else primary
        if primary is not None
        else cleanups[0]
        if cleanups
        else None
    )
    for signal in (*cleanups, *((primary,) if primary is not None else ())):
        _scrub_signal(signal)
    cleanups = ()
    primary = None
    cleanup_cancellation = None
    signal = None
    if selected is not None:
        _raise_signal(selected)
    if (
        type(transaction) is not LocalTransaction
        or not runtime.transactions.owns_active_write_transaction(transaction)
        or transaction._read_only
        or set(transaction._extras) != set(_AUTHORITY_POLICIES)
        or transaction._extra_read_policies != _AUTHORITY_POLICIES
        or {name: transaction._extras[name].lock_key for name in sorted(_AUTHORITY_POLICIES)}
        != canonical_keys
        or not _ASSESSMENT_TARGETS.issubset(runtime.transactions.target_names)
    ):
        raise ValueError("invalid assessment transaction")
    held: LocalTransactionSnapshot | None = None
    parsed: _ParsedSnapshot | None = None
    projection: _VisibleSnapshot | None = None
    try:
        names = (*sorted(_ASSESSMENT_TARGETS), *sorted(_AUTHORITY_POLICIES))
        held = LocalTransactionSnapshot(
            MappingProxyType({name: transaction.read_optional(name) for name in names}),
            False,
        )
        parsed = _parse_held(held)
        config = parsed.config
        if config != runtime.config:
            raise ValueError("assessment runtime config changed")
        principals = _actor_principals(actor, config, held.content.get("acl_policy"))
        projection = _visible_projection(
            config=config,
            graph=parsed.graph,
            evidence=parsed.evidence,
            ingestions=parsed.ingestions,
            cases=parsed.cases,
            proposals=parsed.proposals,
            decisions=parsed.decisions,
            clarifications=parsed.clarifications,
            history=parsed.history,
            actor=actor,
            principals=principals,
        )
        return _snapshot_model(
            config=config,
            graph=projection.graph,
            evidence=projection.evidence,
            ingestions=projection.ingestions,
            cases=projection.cases,
            clarifications=projection.clarifications,
            history=projection.history,
            principals=projection.principals,
        )
    finally:
        held = None
        parsed = None
        projection = None
        actor = ""


def _raise_signal(signal: BaseException) -> None:
    raise signal.with_traceback(None)


def _scrub_signal(signal: BaseException) -> BaseException:
    old_traceback = signal.__traceback__
    signal.args = ()
    signal.__dict__.clear()
    signal.__traceback__ = None
    signal.__cause__ = None
    signal.__context__ = None
    if old_traceback is not None:
        traceback.clear_frames(old_traceback)
    old_traceback = None
    return signal


def build_assessment_snapshot(
    runtime: AssessmentRuntimeProtocol,
    actor: str,
) -> AssessmentSnapshot:
    """Return one immutable visible projection or a fixed, context-free failure."""
    result: AssessmentSnapshot | None = None
    signal: BaseException | None = None
    failed = False
    try:
        result = _build_assessment_snapshot(runtime, actor)
    except Exception as caught:  # noqa: BLE001 - expose one fixed assessment boundary
        _scrub_signal(caught)
        failed = True
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        signal = _scrub_signal(caught)
    finally:
        runtime = cast("AssessmentRuntimeProtocol", None)
        actor = ""
    if signal is not None:
        caught_signal = signal
        signal = None
        _raise_signal(caught_signal)
    if failed or result is None:
        raise AssessmentUnavailable() from None
    return result


def build_assessment_snapshot_from_transaction(
    runtime: AssessmentRuntimeProtocol,
    actor: str,
    transaction: LocalTransaction,
) -> AssessmentSnapshot:
    """Build a visible assessment from one caller-held authenticated transaction."""
    result: AssessmentSnapshot | None = None
    signal: BaseException | None = None
    failed = False
    try:
        result = _build_assessment_snapshot_from_transaction(runtime, actor, transaction)
    except Exception as caught:  # noqa: BLE001 - expose one fixed assessment boundary
        _scrub_signal(caught)
        failed = True
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        signal = _scrub_signal(caught)
    finally:
        runtime = cast("AssessmentRuntimeProtocol", None)
        transaction = cast("LocalTransaction", None)
        actor = ""
    if signal is not None:
        caught_signal = signal
        signal = None
        _raise_signal(caught_signal)
    if failed or result is None:
        raise AssessmentUnavailable() from None
    return result


__all__ = [
    "AssessmentUnavailable",
    "build_assessment_snapshot",
    "build_assessment_snapshot_from_transaction",
]
