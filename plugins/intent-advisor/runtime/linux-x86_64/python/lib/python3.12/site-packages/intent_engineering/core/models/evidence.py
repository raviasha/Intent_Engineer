"""Immutable, version-addressed source evidence records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Literal, cast

from pydantic import ConfigDict, Field, field_serializer, field_validator

from intent_engineering.core.models._base import StrictModel

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


def freeze_json(value: JsonValue) -> object:
    """Detach and recursively freeze a JSON-compatible value."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze_json(item) for item in value)
    return value


def thaw_json(value: object) -> JsonValue:
    """Produce a JSON-compatible copy of a recursively frozen value."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return cast(JsonValue, value)


class EvidenceRef(StrictModel):
    """A stable reference to immutable evidence."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str
    locator: str | None = None


class EvidenceRecord(StrictModel):
    """A normalized, version-addressed record captured from a source."""

    model_config = ConfigDict(frozen=True)

    id: str
    connector_type: str
    external_object_id: str
    external_version: str
    author: str | None
    observed_at: datetime
    source_locator: str
    content_hash: str
    payload: Mapping[str, JsonValue]
    parent_ref: str | None = None
    acl: tuple[str, ...] = ()

    @field_validator("payload")
    @classmethod
    def freeze_payload(cls, payload: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return cast(Mapping[str, JsonValue], freeze_json(dict(payload)))

    @field_serializer("payload")
    def serialize_payload(self, payload: Mapping[str, JsonValue]) -> JsonValue:
        return thaw_json(payload)

    @property
    def identity_key(self) -> str:
        """Return the idempotency key for this exact external evidence version."""
        return (
            f"{self.connector_type}|{self.external_object_id}|{self.external_version}|"
            f"{self.content_hash}"
        )


class EvidenceIngestion(StrictModel):
    """One atomic connector-instance association in the durable evidence ledger."""

    model_config = ConfigDict(frozen=True, strict=True)

    storage_schema_version: Literal[1] = 1
    connector_id: str = Field(min_length=1, pattern=r".*\S.*")
    sequence: int = Field(ge=1)
    predecessor_id: str | None = None
    evidence: EvidenceRecord

    @field_validator("storage_schema_version", "sequence", mode="before")
    @classmethod
    def reject_boolean_or_string_integers(cls, value: object) -> object:
        """Keep ledger control fields exact despite Literal's bool equality semantics."""
        if type(value) is not int:
            raise ValueError("ledger integer fields must be integers")
        return value


class EvidenceDelta(StrictModel):
    """Evidence newly durable for a source plus prior version links."""

    model_config = ConfigDict(frozen=True)

    added: tuple[EvidenceRecord, ...]
    prior_versions: Mapping[str, str]
    ingestions: tuple[EvidenceIngestion, ...] = ()

    @field_validator("prior_versions")
    @classmethod
    def freeze_prior_versions(cls, prior_versions: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(prior_versions))

    @field_serializer("prior_versions")
    def serialize_prior_versions(self, prior_versions: Mapping[str, str]) -> dict[str, str]:
        return dict(prior_versions)


def is_exact_consumed_prefix(
    consumed_evidence_ids: Sequence[str],
    ledger_evidence_ids: Sequence[str],
) -> bool:
    """Return whether consumption is exactly the ordered prefix of one connector ledger."""
    consumed = tuple(consumed_evidence_ids)
    ledger = tuple(ledger_evidence_ids)
    return len(consumed) <= len(ledger) and consumed == ledger[: len(consumed)]
