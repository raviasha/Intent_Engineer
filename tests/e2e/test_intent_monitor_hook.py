"""Original-agent hints route only current monitor findings; no findings enter hook text."""

import pytest

from intent_engineering.cli.onboarding_hooks import background_onboarding_route
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.agent_host.advisory import (
    background_prompt_route,
    parse_codex_prompt_event,
)
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService, MonitorRequest
from tests.contract.agent_host.test_advisory_plugin import _official_event
from tests.integration.intent_workflow.test_background_intent import NOW, submission


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    initialize_project(tmp_path)
    (tmp_path / "PRD.md").write_text("Keep all project data local.")
    runtime = load_runtime(tmp_path)
    event = parse_codex_prompt_event(_official_event(tmp_path))
    default = background_prompt_route(event.session_id, event.turn_id)
    service = IntentMonitorService(runtime, clock=lambda: NOW)
    service.run(
        MonitorRequest(
            action="configure",
            prd_paths=("PRD.md",),
            session_id="monitor:one",
            return_session_id=default.arguments["session_id"],
            monitor_thread_id="chat:monitor",
            monitor_host_id="host:local",
            return_thread_id="chat:original",
            return_host_id="host:local",
        )
    )
    cycle = service.run(MonitorRequest(action="begin"))
    review = BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            session_id="monitor:one",
            turn_id=cycle["cycle_id"],
            nodes=[],
            understanding="SECRET FINDING TEXT must never be injected by a hook.",
            evidence_refs=cycle["required_evidence_refs"],
        )
    )
    service.run(
        MonitorRequest(
            action="finish",
            cycle_id=cycle["cycle_id"],
            interpretation_ref=review["evidence_ref"],
            actionable=True,
        )
    )
    yield tmp_path, event, default, service
    runtime.close()


def test_pending_monitor_hint_preserves_background_contract(monitor):
    project, event, default, _ = monitor
    route = background_onboarding_route(project, event, default)
    assert route.action == "background_check"
    assert route.mcp_tool == "intent_background_context"
    assert route.arguments == default.arguments
    assert "monitor-intent" in route.message
    assert "acknowledge" in route.message
    assert "SECRET FINDING TEXT" not in route.message
    assert "host:local" not in route.message


def test_other_session_child_and_stale_packet_get_no_monitor_hint(monitor):
    project, event, default, _ = monitor
    other = event.model_copy(update={"session_id": "other-chat"})
    assert (
        "monitor-intent"
        not in background_onboarding_route(
            project, other, background_prompt_route(other.session_id, other.turn_id)
        ).message
    )
    child = event.model_copy(update={"agent_id": "child-agent"})
    assert background_onboarding_route(project, child, default) == default
    (project / "PRD.md").write_text("Changed during review")
    assert "monitor-intent" not in background_onboarding_route(project, event, default).message


def test_setup_handoff_keeps_priority_over_monitor(monitor, monkeypatch):
    project, event, default, _ = monitor
    monkeypatch.setattr(
        "intent_engineering.cli.onboarding_hooks.onboarding_chat_state",
        lambda _: {
            "status": "complete",
            "assessment_run_id": "assessment:one",
            "return_session_id": default.arguments["session_id"],
            "delivered_update_id": None,
        },
    )
    route = background_onboarding_route(project, event, default)
    assert "result-handoff" in route.message
    assert "monitor-intent" not in route.message


def test_wrong_session_does_not_read_monitor_evidence(monitor, monkeypatch):
    project, event, _, _ = monitor

    def forbidden(*args, **kwargs):
        pytest.fail("another session must not inspect monitor findings")

    monkeypatch.setattr(IntentMonitorService, "run", forbidden)
    other = event.model_copy(update={"session_id": "other-chat"})
    result = background_onboarding_route(
        project, other, background_prompt_route(other.session_id, other.turn_id)
    )
    assert "monitor-intent" not in result.message


def test_acknowledged_handoff_has_no_prompt_hint(monitor):
    project, event, default, service = monitor
    result = service.run(MonitorRequest(action="status"))
    service.run(
        MonitorRequest(
            action="acknowledge",
            cycle_id=result["cycle_id"],
            handoff_id=result["handoff"]["handoff_id"],
            delivery_receipt="host:real-receipt",
        )
    )
    assert "monitor-intent" not in background_onboarding_route(project, event, default).message


def test_pending_monitor_with_fifo_config_fails_closed(monitor):
    import os

    project, event, default, _ = monitor
    config = project / ".intent/config.yaml"
    config.unlink()
    os.mkfifo(config)
    assert IntentMonitorService.resume_hint_for_project(project)["pending_actionable"]
    assert "monitor-intent" not in background_onboarding_route(project, event, default).message
