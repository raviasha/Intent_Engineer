"""End-to-end contracts for the consolidated ``intent check`` command."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    Node,
    NodeType,
    RelationType,
    SourceMode,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.team_state.restore import TRUST_ENVIRONMENT_VARIABLE
from tests.helpers.cli import init_git_repo
from tests.helpers.shared_state import (
    artifacts as shared_state_artifacts,
)
from tests.helpers.shared_state import (
    canonical_files,
    install_state_ref,
    trust_environment,
)
from tests.helpers.shared_state import (
    keys as shared_state_keys,
)

NOW = datetime(2026, 9, 7, 10, tzinfo=UTC)


def _reviewed_command_id(argv: list[str]) -> str:
    encoded = json.dumps(argv, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return f"test:sha256:{hashlib.sha256(encoded).hexdigest()}"


def _configure_reviewed_test(repo: Path) -> str:
    runner = repo / "tools/test-runner"
    runner.parent.mkdir(exist_ok=True)
    runner.write_text(
        "#!/usr/bin/python3\nfrom pathlib import Path\nPath('reviewed-ran').write_text('yes')\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    config_path = repo / ".intent/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["test_commands"] = [["tools/test-runner"]]
    config["test_result_paths"] = [".intent/cache/reviewed-test.json"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    return _reviewed_command_id(["tools/test-runner"])


def _ready(repo: Path) -> None:
    initialize_project(repo)
    runtime = load_runtime(repo)
    try:
        content = "Consolidated checks preserve reviewed intent"
        evidence = normalize_raw_source(
            RawSourceObject(
                connector_type="markdown",
                external_object_id="path:approved-intent.md",
                external_version="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
                author="local:owner",
                observed_at=NOW,
                source_locator="approved-intent.md",
                content_hash="sha256:" + hashlib.sha256(content.encode()).hexdigest(),
                payload={"content": content},
            )
        )
        runtime.evidence_store.associate("markdown", evidence)
        node = Node(
            id="intent:check",
            type=NodeType.PRODUCT_INTENT,
            label=content,
            status="active",
            created_by="local:owner",
            created_at=NOW,
            last_modified_by="local:owner",
            last_modified_at=NOW,
            source_mode=SourceMode.EXPLICIT,
            intent_fidelity_confidence=1.0,
            confidence_basis="approved test fixture",
            evidence_refs=(evidence.id,),
        )
        requirement = Node(
            id="requirement:check",
            type=NodeType.REQUIREMENT,
            label="Run the consolidated check",
            status="active",
            created_by="local:owner",
            created_at=NOW,
            last_modified_by="local:owner",
            last_modified_at=NOW,
            source_mode=SourceMode.EXPLICIT,
            intent_fidelity_confidence=1.0,
            confidence_basis="approved test fixture",
            evidence_refs=(evidence.id,),
        )
        edge = Edge(
            id="edge:check-requirement",
            from_id=node.id,
            relation=RelationType.REFINES,
            to_id=requirement.id,
            status="active",
            created_by="local:owner",
            created_at=NOW,
            last_modified_by="local:owner",
            last_modified_at=NOW,
        )
        runtime.graph_store.apply(
            ChangeSet(
                id="changeset:approved-check-baseline",
                actor="local:owner",
                timestamp=NOW,
                baseline_graph_version=0,
                evidence_refs=(evidence.id,),
                nodes_added=(node, requirement),
                nodes_updated=(),
                nodes_superseded=(),
                edges_added=(edge,),
                edges_updated=(),
                edges_superseded=(),
                confidence_changes=(),
                implementation_status_changes=(),
                reconciliation_cases_created=(),
                reconciliation_cases_resolved=(),
                validation_status="validated",
            )
        )
    finally:
        runtime.close()


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _write_result(repo: Path, path: Path) -> None:
    config = json.loads(
        subprocess.run(
            [
                str(Path(os.sys.executable)),
                "-c",
                "import json,yaml; print(json.dumps(yaml.safe_load(open('.intent/config.yaml'))))",
            ],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository_id": config["project_id"],
                "commit_sha": _head(repo),
                "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "status": "passed",
                "test_ids": ["test:repository"],
                "author": "ci:test-runner",
                "acl": [config["local_actor"]],
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_check_emits_one_strict_versioned_json_result_for_an_aligned_project(
    tmp_path: Path,
) -> None:
    """Catches the consolidated command leaking stage logs or omitting a stable result."""
    repo = init_git_repo(tmp_path)
    _ready(repo)

    result = CliRunner().invoke(
        app,
        ["check", "--project", str(repo), "--sources", "markdown,git", "--format", "json"],
    )

    assert result.exit_code == 0, repr(result.exception)
    assert json.loads(result.stdout) | {} == {
        **json.loads(result.stdout),
        "version": "1",
        "schema_version": 1,
        "status": "passed",
        "reason": "checks_passed",
        "exit_code": 0,
    }
    assert "Consolidated checks preserve" not in result.stdout + result.stderr


@pytest.mark.parametrize("path", ("config.yaml", "graph.yaml", "history/.local-transaction.json"))
def test_check_authenticates_readiness_before_opening_mutable_stores(
    tmp_path: Path, path: str
) -> None:
    """Catches config, graph or recovery FIFOs blocking the real check before readiness."""
    repo = init_git_repo(tmp_path)
    _ready(repo)
    workspace = repo / ".intent"
    target = workspace / path
    target.unlink(missing_ok=True)
    os.mkfifo(target)
    before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from intent_engineering.cli.app import app; app()",
            "check",
            "--project",
            str(repo),
            "--format",
            "json",
        ],
        capture_output=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == b""
    assert json.loads(completed.stdout) == {
        "version": "1",
        "schema_version": 1,
        "status": "failed",
        "reason": "readiness_required",
        "exit_code": 1,
        "readiness_status": None,
        "capture_status": None,
        "validation_valid": None,
        "test_evidence_id": None,
        "review_case_count": 0,
        "drift_report": "",
    }
    assert stat.S_ISFIFO(target.lstat().st_mode)
    assert {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before


def test_ci_check_never_initializes_an_unonboarded_repository(tmp_path: Path) -> None:
    """Catches the required check manufacturing or approving an empty baseline."""
    repo = init_git_repo(tmp_path)

    result = CliRunner().invoke(
        app,
        ["check", "--project", str(repo), "--ci", "--require-review", "--format", "json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["reason"] == "readiness_required"
    assert not (repo / ".intent").exists()


def test_ci_check_rejects_a_local_only_manufactured_baseline(tmp_path: Path) -> None:
    """Catches local state satisfying CI without a verified approved shared baseline."""
    repo = init_git_repo(tmp_path)
    _ready(repo)
    result_path = repo / "test-results.json"
    _write_result(repo, result_path)
    workspace = repo / ".intent"
    before = {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }

    result = CliRunner().invoke(
        app,
        [
            "check",
            "--project",
            str(repo),
            "--ci",
            "--test-results",
            "test-results.json",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) | {} == {
        **json.loads(result.stdout),
        "status": "failed",
        "reason": "readiness_required",
        "readiness_status": "shared_state_unavailable",
        "exit_code": 1,
    }
    assert {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file() and not path.is_symlink()
    } == before


def test_ci_check_restores_the_signed_approved_ref_before_readiness_and_capture(
    tmp_path: Path,
) -> None:
    """Catches the production CLI retaining no concrete shared-state restorer."""
    repo = init_git_repo(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/acme/project.git"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    _ready(repo)
    recipient, signer, trust = shared_state_keys(project_id=repo.name)
    release = shared_state_artifacts(canonical_files(repo), recipient, signer, project_id=repo.name)
    install_state_ref(repo, release)
    result_path = repo / "test-results.json"
    _write_result(repo, result_path)
    shutil.rmtree(repo / ".intent")

    result = CliRunner().invoke(
        app,
        [
            "check",
            "--project",
            str(repo),
            "--ci",
            "--test-results",
            "test-results.json",
            "--format",
            "json",
        ],
        env={TRUST_ENVIRONMENT_VARIABLE: trust_environment(trust)},
    )

    assert result.exit_code == 0, (result.stdout, result.stderr, repr(result.exception))
    assert json.loads(result.stdout) | {} == {
        **json.loads(result.stdout),
        "status": "passed",
        "reason": "checks_passed",
        "readiness_status": "ready",
        "exit_code": 0,
    }
    assert (repo / ".intent/cache/shared-state.json").is_file()


def test_check_rejects_an_unknown_source_with_one_fixed_json_failure(tmp_path: Path) -> None:
    """Catches connector selection errors escaping the strict check result boundary."""
    repo = init_git_repo(tmp_path)

    result = CliRunner().invoke(
        app,
        ["check", "--project", str(repo), "--sources", "prompt-command", "--format", "json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) | {} == {
        **json.loads(result.stdout),
        "version": "1",
        "schema_version": 1,
        "status": "failed",
        "reason": "operation_failed",
        "exit_code": 1,
    }
    assert not (repo / ".intent").exists()


def test_check_ingests_a_bound_test_result_once_and_replay_is_a_byte_noop(
    tmp_path: Path,
) -> None:
    """Catches canonical test-result replay appending duplicate evidence or checkpoints."""
    repo = init_git_repo(tmp_path)
    _ready(repo)
    result_path = repo / "test-results.json"
    _write_result(repo, result_path)
    arguments = [
        "check",
        "--project",
        str(repo),
        "--sources",
        "markdown,git",
        "--test-results",
        "test-results.json",
        "--format",
        "json",
    ]

    first = CliRunner().invoke(app, arguments)
    assert first.exit_code == 0, repr(first.exception)
    evidence = (repo / ".intent" / "evidence" / "evidence.jsonl").read_bytes()
    checkpoints = (repo / ".intent" / "cache" / "checkpoints.yaml").read_bytes()
    second = CliRunner().invoke(app, arguments)

    assert second.exit_code == 0, repr(second.exception)
    assert (repo / ".intent" / "evidence" / "evidence.jsonl").read_bytes() == evidence
    assert (repo / ".intent" / "cache" / "checkpoints.yaml").read_bytes() == checkpoints
    stored = load_runtime(repo)
    try:
        records = tuple(
            record for record in stored.evidence() if record.connector_type == "test_result"
        )
    finally:
        stored.close()
    assert len(records) == 1
    assert records[0].payload["commit_sha"] == _head(repo)


def test_partial_connector_failure_returns_three_after_validating_surviving_state(
    tmp_path: Path,
) -> None:
    """Catches one failed connector corrupting or collapsing the consolidated result."""
    repo = tmp_path / "plain-project"
    repo.mkdir()
    _ready(repo)

    result = CliRunner().invoke(
        app,
        ["check", "--project", str(repo), "--sources", "markdown,git", "--format", "json"],
    )

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert (payload["status"], payload["reason"], payload["validation_valid"]) == (
        "partial",
        "capture_partial",
        True,
    )


def test_test_result_path_is_descriptor_safe_and_fails_without_evidence_mutation(
    tmp_path: Path,
) -> None:
    """Catches symlinked result input escaping the project descriptor boundary."""
    repo = init_git_repo(tmp_path)
    _ready(repo)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (repo / "test-results.json").symlink_to(outside)
    evidence_path = repo / ".intent" / "evidence" / "evidence.jsonl"
    before = evidence_path.read_bytes() if evidence_path.exists() else None

    result = CliRunner().invoke(
        app,
        [
            "check",
            "--project",
            str(repo),
            "--test-results",
            "test-results.json",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"
    assert (evidence_path.read_bytes() if evidence_path.exists() else None) == before


def test_check_explicitly_runs_one_reviewed_command_and_ingests_its_artifact(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path)
    _ready(repo)
    command_id = _configure_reviewed_test(repo)

    result = CliRunner().invoke(
        app,
        [
            "check",
            "--project",
            str(repo),
            "--run-test",
            command_id,
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, (result.stdout, result.stderr, repr(result.exception))
    payload = json.loads(result.stdout)
    assert payload["reason"] == "checks_passed"
    assert payload["test_evidence_id"].startswith("evidence:sha256:")
    assert (repo / "reviewed-ran").read_text(encoding="utf-8") == "yes"
    artifact = json.loads((repo / ".intent/cache/reviewed-test.json").read_bytes())
    assert artifact["test_ids"] == [command_id]
    stored = load_runtime(repo)
    try:
        assert any(record.id == payload["test_evidence_id"] for record in stored.evidence())
    finally:
        stored.close()


def test_check_rejects_unreviewed_or_ambiguous_test_execution_without_running(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path)
    _ready(repo)
    command_id = _configure_reviewed_test(repo)
    invalid_invocations = (
        ["--run-test", "test:sha256:" + "f" * 64],
        ["--ci", "--run-test", command_id],
        ["--run-test", command_id, "--test-results", "external.json"],
    )

    for options in invalid_invocations:
        (repo / "reviewed-ran").unlink(missing_ok=True)
        result = CliRunner().invoke(
            app,
            ["check", "--project", str(repo), *options, "--format", "json"],
        )
        assert result.exit_code == 1
        assert json.loads(result.stdout)["reason"] in {"operation_failed", "test_run_failed"}
        assert not (repo / "reviewed-ran").exists()
