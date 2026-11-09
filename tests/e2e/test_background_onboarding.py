"""First-use setup delegates to a chat without blocking the original work."""

import json

from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.onboarding_hooks import background_onboarding_route
from intent_engineering.integrations.agent_host.advisory import (
    background_prompt_route,
    parse_codex_prompt_event,
)
from intent_engineering.intent_workflow.onboarding_reminders import complete_turn
from tests.contract.agent_host.test_advisory_plugin import _official_event


def route(project, turn="one"):
    event = parse_codex_prompt_event(_official_event(project, turn_id=turn))
    return background_onboarding_route(
        project, event, background_prompt_route(event.session_id, event.turn_id)
    )


def test_missing_graph_offers_sources_and_separate_chat_without_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = route(tmp_path)
    assert result.action == "offer_background_onboarding"
    assert "separate chat" in result.message
    assert (
        "Notion" in result.message and "Confluence" in result.message and "Slack" in result.message
    )
    assert "continue" in result.message.lower()
    assert not (tmp_path / ".intent").exists()


def test_deferred_setup_reminds_after_three_completed_turns(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runner = CliRunner()
    result = runner.invoke(app, ["background-onboarding", "--project", str(tmp_path), "--defer"])
    assert result.exit_code == 0, result.output
    for i in range(3):
        assert route(tmp_path, str(i)).action == "background_check"
        complete_turn(tmp_path, "codex:thread-3", str(i))
        complete_turn(tmp_path, "codex:thread-3", str(i))
    assert route(tmp_path, "next").action == "offer_background_onboarding"
    assert not (tmp_path / ".intent").exists()


def test_separate_chat_handoff_is_durable_and_suppresses_duplicate_offers(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runner = CliRunner()
    args = ["background-onboarding", "--project", str(tmp_path)]
    prepared = runner.invoke(app, [*args, "--prepare", "--source", "notion", "--source", "slack"])
    assert prepared.exit_code == 0, prepared.output
    packet = json.loads(prepared.output)
    assert packet["sources"] == ["notion", "slack"]
    assert str(tmp_path) in packet["prompt"]
    assert "continue" in packet["parent_action"]
    assert packet["chat_created"] is False
    started = runner.invoke(app, [*args, "--chat-id", "chat:onboarding"])
    assert started.exit_code == 0, started.output
    assert route(tmp_path).action == "background_check"
    status = json.loads(runner.invoke(app, args).output)
    assert status["chat_id"] == "chat:onboarding"
    assert status["sources"] == ["notion", "slack"]
    assert not (tmp_path / ".intent").exists()
