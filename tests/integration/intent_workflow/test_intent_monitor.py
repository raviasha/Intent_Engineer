"""Durable monitor cycles use offline evidence and actual PRD reads."""

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService, MonitorRequest
from tests.integration.intent_workflow.test_background_intent import NOW, submission


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    initialize_project(tmp_path)
    (tmp_path / "PRD.md").write_text("Keep data local.")
    result = load_runtime(tmp_path)
    yield result
    result.close()


def call(service, action, **kwargs):
    return service.run(MonitorRequest(action=action, **kwargs))


def configured(runtime):
    service = IntentMonitorService(runtime, clock=lambda: NOW)
    result = call(
        service,
        "configure",
        prd_paths=("PRD.md",),
        session_id="monitor:one",
        monitor_thread_id="chat:monitor",
        monitor_host_id="host:one",
        return_thread_id="chat:original",
        return_host_id="host:one",
    )
    assert not result["schedule_confirmed"]
    return service


def finish(runtime, service, cycle, actionable=False):
    ref = BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            session_id="monitor:one",
            turn_id=cycle["cycle_id"],
            nodes=[],
            graph_version=runtime.graph_store.load().version,
            evidence_refs=cycle["required_evidence_refs"],
        )
    )["evidence_ref"]
    return call(
        service, "finish", cycle_id=cycle["cycle_id"], interpretation_ref=ref, actionable=actionable
    )


def test_restart_unchanged_and_prd_only_change(runtime):
    service = configured(runtime)
    first = call(service, "begin")
    assert first["status"] == "needs_interpretation"
    assert (
        call(IntentMonitorService(runtime, clock=lambda: NOW), "begin")["cycle_id"]
        == first["cycle_id"]
    )
    assert finish(runtime, service, first)["handoff"] is None
    assert call(service, "begin")["status"] == "quiet"
    (runtime.root / "PRD.md").write_text("Keep data remote.")
    assert call(service, "begin")["status"] == "needs_interpretation"


def test_stale_prd_redacts_and_acknowledgement_is_idempotent(runtime):
    service = configured(runtime)
    cycle = call(service, "begin")
    result = finish(runtime, service, cycle, actionable=True)
    packet = result["handoff"]
    assert packet["return_thread_id"] == "chat:original"
    ack = {
        "cycle_id": cycle["cycle_id"],
        "handoff_id": packet["handoff_id"],
        "delivery_receipt": "sent:one",
    }
    assert call(service, "acknowledge", **ack)["handoff"] is None
    assert call(service, "acknowledge", **ack)["handoff"] is None
    (runtime.root / "PRD.md").unlink()
    assert call(service, "status")["status"] == "stale"


def test_changed_prd_rejects_finish_and_paths_are_bounded(runtime):
    service = configured(runtime)
    cycle = call(service, "begin")
    (runtime.root / "PRD.md").write_text("Changed")
    with pytest.raises(ValueError, match="stale"):
        finish(runtime, service, cycle)
    with pytest.raises(ValueError):
        MonitorRequest(action="configure", prd_paths=("../secret",))


def test_sources_require_current_checks_and_signals_invalidate_pending_packet(runtime):
    from intent_engineering.intent_workflow.source_refresh import SourceSignal
    from tests.integration.intent_workflow.test_source_refresh import captured, observe, selection

    service = configured(runtime)
    service.refresh.sources(selection())
    cycle = call(service, "begin")
    assert cycle["status"] == "refresh_required"
    cycle = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    assert cycle["status"] == "needs_interpretation"
    assert finish(runtime, service, cycle, actionable=True)["handoff"]
    service.refresh.signal(
        SourceSignal(source_id="source:privacy", event_id="changed:one", observed_at=NOW)
    )
    result = call(service, "status")
    assert result["status"] == "stale"
    assert result["handoff"] is None
    assert result["required_evidence_refs"] == []


def test_missing_prd_has_explicit_evidence_and_symlinks_are_not_read(runtime):
    service = configured(runtime)
    (runtime.root / "PRD.md").unlink()
    (runtime.root / "PRD.md").symlink_to("/etc/passwd")
    cycle = call(service, "begin")
    assert cycle["prds"][0]["status"] == "unreadable"
    assert cycle["prds"][0]["content"] == ""


def test_graph_maintenance_interpretation_can_finish(runtime):
    service = configured(runtime)
    cycle = call(service, "begin")
    review = BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            session_id="monitor:one",
            turn_id=cycle["cycle_id"],
            evidence_refs=cycle["required_evidence_refs"],
        )
    )
    result = call(
        service, "finish", cycle_id=cycle["cycle_id"], interpretation_ref=review["evidence_ref"]
    )
    assert result["status"] == "quiet"
    assert call(service, "begin")["status"] == "quiet"


def test_schedule_receipt_reuses_actual_monitor_and_disallows_replacement(runtime):
    service = configured(runtime)
    assert call(service, "configure", automation_id="automation:actual")["schedule_confirmed"]
    with pytest.raises(ValueError, match="reuse"):
        call(service, "configure", automation_id="automation:duplicate")
    result = call(service, "status")
    assert result["automation_id"] == "automation:actual"


def test_unavailable_source_can_handoff_clarification_without_success_checkpoint(runtime):
    from tests.integration.intent_workflow.test_source_refresh import observe, selection

    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    cycle = call(
        service,
        "begin",
        observations=(
            observe(
                kind="unavailable", complete=False, generation=1, reason="Provider access revoked"
            ),
        ),
    )
    assert cycle["status"] == "refresh_required"
    review = BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            session_id="monitor:one",
            turn_id=cycle["cycle_id"],
            nodes=[],
            classification="needs_clarification",
            question="Restore access to the selected source?",
            evidence_refs=cycle["required_evidence_refs"],
        )
    )
    result = call(
        service, "finish", cycle_id=cycle["cycle_id"], interpretation_ref=review["evidence_ref"]
    )
    assert result["handoff"]["question"] == "Restore access to the selected source?"
    call(
        service,
        "acknowledge",
        cycle_id=cycle["cycle_id"],
        handoff_id=result["handoff"]["handoff_id"],
        delivery_receipt="host:sent",
    )
    assert call(service, "begin")["status"] == "refresh_required"


def test_missing_prd_cannot_finish_as_silent_success(runtime):
    service = configured(runtime)
    (runtime.root / "PRD.md").unlink()
    cycle = call(service, "begin")
    with pytest.raises(ValueError, match="unavailable PRDs"):
        finish(runtime, service, cycle)


@pytest.mark.parametrize(
    "path",
    [
        ".intent/config.yaml",
        ".git/config",
        ".codex/config.toml",
        ".agents/secret",
        ".superpowers/private",
        "a/../PRD.md",
    ],
)
def test_private_paths_rejected(path):
    with pytest.raises(ValueError):
        MonitorRequest(action="configure", prd_paths=(path,))


def test_changed_unfinished_prd_cycle_gets_new_interpretation_identity(runtime):
    service = configured(runtime)
    first = call(service, "begin")
    (runtime.root / "PRD.md").write_text("New requirement")
    second = call(service, "begin")
    assert first["cycle_id"] != second["cycle_id"]
    assert finish(runtime, service, second)["status"] == "quiet"


def test_actor_access_change_redacts_pending_interpretation(runtime):
    import yaml

    service = configured(runtime)
    cycle = call(service, "begin")
    assert finish(runtime, service, cycle, actionable=True)["handoff"]
    path = runtime.root / ".intent/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["local_actor"] = "other"
    path.write_text(yaml.safe_dump(config))
    other = load_runtime(runtime.root)
    try:
        result = call(IntentMonitorService(other, clock=lambda: NOW), "status")
    finally:
        other.close()
    assert result["status"] == "stale"
    assert result["handoff"] is None
    assert result["required_evidence_refs"] == []


def test_code_changes_do_not_trigger_interpretation(runtime):
    service = configured(runtime)
    finish(runtime, service, call(service, "begin"))
    (runtime.root / "implementation.py").write_text("raise Exception('changed code')")
    assert call(service, "begin")["status"] == "quiet"


def test_resume_hint_reads_only_bounded_routing_metadata(runtime, monkeypatch):
    service = configured(runtime)

    def forbidden(*args, **kwargs):
        pytest.fail("resume metadata must not inspect source/PRD/graph evidence")

    monkeypatch.setattr(service.refresh, "refresh", forbidden)
    monkeypatch.setattr(service, "_prds", forbidden)
    monkeypatch.setattr(runtime.graph_store, "load", forbidden)
    result = service.resume_hint()
    assert result["status"] == "configured"
    assert result["monitor_thread_id"] == "chat:monitor"
    assert result["automation_id"] is None
    assert not result["pending_actionable"]


def test_source_change_after_submission_rotates_interpretation_identity(runtime):
    from tests.integration.intent_workflow.test_source_refresh import captured, observe, selection

    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            session_id="monitor:one",
            turn_id=first["cycle_id"],
            nodes=[],
            graph_version=runtime.graph_store.load().version,
            evidence_refs=first["required_evidence_refs"],
        )
    )
    second = call(
        service,
        "begin",
        observations=(
            observe(
                [captured(runtime, version="2", content="New intent")],
                generation=2,
                validator="scope:2",
            ),
        ),
    )
    assert second["cycle_id"] != first["cycle_id"]
    if second["status"] == "refresh_required":
        second = call(
            service,
            "begin",
            observations=(
                observe(
                    [captured(runtime, version="2", content="New intent")],
                    generation=3,
                    validator="scope:2",
                ),
            ),
        )
    assert finish(runtime, service, second)["status"] == "quiet"


def test_pending_delivery_still_requires_new_provider_check_each_cycle(runtime):
    from tests.integration.intent_workflow.test_source_refresh import captured, observe, selection

    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    packet = finish(runtime, service, first, actionable=True)["handoff"]
    next_poll = call(service, "begin")
    assert next_poll["status"] == "refresh_required"
    assert next_poll["source_refresh"]["sources"][0]["action"] == "check_revision"
    assert next_poll["handoff"] is None
    checked = call(
        service,
        "begin",
        observations=(observe(kind="metadata", generation=2, validator="scope:1"),),
    )
    assert checked["status"] == "actionable"
    assert checked["handoff"]["handoff_id"] == packet["handoff_id"]
    assert call(service, "begin")["status"] == "refresh_required"


def test_pending_delivery_discovers_changed_source_without_external_signal(runtime):
    from tests.integration.intent_workflow.test_source_refresh import captured, observe, selection

    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    finish(runtime, service, first, actionable=True)
    call(service, "begin")
    changed = call(
        service,
        "begin",
        observations=(
            observe(
                [captured(runtime, version="2", content="New intent")],
                generation=2,
                validator="scope:2",
            ),
        ),
    )
    assert changed["cycle_id"] != first["cycle_id"]
    assert changed["handoff"] is None


def test_new_receipts_after_submission_require_new_immutable_interpretation(runtime):
    from datetime import timedelta

    from tests.integration.intent_workflow.test_source_refresh import captured, observe, selection

    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            session_id="monitor:one",
            turn_id=first["cycle_id"],
            nodes=[],
            graph_version=runtime.graph_store.load().version,
            evidence_refs=first["required_evidence_refs"],
        )
    )
    later = NOW + timedelta(seconds=1)
    resumed = IntentMonitorService(runtime, clock=lambda: later)
    refreshed = call(
        resumed,
        "begin",
        observations=(
            observe(kind="metadata", generation=2, validator="scope:1", checked_at=later),
        ),
    )
    assert refreshed["cycle_id"] != first["cycle_id"]
    assert refreshed["status"] == "refresh_required"


def test_pending_provider_poll_resumes_without_resetting_its_start(runtime):
    from tests.integration.intent_workflow.test_source_refresh import captured, observe, selection

    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    original = finish(runtime, service, first, actionable=True)
    polling = call(service, "begin")
    restarted = IntentMonitorService(runtime, clock=lambda: NOW)
    again = call(restarted, "begin")
    assert polling["source_refresh"]["task_key"] == again["source_refresh"]["task_key"]
    current = call(restarted, "begin", observations=(observe(kind="metadata", generation=2),))
    assert current["handoff"]["handoff_id"] == original["handoff"]["handoff_id"]
    assert set(current["source_refresh"]["required_evidence_refs"]) <= set(
        current["handoff"]["evidence_refs"]
    )


def test_acknowledged_missing_prd_does_not_advance_success_checkpoint(runtime):
    service = configured(runtime)
    (runtime.root / "PRD.md").unlink()
    first = call(service, "begin")
    result = finish(runtime, service, first, actionable=True)
    call(
        service,
        "acknowledge",
        cycle_id=first["cycle_id"],
        handoff_id=result["handoff"]["handoff_id"],
        delivery_receipt="host:missing-prd",
    )
    second = call(service, "begin")
    assert second["status"] == "needs_interpretation"
    repeated = finish(runtime, service, second, actionable=True)
    assert repeated["handoff"] is None
    assert not repeated["scope_complete"]
    assert call(service, "begin")["status"] == "needs_interpretation"
