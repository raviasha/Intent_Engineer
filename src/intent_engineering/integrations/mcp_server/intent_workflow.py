"""Bounded provider-neutral MCP port for reviewed intent onboarding."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Never, Protocol, cast

from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import BeforeValidator, ConfigDict, Field, ValidationInfo, field_validator

from intent_engineering.cli.intent_workflow import (
    _bootstrap_service,
    _principals,
    _snapshot_config,
    proposal_payload,
)
from intent_engineering.cli.runtime import Runtime
from intent_engineering.core.models import JsonValue, ProjectConfig
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.intent_workflow.authorization import (
    AuthorizationIssuer,
    AuthorizationVerification,
    canonical_graph_digest,
)
from intent_engineering.intent_workflow.bootstrap import BootstrapSubmission
from intent_engineering.intent_workflow.clarification import (
    ClarificationCoordinator,
    ProposalConfirmationResult,
    ProposalConfirmationService,
)
from intent_engineering.intent_workflow.conversation import (
    CODEX_CONVERSATION_REF_BYTES,
    ConversationCapture,
    codex_conversation_binding,
    validate_conversation_ingestion,
    validate_conversation_record,
    verify_codex_conversation_ref,
)
from intent_engineering.intent_workflow.models import (
    ClarificationIntentProposal,
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    ClarificationSession,
    PreflightResult,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.preflight import (
    AgentClassificationSubmission,
    AuthenticatedPreflightResult,
    PreflightService,
    classification_evidence_content,
)
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.secure import SecureDirectory, SecureFile, SecureRead
from intent_engineering.storage.transaction import LocalTransactionExtraReadPolicy
from intent_engineering.storage.yaml.graph_store import parse_graph

_PROPOSE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_SHOW = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_CONFIRM = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_PREFLIGHT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_ADVISORY_PREFLIGHT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_VERIFY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_CLARIFY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
_CLARIFY_CONFIRM = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
_MAX_REQUEST_BYTES = 1_048_576
_MAX_CONFIRMED_NODES = 10_000
_MAX_AUTHORIZATION_ITEMS = 256
_MAX_AUTHORIZATION_FIELD_BYTES = 2_048
_MAX_TOKEN_BYTES = 256
_MAX_JSON_DEPTH = 128
_MAX_JSON_NODES = 65_536
_MAX_CONNECTOR_BINDING_FILES = 256
_MAX_CONNECTOR_BINDING_DEPTH = 8
_MAX_CONNECTOR_BINDING_FILE_BYTES = 1_048_576
_MAX_CONNECTOR_BINDING_TOTAL_BYTES = 8_388_608
_PROPOSAL_ID = r"^proposal:sha256:[0-9a-f]{64}$"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_TASK_ID = r"^task:sha256:[0-9a-f]{64}$"
_CLARIFICATION_ID = r"^clarification:sha256:[0-9a-f]{64}$"
_CONVERSATION_EVIDENCE_ID = r"^evidence:conversation:[0-9a-f]{64}$"
_CODEX_CONVERSATION_REF = r"^codex-prompt:v1:[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DEVICE = re.compile(r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])$", re.IGNORECASE)
type _ProposalIdInput = Annotated[str, Field(pattern=_PROPOSAL_ID)]
type _DigestInput = Annotated[str, Field(pattern=_DIGEST)]
type _NodeIdsInput = Annotated[
    list[str],
    Field(min_length=1, max_length=_MAX_CONFIRMED_NODES),
]


def _task_envelope_input(value: object) -> TaskEnvelope:
    encoded: bytes | None = None
    envelope: TaskEnvelope | None = None
    raw_created_at: object = None
    serialized_created_at: object = None
    try:
        if type(value) is TaskEnvelope:
            encoded = _canonical_json(value.model_dump(mode="json"))
        elif type(value) is dict:
            encoded = _canonical_json(value)
        else:
            raise ValueError("invalid workflow request")
        envelope = TaskEnvelope.model_validate_json(encoded)
        if type(value) is dict:
            raw_created_at = cast(dict[str, object], value).get("created_at")
            serialized_created_at = envelope.model_dump(mode="json")["created_at"]
            if (
                type(raw_created_at) is not str
                or not raw_created_at.endswith("Z")
                or raw_created_at != serialized_created_at
            ):
                raise ValueError("invalid workflow request")
        return envelope
    finally:
        value = raw_created_at = serialized_created_at = None
        encoded = None
        envelope = None


def _classification_input(value: object) -> AgentClassificationSubmission:
    encoded: bytes | None = None
    submission: AgentClassificationSubmission | None = None
    try:
        if type(value) is AgentClassificationSubmission:
            encoded = _canonical_json(value.model_dump(mode="json"))
        elif type(value) is dict:
            encoded = _canonical_json(value)
        else:
            raise ValueError("invalid workflow request")
        submission = AgentClassificationSubmission.model_validate_json(encoded)
        return submission
    finally:
        value = None
        encoded = None
        submission = None


type _TaskEnvelopeInput = Annotated[TaskEnvelope, BeforeValidator(_task_envelope_input)]
type _AgentClassificationInput = Annotated[
    AgentClassificationSubmission,
    BeforeValidator(_classification_input),
]


type _TokenInput = Annotated[str, Field(min_length=1, max_length=_MAX_TOKEN_BYTES)]
type _AuthorizationIdentityInput = Annotated[
    str,
    Field(min_length=1, max_length=_MAX_AUTHORIZATION_FIELD_BYTES),
]
type _AuthorizationTaskInput = Annotated[
    str,
    Field(pattern=_TASK_ID, max_length=_MAX_AUTHORIZATION_FIELD_BYTES),
]
type _AuthorizationPathsInput = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=_MAX_AUTHORIZATION_FIELD_BYTES)]],
    Field(max_length=_MAX_AUTHORIZATION_ITEMS),
]


def _question_input(value: object) -> ClarificationQuestionInput:
    encoded: bytes | None = None
    question: ClarificationQuestionInput | None = None
    try:
        if type(value) is ClarificationQuestionInput:
            encoded = _canonical_json(value.model_dump(mode="json"))
        elif type(value) is dict:
            encoded = _canonical_json(value)
        else:
            raise ValueError("invalid workflow request")
        question = ClarificationQuestionInput.model_validate_json(encoded)
        return question
    finally:
        value = None
        encoded = None
        question = None


def _clarification_submission_input(value: object) -> ClarificationProposalSubmission:
    encoded: bytes | None = None
    submission: ClarificationProposalSubmission | None = None
    try:
        if type(value) is ClarificationProposalSubmission:
            encoded = _canonical_json(value.model_dump(mode="json"))
        elif type(value) is dict:
            _require_canonical_clarification_timestamps(value)
            encoded = _canonical_json(value)
        else:
            raise ValueError("invalid workflow request")
        submission = ClarificationProposalSubmission.model_validate_json(encoded)
        return submission
    finally:
        value = None
        encoded = None
        submission = None


type _ClarificationQuestionInput = Annotated[
    ClarificationQuestionInput,
    BeforeValidator(_question_input),
]
type _ClarificationQuestionsInput = Annotated[
    list[_ClarificationQuestionInput],
    Field(min_length=1, max_length=16),
]
type _ClarificationSubmissionInput = Annotated[
    ClarificationProposalSubmission,
    BeforeValidator(_clarification_submission_input),
]
type _ClarificationIdInput = Annotated[str, Field(pattern=_CLARIFICATION_ID)]
type _ClarificationNodeIdsInput = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=512)]],
    Field(max_length=_MAX_CONFIRMED_NODES),
]


_CLARIFICATION_TIMESTAMP_FIELDS = frozenset(
    {"timestamp", "created_at", "last_modified_at", "last_reassessed_at"}
)


def _canonical_timestamp_spelling(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid workflow request")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid workflow request") from None
    offset = parsed.utcoffset() if parsed.tzinfo is not None else None
    canonical = parsed.isoformat()
    if canonical.endswith("+00:00"):
        canonical = canonical[:-6] + "Z"
    if offset is None or offset.total_seconds() != 0 or value != canonical:
        raise ValueError("invalid workflow request")
    return parsed


def _require_canonical_clarification_timestamps(value: dict[str, object]) -> None:
    pending: list[object] = [value]
    item: object = None
    nested: object = None
    key = ""
    try:
        while pending:
            item = pending.pop()
            if type(item) is dict:
                for key, nested in dict.items(cast(dict[str, object], item)):
                    if key in _CLARIFICATION_TIMESTAMP_FIELDS and nested is not None:
                        _canonical_timestamp_spelling(nested)
                    if type(nested) in {dict, list}:
                        pending.append(nested)
            elif type(item) is list:
                pending.extend(list.__iter__(cast(list[object], item)))
    finally:
        value = {}
        item = nested = None
        key = ""
        pending.clear()


def _submission_input(value: object) -> BootstrapSubmission:
    encoded: bytes | None = None
    submission: BootstrapSubmission | None = None
    try:
        if type(value) is BootstrapSubmission:
            encoded = _canonical_json(value.model_dump(mode="json"))
        elif type(value) is dict:
            encoded = _canonical_json(value)
        else:
            raise ValueError("invalid workflow request")
        submission = BootstrapSubmission.model_validate_json(encoded)
        return submission
    finally:
        value = None
        encoded = None
        submission = None


type _BootstrapSubmissionInput = Annotated[
    BootstrapSubmission,
    BeforeValidator(_submission_input),
]


def _canonical_json(value: object) -> bytes:
    encoded: bytes | None = None
    try:
        _require_exact_json(value)
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise ValueError("workflow request is too large")
        return encoded
    finally:
        value = None
        encoded = None


def _connector_membership_records(
    directory: SecureDirectory,
    *,
    read_content: bool,
) -> tuple[tuple[PurePosixPath, SecureRead], ...]:
    return directory.walk_regular_files_bounded(
        ".yaml",
        max_files=_MAX_CONNECTOR_BINDING_FILES,
        max_depth=_MAX_CONNECTOR_BINDING_DEPTH,
        max_file_bytes=_MAX_CONNECTOR_BINDING_FILE_BYTES,
        max_total_bytes=_MAX_CONNECTOR_BINDING_TOTAL_BYTES,
        reject_symlinks=True,
        read_content=read_content,
    )


def _clarification_authority_read_policies(
    authority_files: Mapping[str, SecureFile],
) -> dict[str, LocalTransactionExtraReadPolicy]:
    policies: dict[str, LocalTransactionExtraReadPolicy] = {}
    for name in authority_files:
        if name.startswith("authority_binding_"):
            policies[name] = LocalTransactionExtraReadPolicy(
                max_bytes=_MAX_CONNECTOR_BINDING_FILE_BYTES,
                nonblocking_regular=True,
                aggregate_group="connector_bindings",
                max_aggregate_bytes=_MAX_CONNECTOR_BINDING_TOTAL_BYTES,
            )
        else:
            policies[name] = LocalTransactionExtraReadPolicy(
                max_bytes=_MAX_CONNECTOR_BINDING_FILE_BYTES,
                nonblocking_regular=True,
            )
    return policies


def _connector_records_digest(
    records: tuple[tuple[PurePosixPath, SecureRead], ...],
) -> str:
    digest = sha256(b"intent.connector-membership.v1\x00")
    encoded = b""
    try:
        for relative, source in records:
            encoded = json.dumps(
                [relative.as_posix(), [list(identity) for identity in source.identities]],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            encoded = b""
        return f"sha256:{digest.hexdigest()}"
    finally:
        encoded = b""


def _connector_membership_snapshot(
    directory: SecureDirectory,
) -> tuple[str, tuple[tuple[PurePosixPath, SecureRead], ...]]:
    records = _connector_membership_records(directory, read_content=True)
    return _connector_records_digest(records), records


def _connector_membership_digest(directory: SecureDirectory) -> str:
    records = _connector_membership_records(directory, read_content=False)
    return _connector_records_digest(records)


def _require_exact_json(value: object) -> None:
    """Require one bounded, unaliased tree of exact JSON-compatible built-ins."""
    pending: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    seen: set[int] = set()
    item: object = None
    key: object = None
    nested: object = None
    mapping: dict[object, object] | None = None
    sequence: list[object] | None = None
    identity: int | None = None
    encoded: bytes | None = None
    depth = 0
    leaving = False
    node_count = 0
    utf8_bytes = 0
    try:
        while pending:
            item, depth, leaving = pending.pop()
            if leaving:
                active.remove(id(item))
                item = None
                continue
            node_count += 1
            if depth > _MAX_JSON_DEPTH or node_count > _MAX_JSON_NODES:
                raise ValueError("invalid workflow request")
            if type(item) is dict:
                mapping = cast(dict[object, object], item)
                identity = id(mapping)
                if identity in active or identity in seen:
                    raise ValueError("invalid workflow request")
                active.add(identity)
                seen.add(identity)
                pending.append((mapping, depth, True))
                for key, nested in dict.items(mapping):
                    if type(key) is not str:
                        raise ValueError("invalid workflow request")
                    node_count += 1
                    encoded = key.encode("utf-8")
                    utf8_bytes += len(encoded)
                    if node_count > _MAX_JSON_NODES or utf8_bytes > _MAX_REQUEST_BYTES:
                        raise ValueError("invalid workflow request")
                    pending.append((nested, depth + 1, False))
            elif type(item) is list:
                sequence = cast(list[object], item)
                identity = id(sequence)
                if identity in active or identity in seen:
                    raise ValueError("invalid workflow request")
                active.add(identity)
                seen.add(identity)
                pending.append((sequence, depth, True))
                pending.extend((nested, depth + 1, False) for nested in list.__iter__(sequence))
            elif type(item) is str:
                encoded = item.encode("utf-8")
                utf8_bytes += len(encoded)
                if utf8_bytes > _MAX_REQUEST_BYTES:
                    raise ValueError("invalid workflow request")
            elif item is not None and type(item) not in {str, bool, int, float}:
                raise ValueError("invalid workflow request")
    finally:
        value = item = key = nested = None
        mapping = None
        sequence = None
        identity = None
        encoded = None
        pending.clear()
        active.clear()
        seen.clear()


class _Request(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BootstrapProposeRequest(_Request):
    submission: BootstrapSubmission

    @field_validator("submission")
    @classmethod
    def require_bounded_submission(cls, value: BootstrapSubmission) -> BootstrapSubmission:
        _canonical_json(value.model_dump(mode="json"))
        return value


class ProposalShowRequest(_Request):
    proposal_id: Annotated[str, Field(pattern=_PROPOSAL_ID)]


class ProposalConfirmRequest(_Request):
    proposal_id: Annotated[str, Field(pattern=_PROPOSAL_ID)]
    proposal_digest: Annotated[str, Field(pattern=_DIGEST)]
    confirmed_node_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512)], ...],
        Field(min_length=1, max_length=_MAX_CONFIRMED_NODES),
    ]

    @field_validator("confirmed_node_ids")
    @classmethod
    def require_unique_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            any(ord(character) < 32 or ord(character) == 127 for character in value)
            for value in values
        ):
            raise ValueError("invalid workflow request")
        return values


def _bounded_identity(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_AUTHORIZATION_FIELD_BYTES
    ):
        raise ValueError("invalid workflow request")
    return value


def _bounded_token(value: str) -> str:
    value = _bounded_identity(value)
    if len(value.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise ValueError("invalid workflow request")
    return value


def _has_reserved_windows_segment(path: PureWindowsPath) -> bool:
    for part in path.parts:
        normalized = part.rstrip(" .")
        stem = normalized.split(".", 1)[0].rstrip(" ")
        if PureWindowsPath(part).is_reserved() or _WINDOWS_DEVICE.fullmatch(stem):
            return True
    return False


def _bounded_path(value: str) -> str:
    value = _bounded_identity(value)
    relative = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        "\\" in value
        or windows.drive
        or windows.is_absolute()
        or _has_reserved_windows_segment(windows)
        or ":" in value
        or relative.is_absolute()
        or relative.as_posix() != value
        or value == "."
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("invalid workflow request")
    return value


class IntentPreflightRequest(_Request):
    envelope: TaskEnvelope
    submission: AgentClassificationSubmission

    @field_validator("envelope")
    @classmethod
    def require_bounded_envelope(cls, value: TaskEnvelope) -> TaskEnvelope:
        _canonical_json(value.model_dump(mode="json"))
        return value

    @field_validator("submission")
    @classmethod
    def require_bounded_submission(
        cls, value: AgentClassificationSubmission
    ) -> AgentClassificationSubmission:
        _canonical_json(value.model_dump(mode="json"))
        return value


class AdvisoryClassificationDraft(_Request):
    """Bounded agent judgment without caller-supplied task or identity authority."""

    classification: TaskClassification
    basis: Annotated[str, Field(min_length=1, max_length=4096)]
    relevant_node_ids: Annotated[tuple[str, ...], Field(max_length=256)] = ()
    evidence_refs: Annotated[tuple[str, ...], Field(max_length=256)] = ()
    semantic_effects: Annotated[tuple[str, ...], Field(max_length=64)] = ()
    uncertainties: Annotated[tuple[str, ...], Field(max_length=64)] = ()
    questions: Annotated[tuple[str, ...], Field(max_length=16)] = ()
    conflict_claims: Annotated[tuple[str, ...], Field(max_length=64)] = ()
    requested_scope: Annotated[tuple[str, ...], Field(max_length=256)] = ()

    @field_validator("basis")
    @classmethod
    def require_basis(cls, value: str) -> str:
        if type(value) is not str or _CONTROL.search(value) or len(value.encode("utf-8")) > 4096:
            raise ValueError("invalid workflow request")
        return value

    @field_validator("relevant_node_ids", "evidence_refs", "requested_scope")
    @classmethod
    def require_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            type(value) is not str
            or not value
            or _CONTROL.search(value)
            or len(value.encode("utf-8")) > _MAX_AUTHORIZATION_FIELD_BYTES
            for value in values
        ) or len(values) != len(set(values)):
            raise ValueError("invalid workflow request")
        return tuple(sorted(values))

    @field_validator("semantic_effects", "uncertainties", "questions", "conflict_claims")
    @classmethod
    def require_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            type(value) is not str
            or not value
            or _CONTROL.search(value)
            or len(value.encode("utf-8")) > 4096
            for value in values
        ) or len(values) != len(set(values)):
            raise ValueError("invalid workflow request")
        return tuple(sorted(values))


class IntentAdvisoryPreflightRequest(_Request):
    conversation_ref: Annotated[
        str,
        Field(
            min_length=CODEX_CONVERSATION_REF_BYTES,
            max_length=CODEX_CONVERSATION_REF_BYTES,
            pattern=_CODEX_CONVERSATION_REF,
        ),
    ]
    request_evidence_ref: Annotated[str, Field(pattern=_CONVERSATION_EVIDENCE_ID)]
    draft: AdvisoryClassificationDraft

    @field_validator("conversation_ref")
    @classmethod
    def require_conversation_ref(cls, value: str) -> str:
        if (
            type(value) is not str
            or _CONTROL.search(value)
            or len(value.encode("utf-8")) != CODEX_CONVERSATION_REF_BYTES
        ):
            raise ValueError("invalid workflow request")
        return value

    @field_validator("request_evidence_ref")
    @classmethod
    def require_request_evidence_ref(cls, value: str) -> str:
        return _bounded_identity(value)

    @field_validator("draft")
    @classmethod
    def require_draft(cls, value: AdvisoryClassificationDraft) -> AdvisoryClassificationDraft:
        _canonical_json(value.model_dump(mode="json"))
        return value


def _advisory_draft_input(value: object) -> AdvisoryClassificationDraft:
    encoded: bytes | None = None
    draft: AdvisoryClassificationDraft | None = None
    try:
        if type(value) is AdvisoryClassificationDraft:
            encoded = _canonical_json(value.model_dump(mode="json"))
        elif type(value) is dict:
            encoded = _canonical_json(value)
        else:
            raise ValueError("invalid workflow request")
        draft = AdvisoryClassificationDraft.model_validate_json(encoded)
        return draft
    finally:
        value = None
        encoded = None
        draft = None


type _AdvisoryDraftInput = Annotated[
    AdvisoryClassificationDraft,
    BeforeValidator(_advisory_draft_input),
]


class AuthorizationVerifyRequest(_Request):
    token: Annotated[str, Field(min_length=1, max_length=_MAX_TOKEN_BYTES)]
    actor: Annotated[str, Field(min_length=1, max_length=_MAX_AUTHORIZATION_FIELD_BYTES)]
    repository_id: Annotated[str, Field(min_length=1, max_length=_MAX_AUTHORIZATION_FIELD_BYTES)]
    task_id: Annotated[
        str,
        Field(pattern=_TASK_ID, max_length=_MAX_AUTHORIZATION_FIELD_BYTES),
    ]
    graph_version: Annotated[int, Field(ge=0)]
    requested_paths: Annotated[
        tuple[
            Annotated[str, Field(min_length=1, max_length=_MAX_AUTHORIZATION_FIELD_BYTES)],
            ...,
        ],
        Field(max_length=_MAX_AUTHORIZATION_ITEMS),
    ] = ()

    @field_validator("token")
    @classmethod
    def require_bounded_token(cls, value: str) -> str:
        return _bounded_token(value)

    @field_validator("actor", "repository_id", "task_id")
    @classmethod
    def require_bounded_identity(cls, value: str) -> str:
        return _bounded_identity(value)

    @field_validator("requested_paths")
    @classmethod
    def require_canonical_unique_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        checked = tuple(_bounded_path(value) for value in values)
        if len(checked) != len(set(checked)):
            raise ValueError("invalid workflow request")
        return tuple(sorted(checked))


def _workflow_timestamp(value: object, info: ValidationInfo) -> object:
    if info.mode != "json":
        return value
    return _canonical_timestamp_spelling(value)


def _require_utc(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("invalid workflow request")
    return value.astimezone(UTC)


class ClarificationOpenRequest(_Request):
    envelope: TaskEnvelope
    classification_evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    questions: Annotated[tuple[ClarificationQuestionInput, ...], Field(min_length=1, max_length=16)]
    opened_by: Annotated[str, Field(min_length=1, max_length=256)]
    opened_at: datetime

    @field_validator("classification_evidence_ref", "opened_by")
    @classmethod
    def require_identity(cls, value: str) -> str:
        return _bounded_identity(value)

    @field_validator("questions")
    @classmethod
    def require_unique_questions(
        cls, values: tuple[ClarificationQuestionInput, ...]
    ) -> tuple[ClarificationQuestionInput, ...]:
        if any(type(item) is not ClarificationQuestionInput for item in values) or len(
            {item.id for item in values}
        ) != len(values):
            raise ValueError("invalid workflow request")
        for item in values:
            _bounded_identity(item.id)
        return values

    @field_validator("opened_at", mode="before")
    @classmethod
    def parse_opened_at(cls, value: object, info: ValidationInfo) -> object:
        return _workflow_timestamp(value, info)

    @field_validator("opened_at")
    @classmethod
    def require_opened_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class ClarificationAnswerRequest(_Request):
    session_id: Annotated[str, Field(pattern=_CLARIFICATION_ID)]
    question_id: Annotated[str, Field(min_length=1, max_length=256)]
    answer_evidence_ref: Annotated[str, Field(pattern=_CONVERSATION_EVIDENCE_ID)]

    @field_validator("session_id", "question_id", "answer_evidence_ref")
    @classmethod
    def require_identity(cls, value: str) -> str:
        return _bounded_identity(value)


class ClarificationProposeRequest(_Request):
    submission: ClarificationProposalSubmission

    @field_validator("submission")
    @classmethod
    def require_bounded_submission(
        cls, value: ClarificationProposalSubmission
    ) -> ClarificationProposalSubmission:
        _canonical_json(value.model_dump(mode="json"))
        return value


class ClarificationConfirmRequest(_Request):
    proposal_id: Annotated[str, Field(pattern=_PROPOSAL_ID)]
    proposal_digest: Annotated[str, Field(pattern=_DIGEST)]
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    at: datetime
    selected_node_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512)], ...],
        Field(max_length=_MAX_CONFIRMED_NODES),
    ] = ()

    @field_validator("actor")
    @classmethod
    def require_actor(cls, value: str) -> str:
        return _bounded_identity(value)

    @field_validator("selected_node_ids")
    @classmethod
    def require_sorted_unique_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values or any(_CONTROL.search(value) for value in values):
            raise ValueError("invalid workflow request")
        return values

    @field_validator("at", mode="before")
    @classmethod
    def parse_at(cls, value: object, info: ValidationInfo) -> object:
        return _workflow_timestamp(value, info)

    @field_validator("at")
    @classmethod
    def require_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class IntentWorkflowPort(Protocol):
    """Narrow workflow contract exposed to MCP registration."""

    async def bootstrap_propose(self, submission: BootstrapSubmission) -> dict[str, object]: ...

    async def proposal_show(self, proposal_id: object) -> dict[str, object]: ...

    async def proposal_confirm(
        self,
        proposal_id: object,
        proposal_digest: object,
        confirmed_node_ids: object,
    ) -> dict[str, object]: ...

    async def preflight(
        self,
        envelope: TaskEnvelope,
        submission: AgentClassificationSubmission,
    ) -> dict[str, object]: ...

    async def advisory_preflight(
        self,
        conversation_ref: str,
        request_evidence_ref: str,
        draft: AdvisoryClassificationDraft,
    ) -> dict[str, object]: ...

    async def authorization_verify(
        self,
        token: str,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        requested_paths: tuple[str, ...],
    ) -> dict[str, object]: ...

    async def clarification_open(
        self,
        envelope: TaskEnvelope,
        classification_evidence_ref: str,
        questions: tuple[ClarificationQuestionInput, ...],
        opened_by: str,
        opened_at: datetime,
    ) -> dict[str, object]: ...

    async def clarification_answer(
        self,
        session_id: str,
        question_id: str,
        answer_evidence_ref: str,
    ) -> dict[str, object]: ...

    async def clarification_propose(
        self,
        submission: ClarificationProposalSubmission,
    ) -> dict[str, object]: ...

    async def clarification_show(self, proposal_id: str) -> dict[str, object]: ...

    async def clarification_confirm(
        self,
        proposal_id: str,
        proposal_digest: str,
        actor: str,
        at: datetime,
        selected_node_ids: tuple[str, ...],
    ) -> dict[str, object]: ...


def _fixed_arguments() -> Never:
    raise MCPError(INVALID_PARAMS, "invalid intent workflow arguments") from None


def validate_intent_workflow_call(name: str, arguments: dict[str, object]) -> None:
    """Validate raw JSON containers before the SDK can coerce or discard input."""
    paths: object = None
    confirmed_node_ids: object = None
    try:
        if type(arguments) is not dict:
            raise ValueError("invalid workflow request")
        _require_exact_json(arguments)
        if type(name) is not str:
            raise ValueError("invalid workflow request")
        if name == "intent_bootstrap_propose":
            if set(arguments) != {"submission"}:
                raise ValueError("invalid workflow request")
            _submission_input(dict.__getitem__(arguments, "submission"))
        elif name == "intent_proposal_show":
            if set(arguments) != {"proposal_id"}:
                raise ValueError("invalid workflow request")
            ProposalShowRequest.model_validate(arguments)
        elif name == "intent_proposal_confirm":
            expected = {"proposal_id", "proposal_digest", "confirmed_node_ids"}
            if set(arguments) != expected:
                raise ValueError("invalid workflow request")
            confirmed_node_ids = dict.__getitem__(arguments, "confirmed_node_ids")
            if type(confirmed_node_ids) is not list:
                raise ValueError("invalid workflow request")
            ProposalConfirmRequest.model_validate(
                {
                    "proposal_id": dict.__getitem__(arguments, "proposal_id"),
                    "proposal_digest": dict.__getitem__(arguments, "proposal_digest"),
                    "confirmed_node_ids": tuple(cast(list[object], confirmed_node_ids)),
                }
            )
        if name == "intent_preflight":
            if set(arguments) != {"envelope", "submission"}:
                raise ValueError("invalid workflow request")
            _task_envelope_input(dict.__getitem__(arguments, "envelope"))
            _classification_input(dict.__getitem__(arguments, "submission"))
        elif name == "intent_advisory_preflight":
            if set(arguments) != {"conversation_ref", "request_evidence_ref", "draft"}:
                raise ValueError("invalid workflow request")
            IntentAdvisoryPreflightRequest.model_validate_json(_canonical_json(arguments))
        elif name == "intent_authorization_verify":
            expected = {
                "token",
                "actor",
                "repository_id",
                "task_id",
                "graph_version",
                "requested_paths",
            }
            paths = dict.get(arguments, "requested_paths")
            if set(arguments) != expected or type(paths) is not list:
                raise ValueError("invalid workflow request")
            AuthorizationVerifyRequest.model_validate(
                {**arguments, "requested_paths": tuple(cast(list[object], paths))}
            )
        elif name == "intent_clarification_open":
            expected = {
                "envelope",
                "classification_evidence_ref",
                "questions",
                "opened_by",
                "opened_at",
            }
            questions = dict.get(arguments, "questions")
            if set(arguments) != expected or type(questions) is not list:
                raise ValueError("invalid workflow request")
            _task_envelope_input(dict.__getitem__(arguments, "envelope"))
            ClarificationOpenRequest.model_validate_json(_canonical_json(arguments))
        elif name == "intent_clarification_answer":
            expected = {"session_id", "question_id", "answer_evidence_ref"}
            if set(arguments) != expected:
                raise ValueError("invalid workflow request")
            ClarificationAnswerRequest.model_validate_json(_canonical_json(arguments))
        elif name == "intent_clarification_propose":
            if set(arguments) != {"submission"}:
                raise ValueError("invalid workflow request")
            _clarification_submission_input(dict.__getitem__(arguments, "submission"))
        elif name == "intent_clarification_show":
            if set(arguments) != {"proposal_id"}:
                raise ValueError("invalid workflow request")
            ProposalShowRequest.model_validate(arguments)
        elif name == "intent_clarification_confirm":
            expected = {
                "proposal_id",
                "proposal_digest",
                "actor",
                "at",
                "selected_node_ids",
            }
            selected_node_ids = dict.get(arguments, "selected_node_ids")
            if set(arguments) != expected or type(selected_node_ids) is not list:
                raise ValueError("invalid workflow request")
            ClarificationConfirmRequest.model_validate_json(_canonical_json(arguments))
        elif name not in {
            "intent_bootstrap_propose",
            "intent_proposal_show",
            "intent_proposal_confirm",
            "intent_advisory_preflight",
            "intent_clarification_show",
        }:
            raise ValueError("invalid workflow request")
    except Exception:  # noqa: BLE001 - one fixed raw-request validation boundary
        raise ValueError("invalid intent workflow arguments") from None
    finally:
        name = ""
        arguments = {}
        paths = None
        confirmed_node_ids = None
        if "questions" in locals():
            questions = None
        if "selected_node_ids" in locals():
            selected_node_ids = None


class McpIntentWorkflowServices:
    """Production adapter over one held runtime and Task 3 governance service."""

    def __init__(self, runtime: Runtime, *, clock: Callable[[], datetime]) -> None:
        self.runtime = runtime
        self._clock = clock
        self._issuer = AuthorizationIssuer()
        config_file = runtime.workspace_directory.file("config.yaml")
        self._config_file = config_file.duplicate()
        try:
            self._preflight = PreflightService(
                transactions=runtime.transactions,
                config_file=config_file,
                agent_principal="agent:codex",
                conversation_connector_id="conversation:codex",
                principal_resolver=lambda config, _snapshot: _principals(runtime, config),
            )
        finally:
            config_file.close()
        confirmation_config = runtime.workspace_directory.file("config.yaml")
        confirmation_policy = runtime.workspace_directory.file("approvals/policy.yaml")
        connector_directory = None
        try:
            connector_directory = runtime.workspace_directory.subdirectory("connectors")
            self._clarification_authority_base_files = {
                "authority_config": confirmation_config.duplicate(),
                "authority_policy": confirmation_policy.duplicate(),
            }
            self._clarification_connector_directory = connector_directory.duplicate()
        finally:
            confirmation_config.close()
            confirmation_policy.close()
            if connector_directory is not None:
                connector_directory.close()

    @staticmethod
    def _rejected() -> dict[str, object]:
        return {
            "schema_version": "1",
            "status": "rejected",
            "reason": "intent_workflow_unavailable",
        }

    async def bootstrap_propose(self, submission: BootstrapSubmission) -> dict[str, object]:
        encoded: bytes | None = None
        candidate: BootstrapSubmission | None = None
        try:
            config, _ = _snapshot_config(self.runtime)
            encoded = _canonical_json(submission.model_dump(mode="json"))
            candidate = BootstrapSubmission.model_validate_json(encoded)
            review = _bootstrap_service(self.runtime, config).propose(
                candidate,
                _principals(self.runtime, config),
            )
            return {
                "schema_version": "1",
                "status": "proposed",
                "proposal_id": review.proposal_id,
                "proposal_digest": review.proposal_digest,
                "graph_version": review.baseline_graph_version,
            }
        except Exception:  # noqa: BLE001 - fixed public result has no submission detail
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            submission = cast(BootstrapSubmission, None)
            candidate = None
            encoded = None

    async def proposal_show(self, proposal_id: object) -> dict[str, object]:
        try:
            config, _ = _snapshot_config(self.runtime)
            if type(proposal_id) is not str or re.fullmatch(_PROPOSAL_ID, proposal_id) is None:
                return self._rejected()
            payload = proposal_payload(self.runtime, config, proposal_id)
            return {"schema_version": "1", "status": "proposed", "proposal": payload}
        except Exception:  # noqa: BLE001 - unauthorized and missing stay indistinguishable
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            proposal_id = None

    async def proposal_confirm(
        self,
        proposal_id: object,
        proposal_digest: object,
        confirmed_node_ids: object,
    ) -> dict[str, object]:
        try:
            config, config_bytes = _snapshot_config(self.runtime)
            request = ProposalConfirmRequest.model_validate(
                {
                    "proposal_id": proposal_id,
                    "proposal_digest": proposal_digest,
                    "confirmed_node_ids": confirmed_node_ids,
                }
            )
            preview = proposal_payload(self.runtime, config, request.proposal_id)
            if preview["proposal_digest"] != request.proposal_digest or _snapshot_config(
                self.runtime
            ) != (config, config_bytes):
                return self._rejected()
            at = self._clock()
            if at.tzinfo is None or at.utcoffset() is None:
                return self._rejected()
            graph = _bootstrap_service(self.runtime, config).activate(
                request.proposal_id,
                confirmed_node_ids=request.confirmed_node_ids,
                actor=config.local_actor,
                at=at.astimezone(UTC),
            )
            return {
                "schema_version": "1",
                "status": "activated",
                "proposal_id": request.proposal_id,
                "graph_version": graph.version,
            }
        except Exception:  # noqa: BLE001 - fixed public result has no confirmation detail
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            proposal_id = proposal_digest = confirmed_node_ids = None

    @staticmethod
    def _denied_verification() -> dict[str, object]:
        return {
            "schema_version": 1,
            "authorized": False,
            "classification": None,
            "relevant_node_ids": [],
            "expires_at": None,
        }

    @staticmethod
    def _verification_payload(
        verification: AuthorizationVerification,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "authorized": verification.authorized,
            "classification": (
                None if verification.classification is None else verification.classification.value
            ),
            "relevant_node_ids": list(verification.relevant_node_ids),
            "expires_at": (
                None
                if verification.expires_at is None
                else verification.expires_at.isoformat().replace("+00:00", "Z")
            ),
        }

    async def preflight(
        self,
        envelope: TaskEnvelope,
        submission: AgentClassificationSubmission,
    ) -> dict[str, object]:
        token: str | None = None
        result: PreflightResult | None = None
        detached_envelope: TaskEnvelope | None = None
        detached_submission: AgentClassificationSubmission | None = None
        authenticated: AuthenticatedPreflightResult | None = None
        minted_token: str | None = None
        try:
            if (
                type(envelope) is not TaskEnvelope
                or type(submission) is not AgentClassificationSubmission
            ):
                return self._rejected()
            detached_envelope = TaskEnvelope.model_validate_json(envelope.model_dump_json())
            detached_submission = AgentClassificationSubmission.model_validate_json(
                submission.model_dump_json()
            )
            config, config_bytes = _snapshot_config(self.runtime)
            principals = _principals(self.runtime, config)
            authenticated = self._preflight.evaluate_authenticated(
                detached_envelope,
                detached_submission,
                principals=principals,
            )
            result = authenticated.result
            payload = cast(dict[str, object], result.model_dump(mode="json"))
            if result.authorized:
                with self.runtime.transactions.read_transaction(
                    {"config": self._config_file}
                ) as transaction:
                    graph_content = transaction.read("graph")
                    graph = parse_graph(graph_content)
                    if (
                        transaction.read("config") != config_bytes
                        or graph.version != result.graph_version
                        or canonical_graph_digest(graph_content) != authenticated.graph_digest
                        or config.project_id != detached_envelope.repository_id
                        or config.local_actor != detached_envelope.actor
                        or _principals(self.runtime, config) != principals
                    ):
                        return self._rejected()
                    minted_token = self._issuer.issue(
                        detached_envelope,
                        result,
                        graph_content=graph_content,
                        now=self._clock(),
                    )
                    if (
                        canonical_graph_digest(transaction.read("graph"))
                        != authenticated.graph_digest
                    ):
                        return self._rejected()
                payload["authorization_token"] = minted_token
                token = minted_token
                minted_token = None
            return payload
        except Exception:  # noqa: BLE001 - one fixed workflow result
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            envelope = cast(TaskEnvelope, None)
            submission = cast(AgentClassificationSubmission, None)
            detached_envelope = None
            detached_submission = None
            result = None
            token = None
            if minted_token is not None:
                self._issuer.revoke(minted_token)
            minted_token = None
            if "payload" in locals():
                payload = {}
            authenticated = None
            if "graph_content" in locals():
                graph_content = b""
            if "principals" in locals():
                principals = frozenset()
            if "config" in locals():
                config = cast(ProjectConfig, None)
                config_bytes = b""

    @staticmethod
    def _advisory_submission(
        envelope: TaskEnvelope,
        draft: AdvisoryClassificationDraft,
        agent_evidence_ref: str,
    ) -> AgentClassificationSubmission:
        return AgentClassificationSubmission(
            task_id=envelope.id,
            task_digest=envelope.digest,
            graph_version=envelope.graph_version,
            classification=draft.classification,
            basis=draft.basis,
            relevant_node_ids=draft.relevant_node_ids,
            evidence_refs=draft.evidence_refs,
            agent_evidence_ref=agent_evidence_ref,
            semantic_effects=draft.semantic_effects,
            uncertainties=draft.uncertainties,
            questions=draft.questions,
            conflict_claims=draft.conflict_claims,
            requested_scope=draft.requested_scope,
        )

    @staticmethod
    def _advisory_content(
        envelope: TaskEnvelope,
        draft: AdvisoryClassificationDraft,
    ) -> dict[str, JsonValue]:
        return classification_evidence_content(
            task_id=envelope.id,
            task_digest=envelope.digest,
            graph_version=envelope.graph_version,
            classification=draft.classification,
            basis=draft.basis,
            relevant_node_ids=draft.relevant_node_ids,
            evidence_refs=draft.evidence_refs,
            semantic_effects=draft.semantic_effects,
            uncertainties=draft.uncertainties,
            questions=draft.questions,
            conflict_claims=draft.conflict_claims,
            requested_scope=draft.requested_scope,
        )

    async def advisory_preflight(
        self,
        conversation_ref: str,
        request_evidence_ref: str,
        draft: AdvisoryClassificationDraft,
    ) -> dict[str, object]:
        authority_files: dict[str, SecureFile] = {}
        detached_draft: AdvisoryClassificationDraft | None = None
        try:
            validated = IntentAdvisoryPreflightRequest.model_validate(
                {
                    "conversation_ref": conversation_ref,
                    "request_evidence_ref": request_evidence_ref,
                    "draft": draft,
                }
            )
            conversation_binding = codex_conversation_binding(validated.conversation_ref)
            detached_draft = AdvisoryClassificationDraft.model_validate_json(
                validated.draft.model_dump_json()
            )
            clarification_authority = True
            try:
                (
                    config,
                    clarification_principals,
                    authority_files,
                    authority_preimages,
                    authority_membership_digest,
                ) = self._clarification_authority()
                authority_files["config"] = authority_files.pop("authority_config")
                authority_preimages = dict(authority_preimages)
                authority_preimages["config"] = authority_preimages.pop("authority_config")
            except Exception:
                policy_file = self._clarification_authority_base_files["authority_policy"]
                if (
                    policy_file.read_optional_nonblocking(
                        max_bytes=_MAX_CONNECTOR_BINDING_FILE_BYTES
                    )
                    is not None
                ):
                    raise
                clarification_authority = False
                config, config_bytes = _snapshot_config(self.runtime)
                clarification_principals = frozenset()
                authority_files = {"config": self._config_file.duplicate()}
                authority_preimages = {"config": config_bytes}
                authority_membership_digest = ""
            authority_policies = _clarification_authority_read_policies(authority_files)
            policy_principals = _principals(self.runtime, config)
            capture = ConversationCapture(
                self.runtime.evidence_store,
                connector_id="conversation:codex",
            )
            with self.runtime.transactions.transaction(
                rollback_base_exceptions=True,
                extras=authority_files,
                extra_read_policies=authority_policies,
            ) as transaction:

                def authority_matches() -> bool:
                    return (
                        not clarification_authority
                        or _connector_membership_digest(self._clarification_connector_directory)
                        == authority_membership_digest
                    ) and (
                        _principals(self.runtime, config) == policy_principals
                        and all(
                            transaction.read_optional(name) == content
                            for name, content in authority_preimages.items()
                        )
                    )

                if not authority_matches():
                    raise ValueError("advisory authority changed")
                graph = parse_graph(transaction.read("graph"))
                evidence, ingestions, legacy_ids = parse_evidence_lines(
                    transaction.read_optional("evidence")
                )
                if validated.request_evidence_ref in legacy_ids:
                    raise ValueError("advisory request evidence unavailable")
                human, human_request = validate_conversation_ingestion(
                    evidence,
                    ingestions,
                    evidence_ref=validated.request_evidence_ref,
                    conversation_ref=validated.conversation_ref,
                    author=config.local_actor,
                    acl=tuple(sorted(policy_principals)),
                    connector_id="conversation:codex",
                )
                verify_codex_conversation_ref(validated.conversation_ref, human_request)
                ledger = tuple(
                    item for item in ingestions if item.connector_id == "conversation:codex"
                )
                for item in ledger:
                    if item.evidence.payload.get("role") != "human":
                        continue
                    try:
                        existing_binding = codex_conversation_binding(
                            item.evidence.external_object_id
                        )
                    except ValueError:
                        continue
                    if (
                        existing_binding[:2] == conversation_binding[:2]
                        and item.evidence.external_object_id != validated.conversation_ref
                    ):
                        raise ValueError("advisory host turn changed")
                envelope = TaskEnvelope(
                    repository_id=config.project_id,
                    actor=config.local_actor,
                    conversation_ref=validated.conversation_ref,
                    request=human_request,
                    request_evidence_ref=human.id,
                    graph_version=graph.version,
                    created_at=human.observed_at,
                    requested_scope=detached_draft.requested_scope,
                )
                expected_content = self._advisory_content(envelope, detached_draft)
                agent_candidates = tuple(
                    item
                    for item in ledger
                    if item.predecessor_id == human.id
                    and item.evidence.external_object_id == validated.conversation_ref
                    and item.evidence.payload.get("role") == "agent"
                )
                if len(agent_candidates) > 1:
                    raise ValueError("divergent advisory replay")
                if agent_candidates:
                    agent_entry = agent_candidates[0]
                    agent_content = validate_conversation_record(
                        agent_entry.evidence,
                        conversation_ref=validated.conversation_ref,
                        role="agent",
                        author="agent:codex",
                        acl=human.acl,
                    )
                    if (
                        agent_content != expected_content
                        or agent_entry.sequence
                        != next(item.sequence for item in ledger if item.evidence.id == human.id)
                        + 1
                    ):
                        raise ValueError("divergent advisory replay")
                    agent_ref = agent_entry.evidence.id
                else:
                    captured_at = self._clock()
                    if (
                        type(captured_at) is not datetime
                        or captured_at.tzinfo is None
                        or captured_at.utcoffset() is None
                    ):
                        raise ValueError("invalid advisory clock")
                    captured_at = max(
                        captured_at.astimezone(UTC),
                        human.observed_at + timedelta(microseconds=1),
                    )
                    agent = capture.record_turn(
                        conversation_ref=validated.conversation_ref,
                        role="agent",
                        author="agent:codex",
                        content=expected_content,
                        captured_at=captured_at,
                        acl=human.acl,
                    )
                    agent_ref = agent.id
                submission = self._advisory_submission(envelope, detached_draft, agent_ref)
                result = self._preflight.evaluate(
                    envelope,
                    submission,
                    principals=policy_principals,
                )
                if result.classification is TaskClassification.NEW_OR_AMBIGUOUS:
                    if not clarification_authority:
                        raise ValueError("clarification authority unavailable")
                    questions = tuple(
                        ClarificationQuestionInput(
                            id=f"question:sha256:{sha256(prompt.encode('utf-8')).hexdigest()}",
                            prompt=prompt,
                        )
                        for prompt in result.questions
                    )
                    session = self._clarification_coordinator(
                        config,
                        authority_files,
                        authority_preimages,
                        authority_membership_digest,
                    ).open(
                        envelope,
                        classification_evidence_ref=agent_ref,
                        questions=questions,
                        opened_by="agent:codex",
                        opened_at=envelope.created_at + timedelta(microseconds=2),
                        principals=clarification_principals,
                    )
                    context = dict(result.context)
                    context["clarification_session"] = cast(
                        JsonValue, session.model_dump(mode="json")
                    )
                    result_payload = result.model_dump(mode="json")
                    result_payload["context"] = context
                    result = PreflightResult.model_validate_json(_canonical_json(result_payload))
                if not authority_matches():
                    raise ValueError("advisory authority changed")
                payload = cast(dict[str, object], result.model_dump(mode="json"))
                if "authorization_token" in payload:
                    raise ValueError("invalid advisory result")
                return payload
        except Exception:  # noqa: BLE001 - fixed token-free workflow boundary
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            conversation_ref = request_evidence_ref = ""
            draft = cast(AdvisoryClassificationDraft, None)
            detached_draft = None
            self._close_clarification_authority(authority_files)
            if "validated" in locals():
                validated = cast(IntentAdvisoryPreflightRequest, None)
            if "conversation_binding" in locals():
                conversation_binding = ("", "", "")
            if "existing_binding" in locals():
                existing_binding = ("", "", "")
            if "payload" in locals():
                payload = {}
            if "human_request" in locals():
                human_request = ""

    async def authorization_verify(
        self,
        token: str,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        requested_paths: tuple[str, ...],
    ) -> dict[str, object]:
        verification: AuthorizationVerification | None = None
        try:
            request = AuthorizationVerifyRequest.model_validate(
                {
                    "token": token,
                    "actor": actor,
                    "repository_id": repository_id,
                    "task_id": task_id,
                    "graph_version": graph_version,
                    "requested_paths": requested_paths,
                }
            )
            config, config_bytes = _snapshot_config(self.runtime)
            principals = _principals(self.runtime, config)
            with self.runtime.transactions.read_transaction(
                {"config": self._config_file}
            ) as transaction:
                graph_content = transaction.read("graph")
                graph = parse_graph(graph_content)
                graph_digest = canonical_graph_digest(graph_content)
                if (
                    transaction.read("config") != config_bytes
                    or request.actor != config.local_actor
                    or request.repository_id != config.project_id
                    or request.graph_version != graph.version
                    or config.local_actor not in principals
                    or _principals(self.runtime, config) != principals
                ):
                    return self._denied_verification()
                verification = self._issuer.verify(
                    request.token,
                    actor=config.local_actor,
                    repository_id=config.project_id,
                    task_id=request.task_id,
                    graph_version=graph.version,
                    graph_content=graph_content,
                    requested_paths=request.requested_paths,
                    now=self._clock(),
                )
                if canonical_graph_digest(transaction.read("graph")) != graph_digest:
                    return self._denied_verification()
            return self._verification_payload(verification)
        except Exception:  # noqa: BLE001 - all denials have one reduced public shape
            return self._denied_verification()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            token = actor = repository_id = task_id = ""
            graph_version = -1
            requested_paths = ()
            verification = None
            if "graph_content" in locals():
                graph_content = b""
                graph_digest = ""
            if "request" in locals():
                request = cast(AuthorizationVerifyRequest, None)
            if "principals" in locals():
                principals = frozenset()
            if "config" in locals():
                config = cast(ProjectConfig, None)
                config_bytes = b""

    def _clarification_authority(
        self,
    ) -> tuple[
        ProjectConfig,
        frozenset[str],
        dict[str, SecureFile],
        dict[str, bytes | None],
        str,
    ]:
        authority_files = {
            name: file.duplicate()
            for name, file in self._clarification_authority_base_files.items()
        }
        try:
            membership_digest, bindings = _connector_membership_snapshot(
                self._clarification_connector_directory
            )
            for index, (relative, _source) in enumerate(bindings):
                authority_files[f"authority_binding_{index}"] = (
                    self._clarification_connector_directory.file(relative)
                )
            snapshot = self.runtime.transactions.snapshot(
                authority_files,
                extra_read_policies=_clarification_authority_read_policies(authority_files),
            )
            if _connector_membership_digest(
                self._clarification_connector_directory
            ) != membership_digest or any(
                snapshot.content.get(f"authority_binding_{index}") != source.content
                for index, (_relative, source) in enumerate(bindings)
            ):
                raise ValueError("clarification authority unavailable")
            config, policy, provider_principals = ProposalConfirmationService._authority(snapshot)
            if config != self.runtime.config:
                raise ValueError("clarification authority unavailable")
            aliases = ProposalConfirmationService._aliases(
                config.local_actor,
                policy,
                provider_principals,
            )
            principals = frozenset({"agent:codex", *aliases})
            if config.local_actor not in principals:
                raise ValueError("clarification authority unavailable")
            preimages = {name: snapshot.content.get(name) for name in authority_files}
            return config, principals, authority_files, preimages, membership_digest
        except BaseException:
            for file in authority_files.values():
                file.close()
            authority_files.clear()
            raise

    @staticmethod
    def _close_clarification_authority(authority_files: dict[str, SecureFile]) -> None:
        for file in authority_files.values():
            file.close()
        authority_files.clear()

    def _clarification_coordinator(
        self,
        config: ProjectConfig,
        authority_files: Mapping[str, SecureFile],
        authority_preimages: Mapping[str, bytes | None],
        authority_membership_digest: str,
    ) -> ClarificationCoordinator:
        return ClarificationCoordinator(
            graph_store=self.runtime.graph_store,
            evidence_store=self.runtime.evidence_store,
            proposal_store=self.runtime.intent_proposals,
            transactions=self.runtime.transactions,
            config=config,
            capture=ConversationCapture(
                self.runtime.evidence_store,
                connector_id="conversation:codex",
            ),
            authority_files=authority_files,
            authority_preimages=authority_preimages,
            authority_read_policies=_clarification_authority_read_policies(authority_files),
            authority_membership_digest=authority_membership_digest,
            authority_membership_resolver=lambda: _connector_membership_digest(
                self._clarification_connector_directory
            ),
        )

    def _clarification_confirmation_service(
        self,
        authority_files: Mapping[str, SecureFile],
        authority_preimages: Mapping[str, bytes | None],
        authority_membership_digest: str,
    ) -> ProposalConfirmationService:
        binding_files = {
            name: file
            for name, file in authority_files.items()
            if name.startswith("authority_binding_")
        }
        return ProposalConfirmationService(
            graph_store=self.runtime.graph_store,
            evidence_store=self.runtime.evidence_store,
            case_store=self.runtime.case_store,
            proposal_store=self.runtime.intent_proposals,
            changeset_executor=LocalChangeSetExecutor(
                self.runtime.graph_store,
                self.runtime.case_store,
                self.runtime.transactions,
            ),
            transactions=self.runtime.transactions,
            config_file=authority_files["authority_config"],
            policy_file=authority_files["authority_policy"],
            binding_files=binding_files,
            authority_read_policies=_clarification_authority_read_policies(authority_files),
            authority_preimages=authority_preimages,
            authority_membership_digest=authority_membership_digest,
            authority_membership_resolver=lambda: _connector_membership_digest(
                self._clarification_connector_directory
            ),
        )

    async def clarification_open(
        self,
        envelope: TaskEnvelope,
        classification_evidence_ref: str,
        questions: tuple[ClarificationQuestionInput, ...],
        opened_by: str,
        opened_at: datetime,
    ) -> dict[str, object]:
        detached_envelope: TaskEnvelope | None = None
        detached_questions: tuple[ClarificationQuestionInput, ...] = ()
        authority_files: dict[str, SecureFile] = {}
        try:
            request = ClarificationOpenRequest.model_validate(
                {
                    "envelope": envelope,
                    "classification_evidence_ref": classification_evidence_ref,
                    "questions": questions,
                    "opened_by": opened_by,
                    "opened_at": opened_at,
                }
            )
            (
                config,
                principals,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ) = self._clarification_authority()
            if request.envelope.actor != config.local_actor or request.opened_by != "agent:codex":
                return self._rejected()
            detached_envelope = TaskEnvelope.model_validate_json(request.envelope.model_dump_json())
            detached_questions = tuple(
                ClarificationQuestionInput.model_validate_json(item.model_dump_json())
                for item in request.questions
            )
            session = self._clarification_coordinator(
                config,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ).open(
                detached_envelope,
                classification_evidence_ref=request.classification_evidence_ref,
                questions=detached_questions,
                opened_by=request.opened_by,
                opened_at=request.opened_at,
                principals=principals,
            )
            return {
                "schema_version": 1,
                "status": session.status,
                "session": session.model_dump(mode="json"),
            }
        except Exception:  # noqa: BLE001 - fixed result contains no question body
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            envelope = cast(TaskEnvelope, None)
            classification_evidence_ref = opened_by = ""
            questions = ()
            opened_at = cast(datetime, None)
            detached_envelope = None
            detached_questions = ()
            self._close_clarification_authority(authority_files)
            if "request" in locals():
                request = cast(ClarificationOpenRequest, None)
            if "session" in locals():
                session = cast(ClarificationSession, None)
            if "principals" in locals():
                principals = frozenset()

    async def clarification_answer(
        self,
        session_id: str,
        question_id: str,
        answer_evidence_ref: str,
    ) -> dict[str, object]:
        authority_files: dict[str, SecureFile] = {}
        try:
            request = ClarificationAnswerRequest.model_validate(
                {
                    "session_id": session_id,
                    "question_id": question_id,
                    "answer_evidence_ref": answer_evidence_ref,
                }
            )
            (
                config,
                principals,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ) = self._clarification_authority()
            session = self._clarification_coordinator(
                config,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ).answer_evidence(
                request.session_id,
                question_id=request.question_id,
                answer_evidence_ref=request.answer_evidence_ref,
                evidence_acl=tuple(sorted(_principals(self.runtime, config))),
                principals=principals,
            )
            return {
                "schema_version": 1,
                "status": session.status,
                "session": session.model_dump(mode="json"),
            }
        except Exception:  # noqa: BLE001 - fixed result contains no answer body
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            session_id = question_id = answer_evidence_ref = ""
            self._close_clarification_authority(authority_files)
            if "request" in locals():
                request = cast(ClarificationAnswerRequest, None)
            if "session" in locals():
                session = cast(ClarificationSession, None)
            if "principals" in locals():
                principals = frozenset()

    async def clarification_propose(
        self,
        submission: ClarificationProposalSubmission,
    ) -> dict[str, object]:
        detached: ClarificationProposalSubmission | None = None
        authority_files: dict[str, SecureFile] = {}
        try:
            request = ClarificationProposeRequest.model_validate({"submission": submission})
            (
                config,
                principals,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ) = self._clarification_authority()
            if request.submission.actor != config.local_actor:
                return self._rejected()
            detached = ClarificationProposalSubmission.model_validate_json(
                request.submission.model_dump_json()
            )
            proposal = self._clarification_coordinator(
                config,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ).propose(
                detached,
                principals=principals,
            )
            return {
                "schema_version": 1,
                "status": "proposed",
                "proposal_id": proposal.id,
                "proposal_digest": proposal.digest,
                "graph_version": proposal.baseline_graph_version,
                "clarification_session_id": proposal.clarification_session_id,
                "task_id": proposal.task_id,
            }
        except Exception:  # noqa: BLE001 - fixed result contains no candidate body
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            submission = cast(ClarificationProposalSubmission, None)
            detached = None
            self._close_clarification_authority(authority_files)
            if "request" in locals():
                request = cast(ClarificationProposeRequest, None)
            if "proposal" in locals():
                proposal = cast(ClarificationIntentProposal, None)
            if "principals" in locals():
                principals = frozenset()

    async def clarification_show(self, proposal_id: str) -> dict[str, object]:
        authority_files: dict[str, SecureFile] = {}
        try:
            request = ProposalShowRequest.model_validate({"proposal_id": proposal_id})
            (
                config,
                _principals_live,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ) = self._clarification_authority()
            authority_policies = _clarification_authority_read_policies(authority_files)
            with self.runtime.transactions.read_transaction(
                authority_files,
                extra_read_policies=authority_policies,
            ) as transaction:

                def authority_matches() -> bool:
                    return _connector_membership_digest(
                        self._clarification_connector_directory
                    ) == authority_membership_digest and all(
                        transaction.read_optional(name) == content
                        for name, content in authority_preimages.items()
                    )

                if not authority_matches():
                    raise ValueError("clarification authority changed")
                snapshot = self.runtime.transactions.snapshot(
                    authority_files,
                    extra_read_policies=authority_policies,
                )
                graph_content = snapshot.content.get("graph")
                if graph_content is None:
                    raise ValueError("clarification graph unavailable")
                graph = parse_graph(graph_content)
                evidence, _ingestions, _legacy = parse_evidence_lines(
                    snapshot.content.get("evidence")
                )
                if self.runtime.intent_proposals.bytes() != snapshot.content.get(
                    "intent_proposals"
                ):
                    raise ValueError("clarification proposal ledger changed")
                proposal = self.runtime.intent_proposals.get(request.proposal_id)
                if not isinstance(proposal, ClarificationIntentProposal):
                    raise TypeError("clarification proposal unavailable")
                decision = self.runtime.intent_proposals.decision_for(proposal.id)
                session = self.runtime.intent_proposals.session(proposal.clarification_session_id)
                events = self.runtime.intent_proposals.clarification_events(session.id)
                _policy_config, policy, provider_principals = (
                    ProposalConfirmationService._authority(snapshot)
                )
                actor_aliases = frozenset(
                    ProposalConfirmationService._aliases(
                        config.local_actor,
                        policy,
                        provider_principals,
                    )
                )
                if (
                    _policy_config != config
                    or decision is not None
                    or session.status != "proposed"
                    or proposal.proposed_by != config.local_actor
                    or proposal.baseline_graph_version != graph.version
                    or not events
                    or events[-1].event_type != "proposed"
                    or events[-1].proposal_id != proposal.id
                    or events[-1].session != session
                    or not refs_allowed(proposal.evidence_refs, evidence, actor_aliases)
                ):
                    raise ValueError("clarification proposal unavailable")
                ProposalConfirmationService._validate_source_roles(
                    proposal,
                    config,
                    evidence,
                    snapshot,
                )
                preview = proposal.model_dump(mode="json")
                preview["proposal_id"] = preview.pop("id")
                preview["proposal_digest"] = proposal.digest
                if not authority_matches():
                    raise ValueError("clarification authority changed")
            return {
                "schema_version": 1,
                "status": "proposed",
                "proposal": preview,
            }
        except Exception:  # noqa: BLE001 - hidden, missing, and unavailable are identical
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            proposal_id = ""
            self._close_clarification_authority(authority_files)
            if "request" in locals():
                request = cast(ProposalShowRequest, None)
            if "preview" in locals():
                preview = {}
            if "proposal" in locals():
                proposal = cast(ClarificationIntentProposal, None)
            if "actor_aliases" in locals():
                actor_aliases = frozenset()

    async def clarification_confirm(
        self,
        proposal_id: str,
        proposal_digest: str,
        actor: str,
        at: datetime,
        selected_node_ids: tuple[str, ...],
    ) -> dict[str, object]:
        authority_files: dict[str, SecureFile] = {}
        try:
            request = ClarificationConfirmRequest.model_validate(
                {
                    "proposal_id": proposal_id,
                    "proposal_digest": proposal_digest,
                    "actor": actor,
                    "at": at,
                    "selected_node_ids": selected_node_ids,
                }
            )
            (
                config,
                _principals_live,
                authority_files,
                authority_preimages,
                authority_membership_digest,
            ) = self._clarification_authority()
            if request.actor != config.local_actor:
                return self._rejected()
            confirmation = self._clarification_confirmation_service(
                authority_files,
                authority_preimages,
                authority_membership_digest,
            )
            try:
                result = confirmation.confirm(
                    request.proposal_id,
                    proposal_digest=request.proposal_digest,
                    actor=request.actor,
                    at=request.at,
                    selected_node_ids=request.selected_node_ids,
                )
            finally:
                confirmation.close()
            return cast(dict[str, object], result.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - fixed result contains no proposal detail
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            proposal_id = proposal_digest = actor = ""
            at = cast(datetime, None)
            selected_node_ids = ()
            self._close_clarification_authority(authority_files)
            if "request" in locals():
                request = cast(ClarificationConfirmRequest, None)
            if "result" in locals():
                result = cast(ProposalConfirmationResult, None)
            if "_principals_live" in locals():
                _principals_live = frozenset()
            if "confirmation" in locals():
                confirmation = cast(ProposalConfirmationService, None)


def load_intent_workflow_services(
    runtime: Runtime,
    *,
    clock: Callable[[], datetime] | None = None,
) -> McpIntentWorkflowServices:
    """Bind workflow tools to the exact already-held runtime snapshot."""
    return McpIntentWorkflowServices(
        runtime,
        clock=(lambda: datetime.now(UTC)) if clock is None else clock,
    )


def register_intent_workflow_tools(
    server: MCPServer,
    services: IntentWorkflowPort,
) -> None:
    """Register only proposal onboarding, inspection, and governed confirmation."""

    @server.tool(
        name="intent_bootstrap_propose",
        annotations=_PROPOSE,
        structured_output=True,
    )
    async def bootstrap_propose(submission: _BootstrapSubmissionInput) -> dict[str, object]:
        try:
            request = BootstrapProposeRequest.model_validate({"submission": submission})
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            submission = cast(BootstrapSubmission, None)
            _fixed_arguments()
        try:
            detached = BootstrapSubmission.model_validate_json(request.submission.model_dump_json())
            return await services.bootstrap_propose(detached)
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            submission = cast(BootstrapSubmission, None)
            detached = cast(BootstrapSubmission, None)
            request = cast(BootstrapProposeRequest, None)

    @server.tool(
        name="intent_proposal_show",
        annotations=_SHOW,
        structured_output=True,
    )
    async def proposal_show(proposal_id: _ProposalIdInput) -> dict[str, object]:
        try:
            request = ProposalShowRequest.model_validate({"proposal_id": proposal_id})
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            proposal_id = ""
            _fixed_arguments()
        try:
            return await services.proposal_show(request.proposal_id)
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            proposal_id = ""
            request = cast(ProposalShowRequest, None)

    @server.tool(
        name="intent_proposal_confirm",
        annotations=_CONFIRM,
        structured_output=True,
    )
    async def proposal_confirm(
        proposal_id: _ProposalIdInput,
        proposal_digest: _DigestInput,
        confirmed_node_ids: _NodeIdsInput,
    ) -> dict[str, object]:
        try:
            request = ProposalConfirmRequest.model_validate(
                {
                    "proposal_id": proposal_id,
                    "proposal_digest": proposal_digest,
                    "confirmed_node_ids": tuple(confirmed_node_ids),
                }
            )
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            proposal_id = proposal_digest = ""
            confirmed_node_ids.clear()
            _fixed_arguments()
        try:
            return await services.proposal_confirm(
                request.proposal_id,
                request.proposal_digest,
                request.confirmed_node_ids,
            )
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            proposal_id = proposal_digest = ""
            confirmed_node_ids.clear()
            request = cast(ProposalConfirmRequest, None)

    @server.tool(
        name="intent_preflight",
        annotations=_PREFLIGHT,
        structured_output=True,
    )
    async def intent_preflight(
        envelope: _TaskEnvelopeInput,
        submission: _AgentClassificationInput,
    ) -> dict[str, object]:
        detached_envelope: TaskEnvelope | None = None
        detached_submission: AgentClassificationSubmission | None = None
        try:
            request = IntentPreflightRequest.model_validate(
                {"envelope": envelope, "submission": submission}
            )
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            envelope = cast(TaskEnvelope, None)
            submission = cast(AgentClassificationSubmission, None)
            _fixed_arguments()
        try:
            detached_envelope = TaskEnvelope.model_validate_json(request.envelope.model_dump_json())
            detached_submission = AgentClassificationSubmission.model_validate_json(
                request.submission.model_dump_json()
            )
            return await services.preflight(detached_envelope, detached_submission)
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            envelope = cast(TaskEnvelope, None)
            submission = cast(AgentClassificationSubmission, None)
            detached_envelope = None
            detached_submission = None
            request = cast(IntentPreflightRequest, None)

    @server.tool(
        name="intent_advisory_preflight",
        annotations=_ADVISORY_PREFLIGHT,
        structured_output=True,
    )
    async def intent_advisory_preflight(
        conversation_ref: Annotated[
            str,
            Field(
                min_length=CODEX_CONVERSATION_REF_BYTES,
                max_length=CODEX_CONVERSATION_REF_BYTES,
                pattern=_CODEX_CONVERSATION_REF,
            ),
        ],
        request_evidence_ref: Annotated[str, Field(pattern=_CONVERSATION_EVIDENCE_ID)],
        draft: _AdvisoryDraftInput,
    ) -> dict[str, object]:
        detached_draft: AdvisoryClassificationDraft | None = None
        try:
            checked = IntentAdvisoryPreflightRequest.model_validate(
                {
                    "conversation_ref": conversation_ref,
                    "request_evidence_ref": request_evidence_ref,
                    "draft": draft,
                }
            )
            detached_draft = AdvisoryClassificationDraft.model_validate_json(
                checked.draft.model_dump_json()
            )
            return await services.advisory_preflight(
                checked.conversation_ref,
                checked.request_evidence_ref,
                detached_draft,
            )
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            conversation_ref = request_evidence_ref = ""
            draft = cast(AdvisoryClassificationDraft, None)
            detached_draft = None
            checked = cast(IntentAdvisoryPreflightRequest, None)

    @server.tool(
        name="intent_authorization_verify",
        annotations=_VERIFY,
        structured_output=True,
    )
    async def intent_authorization_verify(
        token: _TokenInput,
        actor: _AuthorizationIdentityInput,
        repository_id: _AuthorizationIdentityInput,
        task_id: _AuthorizationTaskInput,
        graph_version: Annotated[int, Field(ge=0)],
        requested_paths: _AuthorizationPathsInput,
    ) -> dict[str, object]:
        try:
            request = AuthorizationVerifyRequest.model_validate(
                {
                    "token": token,
                    "actor": actor,
                    "repository_id": repository_id,
                    "task_id": task_id,
                    "graph_version": graph_version,
                    "requested_paths": tuple(requested_paths),
                }
            )
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            token = actor = repository_id = task_id = ""
            graph_version = -1
            requested_paths.clear()
            _fixed_arguments()
        try:
            return await services.authorization_verify(
                request.token,
                request.actor,
                request.repository_id,
                request.task_id,
                request.graph_version,
                request.requested_paths,
            )
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            token = actor = repository_id = task_id = ""
            graph_version = -1
            requested_paths.clear()
            request = cast(AuthorizationVerifyRequest, None)

    @server.tool(
        name="intent_clarification_open",
        annotations=_CLARIFY,
        structured_output=True,
    )
    async def clarification_open(
        envelope: _TaskEnvelopeInput,
        classification_evidence_ref: _AuthorizationIdentityInput,
        questions: _ClarificationQuestionsInput,
        opened_by: _AuthorizationIdentityInput,
        opened_at: datetime,
    ) -> dict[str, object]:
        detached_envelope: TaskEnvelope | None = None
        detached_questions: tuple[ClarificationQuestionInput, ...] = ()
        try:
            request = ClarificationOpenRequest.model_validate(
                {
                    "envelope": envelope,
                    "classification_evidence_ref": classification_evidence_ref,
                    "questions": tuple(questions),
                    "opened_by": opened_by,
                    "opened_at": opened_at,
                }
            )
            detached_envelope = TaskEnvelope.model_validate_json(request.envelope.model_dump_json())
            detached_questions = tuple(
                ClarificationQuestionInput.model_validate_json(item.model_dump_json())
                for item in request.questions
            )
            return await services.clarification_open(
                detached_envelope,
                request.classification_evidence_ref,
                detached_questions,
                request.opened_by,
                request.opened_at,
            )
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            envelope = cast(TaskEnvelope, None)
            classification_evidence_ref = opened_by = ""
            questions.clear()
            opened_at = cast(datetime, None)
            detached_envelope = None
            detached_questions = ()
            request = cast(ClarificationOpenRequest, None)

    @server.tool(
        name="intent_clarification_answer",
        annotations=_CLARIFY,
        structured_output=True,
    )
    async def clarification_answer(
        session_id: _ClarificationIdInput,
        question_id: _AuthorizationIdentityInput,
        answer_evidence_ref: Annotated[str, Field(pattern=_CONVERSATION_EVIDENCE_ID)],
    ) -> dict[str, object]:
        try:
            request = ClarificationAnswerRequest.model_validate(
                {
                    "session_id": session_id,
                    "question_id": question_id,
                    "answer_evidence_ref": answer_evidence_ref,
                }
            )
            return await services.clarification_answer(
                request.session_id,
                request.question_id,
                request.answer_evidence_ref,
            )
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            session_id = question_id = answer_evidence_ref = ""
            request = cast(ClarificationAnswerRequest, None)

    @server.tool(
        name="intent_clarification_propose",
        annotations=_CLARIFY,
        structured_output=True,
    )
    async def clarification_propose(
        submission: _ClarificationSubmissionInput,
    ) -> dict[str, object]:
        detached: ClarificationProposalSubmission | None = None
        try:
            request = ClarificationProposeRequest.model_validate({"submission": submission})
            detached = ClarificationProposalSubmission.model_validate_json(
                request.submission.model_dump_json()
            )
            return await services.clarification_propose(detached)
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            submission = cast(ClarificationProposalSubmission, None)
            detached = None
            request = cast(ClarificationProposeRequest, None)

    @server.tool(
        name="intent_clarification_show",
        annotations=_SHOW,
        structured_output=True,
    )
    async def clarification_show(proposal_id: _ProposalIdInput) -> dict[str, object]:
        try:
            request = ProposalShowRequest.model_validate({"proposal_id": proposal_id})
            return await services.clarification_show(request.proposal_id)
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            proposal_id = ""
            request = cast(ProposalShowRequest, None)

    @server.tool(
        name="intent_clarification_confirm",
        annotations=_CLARIFY_CONFIRM,
        structured_output=True,
    )
    async def clarification_confirm(
        proposal_id: _ProposalIdInput,
        proposal_digest: _DigestInput,
        actor: _AuthorizationIdentityInput,
        at: datetime,
        selected_node_ids: _ClarificationNodeIdsInput,
    ) -> dict[str, object]:
        try:
            request = ClarificationConfirmRequest.model_validate(
                {
                    "proposal_id": proposal_id,
                    "proposal_digest": proposal_digest,
                    "actor": actor,
                    "at": at,
                    "selected_node_ids": tuple(selected_node_ids),
                }
            )
            return await services.clarification_confirm(
                request.proposal_id,
                request.proposal_digest,
                request.actor,
                request.at,
                request.selected_node_ids,
            )
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            proposal_id = proposal_digest = actor = ""
            at = cast(datetime, None)
            selected_node_ids.clear()
            request = cast(ClarificationConfirmRequest, None)


__all__ = [
    "AuthorizationVerifyRequest",
    "ClarificationAnswerRequest",
    "ClarificationConfirmRequest",
    "ClarificationOpenRequest",
    "ClarificationProposeRequest",
    "IntentWorkflowPort",
    "McpIntentWorkflowServices",
    "load_intent_workflow_services",
    "register_intent_workflow_tools",
    "validate_intent_workflow_call",
]
