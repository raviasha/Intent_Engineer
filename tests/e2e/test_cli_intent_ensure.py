"""End-to-end contracts for the machine-facing ``intent ensure`` command."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import intent_engineering.cli.runtime as runtime_module
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import Graph, Node, NodeType
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.transaction import LocalTransactionCoordinator

NOW = datetime(2026, 9, 7, tzinfo=UTC)


def _ready_project(project: Path) -> None:
    initialize_project(project)
    runtime = load_runtime(project)
    try:
        runtime.graph_store.initialize(
            Graph(
                id="graph:ensure-cli",
                version=1,
                nodes=(
                    Node(
                        id="intent:ensure-cli",
                        type=NodeType.PRODUCT_INTENT,
                        label="Developer readiness starts from an approved baseline",
                        status="active",
                        created_by="local:owner",
                        created_at=NOW,
                        last_modified_by="local:owner",
                        last_modified_at=NOW,
                    ),
                ),
                edges=(),
            )
        )
    finally:
        runtime.close()


def test_ensure_routes_an_uninitialized_repository_to_onboarding_without_writes(
    tmp_path: Path,
) -> None:
    """Catches the automatic developer entry point creating an empty graph or failing closed."""
    project = tmp_path / "uninitialized-project"
    project.mkdir()

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "version": "1",
        "schema_version": 1,
        "status": "onboarding_required",
        "attention_route": "onboarding",
        "graph_version": 0,
        "pending_proposal_ids": [],
        "open_case_ids": [],
    }
    assert not (project / ".intent").exists()


def test_ensure_reports_a_ready_initialized_repository(tmp_path: Path) -> None:
    """Catches the CLI failing to expose an aligned baseline to the developer preset."""
    project = tmp_path / "ready-project"
    project.mkdir()
    _ready_project(project)

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "version": "1",
        "schema_version": 1,
        "status": "ready",
        "attention_route": "home",
        "graph_version": 1,
        "pending_proposal_ids": [],
        "open_case_ids": [],
    }


def test_ensure_reports_invalid_existing_state_without_leaking_details(tmp_path: Path) -> None:
    """Catches malformed local state being mistaken for a new repository or exposing its data."""
    project = tmp_path / "invalid-project"
    project.mkdir()
    (project / ".intent").mkdir()

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "version": "1",
        "schema_version": 1,
        "status": "shared_state_invalid",
        "attention_route": "team_state",
        "graph_version": 0,
        "pending_proposal_ids": [],
        "open_case_ids": [],
    }
    assert str(project) not in result.stdout + result.stderr


def test_ensure_redacts_malformed_configuration_without_leaking_details(tmp_path: Path) -> None:
    """Catches YAML parser errors escaping the machine-facing readiness contract."""
    project = tmp_path / "broken-config-project"
    project.mkdir()
    initialize_project(project)
    marker = "PRIVATE-INVALID-CONFIG-8197"
    (project / ".intent" / "config.yaml").write_text(f"local_actor: [{marker}", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert marker not in result.stdout + result.stderr


def test_ensure_rejects_pending_transaction_without_recovery_writes(tmp_path: Path) -> None:
    """Catches a supposedly read-only readiness call replaying a torn transaction."""
    project = tmp_path / "prepared-transaction-project"
    project.mkdir()
    _ready_project(project)
    runtime = load_runtime(project)

    def crash(stage: str) -> None:
        if stage == "target:graph":
            raise SystemExit()

    coordinator = LocalTransactionCoordinator(
        runtime.workspace_directory.file("history/.local-transaction.json"),
        {
            "graph": runtime.workspace_directory.file("graph.yaml"),
            "history": runtime.workspace_directory.file("history/changesets.jsonl"),
            "cases": runtime.workspace_directory.file("reconciliation/cases.jsonl"),
            "evidence": runtime.workspace_directory.file("evidence/evidence.jsonl"),
            "receipts": runtime.workspace_directory.file("approvals/receipts.jsonl"),
            "approvals": runtime.workspace_directory.file("approvals/approvals.jsonl"),
            "intent_proposals": runtime.workspace_directory.file("history/intent-proposals.jsonl"),
            "webauthn_credentials": runtime.workspace_directory.file(
                "approvals/webauthn-credentials.jsonl"
            ),
            "webauthn_challenges": runtime.workspace_directory.file(
                "approvals/webauthn-challenges.jsonl"
            ),
        },
        fault_hook=crash,
        legacy_target_sets=(
            frozenset({"graph", "history", "cases"}),
            frozenset({"graph", "history", "cases", "evidence", "receipts"}),
            frozenset(
                {
                    "graph",
                    "history",
                    "cases",
                    "evidence",
                    "receipts",
                    "intent_proposals",
                }
            ),
            frozenset(
                {
                    "graph",
                    "history",
                    "cases",
                    "evidence",
                    "receipts",
                    "intent_proposals",
                    "webauthn_credentials",
                    "webauthn_challenges",
                }
            ),
        ),
    )
    try:
        with pytest.raises(SystemExit), coordinator.transaction() as transaction:
            transaction.write("graph", b"not: [a graph")
        before = {
            str(path.relative_to(project / ".intent")): path.read_bytes()
            for path in sorted((project / ".intent").rglob("*"))
            if path.is_file() and not path.is_symlink()
        }
    finally:
        coordinator.close()
        runtime.close()

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert {
        str(path.relative_to(project / ".intent")): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    } == before


def test_ensure_retries_when_the_readiness_snapshot_changes_mid_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a mixed cross-store snapshot being treated as a trustworthy ready result."""
    project = tmp_path / "changing-project"
    project.mkdir()
    _ready_project(project)
    original = runtime_module._capture_readiness_snapshot
    marker = "PRIVATE-CHANGED-GRAPH-8197"
    calls = 0

    def changed_snapshot(workspace):
        nonlocal calls
        captured = original(workspace)
        calls += 1
        if calls == 1:
            (project / ".intent" / "graph.yaml").write_text(f"graph: [{marker}", encoding="utf-8")
        return captured

    monkeypatch.setattr(runtime_module, "_capture_readiness_snapshot", changed_snapshot)

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert calls == 4
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert marker not in result.stdout + result.stderr


def test_ensure_redacts_graph_parse_failure_from_machine_output(tmp_path: Path) -> None:
    """Catches later graph reads leaking malformed state after runtime assembly succeeds."""
    project = tmp_path / "broken-graph-project"
    project.mkdir()
    initialize_project(project)
    marker = "PRIVATE-INVALID-GRAPH-8197"
    (project / ".intent" / "graph.yaml").write_text(f"graph: [{marker}", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["ensure", "--project", str(project), "--preset", "developer", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "shared_state_invalid"
    assert marker not in result.stdout + result.stderr
