"""Public transports and onboarding share durable reconciliation state."""

import json

import pytest
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from tests.integration.intent_workflow.test_initial_reconciliation import (
    env as env,  # noqa: PLC0414 - shared pytest fixture
)
from tests.integration.intent_workflow.test_initial_reconciliation import (
    reviews,
)


def cli(env, payload):
    (env.root / "assessment.json").write_text(json.dumps(payload))
    result = CliRunner().invoke(
        app,
        ["initial-reconciliation", "--project", str(env.root), "--submission", "assessment.json"],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_cli_restarts_resume_and_publish_the_same_assessment(env, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(env.root / "state"))
    payload = {
        "action": "start",
        "session_id": "setup",
        "turn_id": "start",
        "scope": "Production code and PRD",
        "files": [{"path": "prd.md", "role": "requirements"}, {"path": "app.py", "role": "code"}],
    }
    started = cli(env, payload)
    checkpoint = cli(
        env,
        {
            "action": "checkpoint",
            "session_id": "setup",
            "turn_id": "review",
            "run_id": started["run_id"],
            "comparisons": reviews(started, mismatch=True),
        },
    )
    assert checkpoint["notify_user"] is False
    finished = cli(
        env,
        {
            "action": "finish",
            "session_id": "setup",
            "turn_id": "finish",
            "run_id": started["run_id"],
        },
    )
    assert finished["status"] == "ready_with_findings"
    assert len(finished["case_ids"]) == 1
    assert (env.root / "app.py").read_text() == "def store(data):\n    return data\n"
    runner = CliRunner()
    args = ["background-onboarding", "--project", str(env.root)]
    assert runner.invoke(app, [*args, "--prepare"]).exit_code == 0
    assert runner.invoke(app, [*args, "--chat-id", "setup:host"]).exit_code == 0
    done = runner.invoke(app, [*args, "--finished"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.output)["reconciliation"]["status"] == "ready_with_findings"


def test_graph_nodes_alone_do_not_mark_background_onboarding_complete(env, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(env.root / "state"))
    runner = CliRunner()
    args = ["background-onboarding", "--project", str(env.root)]
    runner.invoke(app, [*args, "--prepare"])
    runner.invoke(app, [*args, "--chat-id", "setup:host"])
    result = runner.invoke(app, [*args, "--finished"])
    assert result.exit_code != 0
    assert json.loads(runner.invoke(app, args).output)["status"] == "running"


def test_governed_onboard_reports_pending_assessment_without_mutation(env):
    before = env.graph_store.load()
    result = CliRunner().invoke(
        app, ["onboard", "--project", str(env.root), "--prd", "prd.md", "--yes", "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "ready"  # Baseline activation remains a distinct state.
    assert payload["initial_reconciliation_status"] == "not_started"
    assert payload["next_action"] == "intent_initial_reconciliation"
    assert env.graph_store.load() == before


@pytest.mark.anyio
async def test_mcp_assessment_reads_checkpoint_across_cli_and_server(env):
    started = cli(
        env,
        {
            "action": "start",
            "session_id": "setup",
            "turn_id": "start",
            "scope": "Fixture production code",
            "files": [
                {"path": "app.py", "role": "code"},
                {"path": "prd.md", "role": "requirements"},
            ],
        },
    )
    server = build_server(McpReadServices(env), background=True)
    checkpoint = await server.call_tool(
        "intent_initial_reconciliation",
        {
            "draft": {
                "action": "checkpoint",
                "session_id": "setup",
                "turn_id": "review",
                "run_id": started["run_id"],
                "comparisons": reviews(started),
            }
        },
    )
    assert not checkpoint.is_error
    assert checkpoint.structured_content["completed_comparisons"] == 6
    finished = await server.call_tool(
        "intent_initial_reconciliation",
        {
            "draft": {
                "action": "finish",
                "session_id": "setup",
                "turn_id": "finish",
                "run_id": started["run_id"],
            }
        },
    )
    assert finished.structured_content["status"] == "ready"
    assert finished.structured_content["test_code_traceability"] == "deferred"
