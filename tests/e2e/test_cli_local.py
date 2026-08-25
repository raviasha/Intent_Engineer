"""Subprocess coverage for the local-first CLI contract."""

from __future__ import annotations

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
    assert run_intent(repo, "sync", "--sources", "markdown,missing").returncode == 3
    assert run_intent(repo, "drift", "--require-review").returncode == 4


def test_reconcile_resolve_records_a_changeset_before_case_transition(tmp_path: Path) -> None:
    """Catch direct YAML edits that resolve a case without a graph ChangeSet audit."""
    repo = init_git_repo(tmp_path)
    assert run_intent(repo, "init").returncode == 0
    case_file = repo / ".intent/reconciliation/cases.jsonl"
    case_file.write_text(
        """{\"affected_refs\":[],\"alternatives\":[],\"case_type\":\"CODE_LAG\",\"created_at\":\"2026-08-25T00:00:00Z\",\"detector_id\":\"test\",\"evidence_sides\":[{\"authors\":[\"tester\"],\"claim\":\"old\",\"confidence\":0.9,\"current\":true,\"evidence_refs\":[\"ev-1\"],\"label\":\"requirement\",\"observed_at\":\"2026-08-25T00:00:00Z\",\"source_mode\":\"explicit\"}],\"fingerprint\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\",\"history\":[{\"actor\":\"tester\",\"at\":\"2026-08-25T00:00:00Z\",\"new\":\"proposed\",\"prior\":\"open\"},{\"actor\":\"tester\",\"at\":\"2026-08-25T00:00:00Z\",\"new\":\"needs_human\",\"prior\":\"proposed\"}],\"id\":\"case:test\",\"impact\":\"\",\"requires_human\":true,\"resolution\":null,\"resolved_by_changeset\":null,\"status\":\"needs_human\",\"subject_ref\":\"subject:test\"}\n""",
        encoding="utf-8",
    )
    graph_before = (repo / ".intent/graph.yaml").read_text(encoding="utf-8")
    result = run_intent(repo, "reconcile", "resolve", "case:test", "--format", "json")
    assert result.returncode == 0
    payload = result.json()
    assert payload["case"]["resolved_by_changeset"].startswith("changeset:")
    assert (repo / ".intent/graph.yaml").read_text(encoding="utf-8") != graph_before
    assert "case:test" in (repo / ".intent/history/changesets.jsonl").read_text(encoding="utf-8")
