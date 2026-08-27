"""Strict, detached records exchanged by an intent-aware agent workflow."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, field_serializer, field_validator, model_validator

from intent_engineering.core.models import ChangeSet, JsonValue, SourceRole, SourceRoleAssignment
from intent_engineering.core.models._base import StrictModel

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_REQUEST_BYTES = 16 * 1024
_MAX_SCOPE_BYTES = 2 * 1024
_MAX_SCOPE_ENTRIES = 256
_MAX_CONVERSATION_REF_LENGTH = 512
_MAX_DECISION_NODE_IDS = 10_000


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


def _task_text(value: str, *, label: str, limit: int) -> str:
    if _CONTROL.search(value):
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


ProposalDecisionRecord = ProposalDecision | ProposalDecisionV2


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
        return _task_text(value, label="request", limit=_MAX_REQUEST_BYTES)

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
        return tuple(_task_text(value, label="requested scope entry", limit=_MAX_SCOPE_BYTES) for value in values)

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
    "IntentProposal",
    "PreflightResult",
    "ProposalDecision",
    "ProposalDecisionRecord",
    "ProposalDecisionV2",
    "ProposalKind",
    "SourceRole",
    "SourceRoleAssignment",
    "TaskClassification",
    "TaskEnvelope",
]
