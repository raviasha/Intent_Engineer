"""Append-only durable JSONL storage for reconciliation cases."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from intent_engineering.core.models import ReconciliationCase, ReconciliationStatus
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.secure import SecureFile, coerce_secure_file


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


def serialize_case(case: ReconciliationCase) -> bytes:
    """Return one canonical JSONL lifecycle version."""
    return json.dumps(
        case.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def _record_version(
    latest_by_id: dict[str, ReconciliationCase],
    by_fingerprint: dict[str, ReconciliationCase],
    case: ReconciliationCase,
    *,
    exact_is_noop: bool,
) -> bool:
    """Validate one version against supplied indexes and update them in memory."""
    previous = latest_by_id.get(case.id)
    fingerprint_owner = by_fingerprint.get(case.fingerprint)
    if fingerprint_owner is not None and fingerprint_owner.id != case.id:
        raise ConflictingCaseFingerprint(case.fingerprint)
    if previous is not None:
        if _immutable_identity(previous) != _immutable_identity(case):
            raise ConflictingCaseId(case.id)
        if previous == case:
            if exact_is_noop:
                return False
            raise ConflictingCaseId(case.id)
        if previous.status == case.status:
            raise ConflictingCaseId(case.id)
        if (
            len(case.history) != len(previous.history) + 1
            or case.history[:-1] != previous.history
            or case.history[-1].prior is not previous.status
            or case.history[-1].new is not case.status
        ):
            raise ConflictingCaseId(case.id)
    latest_by_id[case.id] = case
    by_fingerprint[case.fingerprint] = case
    return True


def _parse_versions(
    content: bytes | None,
) -> tuple[dict[str, ReconciliationCase], dict[str, ReconciliationCase]]:
    latest_by_id: dict[str, ReconciliationCase] = {}
    by_fingerprint: dict[str, ReconciliationCase] = {}
    if content is None:
        return latest_by_id, by_fingerprint
    try:
        lines = content.decode("utf-8").splitlines(keepends=True)
    except UnicodeError as error:
        raise CaseStoreError("invalid case store encoding") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise CaseStoreError(f"blank case record at line {line_number}")
        try:
            case = ReconciliationCase.model_validate(
                _migrate_legacy_case_payload(json.loads(line))
            )
            _record_version(
                latest_by_id,
                by_fingerprint,
                case,
                exact_is_noop=True,
            )
        except CaseStoreError:
            raise
        except (json.JSONDecodeError, ValueError) as error:
            raise CaseStoreError(
                f"invalid reconciliation case record at line {line_number}"
            ) from error
    return latest_by_id, by_fingerprint


def validate_case_appends(
    content: bytes | None,
    cases: Sequence[ReconciliationCase],
) -> bytes:
    """Validate an exact sequence against durable lifecycle state before mutation."""
    latest_by_id, by_fingerprint = _parse_versions(content)
    serialized: list[bytes] = []
    for case in cases:
        validated = ReconciliationCase.model_validate(case.model_dump())
        _record_version(
            latest_by_id,
            by_fingerprint,
            validated,
            exact_is_noop=False,
        )
        serialized.append(serialize_case(validated))
    return b"".join(serialized)


def _immutable_identity(case: ReconciliationCase) -> dict[str, object]:
    """Return the non-lifecycle identity that may never change across versions."""
    return case.model_dump(
        mode="json",
        exclude={"status", "resolution", "resolved_by_changeset", "history"},
    )


def _migrate_legacy_case_payload(payload: object) -> dict[str, Any]:
    """Add only the one explicit legacy field before strict public validation."""
    if not isinstance(payload, dict):
        raise TypeError("case record must be a mapping")
    migrated = cast(dict[str, Any], dict(payload))
    if "created_by" in migrated:
        return migrated
    history = migrated.get("history")
    if isinstance(history, list) and history:
        earliest = history[0]
        if isinstance(earliest, dict) and isinstance(earliest.get("actor"), str):
            actor = earliest["actor"].strip()
            if actor:
                migrated["created_by"] = actor
                return migrated
    detector_id = migrated.get("detector_id")
    if not isinstance(detector_id, str) or not detector_id.strip():
        raise ValueError("legacy case has no detector provenance")
    migrated["created_by"] = f"detector:{detector_id}"
    return migrated


class JsonlCaseStore:
    """Durably append lifecycle versions and reconstruct the latest typed cases."""

    def __init__(self, path: Path | SecureFile) -> None:
        self._file = coerce_secure_file(path)
        self.path = self._file.path
        self._latest_by_id: dict[str, ReconciliationCase] = {}
        self._by_fingerprint: dict[str, ReconciliationCase] = {}
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()

    def _record_version_unlocked(self, case: ReconciliationCase) -> None:
        _record_version(
            self._latest_by_id,
            self._by_fingerprint,
            case,
            exact_is_noop=True,
        )

    def _rebuild_index_unlocked(self) -> None:
        """Refresh indexes from disk while the caller owns the shared path lock."""
        latest_by_id, by_fingerprint = _parse_versions(self._file.read_optional())
        self._latest_by_id = latest_by_id
        self._by_fingerprint = by_fingerprint

    def put(self, case: ReconciliationCase) -> bool:
        """Append a new case or lifecycle version, returning false for an exact duplicate."""
        try:
            case = ReconciliationCase.model_validate(case.model_dump())
        except ValidationError as error:
            raise CaseStoreError("invalid reconciliation case") from error
        serialized = serialize_case(case)
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            previous = self._latest_by_id.get(case.id)
            if previous == case:
                return False
            self._record_version_unlocked(case)
            append_durable_line(self._file, serialized)
            return True

    def get(self, case_id: str) -> ReconciliationCase:
        """Return the latest durable version for a stable case ID."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return self._latest_by_id[case_id]

    def find_by_fingerprint(self, fingerprint: str) -> ReconciliationCase | None:
        """Return the latest case having a deterministic drift fingerprint."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            return self._by_fingerprint.get(fingerprint)

    def list(self, status: ReconciliationStatus | None = None) -> Sequence[ReconciliationCase]:
        """Return latest cases in deterministic stable-ID order, optionally by status."""
        with same_path_lock(self._file):
            self._rebuild_index_unlocked()
            cases = tuple(self._latest_by_id.values())
            if status is not None:
                cases = tuple(case for case in cases if case.status is status)
            return tuple(sorted(cases, key=lambda case: case.id))
