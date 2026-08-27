"""Provider-neutral lifecycle contracts for intent-aware coding agents."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Protocol, cast

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.authorization import AuthorizationVerification
from intent_engineering.intent_workflow.models import PreflightResult, TaskEnvelope

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_TASK_ID = r"^task:sha256:[0-9a-f]{64}$"
_COMMIT = r"^[0-9a-f]{40,64}$"
_WINDOWS_DEVICE = re.compile(r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
_MAX_ID_BYTES = 2 * 1024
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_PATH_BYTES = 2 * 1024
_MAX_ITEMS = 256
_MAX_LIVE_TASKS = 1_024

type MutationReason = Literal[
    "authorized",
    "plugin_disabled",
    "intent_preflight_required",
    "scope_mismatch",
    "graph_changed",
    "task_changed",
]


class _HostModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def reject_python_subclasses(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "python":
            _require_exact_python_values(value)
        return value


def _require_exact_python_values(value: object) -> None:
    if value is None or type(value) in {str, bool, int, float, bytes, datetime}:
        return
    if isinstance(value, (str, bool, int, float, bytes, datetime)):
        raise TypeError("host input requires exact built-in scalars and containers")
    if type(value) is tuple:
        for item in cast(tuple[object, ...], value):
            _require_exact_python_values(item)
        return
    if isinstance(value, tuple):
        raise TypeError("host input requires exact built-in scalars and containers")
    if type(value) is list:
        for item in cast(list[object], value):
            _require_exact_python_values(item)
        return
    if isinstance(value, list):
        raise TypeError("host input requires exact built-in scalars and containers")
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            _require_exact_python_values(key)
            _require_exact_python_values(item)
        return
    if isinstance(value, dict):
        raise TypeError("host input requires exact built-in scalars and containers")


def _utc(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if type(value) is not datetime or offset is None or offset.total_seconds() != 0:
        raise ValueError("host timestamp must use UTC")
    return value.astimezone(UTC)


def _timestamp_input(value: object, info: ValidationInfo) -> object:
    if info.mode != "json":
        return value
    if type(value) is not str:
        raise ValueError("invalid host timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid host timestamp") from None
    canonical = parsed.isoformat()
    if canonical.endswith("+00:00"):
        canonical = canonical[:-6] + "Z"
    if value != canonical:
        raise ValueError("host timestamp must use canonical UTC Z form")
    return parsed


def _identity(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_ID_BYTES
    ):
        raise ValueError("invalid host identity")
    return value


def _has_reserved_windows_segment(path: PureWindowsPath) -> bool:
    for part in path.parts:
        normalized = part.rstrip(" .")
        stem = normalized.split(".", 1)[0].rstrip(" ")
        if PureWindowsPath(part).is_reserved() or _WINDOWS_DEVICE.fullmatch(stem):
            return True
    return False


def _path(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or "\\" in value
        or len(value.encode("utf-8")) > _MAX_PATH_BYTES
    ):
        raise ValueError("invalid host path")
    relative = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        relative.is_absolute()
        or windows.drive
        or windows.is_absolute()
        or _has_reserved_windows_segment(windows)
        or ":" in value
        or relative.as_posix() != value
        or value == "."
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("invalid host path")
    return value


def _paths(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > _MAX_ITEMS:
        raise ValueError("invalid host paths")
    checked = tuple(_path(value) for value in values)
    if len(checked) != len(set(checked)) or tuple(sorted(checked)) != checked:
        raise ValueError("host paths must be unique and sorted")
    return checked


def _identities(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > _MAX_ITEMS:
        raise ValueError("invalid host identities")
    checked = tuple(_identity(value) for value in values)
    if len(checked) != len(set(checked)) or tuple(sorted(checked)) != checked:
        raise ValueError("host identities must be unique and sorted")
    return checked


def _request_digest(request: str) -> str:
    encoded: bytes | None = None
    try:
        if type(request) is not str or not request or _CONTROL.search(request):
            raise ValueError("invalid host request")
        encoded = request.encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("invalid host request")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    finally:
        request = ""
        encoded = None


class HostTask(_HostModel):
    """Detached host task state without capability material or raw request text."""

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(pattern=_TASK_ID)]
    actor: str
    repository_id: str
    conversation_ref: str
    request_digest: Annotated[str, Field(pattern=_DIGEST)]
    graph_version: Annotated[int, Field(ge=0)]
    created_at: datetime
    preflight: PreflightResult | None = None

    @field_validator("actor", "repository_id", "conversation_ref")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _identity(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_json_timestamp(cls, value: object, info: ValidationInfo) -> object:
        return _timestamp_input(value, info)

    @field_validator("created_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("preflight", mode="before")
    @classmethod
    def detach_preflight(cls, value: object, info: ValidationInfo) -> PreflightResult | None:
        if value is None:
            return None
        if info.mode == "json" and type(value) is dict:
            return PreflightResult.model_validate_json(
                json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            )
        if type(value) is not PreflightResult:
            raise ValueError("invalid host preflight")
        return PreflightResult.model_validate_json(value.model_dump_json())

    @model_validator(mode="after")
    def validate_binding(self) -> HostTask:
        if self.preflight is not None and (
            self.preflight.task_id != self.id or self.preflight.graph_version != self.graph_version
        ):
            raise ValueError("host preflight binding mismatch")
        return self


class HostTaskResult(_HostModel):
    """Bounded post-task record; Task 9 owns its durable processing."""

    schema_version: Literal[1] = 1
    task_id: Annotated[str, Field(pattern=_TASK_ID)]
    status: Literal["completed", "blocked", "failed", "cancelled"]
    response_evidence_ref: str
    changed_paths: Annotated[tuple[str, ...], Field(max_length=_MAX_ITEMS)] = ()
    commit_sha: Annotated[str, Field(pattern=_COMMIT)] | None = None
    test_refs: Annotated[tuple[str, ...], Field(max_length=_MAX_ITEMS)] = ()
    completed_at: datetime

    @field_validator("response_evidence_ref")
    @classmethod
    def validate_evidence_ref(cls, value: str) -> str:
        return _identity(value)

    @field_validator("changed_paths", mode="before")
    @classmethod
    def require_exact_paths(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "python" and type(value) is not tuple:
            raise ValueError("invalid host paths")
        if info.mode == "json" and type(value) is list:
            return tuple(value)
        return value

    @field_validator("changed_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _paths(values)

    @field_validator("test_refs", mode="before")
    @classmethod
    def require_exact_test_refs(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "python" and type(value) is not tuple:
            raise ValueError("invalid host identities")
        if info.mode == "json" and type(value) is list:
            return tuple(value)
        return value

    @field_validator("test_refs")
    @classmethod
    def validate_test_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _identities(values)

    @field_validator("completed_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("completed_at", mode="before")
    @classmethod
    def parse_json_timestamp(cls, value: object, info: ValidationInfo) -> object:
        return _timestamp_input(value, info)


class MutationDecision(_HostModel):
    """One detached allow/deny decision for an exact normalized path set."""

    schema_version: Literal[1] = 1
    allowed: bool
    reason: MutationReason
    task_id: Annotated[str, Field(pattern=_TASK_ID)]
    graph_version: Annotated[int, Field(ge=0)]
    paths: Annotated[tuple[str, ...], Field(max_length=_MAX_ITEMS)]

    @field_validator("paths", mode="before")
    @classmethod
    def require_exact_paths(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "python" and type(value) is not tuple:
            raise ValueError("invalid host paths")
        if info.mode == "json" and type(value) is list:
            return tuple(value)
        return value

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _paths(values)

    @model_validator(mode="after")
    def validate_decision(self) -> MutationDecision:
        if self.allowed != (self.reason in {"authorized", "plugin_disabled"}):
            raise ValueError("invalid mutation decision")
        return self


class MandatoryHookUnavailable(RuntimeError):
    """Fixed public failure when the host cannot deny before mutation."""

    def __init__(self) -> None:
        super().__init__("Codex mandatory mutation hook is unavailable")


class IntentWorkflowPort(Protocol):
    """Host-facing view of the versioned Task 7 preflight and verification surface."""

    async def before_task(
        self, request: str, actor: str
    ) -> tuple[TaskEnvelope, PreflightResult, str | None]: ...

    async def authorization_verify(
        self,
        *,
        token: str,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        requested_paths: tuple[str, ...],
    ) -> AuthorizationVerification: ...


class AgentHostAdapter(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def before_task(self, request: str, actor: str) -> HostTask: ...

    async def before_mutation(
        self,
        *,
        task: HostTask,
        operation: str,
        paths: tuple[str, ...],
        token: str | None,
    ) -> MutationDecision: ...

    async def after_task(self, task: HostTask, result: HostTaskResult) -> None: ...


def _raise_signal(signal: BaseException) -> None:
    raise signal.with_traceback(None)


class IntentAgentHostAdapter:
    """Delegate host lifecycle calls without duplicating graph or preflight semantics."""

    def __init__(
        self,
        *,
        workflow: IntentWorkflowPort,
        enabled: bool,
        repository_id: str | None = None,
        conversation_ref: str | None = None,
        graph_version: int = 0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(enabled) is not bool or type(graph_version) is not int or graph_version < 0:
            raise ValueError("invalid host adapter configuration")
        if not enabled and (repository_id is None or conversation_ref is None):
            raise ValueError("disabled host adapter requires local task identity")
        self._workflow = workflow
        self._enabled = enabled
        self._repository_id = None if repository_id is None else _identity(repository_id)
        self._conversation_ref = None if conversation_ref is None else _identity(conversation_ref)
        self._graph_version = graph_version
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(UTC)) if clock is None else clock
        )
        self._tasks: dict[str, HostTask] = {}
        self._tokens: dict[str, str] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def before_task(self, request: str, actor: str) -> HostTask:
        signal: BaseException | None = None
        token: str | None = None
        try:
            digest = _request_digest(request)
            checked_actor = _identity(actor)
            if not self._enabled:
                created_at = _utc(self._clock())
                material = (
                    f"{self._repository_id}\0{checked_actor}\0{self._conversation_ref}\0"
                    f"{digest}\0{self._graph_version}\0{created_at.isoformat()}"
                ).encode()
                task_id = f"task:sha256:{hashlib.sha256(material).hexdigest()}"
                return HostTask(
                    id=task_id,
                    actor=checked_actor,
                    repository_id=cast(str, self._repository_id),
                    conversation_ref=cast(str, self._conversation_ref),
                    request_digest=digest,
                    graph_version=self._graph_version,
                    created_at=created_at,
                )
            envelope, preflight, token = await self._workflow.before_task(request, checked_actor)
            if type(envelope) is not TaskEnvelope or type(preflight) is not PreflightResult:
                raise ValueError("invalid host workflow response")
            detached_envelope = TaskEnvelope.model_validate_json(envelope.model_dump_json())
            detached_preflight = PreflightResult.model_validate_json(preflight.model_dump_json())
            if (
                detached_envelope.request != request
                or detached_envelope.actor != checked_actor
            ):
                raise ValueError("host workflow binding mismatch")
            task = HostTask(
                id=detached_envelope.id,
                actor=detached_envelope.actor,
                repository_id=detached_envelope.repository_id,
                conversation_ref=detached_envelope.conversation_ref,
                request_digest=digest,
                graph_version=detached_envelope.graph_version,
                created_at=detached_envelope.created_at,
                preflight=detached_preflight,
            )
            if token is not None and (
                type(token) is not str or not token or len(token.encode("utf-8")) > 256
            ):
                raise ValueError("invalid host workflow response")
            if len(self._tasks) >= _MAX_LIVE_TASKS and task.id not in self._tasks:
                raise ValueError("host task capacity reached")
            self._tasks[task.id] = task
            if token is not None:
                self._tokens[task.id] = token
            return task
        except BaseException as caught:  # noqa: BLE001 - preserve exact cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            request = actor = ""
            token = None
            if "envelope" in locals():
                envelope = cast(TaskEnvelope, None)
                preflight = cast(PreflightResult, None)
                detached_envelope = cast(TaskEnvelope, None)
                detached_preflight = cast(PreflightResult, None)
            if "task" in locals():
                task = cast(HostTask, None)
            if "material" in locals():
                material = b""
            self = cast(IntentAgentHostAdapter, None)  # noqa: PLW0642 - scrub traceback state
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        raise RuntimeError("intent host adapter unavailable") from None

    async def before_mutation(
        self,
        *,
        task: HostTask,
        operation: str,
        paths: tuple[str, ...],
        token: str | None,
    ) -> MutationDecision:
        signal: BaseException | None = None
        capability: str | None = None
        try:
            if not self._enabled:
                return MutationDecision(
                    allowed=True,
                    reason="plugin_disabled",
                    task_id=task.id,
                    graph_version=task.graph_version,
                    paths=(),
                )
            checked_task = HostTask.model_validate_json(task.model_dump_json())
            _identity(operation)
            checked_paths = _paths(paths)
            issued_task = self._tasks.get(checked_task.id)
            if issued_task != checked_task:
                return MutationDecision(
                    allowed=False,
                    reason="task_changed",
                    task_id=checked_task.id,
                    graph_version=checked_task.graph_version,
                    paths=checked_paths,
                )
            capability = token if token is not None else self._tokens.get(checked_task.id)
            if (
                capability is None
                or checked_task.preflight is None
                or not checked_task.preflight.authorized
            ):
                return MutationDecision(
                    allowed=False,
                    reason="intent_preflight_required",
                    task_id=checked_task.id,
                    graph_version=checked_task.graph_version,
                    paths=checked_paths,
                )
            verification = await self._workflow.authorization_verify(
                token=capability,
                actor=checked_task.actor,
                repository_id=checked_task.repository_id,
                task_id=checked_task.id,
                graph_version=checked_task.graph_version,
                requested_paths=checked_paths,
            )
            if verification.authorized:
                reason: MutationReason = "authorized"
            elif verification.reason == "scope_mismatch":
                reason = "scope_mismatch"
            elif verification.reason == "graph_mismatch":
                reason = "graph_changed"
            elif verification.reason == "task_mismatch":
                reason = "task_changed"
            else:
                reason = "intent_preflight_required"
            return MutationDecision(
                allowed=verification.authorized,
                reason=reason,
                task_id=checked_task.id,
                graph_version=checked_task.graph_version,
                paths=checked_paths,
            )
        except BaseException as caught:  # noqa: BLE001 - preserve exact cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            task = cast(HostTask, None)
            operation = ""
            paths = ()
            token = capability = None
            if "checked_task" in locals():
                checked_task = cast(HostTask, None)
                checked_paths = ()
                issued_task = cast(HostTask, None)
            if "verification" in locals():
                verification = cast(AuthorizationVerification, None)
            if "reason" in locals():
                reason = "intent_preflight_required"
            self = cast(IntentAgentHostAdapter, None)  # noqa: PLW0642 - scrub traceback state
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        raise RuntimeError("intent host adapter unavailable") from None

    async def after_task(self, task: HostTask, result: HostTaskResult) -> None:
        if not self._enabled:
            return
        task_id: str | None = None
        try:
            checked_task = HostTask.model_validate_json(task.model_dump_json())
            task_id = checked_task.id
            checked_result = HostTaskResult.model_validate_json(result.model_dump_json())
            if checked_result.task_id != checked_task.id:
                raise ValueError("host task result mismatch")
        finally:
            if task_id is not None:
                self._tokens.pop(task_id, None)
                self._tasks.pop(task_id, None)


__all__ = [
    "AgentHostAdapter",
    "HostTask",
    "HostTaskResult",
    "IntentAgentHostAdapter",
    "IntentWorkflowPort",
    "MandatoryHookUnavailable",
    "MutationDecision",
    "MutationReason",
]
