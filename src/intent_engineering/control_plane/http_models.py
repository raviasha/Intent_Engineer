"""Strict raw-JSON contracts for the local browser control plane."""

from __future__ import annotations

import json
import math
import re
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.control_plane.models import HumanDecisionPayload

MAX_HTTP_BODY_BYTES = 256 * 1024
_MAX_JSON_DEPTH = 128
_MAX_JSON_NODES = 65_536
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_IDENTIFIER_BYTES = 512
_MAX_ANSWER_BYTES = 16 * 1024
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ANSWER_ID_PATTERN = re.compile(r"^answer:[0-9a-f]{64}$")


def require_exact_json(value: object, *, maximum_bytes: int = MAX_HTTP_BODY_BYTES) -> None:
    """Require a finite, bounded, unaliased tree of exact JSON built-ins."""
    pending: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    seen: set[int] = set()
    item: object = None
    nested: object = None
    key: object = None
    mapping: dict[object, object] | None = None
    sequence: list[object] | None = None
    identity: int | None = None
    encoded: bytes | None = None
    depth = 0
    leaving = False
    node_count = 0
    utf8_bytes = 0
    try:
        if type(maximum_bytes) is not int or maximum_bytes < 0:
            raise ValueError("invalid HTTP JSON")
        while pending:
            item, depth, leaving = pending.pop()
            if leaving:
                active.remove(id(item))
                item = None
                continue
            node_count += 1
            if depth > _MAX_JSON_DEPTH or node_count > _MAX_JSON_NODES:
                raise ValueError("invalid HTTP JSON")
            if type(item) is dict:
                mapping = cast(dict[object, object], item)
                identity = id(mapping)
                if identity in active or identity in seen:
                    raise ValueError("invalid HTTP JSON")
                active.add(identity)
                seen.add(identity)
                pending.append((mapping, depth, True))
                for key, nested in dict.items(mapping):
                    if type(key) is not str:
                        raise ValueError("invalid HTTP JSON")
                    encoded = key.encode("utf-8")
                    utf8_bytes += len(encoded)
                    node_count += 1
                    if node_count > _MAX_JSON_NODES or utf8_bytes > maximum_bytes:
                        raise ValueError("invalid HTTP JSON")
                    pending.append((nested, depth + 1, False))
            elif type(item) is list:
                sequence = cast(list[object], item)
                identity = id(sequence)
                if identity in active or identity in seen:
                    raise ValueError("invalid HTTP JSON")
                active.add(identity)
                seen.add(identity)
                pending.append((sequence, depth, True))
                pending.extend((nested, depth + 1, False) for nested in list.__iter__(sequence))
            elif type(item) is str:
                encoded = item.encode("utf-8")
                utf8_bytes += len(encoded)
                if utf8_bytes > maximum_bytes:
                    raise ValueError("invalid HTTP JSON")
            elif type(item) is float:
                if not math.isfinite(item):
                    raise ValueError("invalid HTTP JSON")
            elif item is not None and type(item) not in {bool, int}:
                raise ValueError("invalid HTTP JSON")
    finally:
        value = item = nested = key = None
        mapping = None
        sequence = None
        identity = None
        encoded = None
        pending.clear()
        active.clear()
        seen.clear()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    key = ""
    item: object = None
    try:
        for key, item in pairs:
            if key in result:
                raise ValueError("invalid HTTP JSON")
            result[key] = item
        return result
    finally:
        pairs = []
        key = ""
        item = None


def _reject_constant(_value: str) -> object:
    raise ValueError("invalid HTTP JSON")


class HttpRequestModel(BaseModel):
    """Frozen request base that validates the exact pre-coercion JSON tree."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def require_raw_json_tree(cls, value: object) -> object:
        require_exact_json(value)
        if type(value) is not dict:
            raise ValueError("invalid HTTP JSON")
        return value


class RegistrationOptionsRequest(HttpRequestModel):
    """An intentionally empty request; actor, origin, and time are server-owned."""


class RegistrationVerifyRequest(HttpRequestModel):
    """One exact browser credential response for repository-bound enrollment."""

    response: dict[str, object]


class DecisionOptionsRequest(HttpRequestModel):
    """One canonical decision payload for WebAuthn option issuance."""

    payload: HumanDecisionPayload

    @field_validator("payload", mode="before")
    @classmethod
    def require_canonical_payload(cls, value: object) -> HumanDecisionPayload:
        encoded: bytes | None = None
        try:
            if type(value) is not dict:
                raise ValueError("invalid HTTP request")
            encoded = canonical_json_object(value)
            return HumanDecisionPayload.model_validate_json(encoded)
        finally:
            value = None
            encoded = None


class DecisionVerifyRequest(HttpRequestModel):
    """One exact assertion and the canonical decision it proves."""

    response: dict[str, object]
    payload: HumanDecisionPayload

    @field_validator("payload", mode="before")
    @classmethod
    def require_canonical_payload(cls, value: object) -> HumanDecisionPayload:
        encoded: bytes | None = None
        try:
            if type(value) is not dict:
                raise ValueError("invalid HTTP request")
            encoded = canonical_json_object(value)
            return HumanDecisionPayload.model_validate_json(encoded)
        finally:
            value = None
            encoded = None


class ClarificationAnswerPreviewRequest(HttpRequestModel):
    """One bounded private answer submitted only for authoritative preview."""

    session_id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER_BYTES)
    question_id: str = Field(min_length=1, max_length=256)
    answer: str = Field(min_length=1, max_length=_MAX_ANSWER_BYTES)

    @field_validator("session_id", "question_id", "answer", mode="before")
    @classmethod
    def require_exact_answer_strings(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("invalid clarification answer request")
        return value

    @field_validator("session_id", "question_id", "answer")
    @classmethod
    def bound_answer_bytes(cls, value: str, info: ValidationInfo) -> str:
        maximum = {
            "answer": _MAX_ANSWER_BYTES,
            "question_id": 256,
            "session_id": _MAX_IDENTIFIER_BYTES,
        }[str(info.field_name)]
        if not value or len(value.encode("utf-8")) > maximum:
            raise ValueError("invalid clarification answer request")
        return value


class ClarificationAnswerDiscardRequest(HttpRequestModel):
    """One opaque pending answer identifier to forget without granting authority."""

    answer_id: str = Field(min_length=71, max_length=71)

    @field_validator("answer_id", mode="before")
    @classmethod
    def require_exact_answer_id(cls, value: object) -> str:
        if type(value) is not str or _ANSWER_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid clarification answer discard")
        return value


class _HttpResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def require_detached_json_tree(cls, value: object) -> object:
        require_exact_json(value, maximum_bytes=_MAX_RESPONSE_BYTES)
        if type(value) is not dict:
            raise ValueError("invalid HTTP response")
        return value

    @field_validator("schema_version", mode="before", check_fields=False)
    @classmethod
    def require_exact_schema_version(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("invalid HTTP response")
        return value


class StatusResponse(_HttpResponseModel):
    schema_version: Literal[1]
    status: Literal[
        "ready",
        "onboarding_required",
        "human_attention_required",
        "offline_stale",
        "shared_state_unavailable",
        "shared_state_invalid",
        "upgrade_required",
        "local_only",
    ]
    attention_route: Literal["home", "onboarding", "inbox", "proposal", "team_state"]
    project_id: str = Field(min_length=1, max_length=512)
    repository_id: str = Field(min_length=1, max_length=512)
    graph_version: int = Field(ge=0, le=2**63 - 1)
    pending_proposal_ids: list[str] = Field(max_length=256)
    open_case_ids: list[str] = Field(max_length=256)

    @field_validator("graph_version", mode="before")
    @classmethod
    def require_exact_graph_version(cls, value: object) -> int:
        if type(value) is not int:
            raise ValueError("invalid HTTP response")
        return value

    @field_validator("project_id", "repository_id", mode="before")
    @classmethod
    def require_exact_identifier(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("invalid HTTP response")
        return value

    @field_validator("pending_proposal_ids", "open_case_ids", mode="before")
    @classmethod
    def require_exact_identifier_list(cls, value: object) -> list[str]:
        if type(value) is not list or any(type(item) is not str for item in value):
            raise ValueError("invalid HTTP response")
        return cast(list[str], value)


class ClarificationQuestionResponse(_HttpResponseModel):
    id: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=2048)
    required: bool


class ClarificationSessionResponse(_HttpResponseModel):
    id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER_BYTES)
    task_id: str = Field(min_length=1, max_length=256)
    questions: list[ClarificationQuestionResponse] = Field(min_length=1, max_length=16)


class InboxResponse(_HttpResponseModel):
    schema_version: Literal[1] = 1
    pending_proposal_ids: list[str] = Field(max_length=256)
    open_case_ids: list[str] = Field(max_length=256)
    clarification_sessions: list[ClarificationSessionResponse] = Field(max_length=64)

    @field_validator("pending_proposal_ids", "open_case_ids", mode="before")
    @classmethod
    def require_exact_identifier_list(cls, value: object) -> list[str]:
        if type(value) is not list or any(type(item) is not str for item in value):
            raise ValueError("invalid HTTP response")
        return cast(list[str], value)


class ClarificationAnswerPreviewProjection(_HttpResponseModel):
    kind: Literal["clarification_answer"]
    session_id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER_BYTES)
    question_id: str = Field(min_length=1, max_length=256)
    answer_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    result_evidence_id: str = Field(min_length=1, max_length=_MAX_IDENTIFIER_BYTES)
    result_evidence_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)


class ClarificationAnswerPreviewResponse(_HttpResponseModel):
    schema_version: Literal[1] = 1
    preview_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    preview: ClarificationAnswerPreviewProjection
    payload: HumanDecisionPayload

    @field_validator("payload", mode="before")
    @classmethod
    def require_canonical_payload(cls, value: object) -> HumanDecisionPayload:
        encoded: bytes | None = None
        try:
            if type(value) is not dict:
                raise ValueError("invalid HTTP response")
            encoded = canonical_json_object(value)
            return HumanDecisionPayload.model_validate_json(encoded)
        finally:
            value = None
            encoded = None


class ClarificationAnswerDiscardResponse(_HttpResponseModel):
    schema_version: Literal[1] = 1
    status: Literal["discarded"]
    answer_id: str = Field(pattern=_ANSWER_ID_PATTERN.pattern)


def parse_request_bytes[RequestModelT: HttpRequestModel](
    body: bytes, model: type[RequestModelT]
) -> RequestModelT:
    """Decode one bounded UTF-8 JSON object without duplicate-key loss."""
    text = ""
    loaded: object = None
    result: RequestModelT | None = None
    try:
        if type(body) is not bytes or not body or len(body) > MAX_HTTP_BODY_BYTES:
            raise ValueError("invalid HTTP request")
        text = body.decode("utf-8", errors="strict")
        loaded = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        if type(loaded) is not dict:
            raise ValueError("invalid HTTP request")
        result = model.model_validate(loaded)
        return result
    except (TypeError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("invalid HTTP request") from None
    finally:
        body = b""
        text = ""
        loaded = None
        result = None


def canonical_json_object(value: object) -> bytes:
    """Detach one exact JSON object into canonical UTF-8 bytes."""
    encoded: bytes | None = None
    try:
        require_exact_json(value)
        if type(value) is not dict:
            raise ValueError("invalid HTTP JSON object")
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_HTTP_BODY_BYTES:
            raise ValueError("invalid HTTP JSON object")
        return encoded
    finally:
        value = None
        encoded = None


def detach_response_mapping(value: object) -> dict[str, object]:
    """Return a canonical-copy response mapping after exact output validation."""
    encoded: bytes | None = None
    detached: object = None
    try:
        require_exact_json(value, maximum_bytes=_MAX_RESPONSE_BYTES)
        if type(value) is not dict:
            raise ValueError("invalid HTTP response")
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _MAX_RESPONSE_BYTES:
            raise ValueError("invalid HTTP response")
        detached = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        if type(detached) is not dict:
            raise ValueError("invalid HTTP response")
        return cast(dict[str, object], detached)
    finally:
        value = None
        encoded = None
        detached = None


def parse_response_bytes(value: bytes) -> dict[str, object]:
    """Validate and detach one service-produced JSON object."""
    parsed: object = None
    text = ""
    try:
        if type(value) is not bytes or not value or len(value) > MAX_HTTP_BODY_BYTES:
            raise ValueError("invalid HTTP response")
        text = value.decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
        require_exact_json(parsed)
        if type(parsed) is not dict:
            raise ValueError("invalid HTTP response")
        return detach_response_mapping(parsed)
    finally:
        value = b""
        parsed = None
        text = ""


__all__ = [
    "MAX_HTTP_BODY_BYTES",
    "ClarificationAnswerDiscardRequest",
    "ClarificationAnswerDiscardResponse",
    "ClarificationAnswerPreviewRequest",
    "ClarificationAnswerPreviewResponse",
    "ClarificationQuestionResponse",
    "ClarificationSessionResponse",
    "DecisionOptionsRequest",
    "DecisionVerifyRequest",
    "HttpRequestModel",
    "InboxResponse",
    "RegistrationOptionsRequest",
    "RegistrationVerifyRequest",
    "StatusResponse",
    "canonical_json_object",
    "detach_response_mapping",
    "parse_request_bytes",
    "parse_response_bytes",
    "require_exact_json",
]
