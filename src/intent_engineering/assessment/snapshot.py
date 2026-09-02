"""Descriptor-held, ACL-safe acquisition for detached graph assessment."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
from intent_engineering.intent_workflow import ClarificationEvent
from intent_engineering.intent_workflow.proposal_store import (
    IntentLedgerRecord,
    serialize_intent_ledger_record,
)
from intent_engineering.storage.jsonl.case_store import parse_case_versions
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import (
    LocalTransactionExtraReadPolicy,
    LocalTransactionSnapshot,
)
from intent_engineering.storage.yaml.graph_store import parse_graph

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime

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


def _parse_clarifications(content: bytes | None) -> tuple[ClarificationEvent, ...]:
    if content is None or not content:
        return ()
    if not content.endswith(b"\n"):
        raise ValueError("invalid assessment intent ledger")
    clarifications: list[ClarificationEvent] = []
    for expected_sequence, encoded in enumerate(content.splitlines(keepends=True)):
        if not encoded.endswith(b"\n") or not encoded.strip():
            raise ValueError("invalid assessment intent ledger")
        payload = loads_strict_object(encoded[:-1].decode("utf-8"))
        if _canonical_json(payload) + b"\n" != encoded:
            raise ValueError("invalid assessment intent ledger")
        record = IntentLedgerRecord.model_validate_json(encoded[:-1])
        if (
            record.sequence != expected_sequence
            or serialize_intent_ledger_record(record) != encoded
        ):
            raise ValueError("invalid assessment intent ledger")
        if record.clarification is not None:
            clarifications.append(record.clarification)
    if len({item.id for item in clarifications}) != len(clarifications):
        raise ValueError("duplicate assessment clarification identity")
    return tuple(clarifications)


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
    clarifications = _parse_clarifications(held.content.get("intent_proposals"))
    history = _parse_history(held.content.get("history"))
    return _ParsedSnapshot(
        config=config,
        graph=graph,
        evidence=evidence,
        ingestions=ingestions,
        cases=tuple(latest_cases[item] for item in sorted(latest_cases)),
        clarifications=clarifications,
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


def _history_visible(
    changeset: ChangeSet,
    *,
    visible_evidence_ids: frozenset[str],
    visible_references: frozenset[str],
) -> bool:
    if not set(changeset.evidence_refs).issubset(visible_evidence_ids):
        return False
    if not _changeset_subjects(changeset).issubset(visible_references):
        return False
    embedded_nodes = (
        *changeset.nodes_added,
        *(update.replacement for update in changeset.nodes_updated),
    )
    if any(not set(node.evidence_refs).issubset(visible_evidence_ids) for node in embedded_nodes):
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


def _visible_projection(
    *,
    config: ProjectConfig,
    graph: Graph,
    evidence: Sequence[EvidenceRecord],
    ingestions: Sequence[EvidenceIngestion],
    cases: Sequence[ReconciliationCase],
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
    visible_cases = tuple(
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
    visible_case_ids = frozenset(case.id for case in visible_cases)
    visible_clarifications = tuple(
        sorted(
            (
                event
                for event in clarifications
                if _clarification_evidence_refs(event).issubset(visible_evidence_ids)
            ),
            key=lambda event: event.id,
        )
    )
    visible_clarification_ids = frozenset(event.id for event in visible_clarifications)
    visible_references = frozenset(
        {
            *base_references,
            *visible_case_ids,
            *visible_clarification_ids,
        }
    )
    visible_history = tuple(
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
    runtime: Runtime,
    extras: Mapping[str, SecureFile],
    held: LocalTransactionSnapshot,
) -> None:
    with runtime.transactions.read_transaction(
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


def _build_assessment_snapshot(runtime: Runtime, actor: str) -> AssessmentSnapshot:
    if not _ASSESSMENT_TARGETS.issubset(runtime.transactions.target_names):
        raise ValueError("assessment transaction targets unavailable")
    config_file = runtime.workspace_directory.file("config.yaml")
    acl_policy_file = runtime.workspace_directory.file("approvals/policy.yaml")
    extras = {"acl_policy": acl_policy_file, "config": config_file}
    held: LocalTransactionSnapshot | None = None
    parsed: _ParsedSnapshot | None = None
    projection: _VisibleSnapshot | None = None
    try:
        held = runtime.transactions.snapshot(
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


def _raise_signal(signal: BaseException) -> None:
    raise signal.with_traceback(None)


def build_assessment_snapshot(runtime: Runtime, actor: str) -> AssessmentSnapshot:
    """Return one immutable visible projection or a fixed, context-free failure."""
    result: AssessmentSnapshot | None = None
    signal: BaseException | None = None
    failed = False
    try:
        result = _build_assessment_snapshot(runtime, actor)
    except Exception:  # noqa: BLE001 - expose one fixed assessment boundary
        failed = True
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        caught.__traceback__ = None
        caught.__cause__ = None
        caught.__context__ = None
        signal = caught
    finally:
        runtime = cast("Runtime", None)
        actor = ""
    if signal is not None:
        caught_signal = signal
        signal = None
        _raise_signal(caught_signal)
    if failed or result is None:
        raise AssessmentUnavailable() from None
    return result


__all__ = ["AssessmentUnavailable", "build_assessment_snapshot"]
