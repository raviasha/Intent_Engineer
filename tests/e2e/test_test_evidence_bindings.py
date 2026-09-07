"""Cross-boundary regressions for reviewed execution evidence consumed by the CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest
import structlog
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import CheckRuntimeAdapter, load_runtime
from intent_engineering.control_plane.service import ControlPlaneService
from intent_engineering.core.models import ChangeSet, NodeUpdate
from intent_engineering.intent_workflow.check import CheckService
from intent_engineering.intent_workflow.dev_observer import TestRunStatus
from intent_engineering.storage.secure import SecureDirectory
from tests.e2e.test_intent_dev_automation import _action, _check, _clone
from tests.helpers.shared_state import NOW, git, trust_environment


@pytest.fixture(autouse=True)
def _reset_cli_logging(request: pytest.FixtureRequest) -> None:
    request.addfinalizer(structlog.reset_defaults)


def _local_check(repo: Path, *options: str):
    with patch(
        "intent_engineering.cli.app.CheckService",
        lambda adapter: CheckService(adapter, clock=lambda: NOW),
    ):
        return CliRunner().invoke(app, ["check", "--project", str(repo), *options])


def _restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **options):
    repo, trust = _clone(tmp_path, result_paths=[".intent-ci/test-results.json"], **options)
    monkeypatch.setenv("INTENT_CI_SHARED_STATE_TRUST", trust_environment(trust))
    _action().restore(repo)
    (repo / ".intent-ci").mkdir(exist_ok=True)
    return repo, trust


def _run_local(repo: Path):
    runtime = load_runtime(repo)
    service = ControlPlaneService(runtime, origin="http://localhost:8765", clock=lambda: NOW)
    try:
        command_id = service.observe_development().command_ids[0]
    finally:
        service.close()
        runtime.close()
    return _local_check(repo, "--run-test", command_id)


def test_control_plane_run_is_consumed_by_cli_and_keeps_authentication_identity_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout authentication ID must not make valid service evidence unusable to CI."""
    repo, trust = _restored(tmp_path, monkeypatch)
    runtime = load_runtime(repo)
    service = ControlPlaneService(runtime, origin="http://localhost:8765", clock=lambda: NOW)
    try:
        command_id = service.observe_development().command_ids[0]
        result = anyio.run(service.run_reviewed_tests, command_id)
        assert result.status is TestRunStatus.PASSED
        assert result.artifact is not None
        assert result.artifact.repository_id != service.repository_id
    finally:
        service.close()
        runtime.close()
    first = _check(repo, trust)
    assert first.exit_code == 0, (first.stdout, first.exception)
    evidence = (repo / ".intent/evidence/evidence.jsonl").read_bytes()
    replay = _check(repo, trust)
    assert replay.exit_code == 0, (replay.stdout, replay.exception)
    assert (repo / ".intent/evidence/evidence.jsonl").read_bytes() == evidence


def test_dirty_local_test_cannot_certify_the_failing_committed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing uncommitted code must not produce a HEAD-bound passing artifact."""
    repo, trust = _restored(tmp_path, monkeypatch)
    (repo / "feature.py").write_text("def enabled():\n    return False\n")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-qm", "commit failing behavior")
    (repo / "feature.py").write_text("def enabled():\n    return True\n")
    result = _run_local(repo)
    assert result.exit_code == 1, result.stdout
    assert not (repo / ".intent-ci/test-results.json").exists()
    assert _check(repo, trust).exit_code == 1


@pytest.mark.parametrize("restore_bytes", [False, True])
def test_final_ci_consumer_rejects_code_changed_after_canonical_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_bytes: bool
) -> None:
    """Ingestion must recompute the execution snapshot, even when HEAD is unchanged."""
    repo, trust = _restored(tmp_path, monkeypatch)
    action = _action()
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    source = repo / "feature.py"
    original = source.read_bytes()
    source.write_text("def enabled():\n    return False\n")
    if restore_bytes:
        source.write_bytes(original)
    result = _check(repo, trust)
    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"


@pytest.mark.parametrize("changed", ["commands", "baseline"])
def test_local_consumer_rejects_results_for_another_reviewed_configuration_or_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """A still-passing HEAD cannot reuse tests bound to older approved intent or argv."""
    repo, _trust = _restored(tmp_path, monkeypatch)
    action = _action()
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    if changed == "commands":
        target = repo / ".intent/config.yaml"
        document = yaml.safe_load(target.read_bytes())
        document["test_commands"].append(["tools/test-runner", "fail"])
        target.write_text(yaml.safe_dump(document))
    else:
        runtime = load_runtime(repo)
        try:
            graph = runtime.graph_store.load()
            node = graph.nodes[0].model_copy(update={"label": "Changed approved intent"})
            runtime.graph_store.apply(
                ChangeSet(
                    id="changeset:updated-intent",
                    actor="local:owner",
                    timestamp=NOW,
                    baseline_graph_version=graph.version,
                    evidence_refs=node.evidence_refs,
                    nodes_added=(),
                    nodes_updated=(NodeUpdate(node_id=node.id, replacement=node),),
                    nodes_superseded=(),
                    edges_added=(),
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
    result = _local_check(repo, "--test-results", ".intent-ci/test-results.json")
    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"


def test_one_of_two_reviewed_commands_cannot_satisfy_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consumer independently requires the whole reviewed command set for CI."""
    repo, trust = _restored(
        tmp_path, monkeypatch, commands=[["tools/test-runner"], ["tools/test-runner", "fail"]]
    )
    local = _run_local(repo)
    assert local.exit_code == 0, local.stdout
    result = _check(repo, trust)
    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"


def test_canonical_evidence_preserves_all_execution_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Canonical ingestion must not discard the producer's independently verified bindings."""
    repo, trust = _restored(tmp_path, monkeypatch)
    assert _run_local(repo).exit_code == 0
    artifact = json.loads((repo / ".intent-ci/test-results.json").read_bytes())
    assert artifact["schema_version"] == 2
    assert _check(repo, trust).exit_code == 0
    runtime = load_runtime(repo)
    try:
        evidence = [
            record for record in runtime.evidence() if record.connector_type == "test_result"
        ]
        assert evidence
        for field in ("execution_snapshot", "intent_baseline", "reviewed_commands"):
            assert artifact[field].startswith("sha256:")
            assert evidence[-1].payload[field] == artifact[field]
    finally:
        runtime.close()


def test_final_ci_rechecks_binding_after_assurance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A code mutation during later check stages must prevent a final green result."""
    repo, trust = _restored(tmp_path, monkeypatch)
    action = _action()
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    render = CheckRuntimeAdapter.render_drift

    def mutate_after_render(self, cases):
        report = render(cases)
        (repo / "feature.py").write_text("def enabled():\n    return False\n")
        return report

    monkeypatch.setattr(CheckRuntimeAdapter, "render_drift", mutate_after_render)
    result = _check(repo, trust)
    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"


@pytest.mark.parametrize(
    "missing",
    ["legacy", "schema_version", "execution_snapshot", "intent_baseline", "reviewed_commands"],
)
def test_ci_rejects_legacy_or_incomplete_execution_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Old or stripped result contracts cannot be silently promoted to current evidence."""
    repo, trust = _restored(tmp_path, monkeypatch)
    action = _action()
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    path = repo / ".intent-ci/test-results.json"
    artifact = json.loads(path.read_bytes())
    if missing == "legacy":
        artifact["schema_version"] = 1
        for field in ("execution_snapshot", "intent_baseline", "reviewed_commands"):
            artifact.pop(field, None)
    else:
        artifact.pop(missing, None)
    path.write_text(json.dumps(artifact))
    result = _check(repo, trust)
    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"


def test_final_snapshot_rejects_an_earlier_file_changed_during_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final snapshot must fence all files after its last content read."""
    repo, trust = _restored(tmp_path, monkeypatch)
    action = _action()
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    final_scan = False
    feature_read = False
    attacked = False
    render = CheckRuntimeAdapter.render_drift
    read = SecureDirectory.read_relative

    def arm_final_scan(self, cases):
        nonlocal final_scan
        final_scan = True
        return render(cases)

    def mutate_earlier_file(self, relative, **options):
        nonlocal attacked, feature_read
        result = read(self, relative, **options)
        if final_scan and str(relative) == "feature.py":
            feature_read = True
        if feature_read and not attacked and str(relative) == "tools/test-runner":
            attacked = True
            (repo / "feature.py").write_text("def enabled():\n    return False\n")
        return result

    monkeypatch.setattr(CheckRuntimeAdapter, "render_drift", arm_final_scan)
    monkeypatch.setattr(SecureDirectory, "read_relative", mutate_earlier_file)
    result = _check(repo, trust)
    assert attacked
    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"


@pytest.mark.parametrize("relative", ["config.yaml", "graph.yaml", "history/changesets.jsonl"])
def test_restoring_baseline_bytes_does_not_revalidate_an_earlier_test_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    """Baseline and reviewed config must remain bound to the actual execution interval."""
    repo, trust = _restored(tmp_path, monkeypatch)
    action = _action()
    anyio.run(action.run_tests, repo, NOW)
    action.write_results(repo, NOW)
    target = repo / ".intent" / relative
    original = target.read_bytes()
    target.write_bytes(original + b"\n")
    target.write_bytes(original)
    result = _check(repo, trust)
    assert result.exit_code == 1, result.stdout
    assert json.loads(result.stdout)["reason"] == "test_results_invalid"


def test_local_execution_cannot_temporarily_change_and_restore_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner cannot execute against transient unapproved intent and certify the old baseline."""
    repo, _trust = _restored(tmp_path, monkeypatch)
    runner = repo / "tools/test-runner"
    runner.write_text(
        "#!/usr/bin/python3\nfrom pathlib import Path\n"
        "p = Path('.intent/graph.yaml')\noriginal = p.read_bytes()\n"
        "p.write_bytes(original.replace(b'Approved shared-state baseline', b'Transient intent'))\n"
        "p.write_bytes(original)\n"
    )
    git(repo, "add", "tools/test-runner")
    git(repo, "commit", "-qm", "test runner temporarily changes intent")
    result = _run_local(repo)
    assert result.exit_code == 1, result.stdout
    assert not (repo / ".intent-ci/test-results.json").exists()
