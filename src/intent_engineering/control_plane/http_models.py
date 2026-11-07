"""Strict raw-JSON contracts for the local browser control plane."""

from __future__ import annotations

import json
import math
import re
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.assessment.models import AssessmentReport, NodeScorecard
from intent_engineering.control_plane.models import HumanDecisionPayload
from intent_engineering.intent_workflow.enrichment import EnrichmentQuestion
from intent_engineering.intent_workflow.enrichment_models import EnrichmentSession
from intent_engineering.intent_workflow.models import (
    ClarificationIntentProposal,
    ClarificationProposalSubmission,
)

MAX_HTTP_BODY_BYTES = 256 * 1024
MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_MAX_JSON_DEPTH = 128
_MAX_JSON_NODES = 65_536
_MAX_IDENTIFIER_BYTES = 512
_MAX_ANSWER_BYTES = 16 * 1024
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ANSWER_ID_PATTERN = re.compile(r"^answer:[0-9a-f]{64}$")
_PAGE_CURSOR_PATTERN = re.compile(r"^page:[0-9a-f]{64}:[1-9][0-9]{0,3}$")
_MAX_ASSESSMENT_NODES = 2_000
_MAX_ASSESSMENT_ROWS = 100
_MAX_RUBRIC_CHECKS_PER_DIMENSION = 64
_ENRICHMENT_SESSION_PATTERN = re.compile(r"^refine:[^\x00-\x20\x7f]{1,249}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ANSWER_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


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


class TeamEnrollmentOptionsRequest(HttpRequestModel):
    """One bounded opaque proof consumed only by the GitHub identity verifier."""

    identity_proof: str = Field(min_length=1, max_length=16 * 1024)

    @field_validator("identity_proof", mode="before")
    @classmethod
    def require_exact_proof(cls, value: object) -> str:
        if type(value) is not str or len(value.encode("utf-8")) > 16 * 1024:
            raise ValueError("invalid team enrollment request")
        return value


class TeamEnrollmentVerifyRequest(HttpRequestModel):
    """The WebAuthn registration response for a server-held GitHub identity."""

    response: dict[str, object]


class TeamEnrollmentCancelRequest(HttpRequestModel):
    """An intentionally empty cancellation request."""


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


class ReviewedTestRunRequest(HttpRequestModel):
    """One reviewed command identifier selected by the local human UI."""

    command_id: str = Field(min_length=1, max_length=76)

    @field_validator("command_id", mode="before")
    @classmethod
    def require_exact_command_id(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("invalid reviewed test request")
        return value


class EnrichmentStartRequest(HttpRequestModel):
    """Start one bounded voluntary improvement session."""

    minutes: Literal[5, 15, 30] | None = None
    focus: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("minutes", mode="before")
    @classmethod
    def require_exact_minutes(cls, value: object) -> int | None:
        if value is not None and type(value) is not int:
            raise ValueError("invalid enrichment request")
        return value

    @field_validator("focus", mode="before")
    @classmethod
    def require_exact_focus(cls, value: object) -> str | None:
        if value is not None and (
            type(value) is not str
            or not value
            or len(value.encode("utf-8")) > 256
            or _CONTROL.search(value) is not None
        ):
            raise ValueError("invalid enrichment request")
        return value

    @model_validator(mode="after")
    def require_scope(self) -> EnrichmentStartRequest:
        if self.minutes is None and self.focus is None:
            raise ValueError("invalid enrichment request")
        return self


class EnrichmentSessionRequest(HttpRequestModel):
    """Select one durable enrichment session without placing it in a URL."""

    session_id: str = Field(min_length=8, max_length=256)

    @field_validator("session_id", mode="before")
    @classmethod
    def require_exact_session(cls, value: object) -> str:
        if type(value) is not str or _ENRICHMENT_SESSION_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid enrichment request")
        return value


class EnrichmentGapRequest(EnrichmentSessionRequest):
    """Select the exact current gap for a skip operation."""

    gap_id: str = Field(min_length=1, max_length=256)

    @field_validator("gap_id", mode="before")
    @classmethod
    def require_exact_gap(cls, value: object) -> str:
        if (
            type(value) is not str
            or not value
            or len(value.encode("utf-8")) > 256
            or _CONTROL.search(value) is not None
        ):
            raise ValueError("invalid enrichment request")
        return value


class EnrichmentAnswerRequest(EnrichmentGapRequest):
    """Carry one transient human answer to the evidence-capture boundary."""

    answer: str = Field(min_length=1, max_length=_MAX_ANSWER_BYTES)

    @field_validator("answer", mode="before")
    @classmethod
    def require_exact_answer(cls, value: object) -> str:
        if (
            type(value) is not str
            or not value
            or len(value.encode("utf-8")) > _MAX_ANSWER_BYTES
            or _ANSWER_CONTROL.search(value) is not None
        ):
            raise ValueError("invalid enrichment request")
        return value


class EnrichmentProposalRequest(EnrichmentSessionRequest):
    """Submit an exact governed proposal; it remains review-only."""

    submission: ClarificationProposalSubmission

    @field_validator("submission", mode="before")
    @classmethod
    def require_exact_submission(cls, value: object) -> ClarificationProposalSubmission:
        encoded: bytes | None = None
        try:
            if type(value) is not dict:
                raise ValueError("invalid enrichment request")
            encoded = _canonical_json_bytes(value)
            return ClarificationProposalSubmission.model_validate_json(encoded, strict=True)
        finally:
            value = None
            encoded = None


class _HttpResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def require_detached_json_tree(cls, value: object) -> object:
        require_exact_json(value, maximum_bytes=MAX_HTTP_RESPONSE_BYTES)
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


class EnrichmentSessionResponse(_HttpResponseModel):
    """One session status and, only while open, its single current question."""

    schema_version: Literal[1] = 1
    session: EnrichmentSession
    question: EnrichmentQuestion | None = None

    @field_validator("session", mode="before")
    @classmethod
    def require_exact_enrichment_session(cls, value: object) -> EnrichmentSession:
        if type(value) is not dict:
            raise ValueError("invalid enrichment response")
        return EnrichmentSession.model_validate_json(_canonical_json_bytes(value), strict=True)

    @field_validator("question", mode="before")
    @classmethod
    def require_exact_enrichment_question(cls, value: object) -> EnrichmentQuestion | None:
        if value is None:
            return None
        if type(value) is not dict:
            raise ValueError("invalid enrichment response")
        return EnrichmentQuestion.model_validate_json(_canonical_json_bytes(value), strict=True)

    @model_validator(mode="after")
    def require_one_current_question(self) -> EnrichmentSessionResponse:
        if self.session.status == "open":
            if (
                self.question is None
                or self.session.current_gap_id is None
                or self.question.gap_id != self.session.current_gap_id
            ):
                raise ValueError("invalid enrichment response")
        elif self.question is not None:
            raise ValueError("invalid enrichment response")
        return self


class EnrichmentProposalResponse(_HttpResponseModel):
    """A governed proposal awaiting the existing review and approval path."""

    schema_version: Literal[1] = 1
    session_id: str = Field(min_length=8, max_length=256)
    proposal: ClarificationIntentProposal

    @field_validator("session_id", mode="before")
    @classmethod
    def require_exact_session_id(cls, value: object) -> str:
        if type(value) is not str or _ENRICHMENT_SESSION_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid enrichment response")
        return value

    @field_validator("proposal", mode="before")
    @classmethod
    def require_exact_enrichment_proposal(cls, value: object) -> ClarificationIntentProposal:
        if type(value) is not dict:
            raise ValueError("invalid enrichment response")
        return ClarificationIntentProposal.model_validate_json(
            _canonical_json_bytes(value), strict=True
        )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _bounded_identifier(value: object) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise ValueError("invalid assessment response")
    return value


def _validate_assessment_bounds(report: AssessmentReport) -> None:
    if len(report.nodes) > _MAX_ASSESSMENT_NODES:
        raise ValueError("invalid assessment response")
    for identifier in (
        report.project_id,
        report.graph_id,
        report.project.project_id,
        *report.project.branch_ids,
        *report.project.contributing_node_ids,
        *report.project.contribution_weights,
    ):
        _bounded_identifier(identifier)
    for branch in report.branches:
        for identifier in (
            branch.branch_id,
            branch.root_node_id,
            *branch.node_ids,
            *branch.contribution_weights,
        ):
            _bounded_identifier(identifier)
    for node in report.nodes:
        _bounded_identifier(node.node_id)
        _bounded_identifier(
            node.node_type.value if hasattr(node.node_type, "value") else node.node_type
        )
        for reference in node.blocking_case_refs:
            _bounded_identifier(reference)
        for dimension in node.dimensions:
            if len(dimension.passed) + len(dimension.failed) > _MAX_RUBRIC_CHECKS_PER_DIMENSION:
                raise ValueError("invalid assessment response")
            for check in (*dimension.passed, *dimension.failed):
                _bounded_identifier(check.rule_id)
                for reference in check.references:
                    _bounded_identifier(reference)
            for reference in (*dimension.evidence_refs, *dimension.related_refs):
                _bounded_identifier(reference)
    for gap in report.gaps:
        _bounded_identifier(gap.rule_id)
        for reference in gap.references:
            _bounded_identifier(reference)


def _validate_report_mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("invalid assessment response")
    encoded = _canonical_json_bytes(value)
    report = AssessmentReport.model_validate_json(encoded, strict=True)
    _validate_assessment_bounds(report)
    return cast(dict[str, object], report.model_dump(mode="json"))


def _validate_node_mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("invalid assessment response")
    node = NodeScorecard.model_validate_json(_canonical_json_bytes(value), strict=True)
    _bounded_identifier(node.node_id)
    _bounded_identifier(
        node.node_type.value if hasattr(node.node_type, "value") else node.node_type
    )
    for reference in node.blocking_case_refs:
        _bounded_identifier(reference)
    for dimension in node.dimensions:
        if len(dimension.passed) + len(dimension.failed) > _MAX_RUBRIC_CHECKS_PER_DIMENSION:
            raise ValueError("invalid assessment response")
        for check in (*dimension.passed, *dimension.failed):
            _bounded_identifier(check.rule_id)
            for reference in check.references:
                _bounded_identifier(reference)
        for reference in (*dimension.evidence_refs, *dimension.related_refs):
            _bounded_identifier(reference)
    return cast(dict[str, object], node.model_dump(mode="json"))


class AssessmentPageResponse(_HttpResponseModel):
    """One bounded table page over the same node scorecards as the report."""

    rows: list[dict[str, object]] = Field(max_length=_MAX_ASSESSMENT_ROWS)
    next_cursor: str | None = Field(default=None, max_length=80)

    @field_validator("rows", mode="before")
    @classmethod
    def require_exact_rows(cls, value: object) -> list[dict[str, object]]:
        if type(value) is not list or len(value) > _MAX_ASSESSMENT_ROWS:
            raise ValueError("invalid assessment response")
        return [_validate_node_mapping(item) for item in list.__iter__(value)]

    @field_validator("next_cursor", mode="before")
    @classmethod
    def require_exact_cursor(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or _PAGE_CURSOR_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid assessment response")
        return value


class AssessmentFocusResponse(_HttpResponseModel):
    """One optional visible focus projection without hidden-reference hints."""

    reference: str = Field(min_length=1, max_length=_MAX_IDENTIFIER_BYTES)
    node: dict[str, object] | None
    branch: dict[str, object] | None

    @field_validator("reference", mode="before")
    @classmethod
    def require_exact_reference(cls, value: object) -> str:
        return _bounded_identifier(value)

    @field_validator("node", mode="before")
    @classmethod
    def require_exact_node(cls, value: object) -> dict[str, object] | None:
        return None if value is None else _validate_node_mapping(value)

    @model_validator(mode="after")
    def require_visible_subject(self) -> AssessmentFocusResponse:
        if self.node is None and self.branch is None:
            raise ValueError("invalid assessment response")
        if self.node is not None and self.node.get("node_id") != self.reference:
            raise ValueError("invalid assessment response")
        if self.branch is not None and (
            type(self.branch) is not dict or self.branch.get("branch_id") != self.reference
        ):
            raise ValueError("invalid assessment response")
        return self


class AssessmentResponse(_HttpResponseModel):
    """One bounded graph projection and table page from a shared assessment report."""

    schema_version: Literal[1] = 1
    assessment: dict[str, object]
    focus: AssessmentFocusResponse | None = None
    page: AssessmentPageResponse

    @field_validator("assessment", mode="before")
    @classmethod
    def require_exact_assessment(cls, value: object) -> dict[str, object]:
        return _validate_report_mapping(value)

    @model_validator(mode="after")
    def require_shared_scorecards(self) -> AssessmentResponse:
        raw_nodes = self.assessment.get("nodes")
        raw_branches = self.assessment.get("branches")
        if type(raw_nodes) is not list or type(raw_branches) is not list:
            raise ValueError("invalid assessment response")
        nodes = {
            cast(dict[str, object], item).get("node_id"): item
            for item in raw_nodes
            if type(item) is dict
        }
        for row in self.page.rows:
            identifier = row.get("node_id")
            if identifier not in nodes or nodes[identifier] != row:
                raise ValueError("invalid assessment response")
        if self.focus is not None:
            if self.focus.node is not None and nodes.get(self.focus.reference) != self.focus.node:
                raise ValueError("invalid assessment response")
            branches = {
                cast(dict[str, object], item).get("branch_id"): item
                for item in raw_branches
                if type(item) is dict
            }
            if (
                self.focus.branch is not None
                and branches.get(self.focus.reference) != self.focus.branch
            ):
                raise ValueError("invalid assessment response")
        return self


class AssessmentNodeResponse(_HttpResponseModel):
    """One node lookup bound to the exact visible assessment snapshot."""

    schema_version: Literal[1] = 1
    snapshot_digest: str = Field(pattern=_DIGEST_PATTERN.pattern)
    node: dict[str, object]

    @field_validator("snapshot_digest", mode="before")
    @classmethod
    def require_exact_digest(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("invalid assessment response")
        return value

    @field_validator("node", mode="before")
    @classmethod
    def require_exact_node(cls, value: object) -> dict[str, object]:
        return _validate_node_mapping(value)


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
        require_exact_json(value, maximum_bytes=MAX_HTTP_RESPONSE_BYTES)
        if type(value) is not dict:
            raise ValueError("invalid HTTP response")
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_HTTP_RESPONSE_BYTES:
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
    "MAX_HTTP_RESPONSE_BYTES",
    "AssessmentFocusResponse",
    "AssessmentNodeResponse",
    "AssessmentPageResponse",
    "AssessmentResponse",
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
    "TeamEnrollmentCancelRequest",
    "TeamEnrollmentOptionsRequest",
    "TeamEnrollmentVerifyRequest",
    "canonical_json_object",
    "detach_response_mapping",
    "parse_request_bytes",
    "parse_response_bytes",
    "require_exact_json",
]
