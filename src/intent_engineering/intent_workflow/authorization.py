"""Process-local, short-lived capabilities for validated intent preflight results."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, TypeVar, cast

from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.models import (
    PreflightResult,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.storage.yaml.graph_store import parse_graph, serialize_graph

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TASK_ID = re.compile(r"^task:sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_WINDOWS_DEVICE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])$", re.IGNORECASE
)
_MAX_ID_BYTES = 2 * 1024
_MAX_PATH_BYTES = 2 * 1024
_MAX_SCOPE_ENTRIES = 256
_MAX_LIVE_GRANTS = 1_024
_DEFAULT_TTL = timedelta(minutes=5)
_T = TypeVar("_T")

type AuthorizationReason = Literal[
    "authorized",
    "unknown",
    "expired",
    "actor_mismatch",
    "repository_mismatch",
    "task_mismatch",
    "request_mismatch",
    "graph_mismatch",
    "scope_mismatch",
]
type AuthorizedClassification = Literal[
    TaskClassification.NO_SEMANTIC_IMPACT,
    TaskClassification.ALIGNED,
]


class AuthorizationError(ValueError):
    """One fixed public failure for capability issuance."""

    def __init__(self) -> None:
        super().__init__("intent authorization unavailable")


class _AuthorizationModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


def _utc(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if type(value) is not datetime or offset is None or offset.total_seconds() != 0:
        raise ValueError("authorization timestamp must use UTC")
    return value.astimezone(UTC)


def _identity(value: str, *, task: bool = False) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_ID_BYTES
        or (task and _TASK_ID.fullmatch(value) is None)
    ):
        raise ValueError("invalid authorization identity")
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
        raise ValueError("invalid authorization path")
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
        raise ValueError("invalid authorization path")
    return value


def _paths(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > _MAX_SCOPE_ENTRIES:
        raise ValueError("invalid authorization scope")
    checked = tuple(_path(value) for value in values)
    if len(checked) != len(set(checked)):
        raise ValueError("invalid authorization scope")
    return tuple(sorted(checked))


def _identities(values: tuple[str, ...]) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > _MAX_SCOPE_ENTRIES:
        raise ValueError("invalid authorization identities")
    checked = tuple(_identity(value) for value in values)
    if len(checked) != len(set(checked)):
        raise ValueError("invalid authorization identities")
    return tuple(sorted(checked))


def _token_digest(token: str) -> str:
    encoded: bytes | None = None
    try:
        encoded = token.encode("ascii")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    finally:
        token = ""
        encoded = None


def _request_digest(request: str) -> str:
    encoded: bytes | None = None
    try:
        if type(request) is not str:
            raise ValueError("invalid authorization request")
        encoded = request.encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    finally:
        request = ""
        encoded = None


def canonical_graph_digest(content: bytes) -> str:
    """Derive one digest from validated canonical graph semantics, never caller text."""
    if type(content) is not bytes:
        raise ValueError("invalid authorization graph")
    graph = parse_graph(content)
    canonical = serialize_graph(graph)
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _raise_signal(signal: BaseException) -> None:
    raise signal.with_traceback(None)


class AuthorizationGrant(_AuthorizationModel):
    """Immutable server-side binding retained only for the issuing process."""

    schema_version: Literal[1] = 1
    digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    actor: str
    repository_id: str
    task_id: str
    request_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    classification: AuthorizedClassification
    graph_version: Annotated[int, Field(ge=0)]
    graph_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    permitted_paths: Annotated[tuple[str, ...], Field(max_length=_MAX_SCOPE_ENTRIES)]
    relevant_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_SCOPE_ENTRIES)]
    issued_at: datetime
    expires_at: datetime

    @field_validator("actor", "repository_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _identity(value)

    @field_validator("task_id")
    @classmethod
    def validate_task_identity(cls, value: str) -> str:
        return _identity(value, task=True)

    @field_validator("permitted_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _paths(values)
        if normalized != values:
            raise ValueError("authorization paths must be canonical")
        return values

    @field_validator("relevant_node_ids")
    @classmethod
    def validate_node_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _identities(values)
        if normalized != values:
            raise ValueError("authorization identities must be canonical")
        return values

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_chronology(self) -> AuthorizationGrant:
        if self.expires_at - self.issued_at != _DEFAULT_TTL:
            raise ValueError("invalid authorization chronology")
        if self.classification is TaskClassification.ALIGNED and not self.relevant_node_ids:
            raise ValueError("aligned authorization requires relevant intent")
        if (
            self.classification is TaskClassification.NO_SEMANTIC_IMPACT
            and self.relevant_node_ids
        ):
            raise ValueError("mechanical authorization cannot bind semantic intent")
        return self


class AuthorizationVerification(_AuthorizationModel):
    """Detached capability decision; it contains no token, digest, or grant."""

    schema_version: Literal[1] = 1
    authorized: bool
    classification: TaskClassification | None = None
    relevant_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_SCOPE_ENTRIES)] = ()
    expires_at: datetime | None = None
    reason: AuthorizationReason

    @field_validator("relevant_node_ids")
    @classmethod
    def validate_node_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _identities(values)
        if normalized != values:
            raise ValueError("authorization identities must be canonical")
        return values

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_shape(self) -> AuthorizationVerification:
        if self.authorized:
            if (
                self.reason != "authorized"
                or self.classification
                not in {
                    TaskClassification.NO_SEMANTIC_IMPACT,
                    TaskClassification.ALIGNED,
                }
                or self.expires_at is None
            ):
                raise ValueError("invalid authorized verification")
        elif (
            self.reason == "authorized"
            or self.classification is not None
            or self.relevant_node_ids
            or self.expires_at is not None
        ):
            raise ValueError("invalid denied verification")
        return self


def _denied(reason: AuthorizationReason = "unknown") -> AuthorizationVerification:
    return AuthorizationVerification(authorized=False, reason=reason)


class AuthorizationIssuer:
    """Own a bounded digest-only grant registry for one local process lifetime."""

    def __init__(self, *, max_live_grants: int = _MAX_LIVE_GRANTS) -> None:
        if (
            type(max_live_grants) is not int
            or max_live_grants < 1
            or max_live_grants > _MAX_LIVE_GRANTS
        ):
            raise ValueError("invalid authorization capacity")
        self._max_live_grants = max_live_grants
        self._grants: OrderedDict[str, AuthorizationGrant] = OrderedDict()
        self._lock = threading.RLock()

    @staticmethod
    def _validated_issue(
        envelope: TaskEnvelope,
        result: PreflightResult,
        graph_content: bytes,
        now: datetime,
    ) -> tuple[
        TaskEnvelope,
        PreflightResult,
        datetime,
        str,
        str,
        tuple[str, ...],
        tuple[str, ...],
    ]:
        if type(envelope) is not TaskEnvelope or type(result) is not PreflightResult:
            raise ValueError("invalid authorization input")
        detached_envelope = TaskEnvelope.model_validate_json(envelope.model_dump_json())
        detached_result = PreflightResult.model_validate_json(result.model_dump_json())
        issued_at = _utc(now)
        created_at = _utc(detached_envelope.created_at)
        actor = _identity(detached_envelope.actor)
        repository_id = _identity(detached_envelope.repository_id)
        task_id = _identity(detached_envelope.id, task=True)
        _identity(detached_envelope.request_evidence_ref)
        scope = _paths(detached_envelope.requested_scope)
        relevant_node_ids = _identities(detached_result.relevant_node_ids)
        graph = parse_graph(graph_content)
        graph_digest = canonical_graph_digest(graph_content)
        request_digest = _request_digest(detached_envelope.request)
        if (
            actor != detached_envelope.actor
            or repository_id != detached_envelope.repository_id
            or task_id != detached_envelope.id
            or issued_at < created_at
            or issued_at - created_at > _DEFAULT_TTL
            or detached_result.task_id != detached_envelope.id
            or detached_result.graph_version != detached_envelope.graph_version
            or graph.version != detached_result.graph_version
            or detached_result.classification
            not in {
                TaskClassification.NO_SEMANTIC_IMPACT,
                TaskClassification.ALIGNED,
            }
            or detached_result.authorized is not True
            or detached_result.permitted_scope != scope
            or detached_result.relevant_node_ids != relevant_node_ids
            or detached_result.questions
            or detached_result.review_case_id is not None
            or (
                detached_result.classification is TaskClassification.ALIGNED
                and not relevant_node_ids
            )
            or (
                detached_result.classification is TaskClassification.NO_SEMANTIC_IMPACT
                and relevant_node_ids
            )
        ):
            raise ValueError("invalid authorization result")
        return (
            detached_envelope,
            detached_result,
            issued_at,
            graph_digest,
            request_digest,
            scope,
            relevant_node_ids,
        )

    @staticmethod
    def _evict_expired(
        grants: OrderedDict[str, AuthorizationGrant],
        now: datetime,
    ) -> None:
        expired = tuple(
            digest for digest, grant in grants.items() if now >= grant.expires_at
        )
        for digest in expired:
            grants.pop(digest, None)

    def issue(
        self,
        envelope: TaskEnvelope,
        result: PreflightResult,
        *,
        graph_content: bytes,
        now: datetime,
    ) -> str:
        """Mint one opaque token for an exact authorized detached preflight."""
        token: str | None = None
        digest: str | None = None
        grant: AuthorizationGrant | None = None
        signal: BaseException | None = None
        failed = False
        try:
            (
                detached_envelope,
                detached_result,
                issued_at,
                graph_digest,
                request_digest,
                scope,
                relevant,
            ) = self._validated_issue(envelope, result, graph_content, now)
            with self._lock:
                self._evict_expired(self._grants, issued_at)
                if len(self._grants) >= self._max_live_grants:
                    raise ValueError("authorization capacity reached")
                for _attempt in range(4):
                    token = secrets.token_urlsafe(32)
                    if _TOKEN.fullmatch(token) is None:
                        raise ValueError("invalid capability token")
                    digest = _token_digest(token)
                    if _DIGEST.fullmatch(digest) is None:
                        raise ValueError("invalid capability digest")
                    if digest not in self._grants:
                        break
                else:
                    raise ValueError("capability token collision")
                grant = AuthorizationGrant(
                    digest=digest,
                    actor=detached_envelope.actor,
                    repository_id=detached_envelope.repository_id,
                    task_id=detached_envelope.id,
                    request_digest=request_digest,
                    classification=cast(
                        AuthorizedClassification,
                        detached_result.classification,
                    ),
                    graph_version=detached_result.graph_version,
                    graph_digest=graph_digest,
                    permitted_paths=scope,
                    relevant_node_ids=relevant,
                    issued_at=issued_at,
                    expires_at=issued_at + _DEFAULT_TTL,
                )
                self._grants[digest] = grant
            return token
        except Exception:  # noqa: BLE001 - expose one fixed issuance boundary
            if digest is not None and grant is not None:
                with self._lock:
                    if self._grants.get(digest) == grant:
                        self._grants.pop(digest, None)
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            if digest is not None and grant is not None:
                with self._lock:
                    if self._grants.get(digest) == grant:
                        self._grants.pop(digest, None)
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            envelope = cast(TaskEnvelope, None)
            result = cast(PreflightResult, None)
            now = cast(datetime, None)
            graph_content = b""
            token = None
            digest = None
            grant = None
            if "detached_envelope" in locals():
                detached_envelope = cast(TaskEnvelope, None)
                detached_result = cast(PreflightResult, None)
                issued_at = cast(datetime, None)
                graph_digest = ""
                request_digest = ""
                scope = ()
                relevant = ()
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        if failed:
            raise AuthorizationError() from None
        raise AuthorizationError() from None

    def verify(
        self,
        token: str,
        *,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        graph_content: bytes,
        requested_paths: tuple[str, ...],
        now: datetime,
        request_digest: str | None = None,
    ) -> AuthorizationVerification:
        """Verify every exact operation binding without returning registry material."""
        digest: str | None = None
        matched_digest: str | None = None
        grant: AuthorizationGrant | None = None
        result: AuthorizationVerification | None = None
        signal: BaseException | None = None
        try:
            if (
                type(token) is not str
                or _TOKEN.fullmatch(token) is None
                or len(token.encode("utf-8")) > 256
            ):
                return _denied()
            checked_at = _utc(now)
            checked_actor = _identity(actor)
            checked_repository = _identity(repository_id)
            checked_task = _identity(task_id, task=True)
            checked_request_digest = (
                None
                if request_digest is None
                else _identity(request_digest)
            )
            if type(graph_version) is not int or graph_version < 0:
                raise ValueError("invalid graph version")
            checked_graph_digest = canonical_graph_digest(graph_content)
            checked_paths = _paths(requested_paths)
            digest = _token_digest(token)
            with self._lock:
                for candidate in self._grants:
                    if hmac.compare_digest(digest, candidate):
                        matched_digest = candidate
                if matched_digest is None:
                    return _denied()
                grant = self._grants[matched_digest]
                if checked_at >= grant.expires_at:
                    self._grants.pop(matched_digest, None)
                    return _denied("expired")
                if checked_actor != grant.actor:
                    return _denied("actor_mismatch")
                if checked_repository != grant.repository_id:
                    return _denied("repository_mismatch")
                if checked_task != grant.task_id:
                    return _denied("task_mismatch")
                if (
                    checked_request_digest is not None
                    and (
                        _DIGEST.fullmatch(checked_request_digest) is None
                        or not hmac.compare_digest(checked_request_digest, grant.request_digest)
                    )
                ):
                    return _denied("request_mismatch")
                if (
                    graph_version != grant.graph_version
                    or checked_graph_digest != grant.graph_digest
                ):
                    return _denied("graph_mismatch")
                if (
                    (not checked_paths and grant.permitted_paths)
                    or not set(checked_paths).issubset(grant.permitted_paths)
                ):
                    return _denied("scope_mismatch")
                result = AuthorizationVerification(
                    authorized=True,
                    classification=grant.classification,
                    relevant_node_ids=grant.relevant_node_ids,
                    expires_at=grant.expires_at,
                    reason="authorized",
                )
            return result
        except Exception:  # noqa: BLE001 - malformed and mismatched input is one denial
            return _denied()
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            token = actor = repository_id = task_id = ""
            graph_version = -1
            graph_content = b""
            requested_paths = ()
            request_digest = None
            now = cast(datetime, None)
            digest = matched_digest = None
            grant = None
            if "checked_graph_digest" in locals():
                checked_graph_digest = ""
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        return _denied()

    def revoke_all(self) -> None:
        """Atomically invalidate every capability owned by this process."""
        with self._lock:
            self._grants.clear()

    def consume(
        self,
        token: str,
        *,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        graph_content: bytes,
        requested_paths: tuple[str, ...],
        now: datetime,
        action: Callable[[AuthorizationVerification], _T],
        request_digest: str | None = None,
    ) -> _T | None:
        """Verify and consume one capability while its authorized action commits."""
        digest: str | None = None
        verification: AuthorizationVerification | None = None
        result: _T | None = None
        signal: BaseException | None = None
        try:
            if not callable(action):
                return None
            with self._lock:
                verification = self.verify(
                    token,
                    actor=actor,
                    repository_id=repository_id,
                    task_id=task_id,
                    graph_version=graph_version,
                    graph_content=graph_content,
                    requested_paths=requested_paths,
                    now=now,
                    request_digest=request_digest,
                )
                if not verification.authorized:
                    return None
                digest = _token_digest(token)
                grant = self._grants.get(digest)
                if grant is None:
                    return None
                self._grants.pop(digest, None)
                result = action(verification)
                return result
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            token = actor = repository_id = task_id = ""
            graph_version = -1
            graph_content = b""
            requested_paths = ()
            request_digest = None
            now = cast(datetime, None)
            action = cast(Callable[[AuthorizationVerification], _T], None)
            digest = None
            verification = None
            result = None
            if "grant" in locals():
                grant = None
            self = cast(AuthorizationIssuer, None)  # noqa: PLW0642 - scrub traceback state
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        return None

    def revoke(self, token: str) -> None:
        """Remove only the exact capability when a post-issue live check fails."""
        digest: str | None = None
        try:
            if type(token) is not str or _TOKEN.fullmatch(token) is None:
                return
            digest = _token_digest(token)
            with self._lock:
                self._grants.pop(digest, None)
        finally:
            token = ""
            digest = None


__all__ = [
    "AuthorizationError",
    "AuthorizationGrant",
    "AuthorizationIssuer",
    "AuthorizationReason",
    "AuthorizationVerification",
    "canonical_graph_digest",
]
