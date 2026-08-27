"""Deterministic validation of untrusted coding-agent task classifications."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, field_validator

from intent_engineering.context.provider import ContextProvider
from intent_engineering.core.models import (
    EvidenceIngestion,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    JsonValue,
    NodeType,
    ProjectConfig,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    SourceMode,
    SourceRoleAssignment,
    is_nonterminal_case_status,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import evidence_allowed, refs_allowed
from intent_engineering.intent_workflow.models import (
    IntentProposal,
    PreflightResult,
    ProposalDecisionRecord,
    ProposalKind,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.proposal_store import (
    IntentLedgerRecord,
    serialize_intent_ledger_record,
)
from intent_engineering.storage.jsonl.case_store import (
    parse_case_versions,
    validate_case_appends,
)
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import (
    LocalTransactionCoordinator,
    LocalTransactionSnapshot,
)
from intent_engineering.storage.yaml.graph_store import parse_graph

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^task:sha256:[0-9a-f]{64}$")
_MAX_IDENTIFIERS = 256
_MAX_TEXT_ITEMS = 64
_MAX_TEXT_BYTES = 4096
_MAX_IDENTIFIER_BYTES = 2048


class PreflightError(ValueError):
    """One fixed public failure for rejected or unavailable task preflight."""

    def __init__(self) -> None:
        super().__init__("intent preflight unavailable")


class _SnapshotChanged(ValueError):
    """Internal marker for a case-append preimage race eligible for exact replay."""


class _PreflightModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


def _bounded_text(value: str, *, allow_empty: bool = False) -> str:
    if (
        type(value) is not str
        or (not allow_empty and not value)
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_TEXT_BYTES
    ):
        raise ValueError("invalid classifier text")
    return value


def _canonical_collection(
    values: tuple[str, ...],
    *,
    maximum: int,
    text: bool,
) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > maximum:
        raise ValueError("invalid classifier collection")
    checked: list[str] = []
    for value in values:
        if text:
            checked.append(_bounded_text(value))
        elif (
            type(value) is not str
            or not value
            or _CONTROL.search(value)
            or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
        ):
            raise ValueError("invalid classifier identifier")
        else:
            checked.append(value)
    if len(checked) != len(set(checked)):
        raise ValueError("duplicate classifier collection item")
    return tuple(sorted(checked))


class AgentClassificationSubmission(_PreflightModel):
    """Strict detached classifier proposal; deterministic code remains authoritative."""

    schema_version: Literal[1] = 1
    task_id: str
    task_digest: str
    graph_version: Annotated[int, Field(ge=0)]
    classification: TaskClassification
    basis: str
    relevant_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_IDENTIFIERS)] = ()
    evidence_refs: Annotated[tuple[str, ...], Field(max_length=_MAX_IDENTIFIERS)] = ()
    agent_evidence_ref: str
    semantic_effects: Annotated[tuple[str, ...], Field(max_length=_MAX_TEXT_ITEMS)] = ()
    uncertainties: Annotated[tuple[str, ...], Field(max_length=_MAX_TEXT_ITEMS)] = ()
    questions: Annotated[tuple[str, ...], Field(max_length=_MAX_TEXT_ITEMS)] = ()
    conflict_claims: Annotated[tuple[str, ...], Field(max_length=_MAX_TEXT_ITEMS)] = ()
    requested_scope: Annotated[tuple[str, ...], Field(max_length=_MAX_IDENTIFIERS)] = ()

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if type(value) is not str or _TASK_ID.fullmatch(value) is None:
            raise ValueError("invalid task identifier")
        return value

    @field_validator("task_digest")
    @classmethod
    def validate_task_digest(cls, value: str) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise ValueError("invalid task digest")
        return value

    @field_validator("basis")
    @classmethod
    def validate_basis(cls, value: str) -> str:
        return _bounded_text(value)

    @field_validator(
        "relevant_node_ids",
        "evidence_refs",
        "requested_scope",
    )
    @classmethod
    def canonicalize_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_collection(values, maximum=_MAX_IDENTIFIERS, text=False)

    @field_validator("agent_evidence_ref")
    @classmethod
    def validate_agent_evidence_ref(cls, value: str) -> str:
        return _canonical_collection((value,), maximum=1, text=False)[0]

    @field_validator("semantic_effects", "uncertainties", "questions", "conflict_claims")
    @classmethod
    def canonicalize_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_collection(values, maximum=_MAX_TEXT_ITEMS, text=True)


def classification_evidence_content(
    *,
    task_id: str,
    task_digest: str,
    graph_version: int,
    classification: TaskClassification,
    basis: str,
    relevant_node_ids: Sequence[str] = (),
    evidence_refs: Sequence[str] = (),
    semantic_effects: Sequence[str] = (),
    uncertainties: Sequence[str] = (),
    questions: Sequence[str] = (),
    conflict_claims: Sequence[str] = (),
    requested_scope: Sequence[str] = (),
) -> dict[str, JsonValue]:
    """Return the exact detached content captured for an agent classification turn."""
    return {
        "basis": basis,
        "classification": classification.value,
        "conflict_claims": list(conflict_claims),
        "evidence_refs": list(evidence_refs),
        "graph_version": graph_version,
        "questions": list(questions),
        "relevant_node_ids": list(relevant_node_ids),
        "requested_scope": list(requested_scope),
        "semantic_effects": list(semantic_effects),
        "task_digest": task_digest,
        "task_id": task_id,
        "uncertainties": list(uncertainties),
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parse_proposals(content: bytes) -> tuple[IntentProposal, ...]:
    if content and not content.endswith(b"\n"):
        raise ValueError("invalid intent proposal ledger")
    proposals: dict[str, IntentProposal] = {}
    decisions: dict[str, ProposalDecisionRecord] = {}
    for expected_sequence, encoded in enumerate(content.splitlines(keepends=True)):
        if not encoded.endswith(b"\n") or not encoded.strip():
            raise ValueError("invalid intent proposal ledger")
        payload = loads_strict_object(encoded[:-1].decode("utf-8"))
        if _canonical_json(payload) + b"\n" != encoded:
            raise ValueError("invalid intent proposal ledger")
        record = IntentLedgerRecord.model_validate_json(encoded[:-1])
        if (
            serialize_intent_ledger_record(record) != encoded
            or record.sequence != expected_sequence
        ):
            raise ValueError("invalid intent proposal ledger")
        if record.clarification is not None:
            continue
        if record.proposal is not None:
            if record.proposal.id in proposals:
                raise ValueError("invalid intent proposal ledger")
            proposals[record.proposal.id] = record.proposal
            continue
        decision = record.decision
        if decision is None:
            raise ValueError("invalid intent proposal ledger")
        proposal = proposals.get(decision.proposal_id)
        if (
            proposal is None
            or decision.proposal_digest != proposal.digest
            or decision.baseline_graph_version != proposal.baseline_graph_version
            or decision.proposal_id in decisions
        ):
            raise ValueError("invalid intent proposal ledger")
        decisions[decision.proposal_id] = decision
    return tuple(proposal for proposal_id, proposal in proposals.items() if proposal_id not in decisions)


def _snapshot_state(
    snapshot: LocalTransactionSnapshot,
) -> tuple[
    ProjectConfig,
    Graph,
    tuple[EvidenceRecord, ...],
    tuple[EvidenceIngestion, ...],
    tuple[ReconciliationCase, ...],
    tuple[IntentProposal, ...],
]:
    config_content = snapshot.content.get("config")
    graph_content = snapshot.content.get("graph")
    evidence_content = snapshot.content.get("evidence")
    cases_content = snapshot.content.get("cases")
    proposals_content = snapshot.content.get("intent_proposals")
    if (
        config_content is None
        or graph_content is None
        or evidence_content is None
        or cases_content is None
        or proposals_content is None
    ):
        raise ValueError("missing preflight snapshot")
    loaded_config = yaml.safe_load(config_content.decode("utf-8"))
    if not isinstance(loaded_config, dict):
        raise TypeError("invalid project config")
    config = ProjectConfig.model_validate_json(
        json.dumps(
            cast(dict[str, Any], loaded_config),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    graph = parse_graph(graph_content)
    evidence, ingestions, _ = parse_evidence_lines(evidence_content)
    case_versions = parse_case_versions(cases_content)
    latest_cases = {case.id: case for case in case_versions}
    cases = tuple(latest_cases[case_id] for case_id in sorted(latest_cases))
    proposals = _parse_proposals(proposals_content)
    return config, graph, evidence, ingestions, cases, proposals


def _principals(values: frozenset[str]) -> frozenset[str]:
    if (
        type(values) is not frozenset
        or not values
        or len(values) > _MAX_IDENTIFIERS
        or any(type(value) is not str or not value or _CONTROL.search(value) for value in values)
    ):
        raise ValueError("invalid authenticated principals")
    return frozenset(values)


PrincipalResolver = Callable[
    [ProjectConfig, LocalTransactionSnapshot],
    frozenset[str],
]


def _project_principals(
    config: ProjectConfig,
    _snapshot: LocalTransactionSnapshot,
) -> frozenset[str]:
    """Derive the default authenticated identity solely from held project policy."""
    return frozenset({config.local_actor})


def _turn_ingestion(
    evidence_id: str,
    ingestions: Sequence[EvidenceIngestion],
    connector_id: str,
) -> EvidenceIngestion:
    matches = tuple(
        item
        for item in ingestions
        if item.connector_id == connector_id and item.evidence.id == evidence_id
    )
    if len(matches) != 1:
        raise ValueError("conversation turn is unavailable")
    return matches[0]


def _case_fingerprint(material: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(material)).hexdigest()


def _case_identity(case: ReconciliationCase) -> dict[str, object]:
    """Return the exact immutable case semantics eligible for stable replay."""
    return case.model_dump(
        mode="json",
        exclude={"status", "resolution", "resolved_by_changeset", "history"},
    )


def _raise_signal(signal: BaseException) -> None:
    raise signal.with_traceback(None)


class PreflightService:
    """Validate one untrusted classification against one consistent local snapshot."""

    def __init__(
        self,
        *,
        transactions: LocalTransactionCoordinator,
        config_file: SecureFile,
        agent_principal: str,
        conversation_connector_id: str = "conversation:codex",
        principal_resolver: PrincipalResolver | None = None,
    ) -> None:
        required = {"graph", "evidence", "cases", "intent_proposals"}
        if not required.issubset(transactions.target_names):
            raise ValueError("preflight transaction targets are unavailable")
        self._transactions = transactions
        self._config_file = config_file.duplicate()
        self._agent_principal = _bounded_text(agent_principal)
        self._conversation_connector_id = _bounded_text(conversation_connector_id)
        self._principal_resolver = principal_resolver or _project_principals

    def _validate_turns(
        self,
        envelope: TaskEnvelope,
        submission: AgentClassificationSubmission,
        evidence: tuple[EvidenceRecord, ...],
        ingestions: tuple[EvidenceIngestion, ...],
        principals: frozenset[str],
    ) -> tuple[EvidenceRecord, EvidenceRecord]:
        human_entry = _turn_ingestion(
            envelope.request_evidence_ref,
            ingestions,
            self._conversation_connector_id,
        )
        agent_entry = _turn_ingestion(
            submission.agent_evidence_ref,
            ingestions,
            self._conversation_connector_id,
        )
        human = human_entry.evidence
        agent = agent_entry.evidence
        agent_payload = cast(dict[str, JsonValue], agent.model_dump(mode="json")["payload"])
        expected_agent_content = classification_evidence_content(
            task_id=submission.task_id,
            task_digest=submission.task_digest,
            graph_version=submission.graph_version,
            classification=submission.classification,
            basis=submission.basis,
            relevant_node_ids=submission.relevant_node_ids,
            evidence_refs=submission.evidence_refs,
            semantic_effects=submission.semantic_effects,
            uncertainties=submission.uncertainties,
            questions=submission.questions,
            conflict_claims=submission.conflict_claims,
            requested_scope=submission.requested_scope,
        )
        if (
            human.connector_type != "conversation"
            or agent.connector_type != "conversation"
            or human.external_object_id != envelope.conversation_ref
            or agent.external_object_id != envelope.conversation_ref
            or human.source_locator != envelope.conversation_ref
            or agent.source_locator != envelope.conversation_ref
            or human.author != envelope.actor
            or agent.author != self._agent_principal
            or human.observed_at != envelope.created_at
            or agent.observed_at < human.observed_at
            or human.payload.get("role") != "human"
            or human.payload.get("content") != envelope.request
            or agent_payload.get("role") != "agent"
            or agent_payload.get("content") != expected_agent_content
            or human.acl != agent.acl
            or agent_entry.predecessor_id != human.id
            or agent_entry.sequence != human_entry.sequence + 1
            or not evidence_allowed(human, principals)
            or not evidence_allowed(agent, principals)
            or human.id == agent.id
        ):
            raise ValueError("invalid conversation evidence")
        evidence_index = {record.id: record for record in evidence}
        if evidence_index.get(human.id) != human or evidence_index.get(agent.id) != agent:
            raise ValueError("invalid conversation evidence")
        return human, agent

    def _proposal_index(
        self,
        proposals: Sequence[IntentProposal],
        graph: Graph,
        evidence: tuple[EvidenceRecord, ...],
        ingestions: tuple[EvidenceIngestion, ...],
        principals: frozenset[str],
        config: ProjectConfig,
    ) -> dict[str, IntentProposal]:
        result: dict[str, IntentProposal] = {}
        evidence_index = {record.id: record for record in evidence}
        connectors: dict[str, set[str]] = {}
        for ingestion in ingestions:
            connectors.setdefault(ingestion.evidence.id, set()).add(ingestion.connector_id)
        for proposal in proposals:
            candidates = proposal.changeset.nodes_added
            candidate_ids = tuple(node.id for node in candidates)
            core_ids = set(proposal.core_node_ids)
            provisional_ids = set(proposal.provisional_node_ids)
            declared_ids = core_ids | provisional_ids
            allowed_types = (
                {NodeType.REQUIREMENT, NodeType.CAPABILITY, NodeType.ACCEPTANCE_CRITERION}
                if proposal.kind is ProposalKind.REQUIREMENT
                else None
            )
            if (
                proposal.baseline_graph_version != graph.version
                or proposal.proposed_by != self._agent_principal
                or proposal.changeset.baseline_graph_version != graph.version
                or proposal.changeset.actor != proposal.proposed_by
                or proposal.changeset.timestamp != proposal.proposed_at
                or len(candidate_ids) != len(set(candidate_ids))
                or core_ids & provisional_ids
                or declared_ids != set(candidate_ids)
                or proposal.changeset.evidence_refs != proposal.evidence_refs
                or not proposal.evidence_refs
                or len(proposal.evidence_refs) != len(set(proposal.evidence_refs))
                or not refs_allowed(proposal.evidence_refs, evidence, principals)
                or (proposal.kind is ProposalKind.BOOTSTRAP and not core_ids)
                or proposal.changeset.nodes_updated
                or proposal.changeset.nodes_superseded
                or proposal.changeset.edges_updated
                or proposal.changeset.edges_superseded
                or proposal.changeset.confidence_changes
                or proposal.changeset.implementation_status_changes
                or proposal.changeset.reconciliation_cases_created
                or proposal.changeset.reconciliation_cases_resolved
                or proposal.changeset.validation_status != "validated"
            ):
                raise ValueError("invalid provisional association")
            configured_roles = set(config.source_roles)
            submitted_roles = set(proposal.source_roles)
            if (
                (proposal.kind is ProposalKind.BOOTSTRAP and not submitted_roles)
                or not submitted_roles.issubset(configured_roles)
            ):
                raise ValueError("invalid provisional source role")
            used_roles: set[SourceRoleAssignment] = set()
            for evidence_id in proposal.evidence_refs:
                record = evidence_index[evidence_id]
                matched = {
                    role
                    for connector_id in connectors.get(evidence_id, set())
                    if (
                        role := self._configured_role(
                            config,
                            connector_id,
                            record.source_locator,
                        )
                    )
                    is not None
                    and role in submitted_roles
                }
                if submitted_roles and not matched:
                    raise ValueError("invalid provisional source association")
                used_roles.update(matched)
            if used_roles != submitted_roles:
                raise ValueError("invalid provisional source association")
            known_node_ids = {node.id for node in graph.nodes} | set(candidate_ids)
            for node in candidates:
                graph.type_registry.assert_registered(node.type)
                if (
                    node.status != "proposed"
                    or node.source_mode is not SourceMode.INFERRED
                    or not node.evidence_refs
                    or len(node.evidence_refs) != len(set(node.evidence_refs))
                    or not set(node.evidence_refs).issubset(proposal.evidence_refs)
                    or not refs_allowed(node.evidence_refs, evidence, principals)
                    or node.created_by != proposal.proposed_by
                    or node.last_modified_by != proposal.proposed_by
                    or node.created_at != proposal.proposed_at
                    or node.last_modified_at != proposal.proposed_at
                    or node.last_reassessed_at != proposal.proposed_at
                    or node.intent_fidelity_confidence is None
                    or not node.confidence_basis
                    or (allowed_types is not None and node.type not in allowed_types)
                ):
                    raise ValueError("invalid provisional candidate")
            for edge in proposal.changeset.edges_added:
                if (
                    edge.status != "proposed"
                    or edge.from_id not in known_node_ids
                    or edge.to_id not in known_node_ids
                    or edge.created_by != proposal.proposed_by
                    or edge.last_modified_by != proposal.proposed_by
                    or edge.created_at != proposal.proposed_at
                    or edge.last_modified_at != proposal.proposed_at
                ):
                    raise ValueError("invalid provisional edge")
            for node_id in proposal.provisional_node_ids:
                if node_id in result:
                    raise ValueError("duplicate provisional identity")
                result[node_id] = proposal
        return result

    @staticmethod
    def _configured_role(
        config: ProjectConfig,
        connector_id: str,
        locator: str,
    ) -> SourceRoleAssignment | None:
        assignments = tuple(
            assignment
            for assignment in config.source_roles
            if assignment.connector_id == connector_id
        )
        exact = tuple(assignment for assignment in assignments if assignment.scope == locator)
        if exact:
            return exact[0]
        inherited = tuple(
            assignment
            for assignment in assignments
            if assignment.inherited
            and locator.startswith(f"{assignment.scope.rstrip('/')}/")
        )
        return max(inherited, key=lambda assignment: len(assignment.scope), default=None)

    def _validate_references(
        self,
        submission: AgentClassificationSubmission,
        graph: Graph,
        evidence: tuple[EvidenceRecord, ...],
        proposals: tuple[IntentProposal, ...],
        principals: frozenset[str],
        config: ProjectConfig,
        ingestions: tuple[EvidenceIngestion, ...],
    ) -> tuple[tuple[object, ...], tuple[str, ...]]:
        active = {node.id: node for node in graph.nodes if node.status == "active"}
        provisional = self._proposal_index(
            proposals,
            graph,
            evidence,
            ingestions,
            principals,
            config,
        )
        selected: list[object] = []
        permitted_evidence: set[str] = set()
        for node_id in submission.relevant_node_ids:
            node = active.get(node_id)
            proposal = provisional.get(node_id)
            if node is not None:
                if not refs_allowed(node.evidence_refs, evidence, principals):
                    raise ValueError("unavailable semantic identity")
                selected.append(node)
                permitted_evidence.update(node.evidence_refs)
                continue
            if proposal is not None:
                if not refs_allowed(proposal.evidence_refs, evidence, principals):
                    raise ValueError("unavailable provisional identity")
                selected.append(proposal)
                permitted_evidence.update(proposal.evidence_refs)
                continue
            raise ValueError("unavailable semantic identity")
        if not refs_allowed(submission.evidence_refs, evidence, principals):
            raise ValueError("unavailable cited evidence")
        if not set(submission.evidence_refs).issubset(permitted_evidence):
            raise ValueError("foreign cited evidence")
        return tuple(selected), tuple(sorted(permitted_evidence))

    @staticmethod
    def _blocking_cases(
        cases: Sequence[ReconciliationCase], relevant_node_ids: tuple[str, ...]
    ) -> tuple[ReconciliationCase, ...]:
        relevant = set(relevant_node_ids)
        return tuple(
            case
            for case in cases
            if is_nonterminal_case_status(case.status)
            and (case.subject_ref in relevant or bool(set(case.affected_refs) & relevant))
        )

    def _new_conflict_case(
        self,
        envelope: TaskEnvelope,
        submission: AgentClassificationSubmission,
        graph: Graph,
        evidence: tuple[EvidenceRecord, ...],
    ) -> ReconciliationCase:
        nodes = {node.id: node for node in graph.nodes}
        active_refs = tuple(
            sorted(
                {
                    evidence_ref
                    for node_id in submission.relevant_node_ids
                    for evidence_ref in nodes[node_id].evidence_refs
                }
            )
        )
        evidence_index = {record.id: record for record in evidence}
        active_authors = tuple(
            sorted(
                {
                    author
                    for reference in active_refs
                    if (author := evidence_index[reference].author) is not None
                }
            )
        )
        if not active_refs or not active_authors:
            raise ValueError("ungrounded conflict")
        material = {
            "active_evidence": list(active_refs),
            "claims": list(submission.conflict_claims),
            "graph_id": graph.id,
            "human_evidence": envelope.request_evidence_ref,
            "agent_evidence": submission.agent_evidence_ref,
            "relevant_node_ids": list(submission.relevant_node_ids),
        }
        fingerprint = _case_fingerprint(material)
        cross_author = envelope.actor not in active_authors
        return ReconciliationCase(
            id=f"case:preflight:{fingerprint}",
            subject_ref=submission.relevant_node_ids[0],
            case_type=(
                ReconciliationCaseType.CONFLICTING_SOURCES
                if cross_author
                else ReconciliationCaseType.POSSIBLE_INTENT_CHANGE
            ),
            affected_refs=submission.relevant_node_ids,
            evidence_sides=(
                EvidenceSide(
                    label="active_semantics",
                    claim="Current active semantic position",
                    evidence_refs=active_refs,
                    observed_at=max(evidence_index[ref].observed_at for ref in active_refs),
                    authors=active_authors,
                    confidence=0.95,
                    source_mode=SourceMode.EXPLICIT,
                ),
                EvidenceSide(
                    label="task_request",
                    claim="Task request classified as conflicting",
                    evidence_refs=(
                        envelope.request_evidence_ref,
                        submission.agent_evidence_ref,
                    ),
                    observed_at=max(
                        envelope.created_at,
                        evidence_index[submission.agent_evidence_ref].observed_at,
                    ),
                    authors=(envelope.actor, self._agent_principal),
                    confidence=1.0,
                    source_mode=SourceMode.EXPLICIT,
                ),
            ),
            detector_id="intent_workflow.preflight.v1",
            fingerprint=fingerprint,
            created_at=envelope.created_at,
            created_by=self._agent_principal,
            status=ReconciliationStatus.OPEN,
            requires_human=True,
        )

    def _create_or_reuse_case(
        self,
        snapshot: LocalTransactionSnapshot,
        cases: tuple[ReconciliationCase, ...],
        candidate: ReconciliationCase,
        evidence: tuple[EvidenceRecord, ...],
        principals: frozenset[str],
    ) -> ReconciliationCase:
        reusable = self._reusable_case(cases, candidate, evidence, principals)
        if reusable is not None:
            return reusable
        cases_content = snapshot.content.get("cases")
        if cases_content is None:
            raise ValueError("case snapshot unavailable")
        append = validate_case_appends(cases_content, (candidate,))
        try:
            with self._transactions.transaction(
                rollback_base_exceptions=True,
                extras={"config": self._config_file},
            ) as transaction:
                if any(
                    transaction.read_optional(name) != content
                    for name, content in snapshot.content.items()
                ):
                    raise _SnapshotChanged("preflight snapshot changed")
                transaction.append("cases", append)
            return candidate
        except _SnapshotChanged:
            fresh = self._transactions.snapshot({"config": self._config_file})
            if any(
                name != "cases" and fresh.content.get(name) != content
                for name, content in snapshot.content.items()
            ):
                raise ValueError("preflight snapshot changed") from None
            _, _, fresh_evidence, _, fresh_cases, _ = _snapshot_state(fresh)
            winner = self._reusable_case(
                fresh_cases,
                candidate,
                fresh_evidence,
                principals,
            )
            if winner is None:
                raise ValueError("conflict case unavailable") from None
            return winner

    @staticmethod
    def _reusable_case(
        cases: tuple[ReconciliationCase, ...],
        candidate: ReconciliationCase,
        evidence: tuple[EvidenceRecord, ...],
        principals: frozenset[str],
    ) -> ReconciliationCase | None:
        matches = tuple(
            case
            for case in cases
            if case.id == candidate.id or case.fingerprint == candidate.fingerprint
        )
        if matches:
            if (
                len(matches) != 1
                or not is_nonterminal_case_status(matches[0].status)
                or _case_identity(matches[0]) != _case_identity(candidate)
                or not refs_allowed(matches[0].all_evidence_refs, evidence, principals)
            ):
                raise ValueError("conflict case unavailable")
            return matches[0]
        return None

    def _authenticate_snapshot(self, snapshot: LocalTransactionSnapshot) -> None:
        """Fail closed unless every authorization preimage remains exact under lock."""
        with self._transactions.read_transaction({"config": self._config_file}) as transaction:
            if any(
                transaction.read_optional(name) != content
                for name, content in snapshot.content.items()
            ):
                raise ValueError("preflight snapshot changed")

    def _evaluate(
        self,
        envelope: TaskEnvelope,
        submission: AgentClassificationSubmission,
        principals: frozenset[str],
    ) -> PreflightResult:
        if type(envelope) is not TaskEnvelope or type(submission) is not AgentClassificationSubmission:
            raise ValueError("invalid preflight input")
        envelope = TaskEnvelope.model_validate_json(envelope.model_dump_json())
        submission = AgentClassificationSubmission.model_validate_json(submission.model_dump_json())
        principals = _principals(principals)
        snapshot = self._transactions.snapshot({"config": self._config_file})
        config, graph, evidence, ingestions, cases, proposals = _snapshot_state(snapshot)
        authenticated_principals = _principals(self._principal_resolver(config, snapshot))
        if (
            config.project_id != envelope.repository_id
            or envelope.actor != config.local_actor
            or envelope.actor not in principals
            or config.local_actor not in principals
            or principals != authenticated_principals
            or graph.version != envelope.graph_version
            or submission.graph_version != graph.version
            or submission.task_id != envelope.id
            or submission.task_digest != envelope.digest
            or submission.requested_scope != tuple(sorted(envelope.requested_scope))
        ):
            raise ValueError("stale preflight binding")
        human, agent = self._validate_turns(
            envelope, submission, evidence, ingestions, principals
        )
        selected, relevant_evidence = self._validate_references(
            submission,
            graph,
            evidence,
            proposals,
            principals,
            config,
            ingestions,
        )
        if not set(relevant_evidence).issubset(submission.evidence_refs):
            raise ValueError("incomplete semantic citation")
        blocking = self._blocking_cases(cases, submission.relevant_node_ids)
        authorized = False
        questions: tuple[str, ...] = ()
        review_case_id: str | None = None
        permitted_scope: tuple[str, ...] = ()
        context: dict[str, JsonValue] = {}

        match submission.classification:
            case TaskClassification.NO_SEMANTIC_IMPACT:
                if (
                    submission.semantic_effects
                    or submission.uncertainties
                    or submission.relevant_node_ids
                    or submission.conflict_claims
                    or submission.questions
                    or submission.evidence_refs
                ):
                    raise ValueError("invalid mechanical classification")
                authorized = True
                permitted_scope = submission.requested_scope
            case TaskClassification.ALIGNED:
                if (
                    not submission.relevant_node_ids
                    or any(isinstance(item, IntentProposal) for item in selected)
                    or submission.uncertainties
                    or submission.questions
                    or submission.conflict_claims
                    or blocking
                ):
                    raise ValueError("invalid aligned classification")
                provider = ContextProvider(graph, cases, config, evidence)
                pack = provider.for_refs(submission.relevant_node_ids, actor=principals)
                context = cast(dict[str, JsonValue], pack.model_dump(mode="json"))
                authorized = True
                permitted_scope = submission.requested_scope
            case TaskClassification.NEW_OR_AMBIGUOUS:
                if not submission.questions or submission.conflict_claims:
                    raise ValueError("invalid ambiguous classification")
                questions = submission.questions
            case TaskClassification.CONFLICTING:
                if (
                    not submission.relevant_node_ids
                    or not submission.conflict_claims
                    or submission.questions
                    or any(isinstance(item, IntentProposal) for item in selected)
                ):
                    raise ValueError("invalid conflicting classification")
                candidate = self._new_conflict_case(envelope, submission, graph, evidence)
                review_case_id = self._create_or_reuse_case(
                    snapshot,
                    cases,
                    candidate,
                    evidence,
                    principals,
                ).id
            case _:
                raise ValueError("invalid classification")

        result_evidence = tuple(
            dict.fromkeys((human.id, agent.id, *submission.evidence_refs))
        )
        result = PreflightResult(
            task_id=envelope.id,
            graph_version=graph.version,
            classification=submission.classification,
            authorized=authorized,
            basis=f"deterministically validated {submission.classification.value}",
            relevant_node_ids=submission.relevant_node_ids,
            evidence_refs=result_evidence,
            questions=questions,
            review_case_id=review_case_id,
            permitted_scope=permitted_scope,
            context=context,
        )
        if result.authorized:
            self._authenticate_snapshot(snapshot)
        return result

    def evaluate(
        self,
        envelope: TaskEnvelope,
        submission: AgentClassificationSubmission,
        *,
        principals: frozenset[str],
    ) -> PreflightResult:
        """Return one validated result or the fixed fail-closed public error."""
        result: PreflightResult | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._evaluate(envelope, submission, principals)
        except Exception:  # noqa: BLE001 - expose one fixed preflight boundary
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            try:
                self._transactions.recover()
            finally:
                caught.__traceback__ = None
                signal = caught
        finally:
            envelope = cast(TaskEnvelope, None)
            submission = cast(AgentClassificationSubmission, None)
            principals = frozenset()
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        if failed or result is None:
            raise PreflightError() from None
        return result


__all__ = [
    "AgentClassificationSubmission",
    "PreflightError",
    "PreflightService",
    "PrincipalResolver",
    "classification_evidence_content",
]
