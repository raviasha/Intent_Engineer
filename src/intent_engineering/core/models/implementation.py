"""Evidence-backed implementation status claims."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from intent_engineering.core.models.enums import ImplementationStatus


class ImplementationClaim(BaseModel):
    """A separately-versioned claim about how a requirement behaves in code."""

    model_config = ConfigDict(frozen=True)

    id: str
    status: ImplementationStatus
    requirement_refs: tuple[str, ...]
    current_behavior: str
    code_evidence: tuple[str, ...]
    test_evidence: tuple[str, ...] = ()
    verified_commit: str | None = None
    verified_at: datetime | None = None
    test_evidence_required: bool = True

    @model_validator(mode="after")
    def require_baseline_evidence(self) -> "ImplementationClaim":
        if self.status is not ImplementationStatus.IMPLEMENTED_BASELINE:
            return self

        missing: list[str] = []
        if not self.requirement_refs:
            missing.append("requirement_refs")
        if not self.code_evidence:
            missing.append("code_evidence")
        if self.verified_commit is None:
            missing.append("verified_commit")
        if self.verified_at is None:
            missing.append("verified_at")
        if self.test_evidence_required and not self.test_evidence:
            missing.append("test_evidence")
        if missing:
            raise ValueError(f"implemented_baseline requires: {', '.join(missing)}")
        return self
