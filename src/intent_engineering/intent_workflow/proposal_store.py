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
from intent_engineering.intent_workflow.models import IntentProposal, ProposalDecision
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
    proposal: IntentProposal | None = None
    decision: ProposalDecision | None = None

    @model_validator(mode="after")
    def require_exactly_one_payload(self) -> Self:
        if (self.proposal is None) == (self.decision is None):
            raise ValueError("invalid intent proposal ledger")
        return self


@dataclass(frozen=True)
class _LedgerState:
    content: bytes
    proposals: dict[str, IntentProposal]
    decisions: dict[str, ProposalDecision]


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _serialize(record: IntentLedgerRecord) -> bytes:
    return _canonical_json(record.model_dump(mode="json")) + b"\n"


def _validated_proposal(value: IntentProposal) -> IntentProposal | None:
    validated: IntentProposal | None = None
    try:
        validated = IntentProposal.model_validate_json(value.model_dump_json())
        return validated
    except (AttributeError, TypeError, ValidationError, ValueError):
        return None
    finally:
        value = cast(IntentProposal, None)
        validated = None


def _validated_decision(value: ProposalDecision) -> ProposalDecision | None:
    validated: ProposalDecision | None = None
    try:
        validated = ProposalDecision.model_validate_json(value.model_dump_json())
        return validated
    except (AttributeError, TypeError, ValidationError, ValueError):
        return None
    finally:
        value = cast(ProposalDecision, None)
        validated = None


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
        encoded_line: bytes | None = None
        line: bytes | None = None
        payload: dict[str, object] | None = None
        canonical: bytes | None = None
        record: IntentLedgerRecord | None = None
        proposal: IntentProposal | None = None
        decision: ProposalDecision | None = None
        proposals: dict[str, IntentProposal] = {}
        decisions: dict[str, ProposalDecision] = {}
        result: _LedgerState | None = None
        try:
            content = self._read_content_unlocked()
            if content is None or (content and not content.endswith(b"\n")):
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
                if _serialize(record) != encoded_line:
                    return None
                if record.sequence != expected_sequence:
                    return None
                proposal = record.proposal
                decision = record.decision
                if proposal is not None:
                    if proposal.id in proposals:
                        return None
                    proposals[proposal.id] = proposal
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
                decisions[decision.proposal_id] = decision
            result = _LedgerState(content, dict(proposals), dict(decisions))
            return result
        except (TypeError, UnicodeError, ValidationError, ValueError):
            return None
        finally:
            content = None
            encoded_line = None
            line = None
            payload = None
            canonical = None
            record = None
            proposal = None
            decision = None
            proposals.clear()
            decisions.clear()
            result = None

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
                    sequence=len(state.proposals) + len(state.decisions), proposal=validated
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
        decision: ProposalDecision,
    ) -> Literal["added", "duplicate", "invalid"]:
        validated: ProposalDecision | None = None
        state: _LedgerState | None = None
        proposal: IntentProposal | None = None
        existing: ProposalDecision | None = None
        record: IntentLedgerRecord | None = None
        encoded: bytes | None = None
        outcome: Literal["added", "duplicate", "invalid"] = "invalid"
        try:
            validated = _validated_decision(decision)
            decision = cast(ProposalDecision, None)
            if validated is None:
                return "invalid"
            with self._locked():
                state = self._decode_unlocked()
                if state is None:
                    return "invalid"
                proposal = state.proposals.get(validated.proposal_id)
                if (
                    proposal is None
                    or validated.proposal_digest != proposal.digest
                    or validated.baseline_graph_version != proposal.baseline_graph_version
                ):
                    return "invalid"
                existing = state.decisions.get(validated.proposal_id)
                if existing is not None:
                    return "duplicate" if existing == validated else "invalid"
                record = IntentLedgerRecord(
                    sequence=len(state.proposals) + len(state.decisions),
                    decision=validated,
                )
                encoded = _serialize(record)
                append_durable_line(self._file, encoded)
                outcome = "added"
            return outcome
        except Exception:  # noqa: BLE001 - convert storage and hostile-model failures later
            return "invalid"
        finally:
            decision = cast(ProposalDecision, None)
            validated = None
            state = None
            proposal = None
            existing = None
            record = None
            encoded = None

    def decide(self, decision: ProposalDecision) -> bool:
        outcome: Literal["added", "duplicate", "invalid"] | None = None
        try:
            outcome = self._decide_result(decision)
        finally:
            decision = cast(ProposalDecision, None)
        if outcome == "invalid":
            raise IntentProposalStoreError() from None
        return outcome == "added"

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

    def decision_for(self, proposal_id: str) -> ProposalDecision | None:
        state: _LedgerState | None = None
        result: ProposalDecision | None = None
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


__all__ = ["IntentLedgerRecord", "IntentProposalStore", "IntentProposalStoreError"]
