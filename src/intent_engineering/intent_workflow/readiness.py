"""Read-only readiness projections for developer automation."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import ConfigDict, Field, field_validator

from intent_engineering.core.models import ReconciliationCase, is_nonterminal_case_status
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.models import ClarificationEvent
from intent_engineering.intent_workflow.onboarding import (
    OnboardingProposalStore,
    OnboardingRuntime,
    OnboardingState,
    inspect_onboarding,
)

_MAX_ATTENTION_IDS = 256
_MAX_IDENTIFIER_BYTES = 512


class EnsureStatus(StrEnum):
    """Machine-readable states returned by bounded readiness checks."""

    READY = "ready"
    ONBOARDING_REQUIRED = "onboarding_required"
    HUMAN_ATTENTION_REQUIRED = "human_attention_required"
    OFFLINE_STALE = "offline_stale"
    SHARED_STATE_UNAVAILABLE = "shared_state_unavailable"
    SHARED_STATE_INVALID = "shared_state_invalid"
    UPGRADE_REQUIRED = "upgrade_required"


class ReadinessTarget(StrEnum):
    """The bounded review location associated with a readiness result."""

    HOME = "home"
    ONBOARDING = "onboarding"
    INBOX = "inbox"
    PROPOSAL = "proposal"
    TEAM_STATE = "team_state"


class EnsurePreset(StrEnum):
    """The explicit non-authoritative readiness profiles supported today."""

    DEVELOPER = "developer"


class _ReadinessModel(StrictModel):
    """Frozen, strict transport records for the readiness boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EnsureRequest(_ReadinessModel):
    """One idempotent readiness request that cannot grant human authority."""

    schema_version: Literal[1] = 1
    preset: EnsurePreset = EnsurePreset.DEVELOPER
    offline: bool = False


class EnsureResult(_ReadinessModel):
    """A bounded readiness projection without graph mutation or attribution."""

    schema_version: Literal[1] = 1
    status: EnsureStatus
    attention_route: ReadinessTarget
    graph_version: Annotated[int, Field(ge=0)]
    pending_proposal_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_ATTENTION_IDS)]
    open_case_ids: Annotated[tuple[str, ...], Field(max_length=_MAX_ATTENTION_IDS)]

    @field_validator("pending_proposal_ids", "open_case_ids")
    @classmethod
    def require_bounded_unique_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("duplicate readiness identifiers")
        for value in values:
            if (
                type(value) is not str
                or not value
                or len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES
            ):
                raise ValueError("invalid readiness identifier")
        return values


class ReadinessError(ValueError):
    """Fixed public failure for inconsistent local readiness state."""

    def __init__(self) -> None:
        super().__init__("intent readiness unavailable")


class ReadinessProposalStore(OnboardingProposalStore, Protocol):
    """Proposal projection additionally needed to gate unresolved clarification work."""

    def clarification_events(self) -> tuple[ClarificationEvent, ...]: ...


class ReadinessRuntime(OnboardingRuntime, Protocol):
    """Read-only runtime surface required for one readiness snapshot."""

    @property
    def intent_proposals(self) -> ReadinessProposalStore: ...

    def cases(self) -> tuple[ReconciliationCase, ...]: ...


class ReadinessService:
    """Classify one existing local snapshot without writing state or granting authority."""

    def __init__(self, runtime: ReadinessRuntime) -> None:
        self._runtime = runtime

    def ensure(self, request: EnsureRequest) -> EnsureResult:
        """Return a deterministic readiness result for one validated request."""
        if type(request) is not EnsureRequest:
            raise ReadinessError()
        failed = False
        try:
            onboarding = inspect_onboarding(self._runtime)
            if onboarding.state is OnboardingState.REQUIRED:
                return EnsureResult(
                    status=EnsureStatus.ONBOARDING_REQUIRED,
                    attention_route=ReadinessTarget.ONBOARDING,
                    graph_version=onboarding.graph_version,
                    pending_proposal_ids=onboarding.pending_proposal_ids,
                    open_case_ids=(),
                )
            if onboarding.state is OnboardingState.REVIEW_REQUIRED:
                return EnsureResult(
                    status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
                    attention_route=ReadinessTarget.INBOX,
                    graph_version=onboarding.graph_version,
                    pending_proposal_ids=onboarding.pending_proposal_ids,
                    open_case_ids=(),
                )
            if onboarding.state is not OnboardingState.READY:
                raise ReadinessError()
            open_case_ids = self._open_case_ids()
            if open_case_ids or self._requires_clarification_attention():
                return EnsureResult(
                    status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
                    attention_route=ReadinessTarget.INBOX,
                    graph_version=onboarding.graph_version,
                    pending_proposal_ids=onboarding.pending_proposal_ids,
                    open_case_ids=open_case_ids,
                )
            return EnsureResult(
                status=EnsureStatus.READY,
                attention_route=ReadinessTarget.HOME,
                graph_version=onboarding.graph_version,
                pending_proposal_ids=onboarding.pending_proposal_ids,
                open_case_ids=open_case_ids,
            )
        except Exception:  # noqa: BLE001 - fixed secret-free readiness boundary
            failed = True
        if failed:
            raise ReadinessError() from None
        raise ReadinessError()

    def _open_case_ids(self) -> tuple[str, ...]:
        cases = self._runtime.cases()
        if type(cases) is not tuple or len(cases) > _MAX_ATTENTION_IDS:
            raise ReadinessError()
        return tuple(case.id for case in cases if is_nonterminal_case_status(case.status))

    def _requires_clarification_attention(self) -> bool:
        events = self._runtime.intent_proposals.clarification_events()
        if type(events) is not tuple or len(events) > _MAX_ATTENTION_IDS:
            raise ReadinessError()
        latest_sessions = {event.session.id: event.session for event in events}
        return any(
            session.status == "open"
            and bool(
                {question.id for question in session.questions}
                - {answer.question_id for answer in session.answers}
            )
            for session in latest_sessions.values()
        )


__all__ = [
    "EnsurePreset",
    "EnsureRequest",
    "EnsureResult",
    "EnsureStatus",
    "ReadinessError",
    "ReadinessRuntime",
    "ReadinessService",
    "ReadinessTarget",
]
