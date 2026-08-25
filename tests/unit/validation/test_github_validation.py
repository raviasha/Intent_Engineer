"""Deep validation for repository-scoped GitHub ledgers and cursors."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.capture.github.connector import GitHubCheckpoint
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import JsonValue
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.validation import validate_project

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def _issue_payload(repository: str, updated_at: str) -> dict[str, JsonValue]:
    return {
        "kind": "issue",
        "repository": repository,
        "provider_id": 1042,
        "number": 42,
        "title": "Durable evidence",
        "body": None,
        "state": "open",
        "labels": [],
        "milestone": None,
        "updated_at": updated_at,
    }


def _seed_github(root: Path) -> tuple[str, str]:
    initialize_project(root)
    runtime = load_runtime(root)
    payload = _issue_payload("acme/demo", "2026-08-25T10:00:00Z")
    encoded = (
        __import__("json")
        .dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        .encode("utf-8")
    )
    record = normalize_raw_source(
        RawSourceObject(
            connector_type="github",
            external_object_id="github:acme/demo:issue:42",
            external_version="2026-08-25T10:00:00Z",
            author="octocat",
            observed_at=datetime(2026, 8, 25, 10, tzinfo=UTC),
            source_locator="https://github.com/acme/demo/issues/42",
            content_hash=f"sha256:{sha256(encoded).hexdigest()}",
            payload=payload,
        )
    )
    runtime.evidence_store.associate("github:acme/demo", record)
    cursor = GitHubCheckpoint(
        repository="acme/demo",
        etags={"issues": '"issues-1"'},
        newest_updated_at=datetime(2026, 8, 25, 10, tzinfo=UTC),
    ).encode()
    runtime.checkpoint_store.compare_and_set(
        "github:acme/demo",
        expected=None,
        cursor=cursor,
        committed_at=NOW,
        consumed_evidence_ids=(record.id,),
    )
    return record.id, cursor


def _codes(root: Path) -> tuple[str, ...]:
    return tuple(item.code for item in validate_project(root).diagnostics)


def _seed_association(
    root: Path,
    *,
    connector_type: str,
    external_object_id: str,
    external_version: str,
    observed_at: datetime,
    payload: dict[str, JsonValue],
    cursor: str | None,
) -> str:
    initialize_project(root)
    runtime = load_runtime(root)
    encoded = (
        __import__("json")
        .dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        .encode("utf-8")
    )
    record = normalize_raw_source(
        RawSourceObject(
            connector_type=connector_type,
            external_object_id=external_object_id,
            external_version=external_version,
            author=None,
            observed_at=observed_at,
            source_locator="https://github.com/acme/demo/issues/42",
            content_hash=f"sha256:{sha256(encoded).hexdigest()}",
            payload=payload,
        )
    )
    runtime.evidence_store.associate("github:acme/demo", record)
    runtime.checkpoint_store.compare_and_set(
        "github:acme/demo",
        expected=None,
        cursor=cursor,
        committed_at=NOW,
        consumed_evidence_ids=(record.id,),
    )
    return record.id


def test_valid_github_ledger_and_checkpoint_pass_deep_validation(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _seed_github(root)

    report = validate_project(root)

    assert report.valid is True
    assert report.diagnostics == ()


def test_foreign_repository_cursor_is_diagnosed_without_cursor_or_path_leakage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _, cursor = _seed_github(root)
    checkpoint_path = root / ".intent/cache/checkpoints.yaml"
    data = yaml.safe_load(checkpoint_path.read_text())
    data["checkpoints"]["github:acme/demo"]["cursor"] = cursor.replace("acme/demo", "acme/foreign")
    checkpoint_path.write_text(yaml.safe_dump(data, sort_keys=True))

    report = validate_project(root)

    assert "checkpoint.cursor_invalid" in tuple(item.code for item in report.diagnostics)
    rendered = report.model_dump_json()
    assert "foreign" not in rendered


def test_unknown_connector_and_foreign_or_nonprefix_consumption_remain_errors(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    evidence_id, _ = _seed_github(root)
    checkpoint_path = root / ".intent/cache/checkpoints.yaml"
    data = yaml.safe_load(checkpoint_path.read_text())
    record = data["checkpoints"].pop("github:acme/demo")
    record["connector_id"] = "github:acme/unknown"
    record["consumed_evidence_ids"] = (evidence_id, "evidence:missing")
    data["checkpoints"]["github:acme/unknown"] = record
    checkpoint_path.write_text(yaml.safe_dump(data, sort_keys=True))

    codes = _codes(root)

    assert "checkpoint.consumed_evidence_missing" in codes
    assert "checkpoint.consumed_evidence_prefix_invalid" in codes
    assert "checkpoint.cursor_invalid" in codes


def test_truly_unknown_connector_id_remains_an_error(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _seed_github(root)
    checkpoint_path = root / ".intent/cache/checkpoints.yaml"
    data = yaml.safe_load(checkpoint_path.read_text())
    record = data["checkpoints"].pop("github:acme/demo")
    record["connector_id"] = "unknown:connector"
    data["checkpoints"]["unknown:connector"] = record
    checkpoint_path.write_text(yaml.safe_dump(data, sort_keys=True))

    assert "checkpoint.connector_unknown" in _codes(root)


def test_github_checkpoint_rejects_non_github_consumed_association(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    cursor = GitHubCheckpoint(repository="acme/demo").encode()
    _seed_association(
        root,
        connector_type="git",
        external_object_id=f"commit:{'a' * 40}",
        external_version="a" * 40,
        observed_at=NOW,
        payload={"sha": "a" * 40},
        cursor=cursor,
    )

    assert "checkpoint.consumed_evidence_foreign" in _codes(root)


def test_github_checkpoint_rejects_foreign_repository_consumed_association(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    observed = datetime(2026, 8, 25, 10, tzinfo=UTC)
    cursor = GitHubCheckpoint(repository="acme/demo", newest_updated_at=observed).encode()
    _seed_association(
        root,
        connector_type="github",
        external_object_id="github:acme/other:issue:42",
        external_version="2026-08-25T10:00:00Z",
        observed_at=observed,
        payload=_issue_payload("acme/other", "2026-08-25T10:00:00Z"),
        cursor=cursor,
    )

    assert "checkpoint.consumed_evidence_foreign" in _codes(root)


def test_github_checkpoint_requires_cursor_after_evidence_is_consumed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    observed = datetime(2026, 8, 25, 10, tzinfo=UTC)
    _seed_association(
        root,
        connector_type="github",
        external_object_id="github:acme/demo:issue:42",
        external_version="2026-08-25T10:00:00Z",
        observed_at=observed,
        payload=_issue_payload("acme/demo", "2026-08-25T10:00:00Z"),
        cursor=None,
    )

    assert "checkpoint.cursor_invalid" in _codes(root)


@pytest.mark.parametrize(
    "newest_updated_at",
    [None, datetime(2026, 8, 25, 9, tzinfo=UTC)],
)
def test_github_checkpoint_requires_exact_newest_mutable_timestamp(
    tmp_path: Path,
    newest_updated_at: datetime | None,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    observed = datetime(2026, 8, 25, 10, tzinfo=UTC)
    cursor = GitHubCheckpoint(
        repository="acme/demo",
        newest_updated_at=newest_updated_at,
    ).encode()
    _seed_association(
        root,
        connector_type="github",
        external_object_id="github:acme/demo:issue:42",
        external_version="2026-08-25T10:00:00Z",
        observed_at=observed,
        payload=_issue_payload("acme/demo", "2026-08-25T10:00:00Z"),
        cursor=cursor,
    )

    assert "checkpoint.cursor_invalid" in _codes(root)


@pytest.mark.parametrize(
    ("newest_commit_sha", "expected_code"),
    [(None, "checkpoint.cursor_invalid"), ("b" * 40, "checkpoint.evidence_missing")],
)
def test_github_checkpoint_rejects_missing_or_foreign_newest_commit_sha(
    tmp_path: Path,
    newest_commit_sha: str | None,
    expected_code: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    cursor = GitHubCheckpoint(repository="acme/demo", newest_commit_sha=newest_commit_sha).encode()
    _seed_association(
        root,
        connector_type="github",
        external_object_id=f"github:acme/demo:commit:{'a' * 40}",
        external_version="a" * 40,
        observed_at=NOW,
        payload={"kind": "commit", "repository": "acme/demo", "sha": "a" * 40},
        cursor=cursor,
    )

    assert expected_code in _codes(root)


def test_github_checkpoint_rejects_malformed_scoped_external_identity(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    cursor = GitHubCheckpoint(repository="acme/demo").encode()
    _seed_association(
        root,
        connector_type="github",
        external_object_id="github:acme/demo:commit:not-a-sha",
        external_version="not-a-sha",
        observed_at=NOW,
        payload={"kind": "commit", "repository": "acme/demo", "sha": "not-a-sha"},
        cursor=cursor,
    )

    assert "checkpoint.consumed_evidence_foreign" in _codes(root)
