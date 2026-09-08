"""Black-box contracts for the read-only ``intent assess`` command."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.cli import assessment as assessment_cli
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import EvidenceRecord, Graph
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import serialize_graph
from tests.helpers.cli import init_git_repo, run_intent

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "assessment" / "rubric-v1.yaml"


def _durable_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _leave_transaction_journal(project: Path, stage: str) -> None:
    workspace = SecureDirectory.open(project / ".intent")
    paths = {
        "graph": "graph.yaml",
        "history": "history/changesets.jsonl",
        "cases": "reconciliation/cases.jsonl",
        "evidence": "evidence/evidence.jsonl",
        "receipts": "approvals/receipts.jsonl",
        "approvals": "approvals/approvals.jsonl",
        "intent_proposals": "history/intent-proposals.jsonl",
        "webauthn_credentials": "approvals/webauthn-credentials.jsonl",
        "webauthn_challenges": "approvals/webauthn-challenges.jsonl",
    }
    targets = {name: workspace.file(path) for name, path in paths.items()}

    def crash(current: str) -> None:
        if current == stage:
            raise SystemExit

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        targets,
        fault_hook=crash,
    )
    try:
        with pytest.raises(SystemExit), coordinator.transaction() as transaction:
            transaction.write("graph", b"torn: [")
    finally:
        coordinator.close()
        for target in targets.values():
            target.close()
        workspace.close()


@pytest.fixture(autouse=True)
def _reset_cli_logging(request: pytest.FixtureRequest) -> None:
    request.addfinalizer(structlog.reset_defaults)


@pytest.fixture
def initialized_project(tmp_path: Path) -> Iterator[Path]:
    project = init_git_repo(tmp_path)
    assert run_intent(project, "init").returncode == 0
    payload = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    runtime = load_runtime(project)
    try:
        (project / ".intent/graph.yaml").write_bytes(
            serialize_graph(Graph.model_validate(payload["graph"]))
        )
        for item in payload["evidence"]:
            runtime.evidence_store.put(EvidenceRecord.model_validate(item))
    finally:
        runtime.close()
    yield project


def test_assess_json_is_read_only_versioned_and_explainable(initialized_project: Path) -> None:
    """Catches assessment output mutating state or omitting its rubric explanations."""
    before = _durable_bytes(initialized_project)

    result = run_intent(initialized_project, "assess", "--format", "json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == "1"
    assert payload["assessment"]["schema_version"] == 1
    assert payload["assessment"]["nodes"][0]["dimensions"]
    assert payload["semantic_digest"].startswith("sha256:")
    assert _durable_bytes(initialized_project) == before


@pytest.mark.parametrize("output_format", ("text", "markdown"))
def test_assess_human_formats_remain_versioned_and_explainable(
    initialized_project: Path,
    output_format: str,
) -> None:
    """Catches human renderers silently replacing the shared versioned report contract."""
    result = run_intent(initialized_project, "assess", "--format", output_format)

    assert result.returncode == 0
    assert '"version": "1"' in result.stdout
    assert '"assessment"' in result.stdout
    if output_format == "markdown":
        assert result.stdout.startswith("# Intent Engineering\n\n```json\n")


def test_assess_focus_returns_visible_node_and_overlapping_branch_scorecards(
    initialized_project: Path,
) -> None:
    """Catches a root-node/branch ID collision making one visible scorecard unreachable."""
    result = run_intent(
        initialized_project,
        "assess",
        "--focus",
        "intent:export",
        "--format",
        "json",
    )

    assert result.returncode == 0
    focus = json.loads(result.stdout)["focus"]
    assert focus["reference"] == "intent:export"
    assert focus["node"]["node_id"] == "intent:export"
    assert focus["branch"]["branch_id"] == "intent:export"


def test_assess_unknown_or_hidden_focus_fails_fixed_and_stays_read_only(
    initialized_project: Path,
) -> None:
    """Catches focus lookup disclosing whether a reference is hidden or merely absent."""
    before = _durable_bytes(initialized_project)

    result = run_intent(
        initialized_project,
        "assess",
        "--focus",
        "PRIVATE-HIDDEN",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment unavailable\n"
    assert "PRIVATE-HIDDEN" not in result.stderr
    assert _durable_bytes(initialized_project) == before


@pytest.mark.parametrize("stage", ("target:graph", "journal_committed"))
def test_assess_never_recovers_or_removes_an_incomplete_transaction(
    initialized_project: Path,
    stage: str,
) -> None:
    """Catches a nominally read-only assessment restoring targets or deleting a journal."""
    _leave_transaction_journal(initialized_project, stage)
    before = _durable_bytes(initialized_project)

    result = run_intent(initialized_project, "assess", "--format", "json")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment unavailable\n"
    assert _durable_bytes(initialized_project) == before


def test_assessment_gate_cli_parses_exact_reports_and_emits_the_bound_decision(
    initialized_project: Path,
) -> None:
    """Catches CI using a different report parser or gate path from the public implementation."""
    assessed = run_intent(initialized_project, "assess", "--format", "json")
    assert assessed.returncode == 0
    (initialized_project / "base-assessment.json").write_text(assessed.stdout, encoding="utf-8")
    (initialized_project / "head-assessment.json").write_text(assessed.stdout, encoding="utf-8")

    result = run_intent(
        initialized_project,
        "assessment-gate",
        "--base-report",
        "base-assessment.json",
        "--head-report",
        "head-assessment.json",
        "--format",
        "json",
    )

    assert result.returncode == 0
    gate = json.loads(result.stdout)["gate"]
    assert gate["schema_version"] == 1
    assert gate["exit_code"] == 0
    assert gate["base_assessment_digest"] == json.loads(assessed.stdout)["semantic_digest"]
    assert gate["head_assessment_digest"] == json.loads(assessed.stdout)["semantic_digest"]
    assert gate["gate_policy_digest"].startswith("sha256:")


@pytest.mark.parametrize(
    "content",
    (
        b'{"assessment":{}, "semantic_digest":"sha256:bad","version":"1"}\n',
        b'{"assessment":{},"assessment":{},"semantic_digest":"sha256:bad","version":"1"}\n',
        b"{" + b"x" * (32 * 1024 * 1024) + b"}\n",
    ),
    ids=("noncanonical", "duplicate", "oversized"),
)
def test_assessment_gate_cli_rejects_noncanonical_duplicate_or_oversized_reports(
    initialized_project: Path,
    content: bytes,
) -> None:
    """Catches attacker-controlled report bytes bypassing the bounded canonical parser."""
    (initialized_project / "base-assessment.json").write_bytes(content)
    assessed = run_intent(initialized_project, "assess", "--format", "json")
    (initialized_project / "head-assessment.json").write_text(assessed.stdout, encoding="utf-8")

    result = run_intent(
        initialized_project,
        "assessment-gate",
        "--base-report",
        "base-assessment.json",
        "--head-report",
        "head-assessment.json",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment gate unavailable\n"


@pytest.mark.parametrize("mutation", ("array_order", "normalized_map"))
def test_assessment_gate_cli_rejects_model_noncanonical_report_material(
    initialized_project: Path,
    mutation: str,
) -> None:
    """Catches raw JSON that becomes canonical only after assessment-model normalization."""
    assessed = run_intent(initialized_project, "assess", "--format", "json")
    assert assessed.returncode == 0
    base = json.loads(assessed.stdout)
    if mutation == "array_order":
        dimensions = base["assessment"]["nodes"][0]["dimensions"]
        assert len(dimensions) > 1
        dimensions.reverse()
    else:
        weights = base["assessment"]["branches"][0]["contribution_weights"]
        assert weights
        base["assessment"]["branches"][0]["contribution_weights"] = {}
    (initialized_project / "base-assessment.json").write_text(
        json.dumps(base, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (initialized_project / "head-assessment.json").write_text(
        assessed.stdout,
        encoding="utf-8",
    )

    result = run_intent(
        initialized_project,
        "assessment-gate",
        "--base-report",
        "base-assessment.json",
        "--head-report",
        "head-assessment.json",
        "--format",
        "json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment gate unavailable\n"


def test_assess_closes_runtime_on_post_load_failure(
    initialized_project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches descriptor leakage when rendering fails after a runtime is assembled."""
    runtime = load_runtime(initialized_project)
    closed = False
    original_close = runtime.close

    def close() -> None:
        nonlocal closed
        closed = True
        original_close()

    object.__setattr__(runtime, "close", close)
    monkeypatch.setattr(assessment_cli, "load_assessment_runtime", lambda _project: runtime)
    monkeypatch.setattr(
        assessment_cli,
        "_assessment_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("PRIVATE-HIDDEN")),
    )

    result = CliRunner().invoke(
        app,
        ["assess", "--project", str(initialized_project), "--format", "json"],
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: assessment unavailable\n"
    assert "PRIVATE-HIDDEN" not in str(result.exception)
    assert closed is True
