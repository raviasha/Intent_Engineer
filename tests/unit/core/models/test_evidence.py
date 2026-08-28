"""Tests for immutable, version-addressed evidence and project records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import (
    EvidenceDelta,
    EvidenceRecord,
    ProjectConfig,
    SyncCheckpoint,
)
from intent_engineering.core.models.schemas import schema_bytes

NOW = datetime(2026, 8, 25, tzinfo=UTC)
SCHEMA_DIRECTORY = Path(__file__).parents[4] / "schemas"


def evidence_record(**changes: object) -> EvidenceRecord:
    payload: dict[str, object] = {
        "id": "ev-git-1",
        "connector_type": "git",
        "external_object_id": "commit:abc",
        "external_version": "abc",
        "author": "developer@example.com",
        "observed_at": NOW,
        "source_locator": "git:abc",
        "content_hash": "sha256:1234",
        "payload": {"message": "Add local export", "files": ["README.md"]},
    }
    payload.update(changes)
    return EvidenceRecord(**payload)


def test_evidence_identity_includes_version_and_hash() -> None:
    record = evidence_record()

    assert record.identity_key == "git|commit:abc|abc|sha256:1234"
    assert record.model_copy(update={"payload": {"message": "changed"}}).id == "ev-git-1"


def test_misspelled_acl_is_rejected_instead_of_being_dropped() -> None:
    payload = evidence_record().model_dump()
    payload["acll"] = ["developers"]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EvidenceRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("model", "payload", "unknown_field"),
    [
        (ProjectConfig, {"project_id": "project", "local_actor": "tester"}, "graph_pat"),
        (
            SyncCheckpoint,
            {"connector_id": "git", "cursor": None, "committed_at": NOW},
            "commited_at",
        ),
    ],
)
def test_public_project_models_reject_unknown_fields(
    model: type[ProjectConfig | SyncCheckpoint],
    payload: dict[str, object],
    unknown_field: str,
) -> None:
    raw = dict(payload)
    raw[unknown_field] = "unexpected"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(raw)


def test_evidence_payload_is_deeply_immutable_and_detached_from_input() -> None:
    payload = {"message": "Add local export", "files": ["README.md"]}
    record = evidence_record(payload=payload, acl=["developers"])
    payload["files"].append("pyproject.toml")

    assert record.payload == {"message": "Add local export", "files": ("README.md",)}
    assert record.acl == ("developers",)
    with pytest.raises(TypeError):
        record.payload["message"] = "changed"  # type: ignore[index]


def test_evidence_delta_normalizes_collections_to_immutable_values() -> None:
    delta = EvidenceDelta(added=[evidence_record()], prior_versions={"commit:abc": "prior"})

    assert isinstance(delta.added, tuple)
    assert dict(delta.prior_versions) == {"commit:abc": "prior"}
    with pytest.raises(TypeError):
        delta.prior_versions["commit:abc"] = "changed"  # type: ignore[index]


def test_project_config_has_immutable_independent_context_limits() -> None:
    first = ProjectConfig(project_id="first", local_actor="tester")
    second = ProjectConfig(project_id="second", local_actor="tester")

    assert first.context_limits == {
        "relevant_intent": 10,
        "relevant_requirements": 10,
        "decisions": 10,
        "constraints": 10,
        "acceptance_criteria": 10,
        "code_refs": 20,
        "test_refs": 20,
        "open_reconciliation_cases": 10,
        "evidence_refs": 20,
    }
    assert first.context_limits is not second.context_limits
    with pytest.raises(TypeError):
        first.context_limits["decisions"] = 20  # type: ignore[index]


def test_checkpoint_is_immutable() -> None:
    checkpoint = SyncCheckpoint(connector_id="git", cursor=None, committed_at=NOW)

    assert checkpoint.connector_id == "git"
    with pytest.raises(ValidationError):
        checkpoint.cursor = "abc"  # type: ignore[misc]


def test_checkpoint_consumption_boundary_is_versioned_and_unique() -> None:
    checkpoint = SyncCheckpoint(
        connector_id="git",
        cursor="abc",
        committed_at=NOW,
        consumed_evidence_ids=("evidence:one", "evidence:two"),
    )

    assert checkpoint.consumption_schema_version == 1
    assert checkpoint.consumed_evidence_ids == ("evidence:one", "evidence:two")
    with pytest.raises(ValidationError):
        SyncCheckpoint(
            connector_id="git",
            cursor="abc",
            committed_at=NOW,
            consumed_evidence_ids=("evidence:one", "evidence:one"),
        )


@pytest.mark.parametrize(("name", "model"), [("graph", "Graph"), ("evidence", "EvidenceRecord")])
def test_checked_in_schemas_match_deterministic_regeneration(name: str, model: str) -> None:
    assert (SCHEMA_DIRECTORY / f"{name}.schema.json").read_bytes() == schema_bytes(model)
