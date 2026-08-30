"""Descriptor-safe, append-only persistence for bounded WebAuthn state."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, ValidationError, field_validator, model_validator

from intent_engineering.control_plane.models import ChallengeRecord, CredentialRecord
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile, UnsafePathError, coerce_secure_file
from intent_engineering.storage.transaction import LocalTransactionCoordinator


class _ChallengeFrame(ChallengeRecord):
    """One typed event in the append-only challenge transition ledger."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    kind: Literal["issue", "consume"]
    consumed_at: datetime | None = None

    @field_validator("consumed_at", mode="before")
    @classmethod
    def require_canonical_consumed_at(cls, value: object) -> datetime | None:
        if value is None:
            return None
        # The parent model's timestamp validation is intentionally reused through
        # the exact canonical representation of an otherwise identical challenge.
        return ChallengeRecord.model_validate(
            {
                "id": "challenge:" + "0" * 64,
                "project_id": "timestamp",
                "repository_id": "repo:sha256:" + "0" * 64,
                "actor": "timestamp",
                "ceremony": "registration",
                "challenge": "timestamp",
                "issued_at": value,
                "expires_at": value,
            }
        ).issued_at

    @model_validator(mode="after")
    def require_valid_transition(self) -> _ChallengeFrame:
        if (self.kind == "issue") != (self.consumed_at is None):
            raise ValueError("invalid challenge ledger")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )


def _validated_credential(value: object) -> CredentialRecord | None:
    try:
        if not isinstance(value, CredentialRecord):
            return None
        return CredentialRecord.model_validate_json(value.model_dump_json())
    except (TypeError, ValidationError, ValueError):
        return None


def _validated_challenge(value: object) -> ChallengeRecord | None:
    try:
        if not isinstance(value, ChallengeRecord):
            return None
        return ChallengeRecord.model_validate_json(value.model_dump_json())
    except (TypeError, ValidationError, ValueError):
        return None


def _within_open_lifetime(record: ChallengeRecord, consumed_at: datetime) -> bool:
    return record.issued_at <= consumed_at < record.expires_at


class WebAuthnCredentialStore:
    """Immutable credential enrollment records in one canonical JSONL ledger."""

    def __init__(
        self,
        source: Path | SecureFile,
        *,
        transactions: LocalTransactionCoordinator | None = None,
    ) -> None:
        self._file = coerce_secure_file(source)
        if transactions is not None and not transactions.target_matches(
            "webauthn_credentials", self._file
        ):
            self._file.close()
            raise ValueError("credential ledger unavailable")
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

    def _records_unlocked(self) -> dict[str, CredentialRecord] | None:
        try:
            content = self._file.read_optional_nonblocking()
        except UnsafePathError:
            return None
        if content is None:
            return {}
        records: dict[str, CredentialRecord] = {}
        try:
            for encoded in content.splitlines(keepends=True):
                if not encoded.endswith(b"\n") or encoded == b"\n":
                    return None
                payload = loads_strict_object(encoded[:-1].decode("utf-8"))
                record = CredentialRecord.model_validate_json(encoded[:-1])
                if record.canonical_bytes() != encoded:
                    return None
                if record.id in records:
                    return None
                if json.loads(record.canonical_bytes()) != payload:
                    return None
                records[record.id] = record
        except (TypeError, UnicodeError, ValidationError, ValueError):
            return None
        return records

    def list(self) -> tuple[CredentialRecord, ...]:
        result: tuple[CredentialRecord, ...] = ()
        unavailable = False
        try:
            with self._locked():
                records = self._records_unlocked()
                if records is None:
                    unavailable = True
                else:
                    result = tuple(records.values())
        except Exception:  # noqa: BLE001 - fixed public integrity boundary
            unavailable = True
        if unavailable:
            raise ValueError("credential ledger unavailable")
        return result

    def put(self, record: CredentialRecord) -> bool:
        validated = _validated_credential(record)
        if validated is None:
            raise ValueError("credential ledger unavailable") from None
        added = False
        unavailable = False
        try:
            with self._locked():
                records = self._records_unlocked()
                if records is None:
                    unavailable = True
                else:
                    existing = records.get(validated.id)
                    if existing is not None:
                        unavailable = existing != validated
                    else:
                        append_durable_line(self._file, validated.canonical_bytes())
                        added = True
        except Exception:  # noqa: BLE001 - fixed public integrity boundary
            unavailable = True
        if unavailable:
            raise ValueError("credential ledger unavailable")
        return added


class WebAuthnChallengeStore:
    """Append-only issue/consume transitions with an exact at-most-once consume."""

    def __init__(
        self,
        source: Path | SecureFile,
        *,
        transactions: LocalTransactionCoordinator | None = None,
    ) -> None:
        self._file = coerce_secure_file(source)
        if transactions is not None and not transactions.target_matches(
            "webauthn_challenges", self._file
        ):
            self._file.close()
            raise ValueError("challenge unavailable")
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

    def _records_unlocked(self) -> tuple[dict[str, ChallengeRecord], set[str]] | None:
        try:
            content = self._file.read_optional_nonblocking()
        except UnsafePathError:
            return None
        if content is None:
            return {}, set()
        issued: dict[str, ChallengeRecord] = {}
        consumed: set[str] = set()
        try:
            for encoded in content.splitlines(keepends=True):
                if not encoded.endswith(b"\n") or encoded == b"\n":
                    return None
                payload = loads_strict_object(encoded[:-1].decode("utf-8"))
                frame = _ChallengeFrame.model_validate_json(encoded[:-1])
                if (
                    frame.canonical_bytes() != encoded
                    or json.loads(frame.canonical_bytes()) != payload
                ):
                    return None
                record = ChallengeRecord.model_validate(
                    frame.model_dump(exclude={"kind", "consumed_at"})
                )
                if frame.kind == "issue":
                    if record.id in issued:
                        return None
                    issued[record.id] = record
                elif (
                    issued.get(record.id) != record
                    or record.id in consumed
                    or frame.consumed_at is None
                    or not _within_open_lifetime(record, frame.consumed_at)
                ):
                    return None
                else:
                    consumed.add(record.id)
        except (TypeError, UnicodeError, ValidationError, ValueError):
            return None
        return issued, consumed

    def issue(self, record: ChallengeRecord) -> bool:
        validated = _validated_challenge(record)
        if validated is None:
            raise ValueError("challenge unavailable") from None
        added = False
        unavailable = False
        try:
            frame = _ChallengeFrame.model_validate(
                {**validated.model_dump(mode="json"), "kind": "issue"}
            )
            with self._locked():
                state = self._records_unlocked()
                if state is None:
                    unavailable = True
                else:
                    issued, _ = state
                    existing = issued.get(validated.id)
                    if existing is not None:
                        unavailable = existing != validated
                    else:
                        append_durable_line(self._file, frame.canonical_bytes())
                        added = True
        except Exception:  # noqa: BLE001 - fixed public integrity boundary
            unavailable = True
        if unavailable:
            raise ValueError("challenge unavailable")
        return added

    def consume(self, challenge_id: str, now: datetime) -> ChallengeRecord:
        invalid_now = False
        validated_now: datetime | None = None
        try:
            validated_now = _ChallengeFrame.model_validate(
                {
                    **challenge_record_for_timestamp(now).model_dump(mode="json"),
                    "kind": "consume",
                    "consumed_at": now,
                }
            ).consumed_at
        except (TypeError, ValidationError, ValueError):
            invalid_now = True
        if invalid_now or type(challenge_id) is not str or validated_now is None:
            raise ValueError("challenge unavailable")
        result: ChallengeRecord | None = None
        unavailable = False
        try:
            with self._locked():
                state = self._records_unlocked()
                if state is None:
                    unavailable = True
                else:
                    issued, consumed = state
                    record = issued.get(challenge_id)
                    if (
                        record is None
                        or challenge_id in consumed
                        or not _within_open_lifetime(record, validated_now)
                    ):
                        unavailable = True
                    else:
                        frame = _ChallengeFrame.model_validate(
                            {
                                **record.model_dump(mode="json"),
                                "kind": "consume",
                                "consumed_at": validated_now,
                            }
                        )
                        append_durable_line(self._file, frame.canonical_bytes())
                        result = record
        except Exception:  # noqa: BLE001 - fixed public integrity boundary
            unavailable = True
        if unavailable or result is None:
            raise ValueError("challenge unavailable")
        return result


def challenge_record_for_timestamp(value: object) -> ChallengeRecord:
    """Reuse strict canonical timestamp validation without retaining caller data."""
    return ChallengeRecord.model_validate(
        {
            "id": "challenge:" + "0" * 64,
            "project_id": "timestamp",
            "repository_id": "repo:sha256:" + "0" * 64,
            "actor": "timestamp",
            "ceremony": "registration",
            "challenge": "timestamp",
            "issued_at": value,
            "expires_at": value,
        }
    )
