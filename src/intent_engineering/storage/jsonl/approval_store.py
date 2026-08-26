"""Descriptor-safe append-only stores for immutable plans and approvals."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from intent_engineering.mutations.models import ApprovalRecord, WritePlan
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile, coerce_secure_file


class MutationStoreError(ValueError):
    """Fixed durable-integrity failure for plan and approval ledgers."""


class ConflictingMutationId(MutationStoreError):
    """Raised when one immutable mutation identity is reused for different bytes."""


class _ImmutableJsonlStore[RecordT: (WritePlan, ApprovalRecord)]:
    _model: type[RecordT]

    def __init__(self, path: Path | SecureFile) -> None:
        self._file = coerce_secure_file(path)
        self.path = self._file.path
        self._by_id: dict[str, RecordT] = {}
        with same_path_lock(self._file):
            self._rebuild_unlocked()

    def _decode_unlocked(self) -> dict[str, RecordT] | None:
        content = self._file.read_optional()
        records: dict[str, RecordT] = {}
        encodings: dict[str, bytes] = {}
        try:
            if content is not None:
                lines = content.decode("utf-8").splitlines()
                for line in lines:
                    if not line.strip():
                        return None
                    payload = loads_strict_object(line)
                    record = self._model.model_validate_json(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
                    existing = records.get(record.id)
                    if existing is not None:
                        if existing != record or encodings[record.id] != line.encode("utf-8"):
                            return None
                        continue
                    records.setdefault(record.id, record)
                    encodings[record.id] = line.encode("utf-8")
        except (TypeError, UnicodeError, ValidationError, ValueError):
            return None
        return records

    def _rebuild_unlocked(self) -> None:
        records = self._decode_unlocked()
        if records is None:
            raise MutationStoreError("invalid mutation store") from None
        self._by_id = records

    @staticmethod
    def _serialize(record: RecordT) -> bytes:
        return (
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    def _put_result(
        self,
        record: RecordT,
    ) -> Literal["added", "duplicate", "invalid", "conflict"]:
        try:
            validated = self._model.model_validate_json(record.model_dump_json())
        except (TypeError, ValidationError, ValueError):
            return "invalid"
        with same_path_lock(self._file):
            try:
                self._rebuild_unlocked()
            except MutationStoreError:
                return "invalid"
            existing = self._by_id.get(validated.id)
            if existing is not None:
                if existing != validated:
                    return "conflict"
                return "duplicate"
            append_durable_line(self._file, self._serialize(validated))
            self._by_id[validated.id] = validated
            return "added"

    def put(self, record: RecordT) -> bool:
        outcome = self._put_result(record)
        del record
        if outcome == "invalid":
            raise MutationStoreError("invalid mutation record") from None
        if outcome == "conflict":
            raise ConflictingMutationId("conflicting mutation record") from None
        return outcome == "added"

    def get(self, record_id: str) -> RecordT:
        with same_path_lock(self._file):
            self._rebuild_unlocked()
            return self._by_id[record_id]

    def list(self) -> Sequence[RecordT]:
        with same_path_lock(self._file):
            self._rebuild_unlocked()
            return tuple(self._by_id.values())


class JsonlWritePlanStore(_ImmutableJsonlStore[WritePlan]):
    """Append-only immutable write-plan ledger."""

    _model = WritePlan


class JsonlApprovalStore(_ImmutableJsonlStore[ApprovalRecord]):
    """Append-only independent approval ledger."""

    _model = ApprovalRecord
