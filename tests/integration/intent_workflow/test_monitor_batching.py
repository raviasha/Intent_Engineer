"""Hourly monitor ownership and reopen freshness survive host restarts."""

from datetime import timedelta

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService
from tests.integration.intent_workflow.test_background_intent import NOW, submission
from tests.integration.intent_workflow.test_intent_monitor import call, configured
from tests.integration.intent_workflow.test_source_refresh import captured, observe, selection


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    initialize_project(tmp_path)
    (tmp_path / "PRD.md").write_text("Keep data local.")
    result = load_runtime(tmp_path)
    yield result
    result.close()


def review(runtime, cycle):
    return BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            session_id="monitor:one",
            turn_id=cycle["cycle_id"],
            nodes=[],
            graph_version=runtime.graph_store.load().version,
            evidence_refs=cycle["required_evidence_refs"],
        )
    )["evidence_ref"]


def test_distinct_or_omitted_worker_cannot_read_or_interpret_live_batch(runtime, monkeypatch):
    service = configured(runtime)
    owner = call(service, "begin", worker_id="run:one")

    def forbidden(*args, **kwargs):
        pytest.fail("a competing worker must stop before reading monitor inputs")

    monkeypatch.setattr(service, "_refresh", forbidden)
    monkeypatch.setattr(service, "_prds", forbidden)
    for worker in ("run:two", None):
        for action in ("begin", "status", "acknowledge") if worker else ("begin",):
            result = call(service, action, worker_id=worker)
            assert result["status"] == "busy"
            assert result["cycle_id"] == owner["cycle_id"]
            assert result["required_evidence_refs"] == []
            assert result["handoff"] is None


def test_owner_restart_renews_lease_and_takeover_rejects_old_finish(runtime):
    service = configured(runtime)
    cycle = call(service, "begin", worker_id="run:one")
    ref = review(runtime, cycle)
    later = IntentMonitorService(runtime, clock=lambda: NOW + timedelta(minutes=14))
    resumed = call(later, "begin", worker_id="run:one")
    assert resumed["cycle_id"] == cycle["cycle_id"]
    assert resumed["lease_expires_at"] == (NOW + timedelta(minutes=29)).isoformat()
    competitor = IntentMonitorService(runtime, clock=lambda: NOW + timedelta(minutes=28))
    assert call(competitor, "begin", worker_id="run:two")["status"] == "busy"
    takeover = IntentMonitorService(runtime, clock=lambda: NOW + timedelta(minutes=30))
    fresh = call(takeover, "begin", worker_id="run:two")
    assert fresh["cycle_id"] == cycle["cycle_id"]
    with pytest.raises(ValueError, match="owner"):
        call(
            takeover,
            "finish",
            worker_id="run:one",
            cycle_id=cycle["cycle_id"],
            interpretation_ref=ref,
        )
    assert (
        call(
            takeover,
            "finish",
            worker_id="run:two",
            cycle_id=cycle["cycle_id"],
            interpretation_ref=ref,
        )["status"]
        == "quiet"
    )


def test_expired_worker_cannot_finish_without_reacquiring(runtime):
    service = configured(runtime)
    cycle = call(service, "begin", worker_id="run:one")
    ref = review(runtime, cycle)
    later = IntentMonitorService(runtime, clock=lambda: NOW + timedelta(minutes=15))
    with pytest.raises(ValueError, match="lease"):
        call(
            later, "finish", worker_id="run:one", cycle_id=cycle["cycle_id"], interpretation_ref=ref
        )


def test_pending_source_and_prd_changes_join_one_uninterpreted_batch(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    first = call(service, "begin", worker_id="run:one")
    assert first["status"] == "refresh_required"
    (runtime.root / "PRD.md").write_text("Keep new reports private.")
    batched = call(
        service,
        "begin",
        worker_id="run:one",
        observations=(observe([captured(runtime)], generation=1),),
    )
    assert batched["cycle_id"] == first["cycle_id"]
    assert batched["status"] == "needs_interpretation"
    assert batched["prds"][0]["content"] == "Keep new reports private."
    assert set(batched["source_refresh"]["required_evidence_refs"]) <= set(
        batched["required_evidence_refs"]
    )
    assert batched["scheduling_packet"]["interval_minutes"] == 60


def test_resume_marker_is_metadata_only_and_clears_only_after_fresh_source_check(
    runtime, monkeypatch
):
    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    current = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    call(
        service, "finish", cycle_id=current["cycle_id"], interpretation_ref=review(runtime, current)
    )
    with monkeypatch.context() as no_reads:

        def forbidden(*args, **kwargs):
            pytest.fail("startup catch-up must inspect only routing metadata")

        no_reads.setattr(runtime.graph_store, "load", forbidden)
        no_reads.setattr(service.refresh, "refresh", forbidden)
        no_reads.setattr(service, "_prds", forbidden)
        marker = IntentMonitorService.request_catch_up_for_project(runtime.root)
    assert marker["catch_up_pending"]
    restarted = IntentMonitorService(runtime, clock=lambda: NOW)
    pending = call(restarted, "begin", worker_id="run:resume")
    assert pending["status"] == "refresh_required"
    assert pending["catch_up_pending"]
    checked = call(
        restarted,
        "begin",
        worker_id="run:resume",
        observations=(observe(kind="metadata", generation=2),),
    )
    assert not checked["catch_up_pending"]
    assert not checked["source_refresh"]["sources"][0]["cached"]
    assert not restarted.resume_hint()["catch_up_pending"]


def test_new_resume_marker_during_unfinished_cycle_requires_another_fresh_check(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    IntentMonitorService.request_catch_up_for_project(runtime.root)
    call(service, "begin", worker_id="run:one")
    first = call(
        service,
        "begin",
        worker_id="run:one",
        observations=(observe([captured(runtime)], generation=1),),
    )
    assert not first["catch_up_pending"]
    IntentMonitorService.request_catch_up_for_project(runtime.root)
    second = call(service, "begin", worker_id="run:one")
    assert second["status"] == "refresh_required"
    assert second["catch_up_pending"]
    assert second["source_refresh"]["task_key"] != first["source_refresh"]["task_key"]


def test_completed_owner_finish_retry_is_idempotent(runtime):
    service = configured(runtime)
    cycle = call(service, "begin", worker_id="run:one")
    ref = review(runtime, cycle)
    result = call(
        service, "finish", worker_id="run:one", cycle_id=cycle["cycle_id"], interpretation_ref=ref
    )
    retried = call(
        service, "finish", worker_id="run:one", cycle_id=cycle["cycle_id"], interpretation_ref=ref
    )
    assert result["status"] == retried["status"] == "quiet"
    assert retried["cycle_id"] == cycle["cycle_id"]


def test_forced_pending_poll_stays_forced_after_restart_and_catch_up_clears(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    call(
        service,
        "finish",
        cycle_id=first["cycle_id"],
        interpretation_ref=review(runtime, first),
        actionable=True,
    )
    IntentMonitorService.request_catch_up_for_project(runtime.root)
    pending = call(service, "begin", worker_id="run:resume")
    assert pending["status"] == "refresh_required"
    restarted = IntentMonitorService(runtime, clock=lambda: NOW)
    resumed = call(restarted, "begin", worker_id="run:resume")
    assert resumed["status"] == "refresh_required"
    assert resumed["source_refresh"]["task_key"] == pending["source_refresh"]["task_key"]
    fresh = call(
        restarted,
        "begin",
        worker_id="run:resume",
        observations=(observe(kind="metadata", generation=2),),
    )
    assert fresh["status"] == "actionable"
    assert not fresh["catch_up_pending"]


def test_first_scheduled_run_checks_fresh_even_with_other_chat_cache(runtime):
    from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
    from tests.integration.intent_workflow.test_source_refresh import ready

    refresh = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, refresh)
    service = configured(runtime)
    first = call(service, "begin", worker_id="run:first")
    assert first["status"] == "refresh_required"
    assert first["catch_up_pending"]
    assert not first["source_refresh"]["sources"][0]["cached"]


def test_existing_schedule_requires_actual_hourly_upgrade_receipt(runtime):
    service = configured(runtime)
    old = call(service, "configure", automation_id="automation:actual")
    assert old["schedule_update_required"]
    assert service.resume_hint()["schedule_update_required"]
    confirmed = call(
        service, "configure", automation_id="automation:actual", schedule_interval_minutes=60
    )
    assert not confirmed["schedule_update_required"]
    assert not service.resume_hint()["schedule_update_required"]
    assert confirmed["automation_id"] == "automation:actual"


def test_hourly_schedule_receipt_requires_real_automation_id(runtime):
    service = configured(runtime)
    with pytest.raises(ValueError, match="automation"):
        call(service, "configure", schedule_interval_minutes=60)


def test_hourly_receipt_on_initial_configuration_keeps_first_run_fresh(runtime):
    from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
    from tests.integration.intent_workflow.test_source_refresh import ready

    ready(runtime, SourceRefreshService(runtime, clock=lambda: NOW))
    service = IntentMonitorService(runtime, clock=lambda: NOW)
    call(
        service,
        "configure",
        prd_paths=("PRD.md",),
        session_id="monitor:one",
        monitor_thread_id="chat:monitor",
        monitor_host_id="host:one",
        return_thread_id="chat:original",
        return_host_id="host:one",
        automation_id="automation:actual",
        schedule_interval_minutes=60,
    )
    assert call(service, "begin", worker_id="run:first")["status"] == "refresh_required"


def test_failed_catch_up_survives_restart_until_fresh_verification(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin", worker_id="run:one")
    failed = call(
        service,
        "begin",
        worker_id="run:one",
        observations=(
            observe(kind="unavailable", complete=False, generation=1, reason="No access"),
        ),
    )
    assert failed["catch_up_pending"]
    restarted = IntentMonitorService(runtime, clock=lambda: NOW)
    assert restarted.resume_hint()["catch_up_pending"]
    assert call(restarted, "begin", worker_id="run:one")["status"] == "refresh_required"
    fresh = call(
        restarted,
        "begin",
        worker_id="run:one",
        observations=(observe([captured(runtime)], generation=1),),
    )
    assert not fresh["catch_up_pending"]
    assert fresh["status"] == "needs_interpretation"


def test_forced_provider_check_cannot_fall_back_to_shared_cache_on_restart(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    current = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    call(
        service,
        "finish",
        cycle_id=current["cycle_id"],
        interpretation_ref=review(runtime, current),
        actionable=True,
    )
    pending = call(service, "begin", force_refresh=True, worker_id="run:forced")
    assert pending["status"] == "refresh_required"
    restarted = IntentMonitorService(runtime, clock=lambda: NOW)
    assert call(restarted, "begin", worker_id="run:forced")["status"] == "refresh_required"


def test_catch_up_source_checks_survive_immutable_cycle_rotation_without_second_read(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin", worker_id="run:one")
    first = call(
        service,
        "begin",
        worker_id="run:one",
        observations=(observe([captured(runtime)], generation=1),),
    )
    review(runtime, first)
    IntentMonitorService.request_catch_up_for_project(runtime.root)
    pending = call(service, "begin", worker_id="run:one")
    fresh = call(
        service,
        "begin",
        worker_id="run:one",
        observations=(observe(kind="metadata", generation=2),),
    )
    assert fresh["cycle_id"] != first["cycle_id"]
    assert fresh["source_refresh"]["task_key"] == pending["source_refresh"]["task_key"]
    assert fresh["status"] == "needs_interpretation"
    assert not fresh["catch_up_pending"]


def test_unavailable_prd_does_not_clear_reopen_verification_request(runtime):
    service = configured(runtime)
    (runtime.root / "PRD.md").unlink()
    result = call(service, "begin", worker_id="run:one")
    assert result["catch_up_pending"]
    (runtime.root / "PRD.md").write_text("Keep data local.")
    verified = call(service, "begin", worker_id="run:one")
    assert not verified["catch_up_pending"]


def test_successful_forced_pending_poll_returns_to_hourly_shared_freshness(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    call(
        service,
        "finish",
        cycle_id=first["cycle_id"],
        interpretation_ref=review(runtime, first),
        actionable=True,
    )
    assert call(service, "begin", force_refresh=True)["status"] == "refresh_required"
    checked = call(service, "begin", observations=(observe(kind="metadata", generation=2),))
    assert checked["status"] == "actionable"
    reused = call(service, "begin")
    assert reused["status"] == "actionable"
    assert reused["source_refresh"]["sources"][0]["cached"]


def test_reopen_after_interpretation_requires_fresh_begin_before_finish_or_delivery(runtime):
    service = configured(runtime)
    first = call(service, "begin", worker_id="run:one")
    ref = review(runtime, first)
    IntentMonitorService.request_catch_up_for_project(runtime.root)
    assert call(service, "status", worker_id="run:one")["status"] == "stale"
    with pytest.raises(ValueError, match="catch-up"):
        call(
            service,
            "finish",
            worker_id="run:one",
            cycle_id=first["cycle_id"],
            interpretation_ref=ref,
        )


def actionable_owner(runtime, service):
    cycle = call(service, "begin", worker_id="run:one")
    return call(
        service,
        "finish",
        worker_id="run:one",
        cycle_id=cycle["cycle_id"],
        interpretation_ref=review(runtime, cycle),
        actionable=True,
    )


def test_sender_owns_actionable_packet_through_delivery_acknowledgement(runtime):
    service = configured(runtime)
    sent = actionable_owner(runtime, service)
    assert sent["delivery_authorized"]
    assert sent["handoff"]["delivery_authorized"]
    assert call(service, "begin", worker_id="run:two")["status"] == "busy"
    assert call(service, "status", worker_id="run:two")["status"] == "busy"
    pending = call(service, "begin", worker_id="run:one")
    assert pending["delivery_authorized"]
    assert call(service, "begin", worker_id="run:two")["status"] == "busy"
    ack = call(
        service,
        "acknowledge",
        worker_id="run:one",
        cycle_id=sent["cycle_id"],
        handoff_id=sent["handoff"]["handoff_id"],
        delivery_receipt="host:delivered",
    )
    assert ack["status"] == "quiet"
    assert call(service, "begin", worker_id="run:two")["status"] == "quiet"


def test_recipient_can_validate_and_acknowledge_without_sender_lease(runtime):
    service = configured(runtime)
    sent = actionable_owner(runtime, service)
    recipient = call(service, "status")
    assert recipient["status"] == "actionable"
    assert recipient["handoff"]["handoff_id"] == sent["handoff"]["handoff_id"]
    assert not recipient["delivery_authorized"]
    assert not recipient["handoff"]["delivery_authorized"]
    received = call(
        service,
        "acknowledge",
        cycle_id=sent["cycle_id"],
        handoff_id=sent["handoff"]["handoff_id"],
        delivery_receipt="host:received",
    )
    assert received["status"] == "quiet"
    assert call(service, "begin", worker_id="run:two")["status"] == "quiet"


def test_expired_sender_status_never_authorizes_delivery_and_takeover_is_exclusive(runtime):
    service = configured(runtime)
    sent = actionable_owner(runtime, service)
    later = IntentMonitorService(runtime, clock=lambda: NOW + timedelta(minutes=15))
    expired = call(later, "status", worker_id="run:one")
    assert not expired["delivery_authorized"]
    takeover = call(later, "begin", worker_id="run:two")
    assert takeover["delivery_authorized"]
    assert takeover["handoff"]["handoff_id"] == sent["handoff"]["handoff_id"]
    assert call(later, "status", worker_id="run:one")["status"] == "busy"
    assert call(later, "begin", worker_id="run:three")["status"] == "busy"


def test_scheduled_force_refresh_bypasses_newly_warmed_other_chat_receipt(runtime):
    from intent_engineering.intent_workflow.source_refresh import RefreshRequest

    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin")
    first = call(service, "begin", observations=(observe([captured(runtime)], generation=1),))
    call(service, "finish", cycle_id=first["cycle_id"], interpretation_ref=review(runtime, first))
    later = NOW + timedelta(minutes=59)
    service = IntentMonitorService(runtime, clock=lambda: later)
    warm = service.refresh.refresh(
        RefreshRequest(
            task="Separate task",
            session_id="chat:other",
            turn_id="task:warm",
            observations=(observe(kind="metadata", generation=2, checked_at=later),),
            force_refresh=True,
        )
    )
    assert warm["status"] == "ready"
    scheduled = IntentMonitorService(runtime, clock=lambda: NOW + timedelta(hours=1))
    tick = call(scheduled, "begin", worker_id="run:scheduled", force_refresh=True)
    assert tick["status"] == "refresh_required"
    assert not tick["source_refresh"]["sources"][0]["cached"]


def test_forced_takeover_rechecks_crashed_owners_recent_read_and_keeps_own_retries(runtime):
    service = configured(runtime)
    service.refresh.sources(selection())
    call(service, "begin", worker_id="run:crashed", force_refresh=True)
    first_read_at = NOW + timedelta(minutes=5)
    first_worker = IntentMonitorService(runtime, clock=lambda: first_read_at)
    checked = call(
        first_worker,
        "begin",
        worker_id="run:crashed",
        observations=(observe([captured(runtime)], generation=1, checked_at=first_read_at),),
    )
    assert checked["status"] == "needs_interpretation"

    next_tick = NOW + timedelta(hours=1)
    recovering = IntentMonitorService(runtime, clock=lambda: next_tick)
    takeover = call(recovering, "begin", worker_id="run:recovering", force_refresh=True)
    assert takeover["status"] == "refresh_required"
    assert takeover["source_refresh"]["task_key"] != checked["source_refresh"]["task_key"]
    assert takeover["cycle_id"] != checked["cycle_id"]
    same_worker = call(recovering, "begin", worker_id="run:recovering", force_refresh=True)
    assert same_worker["status"] == "refresh_required"
    assert same_worker["source_refresh"]["task_key"] == takeover["source_refresh"]["task_key"]
    assert same_worker["cycle_id"] == takeover["cycle_id"]
    refreshed = call(
        recovering,
        "begin",
        worker_id="run:recovering",
        force_refresh=True,
        observations=(observe(kind="metadata", generation=2, checked_at=next_tick),),
    )
    assert refreshed["status"] == "needs_interpretation"
    assert refreshed["source_refresh"]["task_key"] == takeover["source_refresh"]["task_key"]
    assert refreshed["cycle_id"] == takeover["cycle_id"]
    retry = call(recovering, "begin", worker_id="run:recovering", force_refresh=True)
    assert retry["source_refresh"]["task_key"] == refreshed["source_refresh"]["task_key"]
    assert retry["cycle_id"] == refreshed["cycle_id"]
