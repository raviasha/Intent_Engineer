"""Black-box CLI onboarding contracts for existing repositories."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
import structlog
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import (
    Edge,
    Node,
    ProjectConfig,
    RelationType,
    SourceMode,
    SourceRole,
    SourceRoleAssignment,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.bootstrap import BootstrapService, BootstrapSubmission
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.secure import SecureDirectory, UnsafePathError
from tests.e2e.test_cli_connectors import _configured_project as _connector_project
from tests.helpers.cli import run_intent

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "prd.md").write_text(
        "# Product\n\nExport reports as local CSV files.\n",
        encoding="utf-8",
    )
    initialize_project(project)
    return project


def _configured_project(tmp_path: Path) -> tuple[Path, BootstrapSubmission]:
    project = _project(tmp_path)
    config_path = project / ".intent" / "config.yaml"
    config = ProjectConfig.model_validate(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    role = SourceRoleAssignment(
        connector_id="markdown",
        scope="docs/prd.md",
        role=SourceRole.DECLARED_INTENT,
        inherited=False,
    )
    config = config.model_copy(update={"source_roles": (role,)})
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    runtime = load_runtime(project)

    async def capture():
        connector = MarkdownConnector(runtime.project_directory, runtime.config)
        sources = await connector.discover(None)
        selected = next(item for item in sources if item.locator == "docs/prd.md")
        return connector.normalize(
            await connector.fetch(selected.external_object_id, selected.external_version)
        )

    evidence = anyio.run(capture)
    runtime.evidence_store.associate("markdown", evidence)
    nodes = (
        Node(
            id="intent:local-export",
            type="PRODUCT_INTENT",
            label="Keep export local",
            status="proposed",
            created_by="agent:codex",
            created_at=NOW,
            last_modified_by="agent:codex",
            last_modified_at=NOW,
            source_mode=SourceMode.INFERRED,
            intent_fidelity_confidence=0.8,
            confidence_basis="Captured PRD",
            last_reassessed_at=NOW,
            evidence_refs=(evidence.id,),
        ),
        Node(
            id="requirement:csv-export",
            type="REQUIREMENT",
            label="Export CSV",
            status="proposed",
            created_by="agent:codex",
            created_at=NOW,
            last_modified_by="agent:codex",
            last_modified_at=NOW,
            source_mode=SourceMode.INFERRED,
            intent_fidelity_confidence=0.8,
            confidence_basis="Captured PRD",
            last_reassessed_at=NOW,
            evidence_refs=(evidence.id,),
        ),
        Node(
            id="criterion:utf8",
            type="ACCEPTANCE_CRITERION",
            label="Use UTF-8",
            status="proposed",
            created_by="agent:codex",
            created_at=NOW,
            last_modified_by="agent:codex",
            last_modified_at=NOW,
            source_mode=SourceMode.INFERRED,
            intent_fidelity_confidence=0.6,
            confidence_basis="Captured PRD",
            last_reassessed_at=NOW,
            evidence_refs=(evidence.id,),
        ),
    )
    edges = (
        Edge(
            id="edge:intent-requirement",
            **{"from": nodes[0].id, "to": nodes[1].id},
            relation=RelationType.REALIZED_BY,
            status="proposed",
            created_by="agent:codex",
            created_at=NOW,
            last_modified_by="agent:codex",
            last_modified_at=NOW,
        ),
    )
    return project, BootstrapSubmission(
        baseline_graph_version=0,
        actor="agent:codex",
        timestamp=NOW,
        evidence_refs=(evidence.id,),
        source_roles=(role,),
        candidate_nodes=nodes,
        candidate_edges=edges,
        core_node_ids=(nodes[0].id, nodes[1].id),
        provisional_node_ids=(nodes[2].id,),
        assumptions=("Spreadsheet-compatible CSV",),
        unanswered_questions=("How are nested values encoded?",),
    )


def _propose(project: Path, submission: BootstrapSubmission):
    runtime = load_runtime(project)
    service = BootstrapService(
        graph_store=runtime.graph_store,
        evidence_store=runtime.evidence_store,
        proposal_store=runtime.intent_proposals,
        changeset_executor=LocalChangeSetExecutor(
            runtime.graph_store, runtime.case_store, runtime.transactions
        ),
        transactions=runtime.transactions,
        config=runtime.config,
    )
    return service.propose(submission, frozenset({runtime.config.local_actor}))


def _state(project: Path) -> dict[str, bytes]:
    workspace = project / ".intent"

    def optional(relative: str) -> bytes:
        path = workspace / relative
        return path.read_bytes() if path.exists() else b"<absent>"

    return {
        "graph": (workspace / "graph.yaml").read_bytes(),
        "history": optional("history/changesets.jsonl"),
        "proposals": (workspace / "history" / "intent-proposals.jsonl").read_bytes(),
        "evidence": optional("evidence/evidence.jsonl"),
        "checkpoints": optional("cache/checkpoints.yaml"),
        "config": (workspace / "config.yaml").read_bytes(),
    }


def test_cli_bootstrap_captures_prd_but_requires_agent_submission(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = _state(project)

    first = run_intent(
        project,
        "bootstrap",
        "--prd",
        "docs/prd.md",
        "--format",
        "json",
    )
    after_first = _state(project)
    second = run_intent(
        project,
        "bootstrap",
        "--prd",
        "docs/prd.md",
        "--format",
        "json",
    )
    evidence_after_second = _state(project)["evidence"]
    prd = project / "docs" / "prd.md"
    metadata = prd.stat()
    os.utime(
        prd,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000_000),
    )
    touched = run_intent(
        project,
        "bootstrap",
        "--prd",
        "docs/prd.md",
        "--format",
        "json",
    )

    assert first.returncode == second.returncode == touched.returncode == 4
    assert first.stderr == second.stderr == touched.stderr == ""
    payload = first.json()
    assert payload["status"] == "agent_submission_required"
    assert payload["graph_version"] == 0
    assert payload["evidence_refs"]
    assert payload["context_packet"] == {
        "connector_id": "markdown",
        "repository_id": "project",
        "scope": "docs/prd.md",
        "source_role": "declared_intent",
    }
    assert "candidate" not in repr(payload).lower()
    assert "proposal" not in repr(payload).lower()
    runtime = load_runtime(project)
    assert len(runtime.evidence()) == 1
    assert runtime.evidence()[0].source_locator == "docs/prd.md"
    assert runtime.graph_store.load().version == 0
    assert runtime.intent_proposals.list() == ()
    assert after_first["graph"] == before["graph"]
    assert after_first["history"] == before["history"]
    assert after_first["proposals"] == before["proposals"]
    assert after_first["checkpoints"] == before["checkpoints"]
    assert _state(project) == after_first
    assert second.json() == first.json()
    assert touched.json() == first.json()
    assert _state(project)["evidence"] == evidence_after_second


def test_sources_add_is_atomic_canonical_and_idempotent(tmp_path: Path) -> None:
    project = _project(tmp_path)

    first = run_intent(
        project,
        "sources",
        "add",
        "markdown",
        "docs",
        "--role",
        "operating_context",
        "--inherited",
        "--format",
        "json",
    )
    second = run_intent(
        project,
        "sources",
        "add",
        "markdown",
        "docs/prd.md",
        "--role",
        "declared_intent",
        "--format",
        "json",
    )
    config_after = (project / ".intent" / "config.yaml").read_bytes()
    replay = run_intent(
        project,
        "sources",
        "add",
        "markdown",
        "docs/prd.md",
        "--role",
        "declared_intent",
        "--format",
        "json",
    )

    assert first.returncode == second.returncode == replay.returncode == 0
    assert replay.json()["status"] == "unchanged"
    assert (project / ".intent" / "config.yaml").read_bytes() == config_after
    config = ProjectConfig.model_validate_json(
        json.dumps(yaml.safe_load(config_after.decode("utf-8")))
    )
    assert [(item.scope, item.role.value, item.inherited) for item in config.source_roles] == [
        ("docs", "operating_context", True),
        ("docs/prd.md", "declared_intent", False),
    ]
    assert config.project_id == "project"
    assert config.local_actor == "local"
    assert config.source_exclusions == (".intent/**", ".git/**")


def test_bootstrap_rejects_unsafe_or_non_markdown_paths_without_mutation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("PRIVATE-OUTSIDE-SOURCE", encoding="utf-8")
    (project / "docs" / "linked.md").symlink_to(outside)
    before = _state(project)

    for candidate in (str(outside), "../outside.md", "docs/linked.md", "docs/prd.txt"):
        result = run_intent(project, "bootstrap", "--prd", candidate, "--format", "json")
        assert result.returncode == 1
        assert result.stdout == ""
        assert result.stderr == "intent error: intent onboarding failed\n"
        assert "PRIVATE" not in result.stderr
        assert _state(project) == before


def test_bootstrap_special_files_and_oversize_input_fail_promptly_without_mutation(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    docs = project / "docs"
    os.link(docs / "prd.md", docs / "hard.md")
    os.mkfifo(docs / "pipe.md")
    unix_socket = socket.socket(socket.AF_UNIX)
    short_socket = Path("/tmp") / f"intent-task4-{os.getpid()}.sock"
    short_socket.unlink(missing_ok=True)
    unix_socket.bind(str(short_socket))
    os.replace(short_socket, docs / "socket.md")
    (docs / "large.md").write_bytes(b"x" * 1_048_577)
    before = _state(project)
    executable = Path(sys.executable).with_name("intent")
    try:
        for relative in ("docs/hard.md", "docs/pipe.md", "docs/socket.md", "docs/large.md"):
            completed = subprocess.run(
                [
                    str(executable),
                    "bootstrap",
                    "--prd",
                    relative,
                    "--format",
                    "json",
                ],
                cwd=project,
                env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                check=False,
                capture_output=True,
                timeout=2,
            )
            assert completed.returncode == 1
            assert completed.stdout == b""
            assert completed.stderr == b"intent error: intent onboarding failed\n"
            assert _state(project) == before
    finally:
        unix_socket.close()


def test_bootstrap_rejects_a_swapped_final_path_without_partial_capture(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    prd = project / "docs" / "prd.md"
    held = project / "docs" / "held.md"
    outside = tmp_path / "replacement.md"
    outside.write_text("PRIVATE-SWAPPED-SOURCE", encoding="utf-8")
    before = _state(project)

    import intent_engineering.cli.intent_workflow as workflow_cli
    from intent_engineering.storage.secure import SecureDirectory

    original_read = SecureDirectory.read_relative
    swapped = False

    def swap_then_read(self, relative, **kwargs):
        nonlocal swapped
        if not swapped and self.path == project and str(relative) == "docs/prd.md":
            os.replace(prd, held)
            prd.symlink_to(outside)
            swapped = True
        return original_read(self, relative, **kwargs)

    monkeypatch.setattr(SecureDirectory, "read_relative", swap_then_read)
    ok, payload, abort = workflow_cli._bootstrap_result(project, "docs/prd.md")

    assert (ok, payload, abort) == (False, None, None)
    assert swapped is True
    assert _state(project) == before


def test_bootstrap_reauthenticates_the_path_after_normalization_before_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    prd = project / "docs" / "prd.md"
    held = project / "docs" / "held.md"
    replacement = tmp_path / "replacement.md"
    replacement.write_text("PRIVATE-POST-READ-SWAP", encoding="utf-8")
    before = _state(project)
    original_normalize = MarkdownConnector.normalize
    swapped = False

    def normalize_then_swap(self, raw):
        nonlocal swapped
        record = original_normalize(self, raw)
        os.replace(prd, held)
        prd.symlink_to(replacement)
        swapped = True
        return record

    monkeypatch.setattr(MarkdownConnector, "normalize", normalize_then_swap)

    import intent_engineering.cli.intent_workflow as workflow_cli

    ok, payload, abort = workflow_cli._bootstrap_result(project, "docs/prd.md")

    assert (ok, payload, abort) == (False, None, None)
    assert swapped is True
    assert _state(project) == before


def test_descriptor_read_is_bounded_when_a_regular_file_keeps_growing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    growing = project / "docs" / "growing.md"
    limit = 1_048_576
    growing.write_bytes(b"x" * limit)
    target_identity = (growing.stat().st_dev, growing.stat().st_ino)
    directory = SecureDirectory.open(project)

    import intent_engineering.storage.secure as secure_storage

    original_read = secure_storage.os.read
    requested = 0

    def grow_before_read(descriptor: int, count: int) -> bytes:
        nonlocal requested
        metadata = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == target_identity:
            with growing.open("ab", buffering=0) as stream:
                stream.write(b"y" * 65_536)
            requested += count
        return original_read(descriptor, count)

    monkeypatch.setattr(secure_storage.os, "read", grow_before_read)
    try:
        with pytest.raises(UnsafePathError):
            directory.read_relative(
                "docs/growing.md",
                nonblocking=True,
                max_bytes=limit,
            )
    finally:
        directory.close()

    assert 0 < requested <= limit + 1


def test_sources_add_rejects_concurrent_config_replacement_without_rewrite(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    config_path = project / ".intent" / "config.yaml"
    replacement = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    replacement["auto_apply_metadata"] = False
    replacement_bytes = yaml.safe_dump(replacement, sort_keys=True).encode("utf-8")

    import intent_engineering.cli.intent_workflow as workflow_cli

    original_snapshot = workflow_cli._snapshot_config

    def snapshot_then_replace(runtime):
        result = original_snapshot(runtime)
        config_path.write_bytes(replacement_bytes)
        return result

    monkeypatch.setattr(workflow_cli, "_snapshot_config", snapshot_then_replace)
    ok, payload, abort = workflow_cli._source_role_result(
        project,
        "markdown",
        "docs/prd.md",
        SourceRole.DECLARED_INTENT,
        False,
    )

    assert (ok, payload, abort) == (False, None, None)
    assert config_path.read_bytes() == replacement_bytes


def test_sources_unchanged_replay_rejects_concurrent_config_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    configured = run_intent(
        project,
        "sources",
        "add",
        "markdown",
        "docs/prd.md",
        "--role",
        "declared_intent",
        "--format",
        "json",
    )
    assert configured.returncode == 0
    config_path = project / ".intent" / "config.yaml"
    replacement = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    replacement["auto_apply_metadata"] = False
    replacement_bytes = yaml.safe_dump(replacement, sort_keys=True).encode("utf-8")

    import intent_engineering.cli.intent_workflow as workflow_cli

    original_snapshot = workflow_cli._snapshot_config

    def snapshot_then_replace(runtime):
        result = original_snapshot(runtime)
        config_path.write_bytes(replacement_bytes)
        return result

    monkeypatch.setattr(workflow_cli, "_snapshot_config", snapshot_then_replace)
    ok, payload, abort = workflow_cli._source_role_result(
        project,
        "markdown",
        "docs/prd.md",
        SourceRole.DECLARED_INTENT,
        False,
    )

    assert (ok, payload, abort) == (False, None, None)
    assert config_path.read_bytes() == replacement_bytes


def test_sources_support_catalog_uri_and_configured_github_scope_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog_project = _connector_project(tmp_path)
    inspection = run_intent(
        catalog_project,
        "connectors",
        "inspect",
        "slack-local",
        "--format",
        "json",
    )
    assert inspection.returncode == 0
    connector_id = inspection.json()["source_role_connector_ids"]["message"]
    assert isinstance(connector_id, str)
    slack = run_intent(
        catalog_project,
        "sources",
        "add",
        connector_id,
        "https://example.slack.com/archives/C111/p1700000001000100",
        "--role",
        "proposed_intent",
        "--format",
        "json",
    )
    assert slack.returncode == 0

    github_parent = tmp_path / "github"
    github_parent.mkdir()
    github_project = _project(github_parent)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/demo")

    import intent_engineering.cli.intent_workflow as workflow_cli

    ok, payload, abort = workflow_cli._source_role_result(
        github_project,
        "github:acme/demo",
        "https://github.com/acme/demo/issues/42",
        SourceRole.PROPOSED_INTENT,
        False,
    )
    assert abort is None
    assert ok is True
    assert payload is not None

    stable = (github_project / ".intent" / "config.yaml").read_bytes()
    for connector_id, scope in (
        ("github:acme/other", "https://github.com/acme/other/issues/42"),
        ("github:Acme/demo", "https://github.com/acme/demo/issues/42"),
        ("github:acme/demo", "https://user@github.com/acme/demo/issues/42"),
        ("github:acme/demo", "https://github.com/acme/other/issues/42"),
    ):
        result = workflow_cli._source_role_result(
            github_project,
            connector_id,
            scope,
            SourceRole.PROPOSED_INTENT,
            False,
        )
        assert result == (False, None, None)
        assert (github_project / ".intent" / "config.yaml").read_bytes() == stable


@dataclass
class _Terminal:
    interactive: bool
    answer: str
    events: list[tuple[str, object]] = field(default_factory=list)

    def is_interactive(self) -> bool:
        return self.interactive

    def display_preview(self, preview: dict[str, object]) -> None:
        self.events.append(("preview", preview))

    def read_confirmation(self, proposal_digest: str) -> str:
        self.events.append(("confirmation", proposal_digest))
        return self.answer


def test_proposal_list_show_and_confirmation_use_acl_safe_full_preview(
    tmp_path: Path, monkeypatch, request
) -> None:
    request.addfinalizer(structlog.reset_defaults)
    project, submission = _configured_project(tmp_path)
    review = _propose(project, submission)
    listed = run_intent(project, "proposals", "list", "--format", "json")
    shown = run_intent(
        project,
        "proposals",
        "show",
        review.proposal_id,
        "--format",
        "json",
    )
    before = _state(project)

    import intent_engineering.cli.intent_workflow as workflow_cli

    rejected = _Terminal(False, "")
    monkeypatch.setattr(workflow_cli, "terminal", lambda: rejected)
    runner = CliRunner()
    noninteractive = runner.invoke(
        app,
        ["proposals", "confirm", review.proposal_id, "--project", str(project)],
    )
    assert noninteractive.exit_code == 4
    assert _state(project) == before

    accepted = _Terminal(True, f"confirm {review.proposal_digest}")
    monkeypatch.setattr(workflow_cli, "terminal", lambda: accepted)
    confirmed = runner.invoke(
        app,
        ["proposals", "confirm", review.proposal_id, "--project", str(project)],
    )

    assert listed.returncode == shown.returncode == confirmed.exit_code == 0
    assert listed.json()["proposals"][0]["proposal_id"] == review.proposal_id
    shown_payload = shown.json()["proposal"]
    assert shown_payload["candidate_changeset"] == review.candidate_changeset.model_dump(
        mode="json"
    )
    assert shown_payload["core_node_ids"] == [node.id for node in review.core_nodes]
    assert shown_payload["provisional_node_ids"] == [node.id for node in review.provisional_nodes]
    assert [event for event, _ in accepted.events] == ["preview", "confirmation"]
    preview = accepted.events[0][1]
    assert isinstance(preview, dict)
    assert preview["proposal_digest"] == review.proposal_digest
    assert preview["candidate_changeset"] == review.candidate_changeset.model_dump(mode="json")
    assert preview["assumptions"] == list(review.assumptions)
    assert preview["unanswered_questions"] == list(review.unanswered_questions)
    assert load_runtime(project).graph_store.load().version == 1
