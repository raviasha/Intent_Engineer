"""Descriptor-safe at-most-once claims and immutable execution receipts."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.mutations.models import ExecutionReceipt
from intent_engineering.storage._atomic import append_durable_line, same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile, coerce_secure_file
from intent_engineering.storage.transaction import LocalTransactionCoordinator


class ReceiptStoreError(ValueError):
    """Fixed public integrity failure for execution claims and receipts."""


_PLAN_ID = r"^write-plan:sha256:[0-9a-f]{64}$"
_APPROVAL_ID = r"^approval:sha256:[0-9a-f]{64}$"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class _Claim(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    storage_schema_version: Literal[1] = 1
    kind: Literal["claim"] = "claim"
    plan_id: str = Field(pattern=_PLAN_ID)
    approval_id: str = Field(pattern=_APPROVAL_ID)
    actor: str
    claimed_at: datetime

    @field_validator("plan_id", "approval_id", "actor")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip() or len(value) > 2048 or _CONTROL.search(value):
            raise ValueError("invalid execution claim")
        return value

    @field_validator("claimed_at")
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("invalid execution claim")
        return value.astimezone(UTC)


class _Completion(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    storage_schema_version: Literal[1] = 1
    kind: Literal["receipt"] = "receipt"
    receipt: ExecutionReceipt


def _serialize(record: _Claim | _Completion) -> bytes:
    return (
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _decode_content(
    content: bytes | None,
) -> (
    tuple[
        dict[tuple[str, str], _Claim],
        dict[tuple[str, str], ExecutionReceipt],
    ]
    | None
):
    claims: dict[tuple[str, str], _Claim] = {}
    receipts: dict[tuple[str, str], ExecutionReceipt] = {}
    try:
        if content is None:
            return claims, receipts
        for encoded_line in content.splitlines(keepends=True):
            if not encoded_line.endswith(b"\n") or not encoded_line.strip():
                return None
            line = encoded_line[:-1].decode("utf-8")
            payload = loads_strict_object(line)
            kind = payload.get("kind")
            if kind == "claim":
                claim = _Claim.model_validate_json(line)
                if _serialize(claim) != encoded_line:
                    return None
                key = (claim.plan_id, claim.approval_id)
                if key in claims or key in receipts:
                    return None
                claims[key] = claim
            elif kind == "receipt":
                completion = _Completion.model_validate_json(line)
                if _serialize(completion) != encoded_line:
                    return None
                receipt = completion.receipt
                key = (receipt.plan_id, receipt.approval_id)
                if key not in claims or key in receipts:
                    return None
                claim = claims[key]
                if claim.actor != receipt.executed_by:
                    return None
                receipts[key] = receipt
            else:
                return None
    except (TypeError, UnicodeError, ValidationError, ValueError):
        return None
    return claims, receipts


def validate_receipt_ledger(content: bytes | None) -> None:
    """Check immutable claim/receipt bytes with the canonical store's integrity rules."""
    if _decode_content(content) is None:
        raise ReceiptStoreError("invalid receipt store") from None


def validate_receipt_completion(
    content: bytes | None,
    receipt: ExecutionReceipt,
) -> bytes:
    """Validate one terminal receipt against locked durable claim bytes."""
    try:
        validated = ExecutionReceipt.model_validate_json(receipt.model_dump_json())
    except (TypeError, ValidationError, ValueError):
        raise ReceiptStoreError("invalid execution receipt") from None
    decoded = _decode_content(content)
    if decoded is None:
        raise ReceiptStoreError("invalid receipt store") from None
    claims, receipts = decoded
    key = (validated.plan_id, validated.approval_id)
    claim = claims.get(key)
    if claim is None or claim.actor != validated.executed_by:
        raise ReceiptStoreError("invalid execution receipt") from None
    existing = receipts.get(key)
    if existing is not None:
        if existing != validated:
            raise ReceiptStoreError("conflicting execution receipt") from None
        return b""
    return _serialize(_Completion(receipt=validated))


def _validated_claim(
    plan_id: object,
    approval_id: object,
    actor: object,
    claimed_at: object,
) -> _Claim | None:
    try:
        return _Claim(
            plan_id=plan_id,  # type: ignore[arg-type]
            approval_id=approval_id,  # type: ignore[arg-type]
            actor=actor,  # type: ignore[arg-type]
            claimed_at=claimed_at,  # type: ignore[arg-type]
        )
    except (TypeError, ValidationError, ValueError):
        return None


class JsonlReceiptStore:
    """Append a durable claim before mutation and at most one terminal receipt."""

    def __init__(
        self,
        path: Path | SecureFile,
        *,
        transactions: LocalTransactionCoordinator | None = None,
    ) -> None:
        self._file = coerce_secure_file(path)
        if transactions is not None and not transactions.target_matches("receipts", self._file):
            raise ValueError("receipt transaction target is unavailable")
        self._transactions = transactions
        self.path = self._file.path
        self._claims: dict[tuple[str, str], _Claim] = {}
        self._receipts: dict[tuple[str, str], ExecutionReceipt] = {}
        with self._locked():
            self._rebuild_unlocked()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self._transactions is None:
            with same_path_lock(self._file):
                yield
            return
        with self._transactions.coordinated(), same_path_lock(self._file):
            yield

    def _decode_unlocked(
        self,
    ) -> (
        tuple[
            dict[tuple[str, str], _Claim],
            dict[tuple[str, str], ExecutionReceipt],
        ]
        | None
    ):
        return _decode_content(self._file.read_optional())

    def _rebuild_unlocked(self) -> None:
        decoded = self._decode_unlocked()
        if decoded is None:
            raise ReceiptStoreError("invalid receipt store") from None
        self._claims, self._receipts = decoded

    def claim(
        self,
        plan_id: str,
        approval_id: str,
        actor: str,
        claimed_at: datetime,
    ) -> bool:
        claim = _validated_claim(plan_id, approval_id, actor, claimed_at)
        del plan_id, approval_id, actor, claimed_at
        if claim is None:
            raise ReceiptStoreError("invalid execution claim") from None
        key = (claim.plan_id, claim.approval_id)
        with self._locked():
            self._rebuild_unlocked()
            if key in self._claims:
                return False
            append_durable_line(self._file, _serialize(claim))
            self._claims[key] = claim
            return True

    def complete(self, receipt: ExecutionReceipt) -> bool:
        with self._locked():
            self._rebuild_unlocked()
            serialized = validate_receipt_completion(self._file.read_optional(), receipt)
            if not serialized:
                return False
            validated = ExecutionReceipt.model_validate_json(receipt.model_dump_json())
            append_durable_line(self._file, serialized)
            self._receipts[(validated.plan_id, validated.approval_id)] = validated
            return True

    def get_for(self, plan_id: str, approval_id: str) -> ExecutionReceipt | None:
        with self._locked():
            self._rebuild_unlocked()
            return self._receipts.get((plan_id, approval_id))

    def is_claimed(self, plan_id: str, approval_id: str) -> bool:
        with self._locked():
            self._rebuild_unlocked()
            return (plan_id, approval_id) in self._claims

    def list(self) -> tuple[ExecutionReceipt, ...]:
        with self._locked():
            self._rebuild_unlocked()
            return tuple(self._receipts[key] for key in sorted(self._receipts))
