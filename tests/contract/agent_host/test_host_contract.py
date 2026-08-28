"""Host-neutral contracts for mandatory intent preflight."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intent_engineering.integrations.agent_host import (
    HostTask,
    HostTaskResult,
    IntentAgentHostAdapter,
    MandatoryHookUnavailable,
    MutationDecision,
)
from intent_engineering.intent_workflow.authorization import AuthorizationVerification
from intent_engineering.intent_workflow.models import (
    PreflightResult,
    TaskClassification,
    TaskEnvelope,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _envelope(
    request: str = "Add CSV export",
    *,
    actor: str = "local:asha",
) -> TaskEnvelope:
    return TaskEnvelope(
        repository_id="demo",
        actor=actor,
        conversation_ref="codex:thread-1:turn-1",
        request=request,
        request_evidence_ref="evidence:conversation:" + "1" * 64,
        graph_version=3,
        created_at=NOW,
        requested_scope=("src/export.py",),
    )


def _preflight(envelope: TaskEnvelope, *, authorized: bool = True) -> PreflightResult:
    return PreflightResult(
        task_id=envelope.id,
        graph_version=envelope.graph_version,
        classification=(
            TaskClassification.ALIGNED if authorized else TaskClassification.NEW_OR_AMBIGUOUS
        ),
        authorized=authorized,
        basis="REQ-42 permits report export",
        relevant_node_ids=("req-csv-export",) if authorized else (),
        evidence_refs=("evidence:prd:v1",),
        questions=() if authorized else ("Which report formats are required?",),
        permitted_scope=envelope.requested_scope if authorized else (),
        context={"requirement": {"id": "req-csv-export"}},
    )


@dataclass
class _Workflow:
    token: str | None = "opaque-capability"
    authorized: bool = True
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def before_task(
        self, request: str, actor: str
    ) -> tuple[TaskEnvelope, PreflightResult, str | None]:
        self.calls.append(("before_task", (request, actor)))
        envelope = _envelope(request)
        return envelope, _preflight(envelope, authorized=self.authorized), self.token

    async def authorization_verify(
        self,
        *,
        token: str,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        requested_paths: tuple[str, ...],
    ) -> AuthorizationVerification:
        self.calls.append(
            (
                "authorization_verify",
                (token, actor, repository_id, task_id, graph_version, requested_paths),
            )
        )
        return AuthorizationVerification(
            authorized=token == "opaque-capability",
            classification=(TaskClassification.ALIGNED if token == "opaque-capability" else None),
            relevant_node_ids=("req-csv-export",) if token == "opaque-capability" else (),
            expires_at=(
                datetime(2026, 8, 26, 12, 5, tzinfo=UTC) if token == "opaque-capability" else None
            ),
            reason="authorized" if token == "opaque-capability" else "unknown",
        )


@pytest.mark.anyio
async def test_enabled_adapter_blocks_mutation_without_matching_grant() -> None:
    workflow = _Workflow(token=None, authorized=False)
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)

    task = await adapter.before_task(request="Add CSV export", actor="local:asha")
    denied = await adapter.before_mutation(
        task=task,
        operation="write_file",
        paths=("src/export.py",),
        token=None,
    )

    assert denied.allowed is False
    assert denied.reason == "intent_preflight_required"


@pytest.mark.anyio
async def test_enabled_adapter_verifies_private_grant_and_returns_detached_decision() -> None:
    workflow = _Workflow()
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)

    task = await adapter.before_task(request="Add CSV export", actor="local:asha")
    decision = await adapter.before_mutation(
        task=task,
        operation="write_file",
        paths=("src/export.py",),
        token=None,
    )

    assert decision.allowed is True
    assert decision.reason == "authorized"
    assert decision.paths == ("src/export.py",)
    assert "opaque-capability" not in task.model_dump_json()
    assert workflow.calls[-1][0] == "authorization_verify"
    assert workflow.calls[-1][1][0] == "opaque-capability"  # type: ignore[index]


@pytest.mark.anyio
@pytest.mark.parametrize("substitution", ["request", "actor"])
async def test_enabled_adapter_rejects_substituted_workflow_task_binding(
    substitution: str,
) -> None:
    class SubstitutingWorkflow(_Workflow):
        async def before_task(
            self, request: str, actor: str
        ) -> tuple[TaskEnvelope, PreflightResult, str | None]:
            del actor
            envelope = _envelope(
                "Different hidden request" if substitution == "request" else request,
                actor="local:mallory" if substitution == "actor" else "local:asha",
            )
            return envelope, _preflight(envelope), "opaque-capability"

    workflow = SubstitutingWorkflow()
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)

    with pytest.raises(ValueError, match="host workflow binding mismatch"):
        await adapter.before_task(request="Add CSV export", actor="local:asha")

    assert adapter._tokens == {}  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_mutation_rejects_modified_copy_of_issued_host_task() -> None:
    workflow = _Workflow()
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)
    task = await adapter.before_task(request="Add CSV export", actor="local:asha")
    modified = task.model_copy(
        update={"request_digest": "sha256:" + "9" * 64}
    )

    decision = await adapter.before_mutation(
        task=modified,
        operation="write_file",
        paths=("src/export.py",),
        token=None,
    )

    assert decision.allowed is False
    assert decision.reason == "task_changed"
    assert [name for name, _arguments in workflow.calls] == ["before_task"]


@pytest.mark.anyio
async def test_invalid_after_task_result_still_revokes_private_task_state() -> None:
    workflow = _Workflow()
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)
    task = await adapter.before_task(request="Add CSV export", actor="local:asha")
    mismatched = HostTaskResult(
        task_id=_envelope("Different completed task").id,
        status="failed",
        response_evidence_ref="evidence:conversation:" + "2" * 64,
        completed_at=NOW,
    )

    with pytest.raises(ValueError, match="host task result mismatch"):
        await adapter.after_task(task, mismatched)

    assert task.id not in adapter._tokens  # type: ignore[attr-defined]
    assert task.id not in adapter._tasks  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_disabled_adapter_is_a_transparent_noop() -> None:
    workflow = _Workflow()
    adapter = IntentAgentHostAdapter(
        workflow=workflow,
        enabled=False,
        repository_id="demo",
        conversation_ref="codex:thread-1:turn-1",
        graph_version=3,
        clock=lambda: NOW,
    )

    task = await adapter.before_task(request="Add CSV export", actor="local:asha")
    decision = await adapter.before_mutation(
        task=task,
        operation="opaque_native_tool",
        paths=(),
        token=None,
    )
    await adapter.after_task(
        task,
        HostTaskResult(
            task_id=task.id,
            status="completed",
            response_evidence_ref="evidence:conversation:" + "2" * 64,
            completed_at=NOW,
        ),
    )

    assert decision.allowed is True
    assert decision.reason == "plugin_disabled"
    assert workflow.calls == []


def test_host_records_are_strict_frozen_detached_and_roundtrip_json() -> None:
    envelope = _envelope()
    source = _preflight(envelope)
    task = HostTask(
        id=envelope.id,
        actor=envelope.actor,
        repository_id=envelope.repository_id,
        conversation_ref=envelope.conversation_ref,
        request_digest="sha256:" + "2" * 64,
        graph_version=envelope.graph_version,
        created_at=NOW,
        preflight=source,
    )

    assert HostTask.model_validate_json(task.model_dump_json()) == task
    result = HostTaskResult(
        task_id=task.id,
        status="completed",
        response_evidence_ref="evidence:conversation:" + "2" * 64,
        changed_paths=("src/export.py",),
        test_refs=("pytest:unit",),
        completed_at=NOW,
    )
    decision = MutationDecision(
        allowed=True,
        reason="authorized",
        task_id=task.id,
        graph_version=task.graph_version,
        paths=("src/export.py",),
    )
    assert HostTaskResult.model_validate_json(result.model_dump_json()) == result
    assert MutationDecision.model_validate_json(decision.model_dump_json()) == decision
    assert task.preflight is not source
    with pytest.raises(TypeError):
        task.preflight.context["changed"] = True  # type: ignore[index,union-attr]
    with pytest.raises(ValidationError):
        HostTask.model_validate_json(
            json.dumps({**task.model_dump(mode="json"), "unknown": True})
        )
    with pytest.raises(ValidationError):
        task.actor = "local:other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changed_path",
    ["../escape.py", "/absolute.py", "C:/drive.py", "NUL.txt", "a\\b.py", "a/./b.py"],
)
def test_host_result_rejects_noncanonical_paths(changed_path: str) -> None:
    with pytest.raises(ValueError):
        HostTaskResult(
            task_id=_envelope().id,
            status="completed",
            response_evidence_ref="evidence:conversation:" + "2" * 64,
            changed_paths=(changed_path,),
            completed_at=NOW,
        )


def test_host_records_reject_non_utc_and_fixed_hook_error_has_no_detail() -> None:
    with pytest.raises(ValueError, match="UTC"):
        HostTaskResult(
            task_id=_envelope().id,
            status="completed",
            response_evidence_ref="evidence:conversation:" + "2" * 64,
            completed_at=NOW.replace(tzinfo=None),
        )
    assert str(MandatoryHookUnavailable()) == "Codex mandatory mutation hook is unavailable"


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-26T12:00:00+00:00",
        "2026-08-26 12:00:00Z",
        "2026-08-26T12:00:00.000000Z",
    ],
)
@pytest.mark.parametrize("record", ["task", "result"])
def test_host_json_rejects_noncanonical_utc_timestamp_aliases(
    timestamp: str,
    record: str,
) -> None:
    envelope = _envelope()
    if record == "task":
        payload = HostTask(
            id=envelope.id,
            actor=envelope.actor,
            repository_id=envelope.repository_id,
            conversation_ref=envelope.conversation_ref,
            request_digest="sha256:" + "2" * 64,
            graph_version=envelope.graph_version,
            created_at=NOW,
        ).model_dump(mode="json")
        payload["created_at"] = timestamp
        model = HostTask
    else:
        payload = HostTaskResult(
            task_id=envelope.id,
            status="completed",
            response_evidence_ref="evidence:conversation:" + "2" * 64,
            completed_at=NOW,
        ).model_dump(mode="json")
        payload["completed_at"] = timestamp
        model = HostTaskResult

    with pytest.raises(ValueError, match="canonical UTC"):
        model.model_validate_json(json.dumps(payload))


class _CancellationSignal(BaseException):
    pass


class _TextSubclass(str):
    pass


class _IntegerSubclass(int):
    pass


def test_host_records_reject_scalar_subclasses_before_normalization() -> None:
    envelope = _envelope()
    with pytest.raises(TypeError, match="exact built-in scalars"):
        HostTask(
            id=envelope.id,
            actor=_TextSubclass(envelope.actor),
            repository_id=envelope.repository_id,
            conversation_ref=envelope.conversation_ref,
            request_digest="sha256:" + "2" * 64,
            graph_version=envelope.graph_version,
            created_at=NOW,
        )
    with pytest.raises(TypeError, match="exact built-in scalars"):
        HostTask(
            id=envelope.id,
            actor=envelope.actor,
            repository_id=envelope.repository_id,
            conversation_ref=envelope.conversation_ref,
            request_digest="sha256:" + "2" * 64,
            graph_version=_IntegerSubclass(envelope.graph_version),
            created_at=NOW,
        )
    with pytest.raises(TypeError, match="exact built-in scalars"):
        HostTaskResult(
            task_id=envelope.id,
            status="completed",
            response_evidence_ref="evidence:conversation:" + "2" * 64,
            changed_paths=(_TextSubclass("src/export.py"),),
            completed_at=NOW,
        )


@pytest.mark.anyio
async def test_mutation_cancellation_identity_and_traceback_do_not_retain_secrets() -> None:
    marker = "PRIVATE-CAPABILITY-MARKER"
    signal = _CancellationSignal()

    class CancellingWorkflow(_Workflow):
        async def authorization_verify(self, **_kwargs: object) -> AuthorizationVerification:
            raise signal

    workflow = CancellingWorkflow(token=marker)
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)
    task = await adapter.before_task(request="Add CSV export", actor="local:asha")

    with pytest.raises(_CancellationSignal) as caught:
        await adapter.before_mutation(
            task=task,
            operation="write_file",
            paths=("src/export.py",),
            token=None,
        )

    assert caught.value is signal
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    reachable_capabilities: list[str] = []
    frame = caught.value.__traceback__
    while frame is not None:
        if "integrations/agent_host" in frame.tb_frame.f_code.co_filename:
            assert marker not in repr(frame.tb_frame.f_locals)
            for value in frame.tb_frame.f_locals.values():
                if isinstance(value, IntentAgentHostAdapter):
                    reachable_capabilities.extend(value._tokens.values())  # type: ignore[attr-defined]
        frame = frame.tb_next
    assert marker not in reachable_capabilities
