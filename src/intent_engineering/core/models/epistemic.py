"""Immutable confidence state and evidence-backed reassessments."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from intent_engineering.core.models.enums import ChangeKind
from intent_engineering.core.models.graph import Confidence


class EpistemicState(BaseModel):
    """Confidence about intent fidelity, independent of implementation status."""

    model_config = ConfigDict(frozen=True)

    confidence: Confidence
    basis: str
    evidence_refs: tuple[str, ...]
    last_reassessed_at: datetime

    @model_validator(mode="after")
    def require_evidence(self) -> "EpistemicState":
        if not self.evidence_refs:
            raise ValueError("epistemic state requires evidence")
        return self


class ConfidenceChange(BaseModel):
    """An auditable, evidence-backed change to epistemic confidence."""

    model_config = ConfigDict(frozen=True)

    change_id: str
    timestamp: datetime
    actor: str
    subject_ref: str
    change_kind: ChangeKind
    prior_confidence: Confidence
    new_confidence: Confidence
    evidence_refs: tuple[str, ...]
    reason: str

    @model_validator(mode="after")
    def require_material_change_with_evidence(self) -> "ConfidenceChange":
        if not self.evidence_refs:
            raise ValueError("confidence change requires evidence")
        if self.prior_confidence == self.new_confidence:
            raise ValueError("confidence change must change confidence")
        return self
