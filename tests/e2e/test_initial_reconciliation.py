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
    assert started["chat_update"]["kind"] == "progress"
    assert started["chat_update"]["reviewed"] == 0
    assert started["chat_update"]["total"] == 6
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
    assert checkpoint["chat_update"]["reviewed"] == 6
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
    assert finished["chat_update"]["findings"] == finished["findings"]
    assert finished["chat_update"]["kind"] == "complete"
    assert (env.root / "app.py").read_text() == "def store(data):\n    return data\n"
    runner = CliRunner()
    args = ["background-onboarding", "--project", str(env.root)]
    assert runner.invoke(app, [*args, "--prepare"]).exit_code == 0
    assert runner.invoke(app, [*args, "--chat-id", "setup:host"]).exit_code == 0
    done = runner.invoke(app, [*args, "--finished", "--assessment-run-id", started["run_id"]])
    assert done.exit_code == 0, done.output
    assert json.loads(done.output)["reconciliation"]["status"] == "ready_with_findings"
    assert json.loads(done.output)["chat_update"] == finished["chat_update"]


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
    assert checkpoint.structured_content["chat_update"]["kind"] == "progress"
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
    assert (
        "No gaps found within the assessed scope"
        in (finished.structured_content["chat_update"]["message"])
    )
    assert finished.structured_content["test_code_traceability"] == "deferred"


def test_setup_returns_findings_to_original_agent_and_acknowledges_delivery(env, monkeypatch):
    from tests.e2e.test_background_onboarding import route

    monkeypatch.setenv("XDG_STATE_HOME", str(env.root / "state"))
    runner = CliRunner()
    args = ["background-onboarding", "--project", str(env.root)]
    prepared = runner.invoke(
        app,
        [
            *args,
            "--prepare",
            "--return-thread-id",
            "parent-real-id",
            "--return-host-id",
            "host:local",
            "--return-session-id",
            route(env.root).arguments["session_id"],
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    assert runner.invoke(app, [*args, "--chat-id", "setup-real-id"]).exit_code == 0
    started = cli(
        env,
        {
            "action": "start",
            "session_id": "setup",
            "turn_id": "start",
            "scope": "Storage",
            "files": [
                {"path": "prd.md", "role": "requirements"},
                {"path": "app.py", "role": "code"},
            ],
        },
    )
    cli(
        env,
        {
            "action": "checkpoint",
            "session_id": "setup",
            "turn_id": "review",
            "run_id": started["run_id"],
            "comparisons": reviews(started, mismatch=True),
        },
    )
    cli(
        env,
        {
            "action": "finish",
            "session_id": "setup",
            "turn_id": "finish",
            "run_id": started["run_id"],
        },
    )
    done = runner.invoke(app, [*args, "--finished", "--assessment-run-id", started["run_id"]])
    assert done.exit_code == 0, done.output
    handoff = json.loads(done.output)["return_handoff"]
    assert handoff["status"] == "pending"
    assert handoff["thread_id"] == "parent-real-id"
    assert handoff["host_id"] == "host:local"
    assert handoff["assessment"]["findings"][0]["recommendation"] == "Persist data locally."
    assert "result-handoff" in route(env.root).message
    from intent_engineering.integrations.agent_host.advisory import codex_prompt_output

    codex_prompt_output(route(env.root).message)
    from intent_engineering.cli.onboarding_hooks import background_onboarding_route
    from intent_engineering.integrations.agent_host.advisory import (
        background_prompt_route,
        parse_codex_prompt_event,
    )
    from tests.contract.agent_host.test_advisory_plugin import _official_event

    event = parse_codex_prompt_event(_official_event(env.root))
    other = event.model_copy(update={"session_id": "other-chat"})
    assert (
        "result-handoff"
        not in background_onboarding_route(
            env.root, other, background_prompt_route(other.session_id, other.turn_id)
        ).message
    )
    child = event.model_copy(update={"agent_id": "child-agent"})
    assert (
        "result-handoff"
        not in background_onboarding_route(
            env.root, child, background_prompt_route(child.session_id, child.turn_id)
        ).message
    )
    original = (env.root / "app.py").read_text()
    (env.root / "app.py").write_text("changed implementation")
    assert runner.invoke(app, [*args, "--result-handoff"]).exit_code != 0
    assert runner.invoke(app, [*args, "--handoff-delivered", handoff["id"]]).exit_code != 0
    (env.root / "app.py").write_text(original)
    assert runner.invoke(app, [*args, "--handoff-delivered", "wrong-id"]).exit_code != 0
    delivered = runner.invoke(app, [*args, "--handoff-delivered", handoff["id"]])
    assert delivered.exit_code == 0, delivered.output
    repeated = json.loads(runner.invoke(app, [*args, "--result-handoff"]).output)
    assert repeated["return_handoff"]["status"] == "delivered"
    assert "result-handoff" not in route(env.root).message


def test_return_route_cannot_be_rebound_or_fabricated_on_ordinary_status(env, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(env.root / "state"))
    runner = CliRunner()
    args = ["background-onboarding", "--project", str(env.root)]
    assert runner.invoke(app, [*args, "--return-thread-id", "parent"]).exit_code != 0
    assert runner.invoke(app, [*args, "--prepare", "--return-thread-id", "parent"]).exit_code == 0
    assert runner.invoke(app, [*args, "--prepare", "--return-thread-id", "other"]).exit_code != 0


def test_finished_retry_does_not_adopt_a_later_background_run(env, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(env.root / "state"))
    runner = CliRunner()
    args = ["background-onboarding", "--project", str(env.root)]
    runner.invoke(app, [*args, "--prepare"])
    runner.invoke(app, [*args, "--chat-id", "setup"])

    def finish_run(session):
        started = cli(
            env,
            {
                "action": "start",
                "session_id": session,
                "turn_id": "start",
                "scope": session,
                "files": [
                    {"path": "prd.md", "role": "requirements"},
                    {"path": "app.py", "role": "code"},
                ],
            },
        )
        cli(
            env,
            {
                "action": "checkpoint",
                "session_id": session,
                "turn_id": "checkpoint",
                "run_id": started["run_id"],
                "comparisons": reviews(started),
            },
        )
        cli(
            env,
            {
                "action": "finish",
                "session_id": session,
                "turn_id": "finish",
                "run_id": started["run_id"],
            },
        )
        return started["run_id"]

    first = finish_run("setup")
    initial = runner.invoke(app, [*args, "--finished", "--assessment-run-id", first])
    assert initial.exit_code == 0, initial.output
    packet = json.loads(initial.output)["return_handoff"]
    assert runner.invoke(app, [*args, "--handoff-delivered", packet["id"]]).exit_code == 0
    later = finish_run("background-worker")
    assert first != later
    repeated = runner.invoke(app, [*args, "--finished"])
    state = json.loads(runner.invoke(app, args).output)
    assert state["assessment_run_id"] == first
    assert state["delivered_update_id"] == packet["id"]
    if repeated.exit_code == 0:
        assert json.loads(repeated.output)["return_handoff"]["status"] == "delivered"
