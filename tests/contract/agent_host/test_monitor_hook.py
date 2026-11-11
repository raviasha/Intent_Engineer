"""Session-start hints never execute semantic review or create host schedules."""

import json
import runpy
from pathlib import Path

import pytest

from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService

PLUGIN = Path(__file__).parents[3] / "plugins/intent-advisor"


def hook():
    return runpy.run_path(str(PLUGIN / "scripts/monitor-hook.py"))["context"]


def test_manifest_retires_automatic_code_hooks():
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    assert "reconcile-hook" not in json.dumps(hooks)
    assert "PostToolUse" not in hooks
    session = hooks["SessionStart"][0]
    assert session["matcher"] == "startup|resume"
    command = session["hooks"][0]
    assert "monitor-hook" in command["command"]
    assert command["timeout"] <= 10
    assert command.get("async", False) is False


@pytest.mark.parametrize(
    "patch",
    [
        {"source": "compact"},
        {"agent_id": "child"},
        {"agent_type": "worker"},
        {"hook_event_name": "Stop"},
        {"cwd": "/wrong"},
        {"session_id": ""},
    ],
)
def test_invalid_or_child_events_are_silent(tmp_path, monkeypatch, patch):
    monkeypatch.chdir(tmp_path)
    event = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": "chat",
        "cwd": str(tmp_path),
        **patch,
    }
    assert hook()(event) == {}
    assert list(tmp_path.iterdir()) == []


def test_unconfigured_project_is_silent(tmp_path, monkeypatch):
    initialize_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert (
        hook()(
            {
                "hook_event_name": "SessionStart",
                "source": "resume",
                "session_id": "chat",
                "cwd": str(tmp_path),
            }
        )
        == {}
    )


@pytest.mark.parametrize("automation_id", [None, "automation-actual"])
def test_configured_hint_uses_only_metadata(tmp_path, monkeypatch, automation_id):
    initialize_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        IntentMonitorService,
        "resume_hint_for_project",
        lambda project: {
            "configured": True,
            "automation_id": automation_id,
            "monitor_thread_id": "monitor-chat",
            "monitor_host_id": "host",
        },
    )
    monkeypatch.setattr(
        IntentMonitorService, "run", lambda *_: pytest.fail("hook must not refresh sources")
    )
    result = hook()(
        {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "session_id": "chat",
            "cwd": str(tmp_path),
        }
    )
    specific = result["hookSpecificOutput"]
    assert specific["hookEventName"] == "SessionStart"
    text = specific["additionalContext"]
    assert len(text) <= 2048
    assert "monitor-chat" in text
    assert "hourly" in text
    assert "does not start" in text
    assert "Never review code" in text
    if automation_id:
        assert automation_id in text
        assert "Reuse" in text
    else:
        assert "not confirmed" in text


def test_real_configured_monitor_produces_resume_hint_without_review(tmp_path, monkeypatch):
    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.intent_workflow.intent_monitor import MonitorRequest

    initialize_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    runtime = load_runtime(tmp_path)
    try:
        IntentMonitorService(runtime).run(
            MonitorRequest(
                action="configure",
                session_id="setup",
                monitor_thread_id="actual-monitor",
                monitor_host_id="host",
                return_thread_id="original",
                return_host_id="host",
                automation_id="actual-schedule",
            )
        )
    finally:
        runtime.close()
    monkeypatch.setattr(IntentMonitorService, "run", lambda *_: pytest.fail("no source refresh"))
    result = hook()(
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "session_id": "chat",
            "cwd": str(tmp_path),
        }
    )
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "actual-monitor" in text
    assert "actual-schedule" in text


def test_session_start_records_catch_up_without_provider_reads(tmp_path, monkeypatch):
    initialize_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        IntentMonitorService,
        "resume_hint_for_project",
        lambda _: {
            "configured": True,
            "automation_id": "existing-schedule",
            "monitor_thread_id": "monitor",
            "monitor_host_id": "host",
        },
    )
    monkeypatch.setattr(
        IntentMonitorService,
        "request_catch_up_for_project",
        lambda project: calls.append(project),
        raising=False,
    )
    result = hook()(
        {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "session_id": "parent",
            "cwd": str(tmp_path),
        }
    )
    assert calls == [tmp_path]
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "hourly" in text
    assert "does not start a turn" in text
    assert "existing-schedule" in text
