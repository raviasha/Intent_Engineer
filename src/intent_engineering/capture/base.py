"""Provider-neutral contracts for source capture adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from typing import Protocol

from pydantic import BaseModel, ConfigDict, field_validator

from intent_engineering.core.models import EvidenceRecord, JsonValue
from intent_engineering.core.models.evidence import freeze_json


class ConnectorError(RuntimeError):
    """Raised when a connector cannot discover or fetch source evidence."""


class SourceObject(BaseModel):
    """A discovered version-addressed object that can be fetched by a connector."""

    model_config = ConfigDict(frozen=True)

    external_object_id: str
    external_version: str
    locator: str


class RawSourceObject(BaseModel):
    """Connector-neutral raw data ready to normalize into immutable evidence."""

    model_config = ConfigDict(frozen=True)

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
        return freeze_json(dict(payload))  # type: ignore[return-value]


class Connector(Protocol):
    """Asynchronous, provider-neutral source capture port."""

    connector_id: str

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        raise NotImplementedError

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        raise NotImplementedError

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        raise NotImplementedError

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        raise NotImplementedError


def evidence_id(raw: RawSourceObject) -> str:
    """Return a stable ID for one immutable external evidence version."""
    material = (
        f"{raw.connector_type}\x00{raw.external_object_id}\x00{raw.external_version}"
        f"\x00{raw.content_hash}"
    )
    return f"evidence:sha256:{sha256(material.encode('utf-8')).hexdigest()}"


def normalize_raw_source(raw: RawSourceObject) -> EvidenceRecord:
    """Construct the shared immutable evidence model without provider vocabulary."""
    return EvidenceRecord(
        id=evidence_id(raw),
        connector_type=raw.connector_type,
        external_object_id=raw.external_object_id,
        external_version=raw.external_version,
        author=raw.author,
        observed_at=raw.observed_at,
        source_locator=raw.source_locator,
        content_hash=raw.content_hash,
        payload=raw.payload,
        parent_ref=raw.parent_ref,
        acl=raw.acl,
    )
