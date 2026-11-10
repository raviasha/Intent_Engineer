"""At-most-once execution of one independently approved external mutation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from intent_engineering.capture.mcp.errors import McpPermissionError
from intent_engineering.capture.mcp.profile_models import ProviderBinding, ProviderProfile
from intent_engineering.capture.mcp.selectors import bind_arguments
from intent_engineering.capture.mcp.session import detached_json
from intent_engineering.core.models import JsonValue
from intent_engineering.mutations.authorization import authenticated_approval_aliases
from intent_engineering.mutations.committer import LocalWriteCommitter
from intent_engineering.mutations.models import (
    ApprovalRecord,
    ExecutionReceipt,
    RemoteObject,
    WritePlan,
    WriteResult,
    provider_binding_hash,
    provider_write_contract_hash,
    receipt_id,
)
from intent_engineering.storage.jsonl.approval_store import (
    JsonlApprovalStore,
    JsonlWritePlanStore,
)
from intent_engineering.storage.jsonl.receipt_store import JsonlReceiptStore


class ExecutionUnavailable(ValueError):
    """Fixed failure for invalid policy, stale approval, or ambiguous prior execution."""


class ExternalMutationGateway(Protocol):
    """The only provider boundary used by approved execution."""

    async def fetch_target(self, plan: WritePlan) -> RemoteObject: ...

    async def execute(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
    ) -> WriteResult: ...


class SuccessfulWriteCommitter(Protocol):
    """Atomically persist a successful receipt/evidence/resolution group."""

    def commit_success(
        self,
        receipt: ExecutionReceipt,
        plan: WritePlan,
        approval: ApprovalRecord,
        result: WriteResult,
    ) -> None: ...


type _ValidatedExecution = tuple[WritePlan, ApprovalRecord, ProviderProfile, ProviderBinding]
type _ProviderStatus = Literal["ok", "permission_denied", "provider_failure", "aborted"]
type _StoreStatus = Literal["ok", "unavailable", "aborted"]


def _exact_string_set(value: object) -> frozenset[str] | None:
    if type(value) is not frozenset or any(
        type(item) is not str or not item.strip() for item in value
    ):
        return None
    return cast(frozenset[str], value)


def _detached_abort(error: BaseException) -> BaseException:
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    return error


async def _fetch_target_safely(
    gateway: ExternalMutationGateway,
    plan: WritePlan,
) -> tuple[_ProviderStatus, RemoteObject | None, BaseException | None]:
    try:
        raw_current = await gateway.fetch_target(plan)
        current = RemoteObject.model_validate_json(raw_current.model_dump_json())
        return "ok", current, None
    except McpPermissionError:
        return "permission_denied", None, None
    except Exception:  # noqa: BLE001 - provider failures cross only as fixed state.
        return "provider_failure", None, None
    except BaseException as error:  # noqa: BLE001 - cancellation/interrupt propagate redacted.
        return "aborted", None, _detached_abort(error)


async def _execute_provider_safely(
    gateway: ExternalMutationGateway,
    operation: str,
    arguments: dict[str, JsonValue],
    before_version: str,
) -> tuple[_ProviderStatus, WriteResult | None, BaseException | None]:
    try:
        raw_result = await gateway.execute(operation, arguments)
        result = WriteResult.model_validate_json(raw_result.model_dump_json())
        if result.resulting_version == before_version:
            raise ValueError("provider result version did not advance")
        return "ok", result, None
    except McpPermissionError:
        return "permission_denied", None, None
    except Exception:  # noqa: BLE001 - provider failures cross only as fixed state.
        return "provider_failure", None, None
    except BaseException as error:  # noqa: BLE001 - cancellation/interrupt propagate redacted.
        return "aborted", None, _detached_abort(error)


def _commit_success_safely(
    committer: SuccessfulWriteCommitter,
    receipt: ExecutionReceipt,
    plan: WritePlan,
    approval: ApprovalRecord,
    result: WriteResult,
) -> tuple[bool, BaseException | None]:
    try:
        committer.commit_success(receipt, plan, approval, result)
        return True, None
    except Exception:  # noqa: BLE001 - local persistence details are always redacted.
        return False, None
    except BaseException as error:  # noqa: BLE001 - cancellation/interrupt propagate redacted.
        return False, _detached_abort(error)


def _get_receipt_safely(
    receipts: JsonlReceiptStore,
    plan_id: str,
    approval_id: str,
) -> tuple[_StoreStatus, ExecutionReceipt | None, BaseException | None]:
    try:
        return "ok", receipts.get_for(plan_id, approval_id), None
    except Exception:  # noqa: BLE001 - storage corruption crosses only as fixed state.
        return "unavailable", None, None
    except BaseException as error:  # noqa: BLE001 - cancellation/interrupt propagate redacted.
        return "aborted", None, _detached_abort(error)


def _claim_receipt_safely(
    receipts: JsonlReceiptStore,
    plan_id: str,
    approval_id: str,
    actor: str,
    now: datetime,
) -> tuple[_StoreStatus, bool, BaseException | None]:
    try:
        return "ok", receipts.claim(plan_id, approval_id, actor, now), None
    except Exception:  # noqa: BLE001 - storage corruption crosses only as fixed state.
        return "unavailable", False, None
    except BaseException as error:  # noqa: BLE001 - cancellation/interrupt propagate redacted.
        return "aborted", False, _detached_abort(error)


def _complete_receipt_safely(
    receipts: JsonlReceiptStore,
    receipt: ExecutionReceipt,
) -> tuple[_StoreStatus, bool, BaseException | None]:
    try:
        return "ok", receipts.complete(receipt), None
    except Exception:  # noqa: BLE001 - storage corruption crosses only as fixed state.
        return "unavailable", False, None
    except BaseException as error:  # noqa: BLE001 - cancellation/interrupt propagate redacted.
        return "aborted", False, _detached_abort(error)


class WriteExecutor:
    """Revalidate, claim, call once, and record one approved provider mutation."""

    def __init__(
        self,
        *,
        plans: JsonlWritePlanStore,
        approvals: JsonlApprovalStore,
        receipts: JsonlReceiptStore,
        profile: ProviderProfile,
        binding: ProviderBinding,
        gateway: ExternalMutationGateway,
        success_committer: SuccessfulWriteCommitter,
        authorized_contributors: frozenset[str],
        authorized_approvers: frozenset[str],
        authorized_executors: frozenset[str],
        identity_aliases: dict[str, frozenset[str]],
    ) -> None:
        self._plans = plans
        self._approvals = approvals
        self._receipts = receipts
        self._profile = profile
        self._binding = binding
        self._gateway = gateway
        self._success_committer = success_committer
        self._authorized_contributors = authorized_contributors
        self._authorized_approvers = authorized_approvers
        self._authorized_executors = authorized_executors
        self._identity_aliases = identity_aliases

    def _validated_inputs(
        self,
        plan_id: object,
        approval_id: object,
        actor: object,
        now: object,
    ) -> tuple[_ValidatedExecution | None, BaseException | None]:
        try:
            if (
                type(plan_id) is not str
                or type(approval_id) is not str
                or type(actor) is not str
                or type(now) is not datetime
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                return None, None
            contributors = _exact_string_set(self._authorized_contributors)
            approvers = _exact_string_set(self._authorized_approvers)
            executors = _exact_string_set(self._authorized_executors)
            if contributors is None or approvers is None or executors is None:
                return None, None
            plan = WritePlan.model_validate_json(self._plans.get(plan_id).model_dump_json())
            approval = ApprovalRecord.model_validate_json(
                self._approvals.get(approval_id).model_dump_json()
            )
            profile = ProviderProfile.model_validate_json(self._profile.model_dump_json())
            binding = ProviderBinding.model_validate_json(self._binding.model_dump_json())
            binding.validate_against(profile)
            if (
                plan.created_by not in contributors
                or approval.actor not in approvers
                or actor not in executors
                or actor != approval.actor
                or approval.plan_id != plan.id
                or approval.plan_hash != plan.canonical_hash
                or approval.target_version != plan.before_version
                or approval.plan_created_at != plan.created_at
                or approval.plan_expires_at != plan.expires_at
                or profile.id != plan.profile_id
                or profile.version != plan.profile_version
                or provider_binding_hash(binding) != plan.binding_hash
                or plan.operation not in profile.writes
            ):
                return None, None
            operation = profile.writes[plan.operation]
            if (
                operation.target_object != plan.object_type
                or plan.target_ref != f"{profile.id}:{plan.target_id}"
                or binding.tools[plan.operation] != plan.provider_operation
                or provider_write_contract_hash(profile, binding, plan.operation)
                != plan.write_contract_hash
            ):
                return None, None
            authenticated_aliases = authenticated_approval_aliases(
                plan,
                binding,
                approval.actor,
                authorized_contributors=contributors,
                authorized_approvers=approvers,
                identity_aliases=self._identity_aliases,
            )
            if authenticated_aliases != approval.actor_aliases:
                return None, None
            after = cast(dict[str, JsonValue], detached_json(dict(plan.after)))
            write_fields = {
                field: after[field] for field in operation.allowed_fields if field in after
            }
            Draft202012Validator(dict(operation.input_schema)).validate(write_fields)
            active_arguments = {
                name: argument
                for name, argument in operation.arguments.items()
                if argument.source != "field" or argument.field in write_fields
            }
            expected_arguments = bind_arguments(
                active_arguments,
                {
                    "target_id": plan.target_id,
                    "before_version": plan.before_version,
                    "fields": write_fields,
                },
            )
            if expected_arguments != dict(plan.arguments):
                return None, None
            return (plan, approval, profile, binding), None
        except Exception:  # noqa: BLE001 - no caller/provider values cross this boundary.
            return None, None
        except BaseException as error:  # noqa: BLE001 - cancellation/interrupt propagate redacted.
            return None, _detached_abort(error)

    @staticmethod
    def _receipt(
        plan: WritePlan,
        approval: ApprovalRecord,
        actor: str,
        now: datetime,
        status: str,
        *,
        resulting_version: str | None = None,
        redacted_error: str | None = None,
    ) -> ExecutionReceipt:
        timestamp = now.astimezone(UTC)
        evidence_ref = None
        material: dict[str, JsonValue] = {
            "schema_version": 1,
            "plan_id": plan.id,
            "plan_hash": plan.canonical_hash,
            "approval_id": approval.id,
            "target_version": plan.before_version,
            "executed_by": actor,
            "status": status,
            "attempted_at": timestamp.isoformat().replace("+00:00", "Z"),
            "completed_at": timestamp.isoformat().replace("+00:00", "Z"),
            "resulting_version": resulting_version,
            "evidence_ref": None,
            "redacted_error": redacted_error,
        }
        if status == "succeeded":
            provisional = receipt_id(material)
            evidence_ref = f"evidence:mcp-write:{provisional.removeprefix('receipt:sha256:')}"
            material["evidence_ref"] = evidence_ref
        return ExecutionReceipt(
            id=receipt_id(material),
            plan_id=plan.id,
            plan_hash=plan.canonical_hash,
            approval_id=approval.id,
            target_version=plan.before_version,
            executed_by=actor,
            status=status,  # type: ignore[arg-type]
            attempted_at=timestamp,
            completed_at=timestamp,
            resulting_version=resulting_version,
            evidence_ref=evidence_ref,
            redacted_error=redacted_error,  # type: ignore[arg-type]
        )

    def _failure_receipt(
        self,
        plan: WritePlan,
        approval: ApprovalRecord,
        actor: str,
        now: datetime,
        code: str,
        *,
        rejected: bool = False,
    ) -> ExecutionReceipt:
        return self._receipt(
            plan,
            approval,
            actor,
            now,
            "rejected" if rejected else "failed",
            redacted_error=code,
        )

    async def execute(
        self,
        plan_id: str,
        approval_id: str,
        *,
        actor: str,
        now: datetime,
    ) -> ExecutionReceipt:
        """Execute one exact approved write, never retrying an ambiguous mutation."""
        validated, abort = self._validated_inputs(plan_id, approval_id, actor, now)
        del plan_id, approval_id
        if abort is not None:
            del actor, now, validated
            raise abort
        if validated is None:
            del actor, now
            raise ExecutionUnavailable("external write execution unavailable") from None
        plan, approval, _profile, _binding = validated
        del validated, _profile, _binding
        store_status, existing, abort = _get_receipt_safely(
            self._receipts,
            plan.id,
            approval.id,
        )
        if abort is not None:
            del plan, approval, actor, now, store_status, existing
            raise abort
        if store_status != "ok":
            del plan, approval, actor, now, store_status, existing
            raise ExecutionUnavailable("external write execution unavailable") from None
        if existing is not None:
            return existing
        timestamp = now.astimezone(UTC)
        if (
            timestamp < plan.created_at
            or timestamp >= plan.expires_at
            or timestamp < approval.approved_at
            or timestamp >= approval.expires_at
        ):
            del plan, approval, actor, now, timestamp
            raise ExecutionUnavailable("external write execution unavailable") from None
        store_status, claimed, abort = _claim_receipt_safely(
            self._receipts,
            plan.id,
            approval.id,
            actor,
            now,
        )
        if abort is not None:
            del plan, approval, actor, now, store_status, claimed
            raise abort
        if store_status != "ok":
            del plan, approval, actor, now, store_status, claimed
            raise ExecutionUnavailable("external write execution unavailable") from None
        if not claimed:
            read_status, completed, abort = _get_receipt_safely(
                self._receipts,
                plan.id,
                approval.id,
            )
            if abort is not None:
                del plan, approval, actor, now, store_status, claimed, read_status, completed
                raise abort
            if read_status != "ok":
                del plan, approval, actor, now, store_status, claimed, read_status, completed
                raise ExecutionUnavailable("external write execution unavailable") from None
            if completed is not None:
                return completed
            del plan, approval, actor, now, store_status, claimed, read_status, completed
            raise ExecutionUnavailable("external write execution unavailable") from None
        fetch_status, current, abort = await _fetch_target_safely(self._gateway, plan)
        if abort is not None:
            del plan, approval, actor, now, fetch_status, current
            raise abort
        if fetch_status == "permission_denied":
            receipt = self._failure_receipt(plan, approval, actor, now, "permission_denied")
            del plan, approval, actor, now, fetch_status, current
            complete_status, completion_written, abort = _complete_receipt_safely(
                self._receipts, receipt
            )
            if abort is not None:
                del receipt, complete_status, completion_written
                raise abort
            if complete_status != "ok" or not completion_written:
                del receipt, complete_status, completion_written
                raise ExecutionUnavailable("external write execution unavailable") from None
            return receipt
        if fetch_status != "ok" or current is None:
            receipt = self._failure_receipt(plan, approval, actor, now, "provider_failure")
            del plan, approval, actor, now, fetch_status, current
            complete_status, completion_written, abort = _complete_receipt_safely(
                self._receipts, receipt
            )
            if abort is not None:
                del receipt, complete_status, completion_written
                raise abort
            if complete_status != "ok" or not completion_written:
                del receipt, complete_status, completion_written
                raise ExecutionUnavailable("external write execution unavailable") from None
            return receipt
        if (
            current.connector_id != plan.connector_id
            or current.profile_id != plan.profile_id
            or current.profile_version != plan.profile_version
            or current.object_type != plan.object_type
            or current.id != plan.target_id
            or current.version != plan.before_version
            or dict(current.content) != dict(plan.before)
        ):
            receipt = self._failure_receipt(
                plan,
                approval,
                actor,
                now,
                "target_changed",
                rejected=True,
            )
            del plan, approval, actor, now, fetch_status, current
            complete_status, completion_written, abort = _complete_receipt_safely(
                self._receipts, receipt
            )
            if abort is not None:
                del receipt, complete_status, completion_written
                raise abort
            if complete_status != "ok" or not completion_written:
                del receipt, complete_status, completion_written
                raise ExecutionUnavailable("external write execution unavailable") from None
            return receipt
        arguments = cast(dict[str, JsonValue], detached_json(dict(plan.arguments)))
        del current
        write_status, result, abort = await _execute_provider_safely(
            self._gateway,
            plan.provider_operation,
            arguments,
            plan.before_version,
        )
        del arguments
        if abort is not None:
            del plan, approval, actor, now, write_status, result
            raise abort
        if write_status == "permission_denied":
            receipt = self._failure_receipt(plan, approval, actor, now, "permission_denied")
            del plan, approval, actor, now, write_status, result
            complete_status, completion_written, abort = _complete_receipt_safely(
                self._receipts, receipt
            )
            if abort is not None:
                del receipt, complete_status, completion_written
                raise abort
            if complete_status != "ok" or not completion_written:
                del receipt, complete_status, completion_written
                raise ExecutionUnavailable("external write execution unavailable") from None
            return receipt
        if write_status != "ok" or result is None:
            receipt = self._failure_receipt(plan, approval, actor, now, "provider_failure")
            del plan, approval, actor, now, write_status, result
            complete_status, completion_written, abort = _complete_receipt_safely(
                self._receipts, receipt
            )
            if abort is not None:
                del receipt, complete_status, completion_written
                raise abort
            if complete_status != "ok" or not completion_written:
                del receipt, complete_status, completion_written
                raise ExecutionUnavailable("external write execution unavailable") from None
            return receipt
        verify_status, verified_target, abort = await _fetch_target_safely(
            self._gateway,
            plan,
        )
        if abort is not None:
            del plan, approval, actor, now, write_status, result, verify_status, verified_target
            raise abort
        if (
            verify_status != "ok"
            or verified_target is None
            or verified_target.connector_id != plan.connector_id
            or verified_target.profile_id != plan.profile_id
            or verified_target.profile_version != plan.profile_version
            or verified_target.object_type != plan.object_type
            or verified_target.id != plan.target_id
            or verified_target.version != result.resulting_version
            or dict(verified_target.content) != dict(plan.after)
        ):
            del plan, approval, actor, now, write_status, result, verify_status, verified_target
            raise ExecutionUnavailable("external write execution unavailable") from None
        verified_result = WriteResult(
            resulting_version=result.resulting_version,
            redacted_result={"status": "verified"},
        )
        del result, verified_target, verify_status
        result = verified_result
        receipt = self._receipt(
            plan,
            approval,
            actor,
            now,
            "succeeded",
            resulting_version=result.resulting_version,
        )
        if receipt.evidence_ref is None:
            del receipt, plan, approval, result, actor, now
            raise ExecutionUnavailable("external write execution unavailable") from None
        committed, abort = _commit_success_safely(
            self._success_committer,
            receipt,
            plan,
            approval,
            result,
        )
        if abort is not None:
            del receipt, plan, approval, result, actor, now, committed
            raise abort
        if not committed:
            del receipt, plan, approval, result, actor, now
            raise ExecutionUnavailable("external write execution unavailable") from None
        return receipt


__all__ = [
    "ExecutionUnavailable",
    "ExternalMutationGateway",
    "LocalWriteCommitter",
    "SuccessfulWriteCommitter",
    "WriteExecutor",
]
