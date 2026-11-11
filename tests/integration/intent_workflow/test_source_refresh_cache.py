"""Shared source receipts preserve original provider read times and local boundaries."""

from datetime import timedelta

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
from tests.integration.intent_workflow.test_source_refresh import (
    NOW,
    captured,
    observe,
    ready,
    reconcile,
    request,
    selection,
)


@pytest.fixture
def runtime(tmp_path):
    initialize_project(tmp_path)
    result = load_runtime(tmp_path)
    yield result
    result.close()


def test_new_chat_reuses_original_success_receipt_without_new_provider_observation(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    original = ready(runtime, service)
    receipts = runtime.evidence_store.ledger("conversation:source-refresh")
    later = NOW + timedelta(minutes=20)
    reused = SourceRefreshService(runtime, clock=lambda: later).refresh(
        request(session_id="chat:other", task="Inspect privacy settings")
    )
    assert reused["status"] == "ready"
    assert reused["required_evidence_refs"] == original["required_evidence_refs"]
    assert reused["sources"][0]["action"] == "reuse"
    assert reused["sources"][0]["cached"] is True
    assert reused["sources"][0]["checked_at"] == NOW.isoformat()
    assert reused["sources"][0]["expires_at"] == (NOW + timedelta(hours=1)).isoformat()
    # A task checkpoint may be appended, but a reuse is never a provider check receipt.
    added = runtime.evidence_store.ledger("conversation:source-refresh")[len(receipts) :]
    assert all(item.evidence.payload["content"]["kind"] != "check" for item in added)


@pytest.mark.parametrize("session_id", ["chat:one", "chat:other"])
def test_one_hour_expiry_applies_to_existing_and_new_tasks_without_sliding_ttl(runtime, session_id):
    ready(runtime, SourceRefreshService(runtime, clock=lambda: NOW))
    near_expiry = SourceRefreshService(runtime, clock=lambda: NOW + timedelta(minutes=59))
    assert near_expiry.refresh(request(session_id=session_id))["status"] == "ready"
    expired = SourceRefreshService(runtime, clock=lambda: NOW + timedelta(hours=1)).refresh(
        request(session_id=session_id)
    )
    assert expired["status"] == "refresh_required"
    assert expired["required_evidence_refs"] == []
    assert expired["sources"][0]["action"] == "check_revision"
    assert expired["sources"][0]["cached"] is False


def test_force_refresh_rejects_cached_task_check_then_accepts_fresh_same_task_followup(runtime):
    ready(runtime, SourceRefreshService(runtime, clock=lambda: NOW))
    later = NOW + timedelta(minutes=10)
    service = SourceRefreshService(runtime, clock=lambda: later)
    assert service.refresh(request(session_id="chat:other"))["status"] == "ready"
    forced = request(session_id="chat:other", force_refresh=True)
    assert service.refresh(forced)["status"] == "refresh_required"
    # A retry cannot silently drop the explicit request for an actual provider read.
    assert service.refresh(request(session_id="chat:other"))["status"] == "refresh_required"
    checked = service.refresh(
        request(
            session_id="chat:other",
            force_refresh=True,
            observations=[observe(kind="metadata", checked_at=later)],
        )
    )
    assert checked["status"] == "ready"
    assert checked["sources"][0]["cached"] is False
    assert checked["sources"][0]["checked_at"] == later.isoformat()
    assert service.refresh(forced)["status"] == "ready"


def test_partial_check_invalidates_receipts_in_other_chats(runtime):
    ready(runtime, SourceRefreshService(runtime, clock=lambda: NOW))
    later = NOW + timedelta(minutes=10)
    service = SourceRefreshService(runtime, clock=lambda: later)
    failed = service.refresh(
        request(
            session_id="chat:other",
            observations=[observe(complete=False, checked_at=later)],
        )
    )
    assert failed["status"] == "refresh_required"
    for session_id in ("chat:one", "chat:third"):
        assert service.refresh(request(session_id=session_id))["status"] == "refresh_required"


def test_complete_content_without_validator_can_finish_and_reuse_until_expiry(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    service.sources(selection())
    service.refresh(request())
    checked = service.refresh(
        request(observations=[observe([captured(runtime)], generation=1, validator=None)])
    )
    assert checked["status"] == "needs_interpretation"
    review = reconcile(runtime, checked)
    assert service.refresh(request(reconciliation_ref=review))["status"] == "ready"
    reused = service.refresh(request(session_id="chat:other"))
    assert reused["status"] == "ready"
    assert reused["sources"][0]["cached"] is True
    assert (
        service.refresh(request(session_id="chat:other", force_refresh=True))["status"]
        == "refresh_required"
    )
    expired = SourceRefreshService(runtime, clock=lambda: NOW + timedelta(hours=1))
    assert expired.refresh(request())["status"] == "refresh_required"


def test_new_chat_still_checks_current_graph_when_receipt_is_cached(runtime):
    from intent_engineering.intent_workflow.background import BackgroundIntentService
    from tests.integration.intent_workflow.test_background_intent import submission

    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    BackgroundIntentService(runtime, clock=lambda: NOW).submit(submission(turn_id="graph:change"))
    result = service.refresh(request(session_id="chat:other", task="Implement local storage"))
    assert result["status"] == "needs_interpretation"
    assert result["sources"][0]["cached"] is True


def test_fixed_clock_force_cannot_treat_shared_receipt_as_a_current_task_read(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    assert service.refresh(request(session_id="chat:other"))["status"] == "ready"
    assert (
        service.refresh(request(session_id="chat:other", force_refresh=True))["status"]
        == "refresh_required"
    )
    checked = service.refresh(
        request(
            session_id="chat:other",
            force_refresh=True,
            observations=[observe(kind="metadata")],
        )
    )
    assert checked["status"] == "ready"
    assert checked["sources"][0]["cached"] is False


def test_revoked_acl_invalidates_cached_receipt_even_if_access_is_later_restored(runtime):
    from hashlib import sha256

    import yaml

    from intent_engineering.capture.base import RawSourceObject, normalize_raw_source

    actor = runtime.config.local_actor
    policy_path = runtime.root / ".intent/approvals/policy.yaml"
    policy = {
        "contributors": [actor],
        "approvers": [actor],
        "executors": [actor],
        "identities": {actor: [actor, "group:readers"]},
    }
    policy_path.write_text(yaml.safe_dump(policy))
    evidence = normalize_raw_source(
        RawSourceObject(
            connector_type="background:notion",
            external_object_id="page:privacy",
            external_version="1",
            source_locator="https://notion.so/privacy",
            author="notion:alice",
            observed_at=NOW,
            content_hash="sha256:" + sha256(b"Keep reports private.").hexdigest(),
            payload={"content": "Keep reports private."},
            acl=("group:readers",),
        )
    )
    runtime.evidence_store.associate("background:notion", evidence)
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    service.sources(selection())
    service.refresh(request())
    service.refresh(request(observations=[observe([evidence.id], generation=1)]))
    assert service.refresh(request(session_id="chat:other"))["required_evidence_refs"]
    policy["identities"] = {actor: [actor]}
    policy_path.write_text(yaml.safe_dump(policy))
    denied = service.refresh(request(session_id="chat:other"))
    assert denied["status"] == "refresh_required"
    assert denied["required_evidence_refs"] == []
    assert denied["review_evidence_refs"] == []
    policy["identities"] = {actor: [actor, "group:readers"]}
    policy_path.write_text(yaml.safe_dump(policy))
    assert service.refresh(request(session_id="chat:third"))["status"] == "refresh_required"


def test_shared_expiration_uses_provider_read_time_before_receipt_submission(runtime):
    ready(runtime, SourceRefreshService(runtime, clock=lambda: NOW))
    SourceRefreshService(runtime, clock=lambda: NOW + timedelta(minutes=10)).refresh(
        request(session_id="chat:delayed")
    )
    read_at = NOW + timedelta(minutes=11)
    submitted_at = NOW + timedelta(minutes=20)
    checked = SourceRefreshService(runtime, clock=lambda: submitted_at).refresh(
        request(
            session_id="chat:delayed",
            observations=[observe(kind="metadata", checked_at=read_at)],
        )
    )
    reused = SourceRefreshService(runtime, clock=lambda: NOW + timedelta(minutes=30)).refresh(
        request(session_id="chat:third")
    )
    assert reused["required_evidence_refs"] == checked["required_evidence_refs"]
    assert reused["sources"][0]["checked_at"] == read_at.isoformat()
    assert reused["sources"][0]["expires_at"] == (read_at + timedelta(hours=1)).isoformat()


def test_same_actor_in_another_project_cannot_reuse_shared_receipt(runtime, tmp_path):
    ready(runtime, SourceRefreshService(runtime, clock=lambda: NOW))
    project = tmp_path / "other-project"
    project.mkdir()
    initialize_project(project)
    other = load_runtime(project)
    try:
        service = SourceRefreshService(other, clock=lambda: NOW)
        assert service.refresh(request())["status"] == "not_configured"
        service.sources(selection())
        result = service.refresh(request())
        assert result["status"] == "refresh_required"
        assert result["required_evidence_refs"] == []
        assert result["sources"][0]["cached"] is False
    finally:
        other.close()


def test_forced_real_read_can_match_cached_observation_in_the_same_second(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    checked = service.refresh(
        request(turn_id="read:metadata", observations=[observe(kind="metadata")])
    )
    cached = service.refresh(request(session_id="chat:other"))
    assert cached["required_evidence_refs"] == checked["required_evidence_refs"]
    assert cached["sources"][0]["cached"] is True
    refreshed = service.refresh(
        request(
            session_id="chat:other",
            force_refresh=True,
            observations=[observe(kind="metadata")],
        )
    )
    assert refreshed["status"] == "ready"
    assert refreshed["sources"][0]["cached"] is False
    assert refreshed["required_evidence_refs"] != cached["required_evidence_refs"]
