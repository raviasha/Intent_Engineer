"""Descriptor-safe append-only persistence for intent proposals and decisions."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import ConfigDict, Field, ValidationError, model_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.models import (
    ClarificationEvent,
    ClarificationIntentProposal,
    ClarificationSession,
    IntentProposal,
    ProposalDecision,
    ProposalDecisionRecord,
    ProposalDecisionV2,
    ProposalDecisionV3,
)
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile, UnsafePathError, coerce_secure_file
from intent_engineering.storage.transaction import LocalTransactionCoordinator


class IntentProposalStoreError(ValueError):
    """Fixed public failure for an unavailable or invalid proposal ledger."""

    def __init__(self) -> None:
        super().__init__("intent proposal ledger unavailable")


class IntentLedgerRecord(StrictModel):
    """One explicitly ordered proposal-ledger frame."""

    model_config = ConfigDict(frozen=True, strict=True)

    schema_version: Literal[1] = 1
    sequence: Annotated[int, Field(ge=0)]
    proposal: ClarificationIntentProposal | IntentProposal | None = None
    decision: ProposalDecisionRecord | None = None
    clarification: ClarificationEvent | None = None

    @model_validator(mode="after")
    def require_exactly_one_payload(self) -> Self:
        if (
            sum(item is not None for item in (self.proposal, self.decision, self.clarification))
            != 1
        ):
            raise ValueError("invalid intent proposal ledger")
        return self


@dataclass(frozen=True)
class _LedgerState:
    content: bytes
    proposals: dict[str, IntentProposal]
    decisions: dict[str, ProposalDecisionRecord]
    clarification_events: tuple[ClarificationEvent, ...]
    sessions: dict[str, ClarificationSession]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _serialize(record: IntentLedgerRecord) -> bytes:
    payload = record.model_dump(mode="json")
    if record.clarification is None:
        payload.pop("clarification", None)
    return _canonical_json(payload) + b"\n"


def serialize_intent_ledger_record(record: IntentLedgerRecord) -> bytes:
    """Return one canonical frame while preserving legacy null-field spelling."""
    return _serialize(IntentLedgerRecord.model_validate_json(record.model_dump_json()))


def _validated_proposal(value: IntentProposal) -> IntentProposal | None:
    validated: IntentProposal | None = None
    try:
        if isinstance(value, ClarificationIntentProposal):
            validated = ClarificationIntentProposal.model_validate_json(value.model_dump_json())
        else:
            validated = IntentProposal.model_validate_json(value.model_dump_json())
        return validated
    except (AttributeError, TypeError, ValidationError, ValueError):
        return None
    finally:
        value = cast(IntentProposal, None)
        validated = None


def _validated_decision(value: ProposalDecisionRecord) -> ProposalDecisionRecord | None:
    validated: ProposalDecisionRecord | None = None
    try:
        if isinstance(value, ProposalDecisionV3):
            validated = ProposalDecisionV3.model_validate_json(value.model_dump_json())
        elif isinstance(value, ProposalDecision):
            validated = ProposalDecision.model_validate_json(value.model_dump_json())
        elif isinstance(value, ProposalDecisionV2):
            validated = ProposalDecisionV2.model_validate_json(value.model_dump_json())
        return validated
    except (AttributeError, TypeError, ValidationError, ValueError):
        return None
    finally:
        value = cast(ProposalDecisionRecord, None)
        validated = None


def parse_intent_ledger(content: bytes) -> _LedgerState | None:
    """Validate held canonical bytes and reconstruct their complete ledger state."""
    encoded_line: bytes | None = None
    line: bytes | None = None
    payload: dict[str, object] | None = None
    canonical: bytes | None = None
    record: IntentLedgerRecord | None = None
    proposal: IntentProposal | None = None
    decision: ProposalDecisionRecord | None = None
    clarification: ClarificationEvent | None = None
    proposals: dict[str, IntentProposal] = {}
    decisions: dict[str, ProposalDecisionRecord] = {}
    clarification_events: list[ClarificationEvent] = []
    sessions: dict[str, ClarificationSession] = {}
    latest_events: dict[str, ClarificationEvent] = {}
    pending_proposal_id: str | None = None
    pending_session_id: str | None = None
    result: _LedgerState | None = None
    try:
        if type(content) is not bytes or (content and not content.endswith(b"\n")):
            return None
        for expected_sequence, encoded_line in enumerate(content.splitlines(keepends=True)):
            if not encoded_line.endswith(b"\n") or not encoded_line.strip():
                return None
            line = encoded_line[:-1]
            payload = loads_strict_object(line.decode("utf-8"))
            canonical = _canonical_json(payload) + b"\n"
            if canonical != encoded_line:
                return None
            record = IntentLedgerRecord.model_validate_json(line)
            if _serialize(record) != encoded_line or record.sequence != expected_sequence:
                return None
            proposal = record.proposal
            decision = record.decision
            clarification = record.clarification
            if pending_proposal_id is not None and (
                not isinstance(proposal, ClarificationIntentProposal)
                or proposal.id != pending_proposal_id
                or proposal.clarification_session_id != pending_session_id
            ):
                return None
            if clarification is not None:
                previous = sessions.get(clarification.session.id)
                previous_event = latest_events.get(clarification.session.id)
                if not IntentProposalStore._valid_clarification_transition(
                    previous, clarification, previous_event
                ):
                    return None
                if clarification.event_type == "closed":
                    bound_proposal = proposals.get(clarification.proposal_id or "")
                    bound_decision = decisions.get(clarification.proposal_id or "")
                    if (
                        not isinstance(bound_proposal, ClarificationIntentProposal)
                        or not isinstance(bound_decision, ProposalDecisionV3)
                        or bound_proposal.clarification_session_id != clarification.session.id
                        or bound_decision.id != clarification.decision_id
                        or bound_decision.actor != clarification.actor
                        or bound_decision.decided_at != clarification.at
                        or bound_decision.activation_changeset_id
                        != clarification.activation_changeset_id
                    ):
                        return None
                clarification_events.append(clarification)
                sessions[clarification.session.id] = clarification.session
                latest_events[clarification.session.id] = clarification
                if clarification.event_type == "proposed":
                    pending_proposal_id = clarification.proposal_id
                    pending_session_id = clarification.session.id
                continue
            if proposal is not None:
                if proposal.id in proposals:
                    return None
                if isinstance(proposal, ClarificationIntentProposal):
                    session = sessions.get(proposal.clarification_session_id)
                    if (
                        session is None
                        or session.status != "proposed"
                        or session.task_id != proposal.task_id
                        or session.baseline_graph_version != proposal.baseline_graph_version
                        or latest_events[session.id].proposal_id != proposal.id
                    ):
                        return None
                proposals[proposal.id] = proposal
                if isinstance(proposal, ClarificationIntentProposal):
                    pending_proposal_id = None
                    pending_session_id = None
                continue
            if decision is None:
                return None
            proposal = proposals.get(decision.proposal_id)
            if (
                proposal is None
                or decision.proposal_digest != proposal.digest
                or decision.baseline_graph_version != proposal.baseline_graph_version
                or decision.proposal_id in decisions
            ):
                return None
            if isinstance(proposal, ClarificationIntentProposal) and (
                not isinstance(decision, ProposalDecisionV3)
                or sessions[proposal.clarification_session_id].status != "proposed"
            ):
                return None
            decisions[decision.proposal_id] = decision
        if pending_proposal_id is not None:
            return None
        if any(
            isinstance(item, ClarificationIntentProposal)
            and item.id in decisions
            and (
                sessions[item.clarification_session_id].status != "closed"
                or latest_events[item.clarification_session_id].proposal_id != item.id
                or latest_events[item.clarification_session_id].decision_id != decisions[item.id].id
            )
            for item in proposals.values()
        ):
            return None
        result = _LedgerState(
            content,
            dict(proposals),
            dict(decisions),
            tuple(clarification_events),
            dict(sessions),
        )
        return result
    except (TypeError, UnicodeError, ValidationError, ValueError):
        return None
    finally:
        content = b""
        encoded_line = None
        line = None
        payload = None
        canonical = None
        record = None
        proposal = None
        decision = None
        clarification = None
        proposals.clear()
        decisions.clear()
        clarification_events.clear()
        sessions.clear()
        latest_events.clear()
        pending_proposal_id = None
        pending_session_id = None
        result = None


class IntentProposalStore:
    """Store immutable proposals and their terminal decisions in one framed ledger."""

    def __init__(
        self,
        path: Path | SecureFile,
        *,
        transactions: LocalTransactionCoordinator | None = None,
    ) -> None:
        self._file = coerce_secure_file(path)
        if transactions is not None and not transactions.target_matches(
            "intent_proposals", self._file
        ):
            self._file.close()
            raise ValueError("intent proposal transaction target is unavailable")
        self._transactions = transactions
        self.path = self._file.path

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._transactions is None:
            with same_path_lock(self._file):
                yield
            return
        with self._transactions.coordinated(), same_path_lock(self._file):
            yield

    def _read_content_unlocked(self) -> bytes | None:
        try:
            return self._file.read_bytes_nonblocking()
        except UnsafePathError:
            try:
                if not self._file.exists():
                    return b""
            except UnsafePathError:
                pass
            return None

    def _decode_unlocked(self) -> _LedgerState | None:
        content: bytes | None = None
        result: _LedgerState | None = None
        try:
            content = self._read_content_unlocked()
            if content is None:
                return None
            result = parse_intent_ledger(content)
            return result
        finally:
            content = None
            result = None

    @staticmethod
    def _valid_clarification_transition(
        previous: ClarificationSession | None,
        event: ClarificationEvent,
        previous_event: ClarificationEvent | None,
    ) -> bool:
        session = event.session
        if previous is None:
            if not (
                previous_event is None
                and event.event_type == "opened"
                and event.predecessor_event_id is None
                and not session.answers
                and session.status == "open"
                and event.actor == session.opened_by
                and event.at == session.opened_at
            ):
                return False
            predecessor = session.classification_evidence_ref
            previous_at = session.opened_at
            for question in session.questions:
                if (
                    question.author != session.opened_by
                    or question.predecessor_evidence_ref != predecessor
                    or question.asked_at < previous_at
                ):
                    return False
                predecessor = question.evidence_ref
                previous_at = question.asked_at
            return True
        immutable_fields = {
            "answers",
            "conflicts",
            "status",
            "latest_event_id",
        }
        if (
            event.predecessor_event_id != previous.latest_event_id
            or previous_event is None
            or event.at < previous_event.at
            or previous.model_dump(mode="json", exclude=immutable_fields)
            != session.model_dump(mode="json", exclude=immutable_fields)
            or event.at < previous.opened_at
        ):
            return False
        if event.event_type == "answered":
            answer = session.answers[-1] if session.answers else None
            expected_predecessor = (
                previous.conflicts[-1].conflicting_evidence_ref
                if previous.conflicts
                else previous.answers[-1].evidence_ref
                if previous.answers
                else previous.questions[-1].evidence_ref
            )
            return (
                previous.status == "open"
                and session.status == "open"
                and len(session.answers) == len(previous.answers) + 1
                and session.answers[:-1] == previous.answers
                and session.conflicts == previous.conflicts
                and answer is not None
                and answer.actor == event.actor
                and answer.answered_at == event.at
                and answer.predecessor_evidence_ref == expected_predecessor
            )
        if event.event_type == "conflicted":
            conflict = session.conflicts[-1] if session.conflicts else None
            accepted = next(
                (
                    item
                    for item in previous.answers
                    if conflict is not None and item.question_id == conflict.question_id
                ),
                None,
            )
            expected_predecessor = (
                previous.conflicts[-1].conflicting_evidence_ref
                if previous.conflicts
                else previous.answers[-1].evidence_ref
            )
            return (
                previous.status == "open"
                and session.status == "open"
                and session.answers == previous.answers
                and len(session.conflicts) == len(previous.conflicts) + 1
                and session.conflicts[:-1] == previous.conflicts
                and conflict is not None
                and conflict.actor == event.actor
                and conflict.observed_at == event.at
                and conflict.predecessor_evidence_ref == expected_predecessor
                and accepted is not None
                and conflict.original_evidence_ref == accepted.evidence_ref
            )
        if event.event_type == "proposed":
            required = {item.id for item in session.questions if item.required}
            answered = {item.question_id for item in session.answers}
            return (
                previous.status == "open"
                and session.status == "proposed"
                and session.answers == previous.answers
                and not session.conflicts
                and required.issubset(answered)
                and event.proposal_id is not None
                and bool(session.answers)
                and event.actor == session.answers[-1].actor
            )
        if event.event_type == "closed":
            return (
                previous.status == "proposed"
                and session.status == "closed"
                and session.answers == previous.answers
                and session.conflicts == previous.conflicts
                and event.proposal_id is not None
                and event.decision_id is not None
                and event.activation_changeset_id is not None
            )
        return False

    def _put_result(self, proposal: IntentProposal) -> Literal["added", "duplicate", "invalid"]:
        validated: IntentProposal | None = None
        state: _LedgerState | None = None
        record: IntentLedgerRecord | None = None
        encoded: bytes | None = None
        outcome: Literal["added", "duplicate", "invalid"] = "invalid"
        try:
            validated = _validated_proposal(proposal)
            proposal = cast(IntentProposal, None)
            if validated is None:
                return "invalid"
            with self._locked():
                state = self._decode_unlocked()
                if state is None:
                    return "invalid"
                if validated.id in state.proposals:
                    return "duplicate" if state.proposals[validated.id] == validated else "invalid"
                record = IntentLedgerRecord(
                    sequence=len(state.content.splitlines()), proposal=validated
                )
                encoded = _serialize(record)
                append_durable_line(self._file, encoded)
                outcome = "added"
            return outcome
        except Exception:  # noqa: BLE001 - convert storage and hostile-model failures later
            return "invalid"
        finally:
            proposal = cast(IntentProposal, None)
            validated = None
            state = None
            record = None
            encoded = None

    def put(self, proposal: IntentProposal) -> bool:
        outcome: Literal["added", "duplicate", "invalid"] | None = None
        try:
            outcome = self._put_result(proposal)
        finally:
            proposal = cast(IntentProposal, None)
        if outcome == "invalid":
            raise IntentProposalStoreError() from None
        return outcome == "added"

    def _decide_result(
        self,
        decision: ProposalDecisionRecord,
    ) -> Literal["added", "duplicate", "invalid"]:
        validated: ProposalDecisionRecord | None = None
        state: _LedgerState | None = None
        proposal: IntentProposal | None = None
        existing: ProposalDecisionRecord | None = None
        record: IntentLedgerRecord | None = None
        encoded: bytes | None = None
        outcome: Literal["added", "duplicate", "invalid"] = "invalid"
        try:
            validated = _validated_decision(decision)
            decision = cast(ProposalDecisionRecord, None)
            if validated is None:
                return "invalid"
            with self._locked():
                state = self._decode_unlocked()
                if state is None:
                    return "invalid"
                proposal = state.proposals.get(validated.proposal_id)
                if (
                    proposal is None
                    or isinstance(proposal, ClarificationIntentProposal)
                    or validated.proposal_digest != proposal.digest
                    or validated.baseline_graph_version != proposal.baseline_graph_version
                ):
                    return "invalid"
                existing = state.decisions.get(validated.proposal_id)
                if existing is not None:
                    return "duplicate" if existing == validated else "invalid"
                record = IntentLedgerRecord(
                    sequence=len(state.content.splitlines()),
                    decision=validated,
                )
                encoded = _serialize(record)
                append_durable_line(self._file, encoded)
                outcome = "added"
            return outcome
        except Exception:  # noqa: BLE001 - convert storage and hostile-model failures later
            return "invalid"
        finally:
            decision = cast(ProposalDecisionRecord, None)
            validated = None
            state = None
            proposal = None
            existing = None
            record = None
            encoded = None

    def decide(self, decision: ProposalDecisionRecord) -> bool:
        outcome: Literal["added", "duplicate", "invalid"] | None = None
        try:
            outcome = self._decide_result(decision)
        finally:
            decision = cast(ProposalDecisionRecord, None)
        if outcome == "invalid":
            raise IntentProposalStoreError() from None
        return outcome == "added"

    def append_clarification(self, event: ClarificationEvent) -> bool:
        """Append one exact clarification transition into the shared chronology."""
        validated: ClarificationEvent | None = None
        state: _LedgerState | None = None
        encoded: bytes | None = None
        outcome = "invalid"
        try:
            validated = ClarificationEvent.model_validate_json(event.model_dump_json())
            with self._locked():
                state = self._decode_unlocked()
                if state is None:
                    raise IntentProposalStoreError()
                existing = tuple(
                    item for item in state.clarification_events if item.id == validated.id
                )
                if existing:
                    if len(existing) == 1 and existing[0] == validated:
                        return False
                    raise IntentProposalStoreError()
                previous = state.sessions.get(validated.session.id)
                previous_event = next(
                    (
                        item
                        for item in reversed(state.clarification_events)
                        if item.session.id == validated.session.id
                    ),
                    None,
                )
                if not self._valid_clarification_transition(previous, validated, previous_event):
                    raise IntentProposalStoreError()
                encoded = _serialize(
                    IntentLedgerRecord(
                        sequence=len(state.content.splitlines()), clarification=validated
                    )
                )
                append_durable_line(self._file, encoded)
                outcome = "added"
        except IntentProposalStoreError:
            raise
        except Exception:  # noqa: BLE001 - expose the established fixed ledger failure
            raise IntentProposalStoreError() from None
        finally:
            event = cast(ClarificationEvent, None)
            validated = None
            state = None
            encoded = None
        return outcome == "added"

    def clarification_events(self, session_id: str | None = None) -> tuple[ClarificationEvent, ...]:
        """Return validated clarification events in exact durable order."""
        with self._locked():
            state = self._decode_unlocked()
        if state is None:
            raise IntentProposalStoreError() from None
        if session_id is None:
            return state.clarification_events
        return tuple(item for item in state.clarification_events if item.session.id == session_id)

    def session(self, session_id: str) -> ClarificationSession:
        """Return the latest immutable state for one clarification session."""
        with self._locked():
            state = self._decode_unlocked()
        if state is None or session_id not in state.sessions:
            raise IntentProposalStoreError() from None
        return state.sessions[session_id]

    def get(self, proposal_id: str) -> IntentProposal:
        state: _LedgerState | None = None
        result: IntentProposal | None = None
        completed = False
        try:
            with self._locked():
                state = self._decode_unlocked()
                if state is not None:
                    result = state.proposals.get(proposal_id)
            completed = True
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            state = None
            result = None
        finally:
            proposal_id = ""
            state = None
            if not completed:
                result = None
        if result is None:
            raise IntentProposalStoreError() from None
        return result

    def list(self) -> tuple[IntentProposal, ...]:
        state: _LedgerState | None = None
        result: tuple[IntentProposal, ...] | None = None
        completed = False
        try:
            with self._locked():
                state = self._decode_unlocked()
                if state is not None:
                    result = tuple(state.proposals.values())
            completed = True
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            state = None
            result = None
        finally:
            state = None
            if not completed:
                result = None
        if result is None:
            raise IntentProposalStoreError() from None
        return result

    def decision_for(self, proposal_id: str) -> ProposalDecisionRecord | None:
        state: _LedgerState | None = None
        result: ProposalDecisionRecord | None = None
        valid = False
        completed = False
        try:
            with self._locked():
                state = self._decode_unlocked()
                if state is not None and proposal_id in state.proposals:
                    result = state.decisions.get(proposal_id)
                    valid = True
            completed = True
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            state = None
            result = None
        finally:
            proposal_id = ""
            state = None
            if not completed:
                result = None
        if not valid:
            raise IntentProposalStoreError() from None
        return result

    def bytes(self) -> bytes:
        """Return the exact validated ledger bytes for audit and transaction assertions."""
        state: _LedgerState | None = None
        result: bytes | None = None
        completed = False
        try:
            with self._locked():
                state = self._decode_unlocked()
                if state is not None:
                    result = state.content
            completed = True
        except Exception:  # noqa: BLE001 - expose one fixed public integrity failure
            state = None
            result = None
        finally:
            state = None
            if not completed:
                result = None
        if result is None:
            raise IntentProposalStoreError() from None
        return result

    def close(self) -> None:
        """Release the store's held descriptor."""
        self._file.close()


__all__ = [
    "IntentLedgerRecord",
    "IntentProposalStore",
    "IntentProposalStoreError",
    "parse_intent_ledger",
    "serialize_intent_ledger_record",
]
