"""Exercise normal background development without an onboarding dashboard."""

import json
import subprocess
import sys
from datetime import UTC, datetime

from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.background_sources import BackgroundSource
from tests.helpers.readiness import put_pending_proposal
from tests.integration.intent_workflow.test_background_intent import submission


def test_missing_graph_context_does_not_offer_setup_or_create_files(tmp_path):
    result = CliRunner().invoke(
        app,
        [
            "background",
            "--project",
            str(tmp_path),
            "--task",
            "Fix spelling",
            "--session-id",
            "chat:one",
        ],
    )
    assert result.exit_code == 0, result.output
    value = json.loads(result.output)
    assert value["graph_version"] == 0
    assert value["questions"] == []
    assert not (tmp_path / ".intent").exists()


def test_cli_bootstraps_and_maintains_without_touching_pending_proposals(tmp_path):
    draft = tmp_path / "submission.json"
    draft.write_text(submission().model_dump_json())
    args = ["background", "--project", str(tmp_path), "--submission", "submission.json"]
    runner = CliRunner()
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["status"] == "maintained"
    runtime = load_runtime(tmp_path)
    pending = put_pending_proposal(runtime)
    runtime.close()
    replay = runner.invoke(app, args)
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output) == json.loads(first.output)
    runtime = load_runtime(tmp_path)
    assert runtime.intent_proposals.decision_for(pending.id) is None
    assert runtime.graph_store.load().version == 1
    runtime.close()


def test_default_prompt_hook_is_lightweight_and_no_global_review_gate(tmp_path):
    event = {
        "session_id": "chat:one",
        "turn_id": "turn:one",
        "cwd": str(tmp_path),
        "transcript_path": None,
        "hook_event_name": "UserPromptSubmit",
        "model": "codex",
        "permission_mode": "default",
        "prompt": "Fix spelling",
    }
    result = subprocess.run(
        [sys.executable, "-m", "intent_engineering.cli.app", "agent-prompt-hook"],
        input=json.dumps(event),
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "intent_background_context" in context
    assert "consequential" in context
    assert "Inbox" not in context
    assert "before implementation" not in context
    assert not (tmp_path / ".intent").exists()


def test_background_submission_schema_is_available_without_workspace(tmp_path):
    result = CliRunner().invoke(
        app, ["background", "--project", str(tmp_path), "--submission-schema"]
    )
    assert result.exit_code == 0, result.output
    schema = json.loads(result.output)
    assert "user_statement" in schema["properties"]
    assert "nodes" in schema["properties"]
    assert not (tmp_path / ".intent").exists()


def test_cli_captures_authorized_source_read_without_claiming_connection(tmp_path):
    runner = CliRunner()
    args = ["background-source", "--project", str(tmp_path)]
    schema = runner.invoke(app, [*args, "--submission-schema"])
    assert schema.exit_code == 0, schema.output
    assert "observed_at" in json.loads(schema.output)["properties"]
    draft = BackgroundSource(
        provider="slack",
        object_id="workspace:channel:message",
        version="1",
        author="slack:author",
        locator="https://slack.com/message",
        observed_at=datetime(2026, 11, 8, tzinfo=UTC),
        content="Private reports",
    )
    (tmp_path / "source.json").write_text(draft.model_dump_json())
    result = runner.invoke(app, [*args, "--submission", "source.json"])
    assert result.exit_code == 0, result.output
    captured = json.loads(result.output)
    assert captured["status"] == "captured"
    assert "connected" not in captured
    runtime = load_runtime(tmp_path)
    try:
        evidence = runtime.evidence_store.get(captured["evidence_ref"])
        assert evidence.author == "slack:author"
        assert evidence.payload["content"] == "Private reports"
        assert runtime.graph_store.load().nodes == ()
    finally:
        runtime.close()
