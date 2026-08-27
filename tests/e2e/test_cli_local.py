"""Subprocess coverage for the local-first CLI contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.helpers.cli import init_git_repo, run_intent


def test_local_quick_start(tmp_path: Path) -> None:
    """Catch a missing command registration or broken local runtime assembly."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    assert (repo / ".intent/config.yaml").exists()
    assert (repo / ".intent/graph.yaml").exists()
    assert run_intent(repo, "validate", "--format", "json").json()["valid"] is True
    assert run_intent(repo, "sync", "--sources", "markdown,git").returncode == 0
    assert run_intent(repo, "validate", "--format", "json").json()["valid"] is True
    status = run_intent(repo, "status", "--format", "json").json()
    assert status["project_id"] == repo.name
    assert run_intent(repo, "context", "--task", "local export").returncode == 0


def test_init_preserves_existing_files_without_force_and_scopes_force(tmp_path: Path) -> None:
    """Catch accidental workspace overwrites or force writes outside .intent."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    graph_path = repo / ".intent/graph.yaml"
    graph_path.write_text("not a graph\n", encoding="utf-8")
    outside = repo / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    assert run_intent(repo, "init").returncode == 1
    assert graph_path.read_text(encoding="utf-8") == "not a graph\n"
    assert run_intent(repo, "init", "--force").returncode == 0
    assert outside.read_text(encoding="utf-8") == "keep"
    assert run_intent(repo, "validate", "--format", "json").json()["valid"] is True


def test_complete_init_is_idempotent_and_byte_preserving(tmp_path: Path) -> None:
    """Catch a second init that treats a valid workspace as an overwrite conflict."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    before = {
        path: path.read_bytes()
        for path in (repo / ".intent").rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }
    assert run_intent(repo, "init").returncode == 0
    after = {
        path: path.read_bytes()
        for path in (repo / ".intent").rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }
    assert after == before


def test_init_refuses_a_symlinked_workspace_without_touching_the_target(tmp_path: Path) -> None:
    """Catch path-based init following .intent outside the selected project."""
    repo = init_git_repo(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    (repo / ".intent").symlink_to(target, target_is_directory=True)
    assert run_intent(repo, "init", "--force").returncode == 1
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (target / "config.yaml").exists()


def test_invalid_source_selection_is_usage_error_without_writes(tmp_path: Path) -> None:
    """Catch invalid connector names being executed as partial runtime syncs."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    before = (repo / ".intent/graph.yaml").read_bytes()
    result = run_intent(repo, "sync", "--sources", "markdown,markdown", "--format", "json")
    assert result.returncode == 2
    assert result.stdout == ""
    assert (repo / ".intent/graph.yaml").read_bytes() == before


def test_invalid_sources_are_usage_errors_before_project_loading(tmp_path: Path) -> None:
    """Catch source parsing after an uninitialized or corrupt runtime is opened."""
    repo = init_git_repo(tmp_path)
    result = run_intent(repo, "sync", "--sources", "missing")
    assert result.returncode == 2
    assert not (repo / ".intent").exists()


def test_uninitialized_project_is_a_redacted_runtime_failure(tmp_path: Path) -> None:
    """Catch commands that bypass the typed local-project boundary."""
    repo = init_git_repo(tmp_path)
    result = run_intent(repo, "status", "--format", "json")
    assert result.returncode == 1
    assert "not initialized" in result.stderr.lower()
    assert str(repo) not in result.stderr


def test_corrupt_graph_is_a_redacted_runtime_failure(tmp_path: Path) -> None:
    """Catch command-level graph loads that leak parser errors or local paths."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    (repo / ".intent/graph.yaml").write_text("not: [valid", encoding="utf-8")
    result = run_intent(repo, "status", "--format", "json")
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "intent error: local operation failed\n"


def test_status_recovers_pending_resolution_journal_before_reading_state(tmp_path: Path) -> None:
    """A non-resolution command must replay an interrupted transaction first."""
    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.storage.transaction import LocalTransactionCoordinator

    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    runtime = load_runtime(repo)
    paths = runtime.resolution._paths()
    snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
    graph = repo / ".intent/graph.yaml"

    def crash(stage: str) -> None:
        if stage == "target:graph":
            raise SystemExit()

    coordinator = LocalTransactionCoordinator(
        runtime.workspace_directory.file("history/.local-transaction.json"),
        {
            "graph": runtime.workspace_directory.file("graph.yaml"),
            "history": runtime.workspace_directory.file("history/changesets.jsonl"),
            "cases": runtime.workspace_directory.file("reconciliation/cases.jsonl"),
        },
        fault_hook=crash,
    )
    with pytest.raises(SystemExit), coordinator.transaction() as transaction:
        transaction.write("graph", b"not: [graph")

    result = run_intent(repo, "status", "--format", "json")

    assert result.returncode == 0
    assert graph.read_bytes() == snapshots[graph]
    assert not coordinator.journal_path.exists()


def test_sync_and_write_share_one_complete_crash_recovery_domain(tmp_path: Path) -> None:
    """A recovered write cannot later clobber a newer synchronized graph."""
    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.core.models import EvidenceRecord
    from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
    from intent_engineering.storage.jsonl.receipt_store import JsonlReceiptStore
    from intent_engineering.storage.transaction import LocalTransactionCoordinator
    from intent_engineering.storage.yaml.graph_store import serialize_graph

    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    runtime = load_runtime(repo)
    workspace = runtime.workspace_directory
    targets = {
        "graph": workspace.file("graph.yaml"),
        "history": workspace.file("history/changesets.jsonl"),
        "cases": workspace.file("reconciliation/cases.jsonl"),
        "evidence": workspace.file("evidence/evidence.jsonl"),
        "receipts": workspace.file("approvals/receipts.jsonl"),
        "intent_proposals": workspace.file("history/intent-proposals.jsonl"),
    }

    def crash(stage: str) -> None:
        if stage == "target:graph":
            raise SystemExit()

    interrupted = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        targets,
        fault_hook=crash,
    )
    with pytest.raises(SystemExit), interrupted.transaction() as transaction:
        transaction.write("graph", b"not: [graph")

    evidence = EvidenceRecord(
        id="evidence:after-interrupted-write",
        connector_type="fixture",
        external_object_id="after-interrupted-write",
        external_version="1",
        author="local:test",
        observed_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        source_locator="fixture://after-interrupted-write",
        content_hash="sha256:" + "1" * 64,
        payload={"kind": "fixture"},
        acl=(),
    )
    assert runtime.evidence_store.put(evidence)
    receipts = JsonlReceiptStore(targets["receipts"], transactions=runtime.transactions)
    assert receipts.claim(
        "write-plan:sha256:" + "2" * 64,
        "approval:sha256:" + "3" * 64,
        "local:test",
        datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )
    assert runtime.transactions.target_names == frozenset(targets)
    with pytest.raises(ValueError):
        JsonlEvidenceStore(
            workspace.file("evidence/wrong-evidence.jsonl"),
            transactions=runtime.transactions,
        )
    with pytest.raises(ValueError):
        JsonlReceiptStore(
            workspace.file("approvals/wrong-receipts.jsonl"),
            transactions=runtime.transactions,
        )
    graph = runtime.graph_store.load()
    with runtime.transactions.transaction() as transaction:
        transaction.write("graph", serialize_graph(graph.model_copy(update={"version": 1})))

    recovered = load_runtime(repo)
    assert recovered.graph_store.load().version == 1
    assert recovered.evidence_store.get(evidence.id) == evidence
    recovered_receipts = JsonlReceiptStore(
        recovered.workspace_directory.file("approvals/receipts.jsonl"),
        transactions=recovered.transactions,
    )
    assert recovered_receipts.is_claimed(
        "write-plan:sha256:" + "2" * 64,
        "approval:sha256:" + "3" * 64,
    )


def test_doctor_reports_redacted_structured_diagnostics_for_corrupt_evidence(
    tmp_path: Path,
) -> None:
    """Validate and doctor share one redacted deep-snapshot diagnostic contract."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    evidence = repo / ".intent/evidence/evidence.jsonl"
    evidence.write_text("not-json\n", encoding="utf-8")
    diagnostic = {
        "code": "evidence.invalid",
        "schema_version": "1",
        "scope": "evidence",
        "severity": "error",
    }

    validated = run_intent(repo, "validate", "--format", "json")
    doctored = run_intent(repo, "doctor", "--format", "json")

    assert validated.returncode == 1 and doctored.returncode == 1
    assert validated.stderr == doctored.stderr == ""
    assert validated.json() == {
        "diagnostics": [diagnostic],
        "graph_id": f"graph:{repo.name}",
        "graph_version": 0,
        "schema_version": "1",
        "valid": False,
        "version": "1",
    }
    assert doctored.json() == {
        "diagnostics": [diagnostic],
        "healthy": False,
        "schema_version": "1",
        "version": "1",
    }


def test_doctor_rejects_symlinked_state_without_reading_its_target(tmp_path: Path) -> None:
    """State files are canonical workspace entries, never indirections."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    target = tmp_path / "external-evidence.jsonl"
    target.write_text("", encoding="utf-8")
    evidence = repo / ".intent/evidence/evidence.jsonl"
    evidence.symlink_to(target)

    result = run_intent(repo, "doctor", "--format", "json")

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.json() == {
        "diagnostics": [
            {
                "code": "evidence.unsafe",
                "schema_version": "1",
                "scope": "evidence",
                "severity": "error",
            }
        ],
        "healthy": False,
        "schema_version": "1",
        "version": "1",
    }


def test_validate_reports_corrupt_transaction_without_path_or_content_leaks(
    tmp_path: Path,
) -> None:
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    secret = "private-preimage-content"
    journal = repo / ".intent/history/.local-transaction.json"
    journal.write_text('{"invalid":"' + secret + '"}', encoding="utf-8")

    result = run_intent(repo, "validate", "--format", "json")

    assert result.returncode == 1
    assert result.stderr == ""
    assert result.json()["diagnostics"] == [
        {
            "code": "transaction.corrupt",
            "schema_version": "1",
            "scope": "transaction",
            "severity": "error",
        }
    ]
    assert secret not in result.stdout
    assert str(repo) not in result.stdout


def test_doctor_holds_original_directory_after_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-open replacement cannot redirect doctor into an external directory."""
    from intent_engineering.storage.secure import SecureDirectory
    from intent_engineering.validation import validate_project

    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    original = SecureDirectory.file
    outside = tmp_path / "outside"
    outside.mkdir()
    swapped = False

    def swap(
        directory: SecureDirectory,
        relative: str | Path,
        *,
        create_parents: bool = False,
    ) -> object:
        nonlocal swapped
        secure_file = original(directory, relative, create_parents=create_parents)
        if str(relative) == "evidence/evidence.jsonl" and not swapped:
            swapped = True
            evidence = repo / ".intent/evidence"
            evidence.rename(repo / ".intent/evidence-held")
            evidence.symlink_to(outside, target_is_directory=True)
        return secure_file

    monkeypatch.setattr(SecureDirectory, "file", swap)
    assert validate_project(repo).valid is True
    assert not list(outside.glob(".*.lock"))


@pytest.mark.parametrize(
    ("args", "marker"),
    [
        (("init", "--help"), "Usage:"),
        (("validate", "--help"), "Usage:"),
        (("ingest", "--help"), "Usage:"),
        (("sync", "--help"), "Usage:"),
        (("drift", "--help"), "Usage:"),
        (("status", "--help"), "Usage:"),
        (("explain", "--help"), "Usage:"),
        (("context", "--help"), "Usage:"),
        (("reconcile", "--help"), "Usage:"),
        (("reconcile", "list", "--help"), "Usage:"),
        (("reconcile", "show", "--help"), "Usage:"),
        (("reconcile", "resolve", "--help"), "Usage:"),
        (("render", "--help"), "Usage:"),
        (("doctor", "--help"), "Usage:"),
    ],
)
def test_documented_command_help_is_available(
    tmp_path: Path, args: tuple[str, ...], marker: str
) -> None:
    """Catch undocumented command groups or broken Typer registration."""
    result = run_intent(init_git_repo(tmp_path), *args)
    assert result.returncode == 0
    assert marker in result.stdout


def test_structured_commands_emit_parseable_versioned_json(tmp_path: Path) -> None:
    """Catch log noise or unversioned payloads on the machine-readable boundary."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    for args in (
        ("validate", "--format", "json"),
        ("status", "--format", "json"),
        ("drift", "--format", "json"),
        ("context", "--task", "export", "--format", "json"),
        ("reconcile", "list", "--format", "json"),
        ("doctor", "--format", "json"),
    ):
        result = run_intent(repo, *args)
        assert result.returncode == 0
        payload = result.json()
        assert payload["version"] == "1"


def test_sync_partial_and_review_exit_codes_are_distinct(tmp_path: Path) -> None:
    """Catch exit-code collapse between recoverable sync and human-review outcomes."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    (repo / ".git").rename(repo / ".git-hidden")
    assert run_intent(repo, "sync", "--sources", "markdown,git").returncode == 3
    assert run_intent(repo, "drift", "--require-review").returncode == 4


def test_markdown_sync_produces_a_deterministic_reconciliation_case(tmp_path: Path) -> None:
    """Catch a runtime that persists evidence but never wires Task 5 case detection."""
    import subprocess

    repo = init_git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src/export.py").write_text(
        "def export() -> str:\n    return 'old'\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/export.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "implement export"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "drift.md").write_text(
        """---
intent_engineering:
  intent_assertion:
    id: assertion:export
    subject_id: requirement:export
    change_kind: initialize
    node_type: REQUIREMENT
    label: Local export
    source_mode: explicit
    evidence_refs: ["$self"]
    confidence: 0.9
  detection_input:
    schema_version: 1
    subject_ref: requirement:export
    affected_refs: [requirement:export]
    compatibility: aligns
    requirement_version: 2
    implementation_version: 1
    requirement:
      label: requirement
      claim: local export
      evidence_refs: ["$self"]
      confidence: 0.9
    implementation:
      label: implementation
      claim: old export
      evidence_refs: ["git-path:src/export.py"]
      confidence: 0.9
---
# Drift fixture
""",
        encoding="utf-8",
    )
    assert run_intent(repo, "init").returncode == 0
    assert run_intent(repo, "sync", "--sources", "markdown,git").returncode == 0
    assert run_intent(repo, "validate", "--format", "json").json()["valid"] is True
    cases = run_intent(repo, "reconcile", "list", "--format", "json").json()["cases"]
    assert len(cases) == 1
    assert cases[0]["case_type"] == "CODE_LAG"
    assert cases[0]["evidence_sides"][0]["evidence_refs"][0].startswith("evidence:sha256:")
    preview = run_intent(repo, "reconcile", "resolve", cases[0]["id"], "--format", "json")
    assert preview.returncode == 4
    preview_payload = preview.json()
    assert preview_payload["case"]["status"] == "needs_human"
    graph_before = (repo / ".intent/graph.yaml").read_bytes()
    cases_before = (repo / ".intent/reconciliation/cases.jsonl").read_bytes()
    for terminal_action in ("defer", "mark_false_positive"):
        history_before = (
            (repo / ".intent/history/changesets.jsonl").read_bytes()
            if (repo / ".intent/history/changesets.jsonl").exists()
            else None
        )
        terminal = run_intent(
            repo,
            "reconcile",
            "resolve",
            cases[0]["id"],
            "--action",
            terminal_action,
            "--approve",
            preview_payload["approval"],
            "--format",
            "json",
        )
        assert terminal.returncode == 1
        assert (repo / ".intent/graph.yaml").read_bytes() == graph_before
        assert (repo / ".intent/reconciliation/cases.jsonl").read_bytes() == cases_before
        history_path = repo / ".intent/history/changesets.jsonl"
        assert (history_path.read_bytes() if history_path.exists() else None) == history_before
    refused = run_intent(
        repo, "reconcile", "resolve", cases[0]["id"], "--approve", "wrong", "--format", "json"
    )
    assert refused.returncode == 1
    assert (repo / ".intent/graph.yaml").read_bytes() == graph_before
    resolved = run_intent(
        repo,
        "reconcile",
        "resolve",
        cases[0]["id"],
        "--approve",
        preview_payload["approval"],
        "--format",
        "json",
    )
    assert resolved.returncode == 0
    assert resolved.json()["case"]["status"] == "resolved"
    assert run_intent(repo, "validate", "--format", "json").json()["valid"] is True
    history = (repo / ".intent/history/changesets.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(history[-1]) == preview_payload["changeset"]


def test_acl_protected_evidence_is_indistinguishable_from_unknown(tmp_path: Path) -> None:
    """Catch local CLI read paths that disclose ACL-protected evidence or its count."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    assert run_intent(repo, "ingest").returncode == 0
    evidence_path = repo / ".intent/evidence/evidence.jsonl"
    envelope = json.loads(evidence_path.read_text(encoding="utf-8"))
    record = envelope["evidence"]
    record["acl"] = ["other"]
    evidence_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
    result = run_intent(repo, "explain", record["id"], "--format", "json")
    assert result.returncode == 1
    assert result.stderr == "intent error: local reference was not found\n"
    assert run_intent(repo, "status", "--format", "json").json()["evidence_count"] == 0


def test_acl_filtered_status_and_render_exclude_protected_topology(tmp_path: Path) -> None:
    """Unreadable evidence removes its nodes, connecting edges, labels, IDs, and case refs."""
    from datetime import UTC, datetime

    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.core.models import (
        Edge,
        EvidenceRecord,
        EvidenceSide,
        Graph,
        Node,
        ReconciliationCase,
    )

    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    runtime = load_runtime(repo)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    public = EvidenceRecord(
        id="evidence:public",
        connector_type="fixture",
        external_object_id="public",
        external_version="1",
        author="fixture",
        observed_at=now,
        source_locator="public.md",
        content_hash="sha256:public",
        payload={},
    )
    protected = EvidenceRecord(
        id="evidence:secret",
        connector_type="fixture",
        external_object_id="secret",
        external_version="1",
        author="fixture",
        observed_at=now,
        source_locator="secret.md",
        content_hash="sha256:secret",
        payload={},
    )
    runtime.evidence_store.put(public)
    runtime.evidence_store.put(protected)
    runtime.graph_store.initialize(
        Graph(
            id="graph:acl",
            version=0,
            nodes=(
                Node(
                    id="node:public",
                    type="REQUIREMENT",
                    label="Public label",
                    status="active",
                    created_by="fixture",
                    created_at=now,
                    last_modified_by="fixture",
                    last_modified_at=now,
                    source_mode="explicit",
                    evidence_refs=(public.id,),
                ),
                Node(
                    id="node:secret",
                    type="REQUIREMENT",
                    label="SECRET LABEL",
                    status="active",
                    created_by="fixture",
                    created_at=now,
                    last_modified_by="fixture",
                    last_modified_at=now,
                    source_mode="explicit",
                    evidence_refs=(protected.id,),
                ),
            ),
            edges=(
                Edge(
                    id="edge:secret",
                    **{"from": "node:public", "to": "node:secret"},
                    relation="VERIFIED_BY",
                    status="active",
                    created_by="fixture",
                    created_at=now,
                    last_modified_by="fixture",
                    last_modified_at=now,
                ),
            ),
        )
    )
    side = EvidenceSide(
        label="secret",
        claim="SECRET CLAIM",
        evidence_refs=(protected.id,),
        observed_at=now,
        authors=("fixture",),
        confidence=0.9,
    )
    runtime.case_store.put(
        ReconciliationCase(
            id="case:secret",
            subject_ref="node:secret",
            case_type="CODE_LAG",
            affected_refs=("node:secret",),
            evidence_sides=(side,),
            detector_id="fixture",
            fingerprint="a" * 64,
            created_at=now,
            created_by="detector:fixture",
        )
    )
    evidence_path = repo / ".intent/evidence/evidence.jsonl"
    evidence_path.write_text(
        public.model_dump_json()
        + "\n"
        + protected.model_copy(update={"acl": ("other",)}).model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    status = run_intent(repo, "status", "--format", "json")
    output = repo / "rendered"
    rendered = run_intent(repo, "render", "--output", str(output), "--format", "json")
    views = (output / "graph.md").read_text(encoding="utf-8") + (output / "graph.mmd").read_text(
        encoding="utf-8"
    )

    assert status.returncode == 0 and rendered.returncode == 0
    assert status.json()["node_count"] == 1
    assert status.json()["edge_count"] == 0
    assert status.json()["open_case_count"] == 0
    assert "SECRET" not in status.stdout + rendered.stdout + views
    assert "node:secret" not in status.stdout + rendered.stdout + views
    assert "edge:secret" not in status.stdout + rendered.stdout + views
    assert "case:secret" not in status.stdout + rendered.stdout + views
    assert "node:public" in views and "Public label" in views


def test_reconcile_resolve_records_a_changeset_before_case_transition(tmp_path: Path) -> None:
    """Catch direct YAML edits that resolve a case without a graph ChangeSet audit."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    assert run_intent(repo, "ingest").returncode == 0
    evidence_id = json.loads(
        (repo / ".intent/evidence/evidence.jsonl").read_text(encoding="utf-8")
    )["evidence"]["id"]
    case_file = repo / ".intent/reconciliation/cases.jsonl"
    case_file.write_text(
        """{\"affected_refs\":[],\"alternatives\":[],\"case_type\":\"CODE_LAG\",\"created_at\":\"2026-08-25T00:00:00Z\",\"detector_id\":\"test\",\"evidence_sides\":[{\"authors\":[\"tester\"],\"claim\":\"old\",\"confidence\":0.9,\"current\":true,\"evidence_refs\":[\"ev-1\"],\"label\":\"requirement\",\"observed_at\":\"2026-08-25T00:00:00Z\",\"source_mode\":\"explicit\"}],\"fingerprint\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"history\":[{\"actor\":\"tester\",\"at\":\"2026-08-25T00:00:00Z\",\"new\":\"proposed\",\"prior\":\"open\"},{\"actor\":\"tester\",\"at\":\"2026-08-25T00:00:00Z\",\"new\":\"needs_human\",\"prior\":\"proposed\"}],\"id\":\"case:test\",\"impact\":\"\",\"requires_human\":true,\"resolution\":null,\"resolved_by_changeset\":null,\"status\":\"needs_human\",\"subject_ref\":\"subject:test\"}\n""".replace(
            "ev-1", evidence_id
        ),
        encoding="utf-8",
    )
    graph_before = (repo / ".intent/graph.yaml").read_bytes()
    result = run_intent(repo, "reconcile", "resolve", "case:test", "--format", "json")
    assert result.returncode == 1
    assert (repo / ".intent/graph.yaml").read_bytes() == graph_before


def test_reconcile_resolve_refuses_missing_case_evidence_without_graph_mutation(
    tmp_path: Path,
) -> None:
    """Catch resolution that applies a graph audit record before evidence prevalidation."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    case_file = repo / ".intent/reconciliation/cases.jsonl"
    case_file.write_text(
        """{\"affected_refs\":[],\"alternatives\":[],\"case_type\":\"CODE_LAG\",\"created_at\":\"2026-08-25T00:00:00Z\",\"detector_id\":\"test\",\"evidence_sides\":[{\"authors\":[\"tester\"],\"claim\":\"old\",\"confidence\":0.9,\"current\":true,\"evidence_refs\":[\"missing\"],\"label\":\"requirement\",\"observed_at\":\"2026-08-25T00:00:00Z\",\"source_mode\":\"explicit\"}],\"fingerprint\":\"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\",\"history\":[{\"actor\":\"tester\",\"at\":\"2026-08-25T00:00:00Z\",\"new\":\"proposed\",\"prior\":\"open\"},{\"actor\":\"tester\",\"at\":\"2026-08-25T00:00:00Z\",\"new\":\"needs_human\",\"prior\":\"proposed\"}],\"id\":\"case:missing\",\"impact\":\"\",\"requires_human\":true,\"resolution\":null,\"resolved_by_changeset\":null,\"status\":\"needs_human\",\"subject_ref\":\"subject:test\"}\n""",
        encoding="utf-8",
    )
    graph_before = (repo / ".intent/graph.yaml").read_bytes()
    result = run_intent(repo, "reconcile", "resolve", "case:missing", "--format", "json")
    assert result.returncode == 1
    assert (repo / ".intent/graph.yaml").read_bytes() == graph_before
    assert not (repo / ".intent/history/changesets.jsonl").exists()
