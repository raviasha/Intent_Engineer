"""Append-only durable JSONL storage for reconciliation cases."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from intent_engineering.core.models import ReconciliationCase, ReconciliationStatus
from intent_engineering.storage._atomic import append_durable_line, same_path_lock


class CaseStoreError(ValueError):
    """Base class for durable case-store integrity failures."""


class ConflictingCaseId(CaseStoreError):
    """Raised when a stable case identity is reused for different semantics."""

    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        super().__init__(f"conflicting reconciliation case id: {case_id}")


class ConflictingCaseFingerprint(CaseStoreError):
    """Raised when a drift fingerprint is assigned to another case identity."""

    def __init__(self, fingerprint: str) -> None:
        self.fingerprint = fingerprint
        super().__init__(f"conflicting reconciliation fingerprint: {fingerprint}")


def _immutable_identity(case: ReconciliationCase) -> dict[str, object]:
    """Return the non-lifecycle identity that may never change across versions."""
    return case.model_dump(
        mode="json",
        exclude={"status", "resolution", "resolved_by_changeset", "history"},
    )


class JsonlCaseStore:
    """Durably append lifecycle versions and reconstruct the latest typed cases."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._latest_by_id: dict[str, ReconciliationCase] = {}
        self._by_fingerprint: dict[str, ReconciliationCase] = {}
        with same_path_lock(self.path):
            self._rebuild_index_unlocked()

    def _record_version_unlocked(self, case: ReconciliationCase) -> None:
        previous = self._latest_by_id.get(case.id)
        by_fingerprint = self._by_fingerprint.get(case.fingerprint)
        if by_fingerprint is not None and by_fingerprint.id != case.id:
            raise ConflictingCaseFingerprint(case.fingerprint)
        if previous is not None:
            if _immutable_identity(previous) != _immutable_identity(case):
                raise ConflictingCaseId(case.id)
            if previous == case:
                return
            if previous.status == case.status:
                raise ConflictingCaseId(case.id)
            if (
                len(case.history) != len(previous.history) + 1
                or case.history[:-1] != previous.history
                or case.history[-1].prior is not previous.status
                or case.history[-1].new is not case.status
            ):
                raise ConflictingCaseId(case.id)
        self._latest_by_id[case.id] = case
        self._by_fingerprint[case.fingerprint] = case

    def _rebuild_index_unlocked(self) -> None:
        """Refresh indexes from disk while the caller owns the shared path lock."""
        self._latest_by_id.clear()
        self._by_fingerprint.clear()
        if not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as source:
            for line_number, line in enumerate(source, start=1):
                if not line.strip():
                    raise CaseStoreError(f"blank case record at line {line_number} in {self.path}")
                try:
                    case = ReconciliationCase.model_validate_json(line)
                    self._record_version_unlocked(case)
                except CaseStoreError:
                    raise
                except (json.JSONDecodeError, ValueError) as error:
                    raise CaseStoreError(
                        f"invalid reconciliation case record at line {line_number} in {self.path}"
                    ) from error

    def put(self, case: ReconciliationCase) -> bool:
        """Append a new case or lifecycle version, returning false for an exact duplicate."""
        try:
            case = ReconciliationCase.model_validate(case.model_dump())
        except ValidationError as error:
            raise CaseStoreError("invalid reconciliation case") from error
        serialized = json.dumps(
            case.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        with same_path_lock(self.path):
            self._rebuild_index_unlocked()
            previous = self._latest_by_id.get(case.id)
            if previous == case:
                return False
            self._record_version_unlocked(case)
            append_durable_line(self.path, serialized)
            return True

    def get(self, case_id: str) -> ReconciliationCase:
        """Return the latest durable version for a stable case ID."""
        with same_path_lock(self.path):
            self._rebuild_index_unlocked()
            return self._latest_by_id[case_id]

    def find_by_fingerprint(self, fingerprint: str) -> ReconciliationCase | None:
        """Return the latest case having a deterministic drift fingerprint."""
        with same_path_lock(self.path):
            self._rebuild_index_unlocked()
            return self._by_fingerprint.get(fingerprint)

    def list(self, status: ReconciliationStatus | None = None) -> Sequence[ReconciliationCase]:
        """Return latest cases in deterministic stable-ID order, optionally by status."""
        with same_path_lock(self.path):
            self._rebuild_index_unlocked()
            cases = tuple(self._latest_by_id.values())
            if status is not None:
                cases = tuple(case for case in cases if case.status is status)
            return tuple(sorted(cases, key=lambda case: case.id))
