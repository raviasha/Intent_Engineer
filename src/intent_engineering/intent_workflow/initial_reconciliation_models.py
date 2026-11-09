"""Host interpretations for a resumable, read-only three-layer assessment."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from intent_engineering.intent_workflow.background_models import BackgroundModel, Identity, Text

Axis = Literal[
    "intent_requirements",
    "requirements_intent",
    "requirements_code",
    "code_requirements",
    "intent_code",
    "source_conflict",
    "document_requirements",
]


class AssessmentFile(BackgroundModel):
    path: Annotated[str, Field(min_length=1, max_length=4096, pattern=r"^[^\x00-\x1f\x7f]+$")]
    role: Literal["requirements", "code"]


class Comparison(BackgroundModel):
    axis: Axis
    subject_ref: Identity
    related_refs: Annotated[tuple[Identity, ...], Field(max_length=256)] = ()
    status: Literal["aligned", "missing", "partial", "conflicting", "deferred", "unknown"]
    evidence_refs: Annotated[tuple[Identity, ...], Field(min_length=1, max_length=512)]
    explanation: Text
    authority_basis: Text
    recommendation: Text | None = None
    recommendation_target: (
        Literal["requirements", "code", "clarify_intent", "source_conflict", "defer"] | None
    ) = None
    confidence: Annotated[float, Field(ge=0, le=0.85)]

    @model_validator(mode="after")
    def validate_comparison(self) -> "Comparison":
        if self.status != "aligned" and not (self.recommendation and self.recommendation_target):
            raise ValueError("every issue requires a recommended change and target")
        if self.status in {"aligned", "partial"} and not self.related_refs:
            raise ValueError("coverage must identify the compared counterpart")
        if self.axis == "source_conflict" and (
            self.status != "conflicting" or self.recommendation_target != "source_conflict"
        ):
            raise ValueError("source conflicts require a user decision, not downstream authority")
        for refs in (self.related_refs, self.evidence_refs):
            if len(refs) != len(set(refs)):
                raise ValueError("duplicate comparison reference")
        return self


class InitialReconciliationRequest(BackgroundModel):
    action: Literal["start", "checkpoint", "finish", "status"]
    session_id: Identity
    turn_id: Identity
    run_id: Identity | None = None
    scope: Text | None = None
    files: Annotated[tuple[AssessmentFile, ...], Field(max_length=4096)] = ()
    comparisons: Annotated[tuple[Comparison, ...], Field(max_length=256)] = ()

    @model_validator(mode="after")
    def validate_request(self) -> "InitialReconciliationRequest":
        if self.action == "start":
            if not self.scope or self.run_id or self.comparisons:
                raise ValueError("start requires an explicit scope, without prior results")
        elif self.files or self.scope:
            raise ValueError("only start selects input files")
        if self.action in {"checkpoint", "finish"} and not self.run_id:
            raise ValueError("assessment identity required")
        if self.action != "checkpoint" and self.comparisons:
            raise ValueError("only checkpoints carry comparisons")
        if self.action == "checkpoint" and not self.comparisons:
            raise ValueError("empty checkpoint")
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("duplicate input file")
        if len({(c.axis, c.subject_ref) for c in self.comparisons}) != len(self.comparisons):
            raise ValueError("duplicate comparison slot")
        return self
