"""Black-box contracts for the guided ``intent onboard`` state machine."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import ProjectConfig, SourceRole, SourceRoleAssignment
from tests.e2e.test_cli_intent_bootstrap import _configured_project, _project, _propose, _state


def _durable_bytes(project: Path) -> dict[str, bytes]:
    """Return every durable onboarding surface without opening source files."""
    return _state(project)


def test_onboard_requires_confirmation_before_capture(tmp_path: Path) -> None:
    """Catches a command that captures a PRD before explicit source consent."""
    project = _project(tmp_path)
    prd = project / "docs" / "prd.md"
    prd.unlink()
    os.mkfifo(prd)
    before = _durable_bytes(project)

    result = CliRunner().invoke(
        app,
        ["onboard", "--project", str(project), "--prd", "docs/prd.md"],
    )

    assert result.exit_code == 1
    assert "Start guided onboarding now?" in result.output
    assert _durable_bytes(project) == before


def test_onboard_yes_captures_prd_and_declares_its_source_role(tmp_path: Path) -> None:
    """Catches a capture that omits the declared-intent source role or agent next action."""
    project = _project(tmp_path)

    result = CliRunner().invoke(
        app,
        ["onboard", "--project", str(project), "--prd", "docs/prd.md", "--yes", "--format", "json"],
    )

    assert result.exit_code == 0, repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["state"] == "proposal_required"
    assert payload["source_role"] == {
        "connector_id": "markdown",
        "inherited": False,
        "role": "declared_intent",
        "scope": "docs/prd.md",
    }
    assert payload["next_action"] == "intent_bootstrap_propose"
    assert payload["authorization_issued"] is False
    assert len(load_runtime(project).evidence()) == 1


def test_onboard_existing_proposal_displays_review_without_recapture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a review replay that recaptures sources or treats --yes as graph approval."""
    project, submission = _configured_project(tmp_path)
    review = _propose(project, submission)
    before = _durable_bytes(project)

    import intent_engineering.cli.intent_workflow as workflow_cli

    monkeypatch.setattr(
        workflow_cli,
        "terminal",
        lambda: (_ for _ in ()).throw(AssertionError("onboard must not activate a proposal")),
    )
    result = CliRunner().invoke(
        app,
        ["onboard", "--project", str(project), "--prd", "docs/prd.md", "--yes", "--format", "json"],
    )

    assert result.exit_code == 0, repr(result.exception)
    payload = json.loads(result.stdout)
    assert payload["state"] == "review_required"
    assert payload["proposal"]["proposal_id"] == review.proposal_id
    assert payload["next_action"] == "intent_proposals_confirm"
    assert payload["authorization_issued"] is False
    assert _durable_bytes(project) == before


def test_onboard_ready_repository_is_a_semantic_noop(tmp_path: Path) -> None:
    """Catches a ready-state replay that reads or changes the supplied PRD."""
    project, submission = _configured_project(tmp_path)
    review = _propose(project, submission)

    import intent_engineering.cli.intent_workflow as workflow_cli
    from tests.e2e.test_cli_intent_bootstrap import _Terminal

    ok, payload, abort = workflow_cli._confirmation_result(
        project,
        review.proposal_id,
        _Terminal(True, f"confirm {review.proposal_digest}"),
    )
    assert (ok, abort) == (True, None)
    assert payload is not None
    before = _durable_bytes(project)

    result = CliRunner().invoke(
        app,
        ["onboard", "--project", str(project), "--prd", "docs/prd.md", "--yes", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["state"] == "ready"
    assert _durable_bytes(project) == before


def test_onboard_rejects_unsafe_special_and_oversize_prds_without_mutation(tmp_path: Path) -> None:
    """Catches unsafe PRD input reaching capture after source consent."""
    project = _project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("PRIVATE-OUTSIDE-SOURCE", encoding="utf-8")
    docs = project / "docs"
    (docs / "linked.md").symlink_to(outside)
    os.mkfifo(docs / "pipe.md")
    (docs / "large.md").write_bytes(b"x" * 1_048_577)
    before = _durable_bytes(project)

    for candidate in (
        str(outside),
        "../outside.md",
        "docs/linked.md",
        "docs/pipe.md",
        "docs/large.md",
    ):
        result = CliRunner().invoke(
            app,
            ["onboard", "--project", str(project), "--prd", candidate, "--yes", "--format", "json"],
        )
        assert result.exit_code == 1
        assert result.stdout == ""
        assert result.stderr == "intent error: intent onboarding failed\n"
        assert "PRIVATE" not in result.stderr
        assert _durable_bytes(project) == before


def test_onboard_replaces_a_prior_role_for_the_prd(tmp_path: Path) -> None:
    """Catches source-role composition that appends a conflicting role instead of replacing it."""
    project = _project(tmp_path)
    config_path = project / ".intent" / "config.yaml"
    config = ProjectConfig.model_validate(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    prior = SourceRoleAssignment(
        connector_id="markdown",
        scope="docs/prd.md",
        role=SourceRole.OPERATING_CONTEXT,
        inherited=False,
    )
    config_path.write_text(
        yaml.safe_dump(
            config.model_copy(update={"source_roles": (prior,)}).model_dump(mode="json"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["onboard", "--project", str(project), "--prd", "docs/prd.md", "--yes", "--format", "json"],
    )

    assert result.exit_code == 0
    updated = load_runtime(project).config
    assert len(updated.source_roles) == 1
    assert updated.source_roles[0].role is SourceRole.DECLARED_INTENT


def test_onboard_preserves_cancellation_identity_and_scrubs_its_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches conversion of cancellation into a public fixed failure."""
    project = _project(tmp_path)
    cancellation = asyncio.CancelledError("private-onboard-cancellation")

    import intent_engineering.cli.intent_workflow as workflow_cli

    monkeypatch.setattr(
        workflow_cli,
        "_bootstrap_result",
        lambda *_args: (False, None, cancellation),
    )
    ok, payload, abort = workflow_cli._onboard_result(project, "docs/prd.md", True)

    assert (ok, payload, abort) == (False, None, cancellation)
    assert cancellation.__traceback__ is None
