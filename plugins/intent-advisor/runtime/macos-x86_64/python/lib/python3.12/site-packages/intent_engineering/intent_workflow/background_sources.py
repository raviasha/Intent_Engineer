"""Versioned evidence from authorized reads performed by the host's source connections."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, field_validator

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_models import BackgroundModel

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime


class BackgroundSource(BackgroundModel):
    provider: Literal["notion", "confluence", "slack", "google-drive", "github", "local"]
    object_id: Annotated[str, Field(min_length=1, max_length=2048)]
    version: Annotated[str, Field(min_length=1, max_length=1024)]
    locator: Annotated[str, Field(min_length=1, max_length=4096)]
    author: Annotated[str | None, Field(min_length=1, max_length=512)] = None
    observed_at: datetime
    content: Annotated[str, Field(min_length=1, max_length=524288)]
    source_acl: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512)], ...], Field(max_length=128)
    ] = ()

    @field_validator("object_id", "version", "locator", "author")
    @classmethod
    def valid_identity(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError("invalid source identity")
        return value

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("source timestamp requires a timezone")
        return value


def capture_source(runtime: Runtime, draft: BackgroundSource) -> dict[str, str]:
    """Store a host-reported read; this does not authenticate or connect a provider."""
    from intent_engineering.cli.intent_workflow import _snapshot_config

    draft = BackgroundSource.model_validate_json(draft.model_dump_json())
    config_file = runtime.workspace_directory.file("config.yaml")
    checkpoints = runtime.workspace_directory.file("cache/checkpoints.yaml")
    try:
        with runtime.transactions.transaction(
            rollback_base_exceptions=True,
            extras={"background_config": config_file, "checkpoints": checkpoints},
        ) as transaction:
            BackgroundIntentService._validate_state(transaction)
            config, _ = _snapshot_config(runtime)
            connector = f"background:{draft.provider}"
            record = normalize_raw_source(
                RawSourceObject(
                    connector_type=connector,
                    external_object_id=draft.object_id,
                    external_version=draft.version,
                    source_locator=draft.locator,
                    author=draft.author,
                    observed_at=draft.observed_at,
                    content_hash=f"sha256:{sha256(draft.content.encode()).hexdigest()}",
                    payload={
                        "content": draft.content,
                        "provider": draft.provider,
                        "capture_method": "host_reported",
                        "source_acl": list(draft.source_acl),
                    },
                    acl=(config.local_actor,),
                )
            )
            previous = next(
                (
                    item.evidence
                    for item in runtime.evidence_store.ledger(connector)
                    if item.evidence.id == record.id
                ),
                None,
            )
            if previous is not None:
                # A later read of the same immutable revision reuses its first observation.
                if record.model_copy(update={"observed_at": previous.observed_at}) != previous:
                    raise ValueError("source revision metadata changed")
                return {"status": "captured", "evidence_ref": previous.id}
            runtime.evidence_store.associate(connector, record)
            BackgroundIntentService._validate_state(transaction)
            return {"status": "captured", "evidence_ref": record.id}
    finally:
        config_file.close()
        checkpoints.close()
