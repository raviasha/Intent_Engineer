"""Strict, detached records exchanged by an intent-aware agent workflow."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from intent_engineering.core.models import ChangeSet, JsonValue, SourceRole, SourceRoleAssignment
from intent_engineering.core.models._base import StrictModel

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROMPT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_SCOPE_BYTES = 2 * 1024
_MAX_SCOPE_ENTRIES = 256
_MAX_CONVERSATION_REF_LENGTH = 512
_MAX_DECISION_NODE_IDS = 10_000
_MAX_CLARIFICATION_ITEMS = 16
_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"


def _canonical_digest(material: object) -> str:
    """Return the SHA-256 identity digest for JSON-safe public record material."""
    encoded = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _validate_json(value: object) -> None:
    """Reject non-JSON values and subclasses before Pydantic can coerce them."""
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError("context JSON keys must be strings")
            _validate_json(item)
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _validate_json(item)
        return
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float and isfinite(value):
        return
    raise ValueError("context must contain exact JSON containers")


def _freeze_json(value: JsonValue) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return cast(JsonValue, value)


def _task_text(value: str, *, label: str, limit: int, prompt: bool = False) -> str:
    if (prompt and _PROMPT_CONTROL.search(value)) or (not prompt and _CONTROL.search(value)):
        raise ValueError(f"invalid task text: {label}")
    if len(value.encode("utf-8")) > limit:
        raise ValueError(f"{label} exceeds maximum size")
    return value


class ProposalKind(StrEnum):
    BOOTSTRAP = "bootstrap"
    REQUIREMENT = "requirement"


class TaskClassification(StrEnum):
    NO_SEMANTIC_IMPACT = "no_semantic_impact"
    ALIGNED = "aligned"
    NEW_OR_AMBIGUOUS = "new_or_ambiguous"
    CONFLICTING = "conflicting"


class _WorkflowModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)


class IntentProposal(_WorkflowModel):
    """Untrusted, content-addressed agent proposal awaiting deterministic validation."""

    schema_version: Literal[1] = 1
    id: str
    kind: ProposalKind
    proposed_by: str
    proposed_at: datetime
    baseline_graph_version: int
    evidence_refs: tuple[str, ...]
    source_roles: tuple[SourceRoleAssignment, ...]
    changeset: ChangeSet
    core_node_ids: tuple[str, ...] = ()
    provisional_node_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    unanswered_questions: tuple[str, ...] = ()
    conflicting_authors: tuple[str, ...] = ()
    destructive: bool = False

    @field_validator("source_roles")
    @classmethod
    def normalize_source_roles(
        cls, source_roles: tuple[SourceRoleAssignment, ...]
    ) -> tuple[SourceRoleAssignment, ...]:
        normalized = tuple(sorted(source_roles, key=lambda item: (item.connector_id, item.scope)))
        pairs = tuple((item.connector_id, item.scope) for item in normalized)
        if len(pairs) != len(set(pairs)):
            raise ValueError("duplicate source role assignment")
        return normalized

    @property
    def digest(self) -> str:
        return _canonical_digest(self.model_dump(mode="json", exclude={"id"}))

    @model_validator(mode="after")
    def validate_identity(self) -> IntentProposal:
        if self.id != f"proposal:{self.digest}":
            raise ValueError("proposal identifier does not match canonical hash")
        return self


class ClarificationIntentProposal(IntentProposal):
    """A requirement proposal bound to one completed clarification session."""

    schema_version: Literal[2] = 2  # type: ignore[assignment]
    clarification_session_id: Annotated[str, Field(min_length=1, max_length=256)]
    task_id: Annotated[str, Field(min_length=1, max_length=256)]


class ProposalDecision(_WorkflowModel):
    """Independent, immutable human confirmation or rejection of one exact proposal."""

    schema_version: Literal[1] = 1
    id: str
    proposal_id: str
    proposal_digest: str
    actor: str
    actor_aliases: tuple[str, ...]
    decided_at: datetime
    action: Literal["confirm", "reject"]
    baseline_graph_version: int

    @property
    def digest(self) -> str:
        return _canonical_digest(self.model_dump(mode="json", exclude={"id"}))

    @model_validator(mode="after")
    def validate_identity(self) -> ProposalDecision:
        if self.id != f"proposal-decision:{self.digest}":
            raise ValueError("proposal decision identifier does not match canonical hash")
        return self


class ProposalDecisionV2(_WorkflowModel):
    """Exact human confirmation of one proposal activation mutation."""

    schema_version: Literal[2] = 2
    id: str
    proposal_id: str
    proposal_digest: str
    actor: str
    actor_aliases: tuple[str, ...]
    decided_at: datetime
    action: Literal["confirm"]
    baseline_graph_version: int
    confirmed_node_ids: Annotated[
        tuple[str, ...], Field(min_length=1, max_length=_MAX_DECISION_NODE_IDS)
    ]
    activation_changeset_id: Annotated[str, Field(min_length=1)]

    @field_validator("confirmed_node_ids")
    @classmethod
    def require_unique_confirmed_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("confirmed node identifiers must be unique")
        return values

    @property
    def digest(self) -> str:
        return _canonical_digest(self.model_dump(mode="json", exclude={"id"}))

    @model_validator(mode="after")
    def validate_identity(self) -> ProposalDecisionV2:
        if self.id != f"proposal-decision:{self.digest}":
            raise ValueError("proposal decision identifier does not match canonical hash")
        return self


class ProposalDecisionV3(ProposalDecision):
    """Exact governed confirmation of one conversational proposal mutation."""

    schema_version: Literal[3] = 3  # type: ignore[assignment]
    action: Literal["confirm"] = "confirm"
    selected_node_ids: Annotated[
        tuple[str, ...], Field(max_length=_MAX_DECISION_NODE_IDS)
    ] = ()
    activation_changeset_id: Annotated[str, Field(min_length=1)]
    activation_graph_effect_digest: Annotated[str, Field(pattern=_HASH_PATTERN)]
    review_case_id: str | None = None
    review_case_preimage_digest: Annotated[str, Field(pattern=_HASH_PATTERN)] | None = None
    proposal_author_aliases: Annotated[tuple[str, ...], Field(min_length=1)]
    conflicting_author_aliases: tuple[str, ...] = ()

    @field_validator(
        "actor_aliases",
        "selected_node_ids",
        "proposal_author_aliases",
        "conflicting_author_aliases",
    )
    @classmethod
    def require_canonical_unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("governed decision identities must be unique and sorted")
        return values

    @model_validator(mode="after")
    def validate_governed_decision(self) -> ProposalDecisionV3:
        if self.actor not in self.actor_aliases:
            raise ValueError("decision aliases omit actor")
        if self.review_case_id is None and self.conflicting_author_aliases:
            raise ValueError("conflicting aliases require a review case")
        if (self.review_case_id is None) != (self.review_case_preimage_digest is None):
            raise ValueError("review case preimage binding mismatch")
        return self


ProposalDecisionRecord = ProposalDecision | ProposalDecisionV2 | ProposalDecisionV3


def _utc_timestamp(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("clarification timestamp must use UTC")
    return value.astimezone(UTC)


class ClarificationQuestionInput(_WorkflowModel):
    """Ephemeral bounded question text captured as evidence before durable use."""

    id: Annotated[str, Field(min_length=1, max_length=256)]
    prompt: Annotated[str, Field(min_length=1, max_length=2048)]
    required: bool = True

    @field_validator("prompt")
    @classmethod
    def bound_prompt_bytes(cls, value: str) -> str:
        if _CONTROL.search(value) or len(value.encode("utf-8")) > 2048:
            raise ValueError("invalid clarification prompt")
        return value


class ClarificationQuestion(_WorkflowModel):
    """A question ledger reference; raw prompt text remains only in evidence."""

    id: Annotated[str, Field(min_length=1, max_length=256)]
    prompt_digest: Annotated[str, Field(pattern=_HASH_PATTERN)]
    evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    author: Annotated[str, Field(min_length=1, max_length=256)]
    asked_at: datetime
    predecessor_evidence_ref: str | None = None
    required: bool = True

    @field_validator("asked_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)


class ClarificationAnswer(_WorkflowModel):
    """An attributed answer digest/reference without a duplicate raw answer body."""

    question_id: Annotated[str, Field(min_length=1, max_length=256)]
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    answered_at: datetime
    evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    answer_digest: Annotated[str, Field(pattern=_HASH_PATTERN)]
    predecessor_evidence_ref: str | None

    @field_validator("answered_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)


class ClarificationConflict(_WorkflowModel):
    """A later divergent answer retained as evidence without replacing the accepted answer."""

    question_id: Annotated[str, Field(min_length=1, max_length=256)]
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    observed_at: datetime
    original_evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    conflicting_evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    conflicting_answer_digest: Annotated[str, Field(pattern=_HASH_PATTERN)]
    predecessor_evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]

    @field_validator("observed_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)


class ClarificationSession(_WorkflowModel):
    """One immutable content-addressed state in a bounded clarification session."""

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(min_length=1, max_length=256)]
    task_id: Annotated[str, Field(min_length=1, max_length=256)]
    conversation_ref: Annotated[str, Field(min_length=1, max_length=512)]
    request_evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    classification_evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    opened_by: Annotated[str, Field(min_length=1, max_length=256)]
    opened_at: datetime
    baseline_graph_version: Annotated[int, Field(ge=0)]
    questions: Annotated[
        tuple[ClarificationQuestion, ...],
        Field(min_length=1, max_length=_MAX_CLARIFICATION_ITEMS),
    ]
    answers: Annotated[tuple[ClarificationAnswer, ...], Field(max_length=_MAX_CLARIFICATION_ITEMS)] = ()
    conflicts: Annotated[
        tuple[ClarificationConflict, ...], Field(max_length=_MAX_CLARIFICATION_ITEMS)
    ] = ()
    status: Literal["open", "proposed", "closed"] = "open"
    latest_event_id: str | None = None

    @field_validator("opened_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def validate_session(self) -> ClarificationSession:
        question_ids = tuple(item.id for item in self.questions)
        answer_ids = tuple(item.question_id for item in self.answers)
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("duplicate clarification question")
        if len(answer_ids) != len(set(answer_ids)) or not set(answer_ids).issubset(question_ids):
            raise ValueError("invalid clarification answers")
        if (
            any(item.question_id not in answer_ids for item in self.conflicts)
            or len({item.conflicting_evidence_ref for item in self.conflicts})
            != len(self.conflicts)
        ):
            raise ValueError("invalid clarification conflicts")
        immutable = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "conversation_ref": self.conversation_ref,
            "request_evidence_ref": self.request_evidence_ref,
            "classification_evidence_ref": self.classification_evidence_ref,
            "opened_by": self.opened_by,
            "opened_at": self.opened_at.isoformat().replace("+00:00", "Z"),
            "baseline_graph_version": self.baseline_graph_version,
            "questions": [item.model_dump(mode="json") for item in self.questions],
        }
        if self.id != f"clarification:{_canonical_digest(immutable)}":
            raise ValueError("clarification session identifier does not match canonical hash")
        return self

    @property
    def answer_evidence_refs(self) -> tuple[str, ...]:
        return tuple(item.evidence_ref for item in self.answers)


class ClarificationEvent(_WorkflowModel):
    """One append-only state transition in the shared proposal ledger chronology."""

    schema_version: Literal[1] = 1
    id: Annotated[str, Field(min_length=1, max_length=256)]
    event_type: Literal["opened", "answered", "conflicted", "proposed", "closed"]
    session: ClarificationSession
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    at: datetime
    predecessor_event_id: str | None = None
    proposal_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    decision_id: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    activation_changeset_id: Annotated[str, Field(min_length=1)] | None = None

    @field_validator("at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)

    @model_validator(mode="after")
    def validate_event(self) -> ClarificationEvent:
        material = {
            "schema_version": self.schema_version,
            "event_type": self.event_type,
            "session": self.session.model_dump(mode="json", exclude={"latest_event_id"}),
            "actor": self.actor,
            "at": self.at.isoformat().replace("+00:00", "Z"),
            "predecessor_event_id": self.predecessor_event_id,
            "proposal_id": self.proposal_id,
            "decision_id": self.decision_id,
            "activation_changeset_id": self.activation_changeset_id,
        }
        if self.id != f"clarification-event:{_canonical_digest(material)}":
            raise ValueError("clarification event identifier does not match canonical hash")
        if self.session.latest_event_id != self.id:
            raise ValueError("clarification event/session binding mismatch")
        expected_status = "proposed" if self.event_type == "proposed" else (
            "closed" if self.event_type == "closed" else "open"
        )
        if self.session.status != expected_status:
            raise ValueError("clarification event status mismatch")
        if self.event_type == "proposed" and (
            self.proposal_id is None
            or self.decision_id is not None
            or self.activation_changeset_id is not None
        ):
            raise ValueError("clarification event proposal binding mismatch")
        if self.event_type == "closed" and (
            self.proposal_id is None
            or self.decision_id is None
            or self.activation_changeset_id is None
        ):
            raise ValueError("clarification closure binding mismatch")
        if self.event_type not in {"proposed", "closed"} and any(
            value is not None
            for value in (self.proposal_id, self.decision_id, self.activation_changeset_id)
        ):
            raise ValueError("unexpected clarification mutation binding")
        return self


class ClarificationProposalSubmission(_WorkflowModel):
    """Detached agent submission over one fully answered clarification session."""

    schema_version: Literal[1] = 1
    session_id: Annotated[str, Field(min_length=1, max_length=256)]
    task_id: Annotated[str, Field(min_length=1, max_length=256)]
    baseline_graph_version: Annotated[int, Field(ge=0)]
    actor: Annotated[str, Field(min_length=1, max_length=256)]
    timestamp: datetime
    evidence_refs: Annotated[tuple[str, ...], Field(min_length=1, max_length=256)]
    source_roles: tuple[SourceRoleAssignment, ...] = ()
    changeset: ChangeSet
    core_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_DECISION_NODE_IDS)] = ()
    provisional_node_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_DECISION_NODE_IDS)] = ()
    assumptions: Annotated[tuple[str, ...], Field(max_length=256)] = ()
    unanswered_questions: Annotated[tuple[str, ...], Field(max_length=256)] = ()
    conflicting_authors: Annotated[tuple[str, ...], Field(max_length=256)] = ()
    destructive: bool = False

    @field_validator("timestamp")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _utc_timestamp(value)

    @field_validator(
        "evidence_refs",
        "core_node_ids",
        "provisional_node_ids",
        "conflicting_authors",
    )
    @classmethod
    def require_unique(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("duplicate clarification proposal identity")
        return values


class TaskEnvelope(_WorkflowModel):
    """Bounded, canonical preflight input built from a single human message."""

    schema_version: Literal[1] = 1
    id: str = ""
    repository_id: str
    actor: str
    conversation_ref: Annotated[str, Field(max_length=_MAX_CONVERSATION_REF_LENGTH)]
    request: str
    request_evidence_ref: str
    graph_version: Annotated[int, Field(ge=0)]
    created_at: datetime
    requested_scope: tuple[str, ...] = ()

    @field_validator("conversation_ref")
    @classmethod
    def validate_conversation_ref(cls, value: str) -> str:
        return _task_text(value, label="conversation reference", limit=_MAX_CONVERSATION_REF_LENGTH)

    @field_validator("request")
    @classmethod
    def validate_request(cls, value: str) -> str:
        return _task_text(value, label="request", limit=_MAX_REQUEST_BYTES, prompt=True)

    @field_validator("repository_id", "actor", "request_evidence_ref")
    @classmethod
    def validate_control_free_identity_fields(cls, value: str) -> str:
        if _CONTROL.search(value):
            raise ValueError("invalid task text")
        return value

    @field_validator("requested_scope")
    @classmethod
    def validate_requested_scope(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > _MAX_SCOPE_ENTRIES:
            raise ValueError("requested scope exceeds maximum entries")
        return tuple(
            _task_text(value, label="requested scope entry", limit=_MAX_SCOPE_BYTES)
            for value in values
        )

    @property
    def digest(self) -> str:
        return _canonical_digest(self.model_dump(mode="json", exclude={"id"}))

    @model_validator(mode="after")
    def assign_or_validate_identity(self) -> TaskEnvelope:
        expected = f"task:{self.digest}"
        if self.id and self.id != expected:
            raise ValueError("task identifier does not match canonical hash")
        if not self.id:
            object.__setattr__(self, "id", expected)
        return self


class PreflightResult(_WorkflowModel):
    """Deterministic classification result with detached JSON context only."""

    schema_version: Literal[1] = 1
    task_id: str
    graph_version: int
    classification: TaskClassification
    authorized: bool
    basis: str
    relevant_node_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    review_case_id: str | None = None
    permitted_scope: tuple[str, ...] = ()
    context: Mapping[str, JsonValue] = Field(default_factory=dict)

    @field_validator("context", mode="before")
    @classmethod
    def require_exact_context(cls, value: object) -> object:
        if type(value) is not dict:
            raise ValueError("context must be an exact dict")
        _validate_json(value)
        return value

    @field_validator("context")
    @classmethod
    def freeze_context(cls, value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], _freeze_json(dict(value)))

    @field_serializer("context")
    def serialize_context(self, value: Mapping[str, JsonValue]) -> JsonValue:
        return _thaw_json(value)


__all__ = [
    "ClarificationAnswer",
    "ClarificationConflict",
    "ClarificationEvent",
    "ClarificationIntentProposal",
    "ClarificationProposalSubmission",
    "ClarificationQuestion",
    "ClarificationQuestionInput",
    "ClarificationSession",
    "IntentProposal",
    "PreflightResult",
    "ProposalDecision",
    "ProposalDecisionRecord",
    "ProposalDecisionV2",
    "ProposalDecisionV3",
    "ProposalKind",
    "SourceRole",
    "SourceRoleAssignment",
    "TaskClassification",
    "TaskEnvelope",
]
