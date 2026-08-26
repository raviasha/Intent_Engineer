"""Proposal-only and independently approved MCP mutation tools."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from typing import Annotated, Literal, Protocol, cast

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field, WithJsonSchema, field_validator, model_validator

from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.connectors import (
    ConnectorCatalog,
    configured_actor_principals,
    connector_catalog,
)
from intent_engineering.cli.runtime import Runtime
from intent_engineering.cli.writes import (
    WriteWorkflow,
    policy_actor_aliases,
    write_workflow,
)
from intent_engineering.core.graph.applier import apply_changeset_with_case_effects
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceRecord,
    Graph,
    JsonValue,
    ProjectConfig,
    ReconciliationStatus,
    ResolutionAction,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy import refs_allowed
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.jsonl.approval_store import JsonlApprovalStore
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile, UnsafePathError

_PROPOSE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_PREVIEW = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
_EXECUTE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)
_PROPOSAL_PATTERN = r"^changeset-proposal:sha256:[0-9a-f]{64}$"
_PLAN_PATTERN = r"^write-plan:sha256:[0-9a-f]{64}$"
_APPROVAL_PATTERN = r"^approval:sha256:[0-9a-f]{64}$"
_MAX_MUTATION_JSON_BYTES = 1_048_576
type _IdentifierInput = Annotated[
    str,
    Field(min_length=1, max_length=512),
]
type _ActionInput = Annotated[
    str,
    Field(min_length=1, max_length=64),
    WithJsonSchema(
        {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "enum": [action.value for action in ResolutionAction],
        }
    ),
]
type _ObjectInput = Annotated[object, WithJsonSchema({"type": "object"})]
type _PlanIdInput = Annotated[str, Field(pattern=_PLAN_PATTERN)]
type _ApprovalIdInput = Annotated[str, Field(pattern=_APPROVAL_PATTERN)]


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_MUTATION_JSON_BYTES:
        raise ValueError("mutation JSON is too large")
    return encoded


class ChangeSetProposal(StrictModel):
    """Immutable content-addressed proposal that has not touched canonical state."""

    model_config = ConfigDict(frozen=True, strict=True)

    schema_version: Literal[1] = 1
    id: str
    proposed_by: str
    proposed_at: datetime
    changeset: ChangeSet

    @field_validator("id")
    @classmethod
    def validate_id_shape(cls, value: str) -> str:
        if re.fullmatch(_PROPOSAL_PATTERN, value) is None:
            raise ValueError("invalid changeset proposal")
        return value

    @field_validator("proposed_by")
    @classmethod
    def validate_actor(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("invalid changeset proposal")
        return value

    @field_validator("proposed_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("invalid changeset proposal")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_identity(self) -> ChangeSetProposal:
        material = self.changeset.model_dump(mode="json", by_alias=True)
        expected = f"changeset-proposal:sha256:{sha256(_canonical_json(material)).hexdigest()}"
        if (
            self.id != expected
            or self.proposed_by != self.changeset.actor
            or self.proposed_at != self.changeset.timestamp.astimezone(UTC)
        ):
            raise ValueError("invalid changeset proposal")
        return self


def _proposal(changeset: ChangeSet) -> ChangeSetProposal:
    material = changeset.model_dump(mode="json", by_alias=True)
    return ChangeSetProposal(
        id=f"changeset-proposal:sha256:{sha256(_canonical_json(material)).hexdigest()}",
        proposed_by=changeset.actor,
        proposed_at=changeset.timestamp,
        changeset=changeset,
    )


def _targets_are_authorized(
    changeset: ChangeSet,
    graph: Graph,
    records: tuple[EvidenceRecord, ...],
    principals: frozenset[str],
    actor: str,
) -> bool:
    direct_evidence = set(changeset.evidence_refs)
    nested_evidence = {
        reference
        for references in (
            *(node.evidence_refs for node in changeset.nodes_added),
            *(update.replacement.evidence_refs for update in changeset.nodes_updated),
            *(change.evidence_refs for change in changeset.confidence_changes),
            *(change.evidence_refs for change in changeset.implementation_status_changes),
        )
        for reference in references
    }
    if not nested_evidence.issubset(direct_evidence):
        return False
    nodes = {node.id: node for node in graph.nodes}
    authorized_nodes = {
        node.id for node in graph.nodes if refs_allowed(node.evidence_refs, records, principals)
    }
    edges = {edge.id: edge for edge in graph.edges}
    added_node_ids = {node.id for node in changeset.nodes_added}
    visible_after = authorized_nodes | added_node_ids
    if changeset.reconciliation_cases_created or changeset.reconciliation_cases_resolved:
        return False
    if any(
        node.created_by != actor
        or node.last_modified_by != actor
        or node.last_modified_at != changeset.timestamp
        or not refs_allowed(node.evidence_refs, records, principals)
        for node in changeset.nodes_added
    ):
        return False
    for node_update in changeset.nodes_updated:
        current_node = nodes.get(node_update.node_id)
        if (
            current_node is None
            or node_update.node_id not in authorized_nodes
            or node_update.replacement.created_by != current_node.created_by
            or node_update.replacement.created_at != current_node.created_at
            or node_update.replacement.last_modified_by != actor
            or node_update.replacement.last_modified_at != changeset.timestamp
            or not refs_allowed(node_update.replacement.evidence_refs, records, principals)
        ):
            return False
    if any(node_id not in authorized_nodes for node_id in changeset.nodes_superseded):
        return False
    if any(
        change.subject_ref not in authorized_nodes
        or change.actor != actor
        or not refs_allowed(change.evidence_refs, records, principals)
        for change in changeset.confidence_changes
    ):
        return False
    if any(
        change.claim_id not in authorized_nodes
        or not refs_allowed(change.evidence_refs, records, principals)
        for change in changeset.implementation_status_changes
    ):
        return False
    if any(
        edge.created_by != actor
        or edge.last_modified_by != actor
        or edge.last_modified_at != changeset.timestamp
        or edge.from_id not in visible_after
        or edge.to_id not in visible_after
        for edge in changeset.edges_added
    ):
        return False
    for edge_update in changeset.edges_updated:
        current_edge = edges.get(edge_update.edge_id)
        if (
            current_edge is None
            or current_edge.from_id not in authorized_nodes
            or current_edge.to_id not in authorized_nodes
            or edge_update.replacement.created_by != current_edge.created_by
            or edge_update.replacement.created_at != current_edge.created_at
            or edge_update.replacement.last_modified_by != actor
            or edge_update.replacement.last_modified_at != changeset.timestamp
            or edge_update.replacement.from_id not in visible_after
            or edge_update.replacement.to_id not in visible_after
        ):
            return False
    return all(
        edge_id in edges
        and edges[edge_id].from_id in authorized_nodes
        and edges[edge_id].to_id in authorized_nodes
        for edge_id in changeset.edges_superseded
    )


class ChangeSetProposalStore:
    """Descriptor-safe append-only ledger for unapplied semantic proposals."""

    def __init__(self, source: SecureFile) -> None:
        self._file = source

    def _records_unlocked(self) -> dict[str, ChangeSetProposal] | None:
        try:
            content = self._file.read_bytes_nonblocking()
        except UnsafePathError:
            try:
                if not self._file.exists():
                    content = None
                else:
                    return None
            except UnsafePathError:
                return None
        if content is not None and content and not content.endswith(b"\n"):
            return None
        records: dict[str, ChangeSetProposal] = {}
        encodings: dict[str, bytes] = {}
        try:
            if content is not None:
                for line in content.splitlines():
                    if not line:
                        return None
                    payload = loads_strict_object(line.decode("utf-8"))
                    encoded = _canonical_json(payload)
                    if encoded != line:
                        return None
                    record = ChangeSetProposal.model_validate_json(encoded)
                    existing = records.get(record.id)
                    if existing is not None:
                        if existing != record or encodings[record.id] != line:
                            return None
                        continue
                    records[record.id] = record
                    encodings[record.id] = line
        except (TypeError, UnicodeError, ValueError):
            return None
        return records

    def put(self, proposal: ChangeSetProposal) -> bool:
        validated: ChangeSetProposal | None = None
        encoded: bytes | None = None
        records: dict[str, ChangeSetProposal] | None = None
        existing: ChangeSetProposal | None = None
        try:
            validated = ChangeSetProposal.model_validate_json(proposal.model_dump_json())
            encoded = _canonical_json(validated.model_dump(mode="json", by_alias=True))
            with same_path_lock(self._file):
                records = self._records_unlocked()
                if records is None:
                    raise ValueError("changeset proposal unavailable") from None
                existing = records.get(validated.id)
                if existing is not None:
                    if existing != validated:
                        raise ValueError("changeset proposal unavailable") from None
                    return False
                append_durable_line(self._file, encoded + b"\n")
            return True
        except BaseException:
            proposal = cast(ChangeSetProposal, None)
            validated = None
            encoded = None
            records = None
            existing = None
            raise

    def list(self) -> tuple[ChangeSetProposal, ...]:
        with same_path_lock(self._file):
            records = self._records_unlocked()
            if records is None:
                raise ValueError("changeset proposal unavailable") from None
            return tuple(records.values())


class MutationPort(Protocol):
    """Narrow service contract exposed to the MCP registration layer."""

    async def propose_changeset(self, changeset: object) -> dict[str, object]: ...

    async def propose_reconciliation(
        self,
        case_id: object,
        action: object,
    ) -> dict[str, object]: ...

    async def preview_write(
        self,
        case_id: object,
        connector_id: object,
        operation: object,
        fields: object,
        action: object,
    ) -> dict[str, object]: ...

    async def execute_write(
        self,
        plan_id: object,
        approval_id: object,
    ) -> dict[str, object]: ...


class McpMutationServices:
    """Production adapter over the already-reviewed local write workflow."""

    def __init__(
        self,
        runtime: Runtime,
        workflow: WriteWorkflow | None,
        proposals: ChangeSetProposalStore,
        connector_snapshot: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.workflow = workflow
        self.proposals = proposals
        self.connector_snapshot = connector_snapshot

    @staticmethod
    def _catalog_snapshot(catalog: ConnectorCatalog) -> str:
        payload = [
            {
                "config": item.config.model_dump(mode="json"),
                "profile": item.profile.model_dump(mode="json"),
            }
            for item in catalog.configured
        ]
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _request_workflow(self) -> tuple[WriteWorkflow, bool]:
        if self.workflow is not None:
            return self.workflow, False
        workflow = write_workflow(self.runtime)
        if (
            self.connector_snapshot is None
            or self._catalog_snapshot(workflow.catalog) != self.connector_snapshot
        ):
            workflow.close()
            raise ValueError("external write configuration unavailable") from None
        return workflow, True

    def _approval_exists(self, approval_id: str) -> bool:
        directory = self.runtime.workspace_directory.subdirectory("approvals")
        store: JsonlApprovalStore | None = None
        try:
            store = JsonlApprovalStore(directory.file("approvals.jsonl"))
            store.get(approval_id)
            return True
        except (KeyError, OSError, UnsafePathError, ValueError):
            return False
        finally:
            if store is not None:
                store.close()
            directory.close()

    @staticmethod
    def _release_workflow(workflow: WriteWorkflow | None, owned: bool) -> None:
        if owned and workflow is not None:
            try:
                workflow.close()
            except Exception:  # noqa: BLE001 - cleanup cannot change a completed public result
                return

    def _authorization(self) -> tuple[str, frozenset[str]] | None:
        try:
            source = self.runtime.workspace_directory.read_relative(
                "config.yaml",
                nonblocking=True,
            )
            config = ProjectConfig.model_validate(load_strict_yaml_mapping_bytes(source.content))
            actor = config.local_actor
            if (
                config.project_id != self.runtime.config.project_id
                or config.graph_path != self.runtime.config.graph_path
                or actor != self.runtime.config.local_actor
            ):
                return None
            principals = frozenset(
                {
                    actor,
                    *configured_actor_principals(self.runtime, actor=actor),
                    *policy_actor_aliases(self.runtime, actor=actor),
                }
            )
            return actor, principals
        except Exception:  # noqa: BLE001 - mutation authorization fails closed
            return None

    @staticmethod
    def _rejected(reason: str = "unavailable") -> dict[str, object]:
        return {"schema_version": "1", "status": "rejected", "reason": reason}

    @staticmethod
    def _proposed(proposal: ChangeSetProposal) -> dict[str, object]:
        return {
            "schema_version": "1",
            "status": "proposed",
            "proposal": cast(
                dict[str, object],
                proposal.model_dump(mode="json", by_alias=True),
            ),
        }

    async def propose_changeset(self, changeset: object) -> dict[str, object]:
        encoded: bytes | None = None
        candidate: ChangeSet | None = None
        proposal: ChangeSetProposal | None = None
        records: tuple[EvidenceRecord, ...] | None = None
        graph: Graph | None = None
        authorization: tuple[str, frozenset[str]] | None = None
        actor: str | None = None
        principals: frozenset[str] | None = None
        try:
            if type(changeset) is not dict:
                return self._rejected("invalid_proposal")
            encoded = _canonical_json(changeset)
            candidate = ChangeSet.model_validate_json(encoded)
            authorization = self._authorization()
            if authorization is None:
                return self._rejected("invalid_proposal")
            actor, principals = authorization
            records = self.runtime.evidence()
            graph = self.runtime.graph_store.load()
            if (
                candidate.actor != actor
                or candidate.baseline_graph_version != graph.version
                or not candidate.is_semantic
                or not refs_allowed(candidate.evidence_refs, records, principals)
                or not _targets_are_authorized(
                    candidate,
                    graph,
                    records,
                    principals,
                    actor,
                )
            ):
                return self._rejected("invalid_proposal")
            apply_changeset_with_case_effects(graph, candidate)
            proposal = _proposal(candidate)
            self.proposals.put(proposal)
            return self._proposed(proposal)
        except Exception:  # noqa: BLE001 - caller sees only the fixed rejection
            return self._rejected("invalid_proposal")
        except BaseException:
            encoded = candidate = proposal = records = graph = None
            authorization = actor = principals = None
            raise
        finally:
            changeset = None
            del changeset

    async def propose_reconciliation(
        self,
        case_id: object,
        action: object,
    ) -> dict[str, object]:
        case = None
        graph: Graph | None = None
        records: tuple[EvidenceRecord, ...] | None = None
        proposed = changeset = proposal = None
        try:
            if type(case_id) is not str or type(action) is not str:
                return self._rejected("invalid_proposal")
            selected_action = ResolutionAction(action)
            if selected_action in {
                ResolutionAction.DEFER,
                ResolutionAction.MARK_FALSE_POSITIVE,
            }:
                return self._rejected("invalid_proposal")
            case = self.runtime.case_store.get(case_id)
            graph = self.runtime.graph_store.load()
            authorization = self._authorization()
            records = self.runtime.evidence()
            authorized_node_ids = (
                set()
                if authorization is None
                else {
                    node.id
                    for node in graph.nodes
                    if refs_allowed(node.evidence_refs, records, authorization[1])
                }
            )
            if (
                authorization is None
                or case.status is not ReconciliationStatus.OPEN
                or not refs_allowed(
                    case.all_evidence_refs,
                    records,
                    authorization[1],
                )
                or case.subject_ref not in authorized_node_ids
                or any(reference not in authorized_node_ids for reference in case.affected_refs)
            ):
                return self._rejected("invalid_proposal")
            proposed, changeset, review_hash = self.runtime.resolution.resolve(
                case_id,
                selected_action,
            )
            if changeset is None or review_hash is None:
                return self._rejected("invalid_proposal")
            proposal = _proposal(changeset)
            return {
                **self._proposed(proposal),
                "case": cast(
                    dict[str, object],
                    proposed.model_dump(mode="json", by_alias=True),
                ),
                "review_hash": review_hash,
            }
        except Exception:  # noqa: BLE001 - identifiers and local state never cross the boundary
            return self._rejected("invalid_proposal")
        except BaseException:
            case = graph = records = proposed = changeset = proposal = None
            raise
        finally:
            case_id = action = None
            del case_id, action

    async def preview_write(
        self,
        case_id: object,
        connector_id: object,
        operation: object,
        fields: object,
        action: object,
    ) -> dict[str, object]:
        selected_fields: dict[str, JsonValue] | None = None
        plan = None
        workflow: WriteWorkflow | None = None
        owned_workflow = False
        try:
            if (
                self._authorization() is None
                or type(case_id) is not str
                or type(connector_id) is not str
                or type(operation) is not str
                or type(fields) is not dict
                or type(action) is not str
            ):
                return self._rejected("preview_unavailable")
            workflow, owned_workflow = self._request_workflow()
            selected_fields = cast(dict[str, JsonValue], json.loads(_canonical_json(fields)))
            plan = await workflow.create_preview(
                case_id,
                connector_id=connector_id,
                operation=operation,
                requested_fields=selected_fields,
                resolution_action=ResolutionAction(action),
            )
            return {
                "schema_version": "1",
                "status": "previewed",
                "plan": cast(dict[str, object], plan.model_dump(mode="json")),
                "plan_id": plan.id,
                "plan_hash": plan.canonical_hash,
            }
        except Exception:  # noqa: BLE001 - provider/local details are fixed at this boundary
            return self._rejected("preview_unavailable")
        except BaseException:
            selected_fields = plan = None
            raise
        finally:
            self._release_workflow(workflow, owned_workflow)
            case_id = connector_id = operation = fields = action = None
            del case_id, connector_id, operation, fields, action

    async def execute_write(
        self,
        plan_id: object,
        approval_id: object,
    ) -> dict[str, object]:
        receipt = payload = None
        workflow: WriteWorkflow | None = None
        owned_workflow = False
        try:
            if (
                type(plan_id) is not str
                or type(approval_id) is not str
                or re.fullmatch(_PLAN_PATTERN, plan_id) is None
                or re.fullmatch(_APPROVAL_PATTERN, approval_id) is None
            ):
                return self._rejected("invalid_arguments")
            if self._authorization() is None:
                return self._rejected("approval_not_found")
            if self.workflow is None and not self._approval_exists(approval_id):
                return self._rejected("approval_not_found")
            workflow, owned_workflow = self._request_workflow()
            workflow.approvals.get(approval_id)
            receipt = await workflow.execute(plan_id, approval_id)
            payload = cast(
                dict[str, object],
                receipt.model_dump(mode="json", by_alias=True),  # type: ignore[attr-defined]
            )
            return {
                "schema_version": "1",
                "status": payload["status"],
                "receipt": payload,
            }
        except KeyError:
            return self._rejected("approval_not_found")
        except Exception:  # noqa: BLE001 - executor exposes only immutable receipts or rejection
            return self._rejected("execution_unavailable")
        except BaseException:
            receipt = payload = None
            raise
        finally:
            self._release_workflow(workflow, owned_workflow)
            plan_id = approval_id = None
            del plan_id, approval_id


def load_mutation_services(runtime: Runtime) -> McpMutationServices:
    """Load guarded writes without making mutation configuration a startup requirement."""
    approvals_directory = runtime.workspace_directory.subdirectory("approvals")
    try:
        proposals = ChangeSetProposalStore(approvals_directory.file("changeset-proposals.jsonl"))
    finally:
        approvals_directory.close()
    try:
        connector_snapshot = McpMutationServices._catalog_snapshot(connector_catalog(runtime))
    except Exception:  # noqa: BLE001 - invalid startup configuration fails closed
        connector_snapshot = None
    return McpMutationServices(runtime, None, proposals, connector_snapshot)


def register_mutation_tools(server: MCPServer, services: MutationPort) -> None:
    """Register proposal, preview, and separately approved execution tools only."""

    @server.tool(
        name="intent_changeset_propose",
        annotations=_PROPOSE,
        structured_output=True,
    )
    async def changeset_propose(changeset: _ObjectInput) -> dict[str, object]:
        try:
            return await services.propose_changeset(changeset)
        finally:
            del changeset

    @server.tool(
        name="intent_reconciliation_propose",
        annotations=_PROPOSE,
        structured_output=True,
    )
    async def reconciliation_propose(
        case_id: _IdentifierInput,
        action: _ActionInput,
    ) -> dict[str, object]:
        try:
            return await services.propose_reconciliation(case_id, action)
        finally:
            del case_id, action

    @server.tool(
        name="intent_write_preview",
        annotations=_PREVIEW,
        structured_output=True,
    )
    async def write_preview(
        case_id: _IdentifierInput,
        connector_id: _IdentifierInput,
        operation: _IdentifierInput,
        fields: _ObjectInput,
        action: _ActionInput,
    ) -> dict[str, object]:
        try:
            return await services.preview_write(
                case_id,
                connector_id,
                operation,
                fields,
                action,
            )
        finally:
            del case_id, connector_id, operation, fields, action

    @server.tool(
        name="intent_write_execute",
        annotations=_EXECUTE,
        structured_output=True,
    )
    async def write_execute(
        plan_id: _PlanIdInput,
        approval_id: _ApprovalIdInput,
    ) -> dict[str, object]:
        try:
            return await services.execute_write(plan_id, approval_id)
        finally:
            del plan_id, approval_id


__all__ = [
    "ChangeSetProposal",
    "ChangeSetProposalStore",
    "McpMutationServices",
    "MutationPort",
    "load_mutation_services",
    "register_mutation_tools",
]
