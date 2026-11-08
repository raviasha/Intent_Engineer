import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from tests.e2e.test_cli_intent_bootstrap import _configured_project, _propose

INFERRED_MARKER = "<!-- intent-engineering: inferred-prd; author=agent:codex -->\n"


def _run(project: Path, *options: str):
    return CliRunner().invoke(
        app, ["onboard", "--project", str(project), "--format", "json", *options]
    )


def test_discovery_and_deferral_do_not_initialize_a_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/PRD.md").write_text("# Product\nShare local reports.")
    result = _run(tmp_path, "--discover")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["prd_candidates"] == ["docs/PRD.md"]
    result = _run(tmp_path, "--defer")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["remind_after_tasks"] == 3
    assert not (tmp_path / ".intent").exists()


def test_discovery_skips_links_hidden_dependencies_and_ignored_files(tmp_path: Path) -> None:
    for folder in (".hidden", "node_modules", "dist", "docs"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "PRD.md").write_text("# Product")
    (tmp_path / "PRD.md").symlink_to(tmp_path / "docs/PRD.md")
    result = _run(tmp_path, "--discover")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["prd_candidates"] == ["docs/PRD.md"]


def test_discovery_reports_ambiguity_and_missing_prd(tmp_path: Path) -> None:
    result = _run(tmp_path, "--discover")
    assert json.loads(result.stdout)["next_action"] == "draft_prd_from_repository"
    for name in ("PRD.md", "requirements.md"):
        (tmp_path / name).write_text("# Product")
    result = _run(tmp_path, "--discover")
    assert json.loads(result.stdout)["next_action"] == "choose_prd"
    assert not (tmp_path / ".intent").exists()


def test_code_inferred_prd_is_not_declared_human_intent(tmp_path: Path) -> None:
    (tmp_path / "draft.md").write_text(
        INFERRED_MARKER + "# Inferred PRD\nObserved: local reports.\nUnknown: why."
    )
    result = _run(tmp_path, "--prd", "draft.md", "--yes", "--inferred")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["source_role"]["role"] == "proposed_intent"
    assert payload["evidence_refs"]
    runtime = load_runtime(tmp_path)
    assert runtime.evidence()[0].author == "agent:codex"
    assert runtime.graph_store.load().version == 0
    runtime.close()


def test_inferred_prd_survives_regular_markdown_recapture(tmp_path: Path) -> None:
    import anyio

    from intent_engineering.capture.markdown.connector import MarkdownConnector

    (tmp_path / "draft.md").write_text(INFERRED_MARKER + "# Inferred PRD\nObserved: local reports.")
    assert _run(tmp_path, "--prd", "draft.md", "--yes", "--inferred").exit_code == 0
    runtime = load_runtime(tmp_path)

    async def capture():
        connector = MarkdownConnector(runtime.project_directory, runtime.config)
        sources = await connector.discover(None)
        source = next(item for item in sources if item.locator == "draft.md")
        return connector.normalize(
            await connector.fetch(source.external_object_id, source.external_version)
        )

    try:
        record = anyio.run(capture)
        assert record.author == "agent:codex"
        runtime.evidence_store.associate("markdown", record)
        assert len(runtime.evidence()) == 1
    finally:
        runtime.close()


def test_ignored_directory_does_not_hide_real_prd(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("cache/\n")
    (tmp_path / "cache").mkdir()
    for index in range(20):
        (tmp_path / f"cache/old-{index}-prd.md").write_text("# Not a project PRD")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/PRD.md").write_text("# Real product")
    result = _run(tmp_path, "--discover")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["prd_candidates"] == ["docs/PRD.md"]


def test_cli_submission_creates_pending_review_without_activating_graph(tmp_path: Path) -> None:
    project, submission = _configured_project(tmp_path)
    (project / "intent-draft.json").write_text(submission.model_dump_json())
    result = _run(project, "--submission", "intent-draft.json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "review_required"
    runtime = load_runtime(project)
    assert runtime.graph_store.load().version == 0
    assert len(runtime.intent_proposals.list()) == 1
    runtime.close()
    # Repeating the same submission reuses its pending review.
    repeated = _run(project, "--submission", "intent-draft.json")
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.stdout)["proposal"] == json.loads(result.stdout)["proposal"]


def test_invalid_or_linked_submission_does_not_create_proposal(tmp_path: Path) -> None:
    project, submission = _configured_project(tmp_path)
    path = project / "intent-draft.json"
    path.write_text(submission.model_copy(update={"baseline_graph_version": 99}).model_dump_json())
    assert _run(project, "--submission", path.name).exit_code != 0
    link = project / "linked.json"
    link.symlink_to(path)
    assert _run(project, "--submission", link.name).exit_code != 0
    runtime = load_runtime(project)
    assert runtime.intent_proposals.list() == ()
    assert runtime.graph_store.load().version == 0
    runtime.close()


def test_marked_prd_retains_proposed_role_without_inferred_flag(tmp_path: Path) -> None:
    (tmp_path / "draft.md").write_text(INFERRED_MARKER + "# Draft\nObserved behavior.")
    result = _run(tmp_path, "--prd", "draft.md", "--yes")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["source_role"]["role"] == "proposed_intent"


def test_inferred_flag_rejects_unmarked_source_without_overwriting_it(tmp_path: Path) -> None:
    source = tmp_path / "draft.md"
    source.write_text("# Human PRD\nKeep this source intact.")
    before = source.read_bytes()
    result = _run(tmp_path, "--prd", "draft.md", "--yes", "--inferred")
    assert result.exit_code == 1
    assert source.read_bytes() == before
    runtime = load_runtime(tmp_path)
    assert runtime.evidence() == ()
    assert runtime.graph_store.load().version == 0
    runtime.close()


def test_deferral_cannot_suppress_pending_graph_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_STATE_HOME", str(cache))
    project, submission = _configured_project(tmp_path)
    _propose(project, submission)
    result = _run(project, "--defer")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "review_required"
    assert not cache.exists()
