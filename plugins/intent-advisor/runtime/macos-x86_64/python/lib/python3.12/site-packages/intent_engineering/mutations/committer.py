"""Atomic local commit of one successful independently approved provider write."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import cast

from intent_engineering.capture.mcp.profile_models import ProviderBinding, ProviderProfile
from intent_engineering.core.graph.applier import apply_changeset_with_case_effects
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceIngestion,
    EvidenceRecord,
    JsonValue,
    ReconciliationStatus,
)
from intent_engineering.mutations.authorization import authenticated_approval_aliases
from intent_engineering.mutations.models import (
    ApprovalRecord,
    ExecutionReceipt,
    WritePlan,
    WriteResult,
    provider_binding_hash,
    provider_write_contract_hash,
)
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.jsonl.case_store import (
    parse_case_versions,
    validate_case_appends,
)
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.jsonl.history_store import serialize_changeset
from intent_engineering.storage.jsonl.receipt_store import validate_receipt_completion
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import parse_graph, serialize_graph


class WriteCommitError(ValueError):
    """Fixed public failure when a post-provider local commit cannot complete."""

    def __init__(self) -> None:
        super().__init__("local write commit failed")


def _canonical_json(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _jsonl(value: EvidenceIngestion) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _changeset_id(material: Mapping[str, JsonValue]) -> str:
    return f"changeset:mcp-write:{hashlib.sha256(_canonical_json(dict(material))).hexdigest()}"


class LocalWriteCommitter:
    """Commit receipt, evidence, resolved case, graph, and history in one journal."""

    def __init__(
        self,
        transactions: LocalTransactionCoordinator,
        *,
        evidence_acl: tuple[str, ...],
        profile: ProviderProfile,
        binding: ProviderBinding,
        authorized_contributors: frozenset[str],
        authorized_approvers: frozenset[str],
        authorized_executors: frozenset[str],
        identity_aliases: dict[str, frozenset[str]],
    ) -> None:
        required = {"graph", "history", "cases", "evidence", "receipts"}
        if not required.issubset(transactions.target_names):
            raise ValueError("local write committer requires complete transaction targets")
        if (
            type(evidence_acl) is not tuple
            or not evidence_acl
            or tuple(sorted(set(evidence_acl))) != evidence_acl
            or any(type(item) is not str or not item.strip() for item in evidence_acl)
        ):
            raise ValueError("invalid write evidence ACL")
        try:
            validated_profile = ProviderProfile.model_validate_json(profile.model_dump_json())
            validated_binding = ProviderBinding.model_validate_json(binding.model_dump_json())
            validated_binding.validate_against(validated_profile)
            if any(
                type(policy) is not frozenset
                or not policy
                or any(type(actor) is not str or not actor.strip() for actor in policy)
                for policy in (
                    authorized_contributors,
                    authorized_approvers,
                    authorized_executors,
                )
            ):
                raise ValueError
            copied_aliases = {
                actor: frozenset(aliases) for actor, aliases in identity_aliases.items()
            }
        except Exception:  # noqa: BLE001 - invalid local authorization fails closed.
            raise ValueError("invalid local write authorization") from None
        self._transactions = transactions
        self._evidence_acl = evidence_acl
        self._profile = validated_profile
        self._binding = validated_binding
        self._authorized_contributors = authorized_contributors
        self._authorized_approvers = authorized_approvers
        self._authorized_executors = authorized_executors
        self._identity_aliases = copied_aliases

    @staticmethod
    def _evidence(
        receipt: ExecutionReceipt,
        plan: WritePlan,
        approval: ApprovalRecord,
        result: WriteResult,
        acl: tuple[str, ...],
    ) -> EvidenceRecord:
        if receipt.evidence_ref is None or receipt.resulting_version is None:
            raise ValueError("successful receipt lacks evidence identity")
        payload: dict[str, JsonValue] = {
            "approval": {
                "actor": approval.actor,
                "actor_aliases": list(approval.actor_aliases),
                "id": approval.id,
            },
            "case_id": plan.case_id,
            "executed_by": receipt.executed_by,
            "plan": {
                "conflicting_authors": list(plan.conflicting_authors),
                "created_by": plan.created_by,
                "created_by_aliases": list(plan.created_by_aliases),
                "evidence_refs": list(plan.evidence_refs),
                "hash": plan.canonical_hash,
                "id": plan.id,
            },
            "provider": {
                "binding_hash": plan.binding_hash,
                "connector_id": plan.connector_id,
                "operation": plan.provider_operation,
                "profile_id": plan.profile_id,
                "profile_version": plan.profile_version,
                "write_contract_hash": plan.write_contract_hash,
            },
            "resolution_action": plan.resolution_action.value,
            "result": cast(JsonValue, result.model_dump(mode="json")["redacted_result"]),
            "target": {
                "after": cast(JsonValue, plan.model_dump(mode="json")["after"]),
                "id": plan.target_id,
                "object_type": plan.object_type,
                "prior_version": plan.before_version,
                "resulting_version": result.resulting_version,
            },
        }
        content_hash = f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"
        return EvidenceRecord(
            id=receipt.evidence_ref,
            connector_type="mcp-write",
            external_object_id=(f"{plan.profile_id}:{plan.object_type}:{plan.target_id}"),
            external_version=result.resulting_version,
            author=receipt.executed_by,
            observed_at=receipt.completed_at,
            source_locator=f"mcp-write://{plan.profile_id}/{plan.object_type}",
            content_hash=content_hash,
            payload=payload,
            acl=acl,
        )

    @staticmethod
    def _evidence_append(
        content: bytes | None,
        connector_id: str,
        evidence: EvidenceRecord,
    ) -> bytes:
        records, ingestions, _legacy_ids = parse_evidence_lines(content)
        if any(record.id == evidence.id for record in records):
            raise ValueError("write evidence already exists without receipt")
        ledger = tuple(item for item in ingestions if item.connector_id == connector_id)
        predecessors = tuple(
            item.evidence
            for item in ledger
            if item.evidence.connector_type == evidence.connector_type
            and item.evidence.external_object_id == evidence.external_object_id
        )
        ingestion = EvidenceIngestion(
            connector_id=connector_id,
            sequence=len(ledger) + 1,
            predecessor_id=predecessors[-1].id if predecessors else None,
            evidence=evidence,
        )
        return _jsonl(ingestion)

    def _commit(
        self,
        receipt: ExecutionReceipt,
        plan: WritePlan,
        approval: ApprovalRecord,
        result: WriteResult,
    ) -> bool:
        try:
            validated_receipt = ExecutionReceipt.model_validate_json(receipt.model_dump_json())
            validated_plan = WritePlan.model_validate_json(plan.model_dump_json())
            validated_approval = ApprovalRecord.model_validate_json(approval.model_dump_json())
            validated_result = WriteResult.model_validate_json(result.model_dump_json())
            profile = ProviderProfile.model_validate_json(self._profile.model_dump_json())
            binding = ProviderBinding.model_validate_json(self._binding.model_dump_json())
            binding.validate_against(profile)
            authenticated_aliases = authenticated_approval_aliases(
                validated_plan,
                binding,
                validated_approval.actor,
                authorized_contributors=self._authorized_contributors,
                authorized_approvers=self._authorized_approvers,
                identity_aliases=self._identity_aliases,
            )
            if (
                validated_receipt.status != "succeeded"
                or validated_receipt.plan_id != validated_plan.id
                or validated_receipt.plan_hash != validated_plan.canonical_hash
                or validated_receipt.approval_id != validated_approval.id
                or validated_receipt.target_version != validated_plan.before_version
                or validated_receipt.resulting_version != validated_result.resulting_version
                or dict(validated_result.redacted_result) != {"status": "verified"}
                or validated_receipt.executed_by != validated_approval.actor
                or validated_approval.plan_id != validated_plan.id
                or validated_approval.plan_hash != validated_plan.canonical_hash
                or validated_approval.target_version != validated_plan.before_version
                or validated_approval.plan_created_at != validated_plan.created_at
                or validated_approval.plan_expires_at != validated_plan.expires_at
                or validated_receipt.executed_by not in self._authorized_executors
                or authenticated_aliases != validated_approval.actor_aliases
                or validated_receipt.attempted_at < validated_approval.approved_at
                or validated_receipt.attempted_at >= validated_approval.expires_at
                or profile.id != validated_plan.profile_id
                or profile.version != validated_plan.profile_version
                or provider_binding_hash(binding) != validated_plan.binding_hash
                or validated_plan.operation not in profile.writes
            ):
                return False
            operation = profile.writes[validated_plan.operation]
            if (
                operation.target_object != validated_plan.object_type
                or validated_plan.target_ref != f"{profile.id}:{validated_plan.target_id}"
                or binding.tools[validated_plan.operation] != validated_plan.provider_operation
                or provider_write_contract_hash(
                    profile,
                    binding,
                    validated_plan.operation,
                )
                != validated_plan.write_contract_hash
            ):
                return False
            evidence = self._evidence(
                validated_receipt,
                validated_plan,
                validated_approval,
                validated_result,
                self._evidence_acl,
            )
            connector_id = f"mcp-write:{validated_plan.binding_hash.removeprefix('sha256:')}"
            with self._transactions.transaction() as transaction:
                graph = parse_graph(transaction.read("graph"))
                evidence_content = transaction.read_optional("evidence")
                records, _, _ = parse_evidence_lines(evidence_content)
                if not set(validated_plan.evidence_refs).issubset(
                    {record.id for record in records}
                ):
                    return False
                latest_cases = {
                    case.id: case
                    for case in parse_case_versions(transaction.read_optional("cases"))
                }
                case = latest_cases.get(validated_plan.case_id)
                if (
                    case is None
                    or case.status is not ReconciliationStatus.NEEDS_HUMAN
                    or case.all_evidence_refs != validated_plan.evidence_refs
                    or validated_plan.target_ref not in {case.subject_ref, *case.affected_refs}
                    or tuple(
                        sorted({author for side in case.evidence_sides for author in side.authors})
                    )
                    != validated_plan.conflicting_authors
                ):
                    return False
                evidence_refs = (*case.all_evidence_refs, evidence.id)
                changeset_material: dict[str, JsonValue] = {
                    "baseline_graph_version": graph.version,
                    "case_id": case.id,
                    "evidence_id": evidence.id,
                    "receipt_id": validated_receipt.id,
                    "resolution_action": validated_plan.resolution_action.value,
                }
                changeset = ChangeSet(
                    id=_changeset_id(changeset_material),
                    actor=validated_receipt.executed_by,
                    timestamp=validated_receipt.completed_at,
                    baseline_graph_version=graph.version,
                    evidence_refs=evidence_refs,
                    nodes_added=(),
                    nodes_updated=(),
                    nodes_superseded=(),
                    edges_added=(),
                    edges_updated=(),
                    edges_superseded=(),
                    confidence_changes=(),
                    implementation_status_changes=(),
                    reconciliation_cases_created=(),
                    reconciliation_cases_resolved=(case.id,),
                    validation_status="approved",
                )
                resolved = transition_case(
                    case,
                    ReconciliationStatus.RESOLVED,
                    validated_receipt.executed_by,
                    validated_receipt.completed_at,
                    resolution=validated_plan.resolution_action,
                    changeset_id=changeset.id,
                )
                next_graph = apply_changeset_with_case_effects(graph, changeset)
                known_nodes = {node.id for node in next_graph.nodes}
                if resolved.subject_ref not in known_nodes or any(
                    reference not in known_nodes for reference in resolved.affected_refs
                ):
                    return False
                case_append = validate_case_appends(
                    transaction.read_optional("cases"),
                    (resolved,),
                )
                evidence_append = self._evidence_append(
                    evidence_content,
                    connector_id,
                    evidence,
                )
                receipt_append = validate_receipt_completion(
                    transaction.read_optional("receipts"),
                    validated_receipt,
                )
                if not receipt_append:
                    return False
                transaction.write("graph", serialize_graph(next_graph))
                transaction.append("history", serialize_changeset(changeset))
                transaction.append("evidence", evidence_append)
                transaction.append("cases", case_append)
                transaction.append("receipts", receipt_append)
            return True
        except Exception:  # noqa: BLE001 - caller receives only the fixed commit error.
            return False

    def commit_success(
        self,
        receipt: ExecutionReceipt,
        plan: WritePlan,
        approval: ApprovalRecord,
        result: WriteResult,
    ) -> None:
        """Commit all success effects or expose one fixed manual-recovery failure."""
        committed = self._commit(receipt, plan, approval, result)
        del receipt, plan, approval, result
        if not committed:
            raise WriteCommitError() from None
