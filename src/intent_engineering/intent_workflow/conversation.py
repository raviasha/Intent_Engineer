"""Immutable, attributed conversation-turn evidence capture."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal, cast

from intent_engineering.core.models import EvidenceIngestion, EvidenceRecord, JsonValue
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROMPT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_CODEX_CONVERSATION_REF = re.compile(
    r"\Acodex-prompt:v1:([0-9a-f]{64}):([0-9a-f]{64}):([0-9a-f]{64})\Z"
)
_MAX_IDENTITY_BYTES = 2 * 1024
_MAX_PROMPT_BYTES = 16 * 1024
_MAX_CONTENT_BYTES = 4 * 1024 * 1024
_MAX_ACL_ENTRIES = 256
CODEX_CONVERSATION_REF_BYTES = 210


class ConversationCaptureError(ValueError):
    """Fixed public failure for invalid or unavailable conversation evidence."""

    def __init__(self) -> None:
        super().__init__("conversation capture unavailable")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _normalize_json(value: object) -> JsonValue:
    if type(value) is dict:
        normalized: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("invalid conversation content")
            normalized[key] = _normalize_json(item)
        return normalized
    if type(value) in {list, tuple}:
        return [_normalize_json(item) for item in cast(tuple[object, ...] | list[object], value)]
    if value is None or type(value) in {str, bool, int, float}:
        _canonical_json(value)
        return cast(JsonValue, value)
    raise ValueError("invalid conversation content")


def _identity(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_IDENTITY_BYTES
    ):
        raise ValueError("invalid conversation identity")
    return value


def _prompt(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _PROMPT_CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_PROMPT_BYTES
    ):
        raise ValueError("invalid conversation request")
    return value


def _codex_digest(label: str, *values: str) -> str:
    return hashlib.sha256(_canonical_json([label, *values])).hexdigest()


def codex_conversation_ref(session_id: str, turn_id: str, request: str) -> str:
    """Bind one host session, turn, and exact human request to a retry-stable locator."""
    checked_session = _identity(session_id)
    checked_turn = _identity(turn_id)
    checked_request = _prompt(request)
    session_digest = _codex_digest("codex-session-v1", checked_session)
    turn_digest = _codex_digest("codex-turn-v1", checked_session, checked_turn)
    request_digest = _codex_digest("codex-request-v1", checked_request)
    reference = f"codex-prompt:v1:{session_digest}:{turn_digest}:{request_digest}"
    if len(reference.encode("utf-8")) != CODEX_CONVERSATION_REF_BYTES:
        raise ValueError("invalid Codex conversation reference")
    return reference


def codex_conversation_binding(conversation_ref: str) -> tuple[str, str, str]:
    """Parse the three opaque digests from one exact public Codex locator."""
    if type(conversation_ref) is not str:
        raise ValueError("invalid Codex conversation reference")
    matched = _CODEX_CONVERSATION_REF.fullmatch(conversation_ref)
    if matched is None:
        raise ValueError("invalid Codex conversation reference")
    return matched.group(1), matched.group(2), matched.group(3)


def verify_codex_conversation_ref(
    conversation_ref: str,
    request: str,
) -> tuple[str, str, str]:
    """Authenticate the caller request against its exact Codex locator digest."""
    binding = codex_conversation_binding(conversation_ref)
    if binding[2] != _codex_digest("codex-request-v1", _prompt(request)):
        raise ValueError("invalid Codex conversation reference")
    return binding


def _utc(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or offset is None
        or offset.total_seconds() != 0
    ):
        raise ValueError("conversation timestamp must use UTC")
    return value.astimezone(UTC)


def _raise_signal(signal: BaseException) -> None:
    raise signal.with_traceback(None)


def _turn_record(
    *,
    conversation_ref: str,
    role: Literal["human", "agent"],
    author: str,
    content: JsonValue,
    captured_at: datetime,
    acl: tuple[str, ...],
) -> EvidenceRecord:
    if role not in {"human", "agent"}:
        raise ValueError("invalid conversation role")
    locator = _identity(conversation_ref)
    principal = _identity(author)
    observed_at = _utc(captured_at)
    if (
        type(acl) is not tuple
        or not acl
        or len(acl) > _MAX_ACL_ENTRIES
        or any(type(item) is not str for item in acl)
    ):
        raise ValueError("invalid conversation ACL")
    normalized_acl = tuple(sorted(_identity(item) for item in acl))
    if len(normalized_acl) != len(set(normalized_acl)):
        raise ValueError("invalid conversation ACL")
    normalized_content = _normalize_json(content)
    encoded_content = _canonical_json(normalized_content)
    if len(encoded_content) > _MAX_CONTENT_BYTES:
        raise ValueError("conversation content exceeds maximum size")
    content_digest = hashlib.sha256(encoded_content).hexdigest()
    version_material = {
        "acl": list(normalized_acl),
        "author": principal,
        "captured_at": observed_at.isoformat().replace("+00:00", "Z"),
        "content_hash": f"sha256:{content_digest}",
        "conversation_ref": locator,
        "role": role,
    }
    version = f"sha256:{hashlib.sha256(_canonical_json(version_material)).hexdigest()}"
    return EvidenceRecord(
        id=f"evidence:conversation:{version.removeprefix('sha256:')}",
        connector_type="conversation",
        external_object_id=locator,
        external_version=version,
        author=principal,
        observed_at=observed_at,
        source_locator=locator,
        content_hash=f"sha256:{content_digest}",
        payload={"role": role, "content": normalized_content},
        acl=normalized_acl,
    )


def validate_conversation_record(
    record: EvidenceRecord,
    *,
    conversation_ref: str,
    role: Literal["human", "agent"],
    author: str,
    acl: tuple[str, ...],
) -> JsonValue:
    """Authenticate every deterministic field of one persisted conversation turn."""
    if type(record) is not EvidenceRecord:
        raise ValueError("invalid conversation evidence")
    payload = record.model_dump(mode="json").get("payload")
    if type(payload) is not dict or set(payload) != {"role", "content"}:
        raise ValueError("invalid conversation evidence")
    content = cast(dict[str, JsonValue], payload)["content"]
    expected = _turn_record(
        conversation_ref=conversation_ref,
        role=role,
        author=author,
        content=content,
        captured_at=record.observed_at,
        acl=acl,
    )
    offset = record.observed_at.utcoffset()
    if offset is None or offset.total_seconds() != 0 or record != expected:
        raise ValueError("invalid conversation evidence")
    return content


def validate_conversation_ingestion(
    records: tuple[EvidenceRecord, ...],
    ingestions: tuple[EvidenceIngestion, ...],
    *,
    evidence_ref: str,
    conversation_ref: str | None,
    author: str,
    acl: tuple[str, ...],
    connector_id: str,
) -> tuple[EvidenceRecord, str]:
    """Resolve one exact current human turn from its authenticated connector ingestion."""
    selected = tuple(
        item
        for item in ingestions
        if item.connector_id == connector_id and item.evidence.id == evidence_ref
    )
    associations = tuple(item for item in ingestions if item.evidence.id == evidence_ref)
    if len(selected) != 1 or associations != selected:
        raise ValueError("invalid conversation evidence")
    ingestion = selected[0]
    record = ingestion.evidence
    indexed = tuple(item for item in records if item.id == evidence_ref)
    locator = record.external_object_id if conversation_ref is None else conversation_ref
    content = validate_conversation_record(
        record,
        conversation_ref=locator,
        role="human",
        author=author,
        acl=acl,
    )
    chain = tuple(
        item
        for item in ingestions
        if item.connector_id == connector_id
        and item.evidence.connector_type == "conversation"
        and item.evidence.external_object_id == locator
    )
    human_versions = tuple(item for item in chain if item.evidence.payload.get("role") == "human")
    if (
        len(indexed) != 1
        or indexed[0] != record
        or type(content) is not str
        or ingestion.predecessor_id is not None
        or human_versions != selected
    ):
        raise ValueError("invalid conversation evidence")
    return record, content


class ConversationCapture:
    """Record exact human and agent turns through the production evidence ledger."""

    def __init__(
        self,
        evidence_store: JsonlEvidenceStore,
        *,
        connector_id: str = "conversation:agent",
    ) -> None:
        self._store = evidence_store
        self.connector_id = _identity(connector_id)

    def _record_turn(
        self,
        *,
        conversation_ref: str,
        role: Literal["human", "agent"],
        author: str,
        content: JsonValue,
        captured_at: datetime,
        acl: tuple[str, ...],
    ) -> EvidenceRecord:
        record = _turn_record(
            conversation_ref=conversation_ref,
            role=role,
            author=author,
            content=content,
            captured_at=captured_at,
            acl=acl,
        )
        self._store.associate(self.connector_id, record)
        return record

    def record_turn(
        self,
        *,
        conversation_ref: str,
        role: Literal["human", "agent"],
        author: str,
        content: JsonValue,
        captured_at: datetime,
        acl: tuple[str, ...],
    ) -> EvidenceRecord:
        """Persist one canonical turn, returning the same record for exact replay."""
        result: EvidenceRecord | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._record_turn(
                conversation_ref=conversation_ref,
                role=role,
                author=author,
                content=content,
                captured_at=captured_at,
                acl=acl,
            )
        except Exception:  # noqa: BLE001 - expose one fixed evidence boundary
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            conversation_ref = ""
            role = "human"
            author = ""
            content = cast(JsonValue, None)
            captured_at = cast(datetime, None)
            acl = ()
        if signal is not None:
            caught_signal = signal
            signal = None
            _raise_signal(caught_signal)
        if failed or result is None:
            raise ConversationCaptureError() from None
        return result


__all__ = [
    "CODEX_CONVERSATION_REF_BYTES",
    "ConversationCapture",
    "ConversationCaptureError",
    "codex_conversation_binding",
    "codex_conversation_ref",
    "validate_conversation_ingestion",
    "validate_conversation_record",
    "verify_codex_conversation_ref",
]
