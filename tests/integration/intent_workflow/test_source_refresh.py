"""Task-bound source checks use real immutable evidence and local transactions."""

from datetime import timedelta

import pytest
import yaml

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_sources import BackgroundSource, capture_source
from intent_engineering.intent_workflow.source_refresh import (
    RefreshRequest,
    SourceRefreshService,
    SourceSelection,
    SourceSignal,
)
from intent_engineering.validation import validate_project_directory
from tests.integration.intent_workflow.test_background_intent import (
    NOW,
    durable,
    submission,
)


@pytest.fixture
def runtime(tmp_path):
    initialize_project(tmp_path)
    result = load_runtime(tmp_path)
    yield result
    result.close()


def selection(**changes):
    return SourceSelection.model_validate(
        {
            "source_id": "source:privacy",
            "provider": "notion",
            "locator": "https://notion.so/privacy",
            "scope": "page:privacy",
            "user_statement": "Use the privacy page as the source of truth.",
            "authoritative": True,
            **changes,
        }
    )


def request(**changes):
    return RefreshRequest.model_validate(
        {
            "task": "Implement privacy settings",
            "session_id": "chat:one",
            "turn_id": "task:one",
            **changes,
        }
    )


def captured(runtime, version="1", content="Keep reports private."):
    return capture_source(
        runtime,
        BackgroundSource(
            provider="notion",
            object_id="page:privacy",
            version=version,
            locator="https://notion.so/privacy",
            author="notion:alice",
            observed_at=NOW,
            content=content,
        ),
    )["evidence_ref"]


def observe(refs=(), **changes):
    return {
        "source_id": "source:privacy",
        "generation": 2,
        "kind": "content",
        "checked_at": NOW,
        "complete": True,
        "validator": "scope:1",
        "evidence_refs": refs,
        **changes,
    }


def reconcile(runtime, result, turn="review:one", **changes):
    return BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            turn_id=turn,
            graph_version=runtime.graph_store.load().version,
            nodes=[],
            evidence_refs=result["required_evidence_refs"],
            **changes,
        )
    )["evidence_ref"]


def ready(runtime, service):
    service.sources(selection())
    assert service.refresh(request())["sources"][0]["action"] == "fetch"
    pending = service.refresh(request(observations=[observe([captured(runtime)], generation=1)]))
    assert pending["status"] == "needs_interpretation"
    review = reconcile(runtime, pending)
    result = service.refresh(request(reconciliation_ref=review))
    assert result["status"] == "ready"
    return result


def test_initial_read_and_unchanged_metadata_reuse_without_content_or_interpretation(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    assert service.refresh(request())["status"] == "not_configured"
    ready(runtime, service)
    before = runtime.evidence_store.ledger("background:notion")
    assert service.refresh(request(turn_id="task:two"))["sources"][0]["action"] == "reuse"
    result = service.refresh(
        request(
            turn_id="task:two",
            observations=[
                observe(kind="metadata", refs=()),
            ],
        )
    )
    assert result["status"] == "ready"
    assert result["sources"][0]["action"] == "reuse"
    assert runtime.evidence_store.ledger("background:notion") == before
    assert validate_project_directory(runtime.project_directory).valid


def test_exact_replay_and_restart_do_not_append_evidence(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    before = durable(runtime.root / ".intent")
    assert SourceRefreshService(runtime, clock=lambda: NOW).refresh(request())["status"] == "ready"
    assert durable(runtime.root / ".intent") == before
    with pytest.raises(ValueError, match="task"):
        service.refresh(request(task="A different task"))
    assert durable(runtime.root / ".intent") == before


@pytest.mark.parametrize(
    "observation",
    [
        observe(kind="metadata", validator="scope:2"),
        observe(kind="metadata", validator=None),
        observe(kind="metadata", complete=False),
        observe(kind="unavailable", complete=False, reason="Access denied"),
        observe(complete=False),
    ],
)
def test_changed_unknown_partial_or_failed_checks_never_return_ready(runtime, observation):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    service.refresh(request(turn_id="task:two"))
    result = service.refresh(request(turn_id="task:two", observations=[observation]))
    assert result["status"] == "refresh_required"
    assert result["sources"][0]["action"] in {"fetch", "retry"}


def test_signal_is_idempotent_and_cannot_be_cleared_by_a_racing_read(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    signal = SourceSignal(source_id="source:privacy", event_id="event:1", observed_at=NOW)
    assert service.signal(signal)["generation"] == 3
    assert service.signal(signal)["generation"] == 3
    stale = service.refresh(request(observations=[observe([captured(runtime)])]))
    assert stale["status"] == "refresh_required"
    assert stale["sources"][0]["action"] == "fetch"
    assert stale["sources"][0]["generation"] == 3
    refreshed = service.refresh(request(observations=[observe([captured(runtime)], generation=3)]))
    assert refreshed["status"] == "ready"  # identical full manifest, already interpreted


def test_changed_manifest_requires_current_evidence_backed_interpretation(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    service.refresh(request(turn_id="task:two"))
    old = captured(runtime)
    new = captured(runtime, "2", "Allow reports to be shared.")
    changed = service.refresh(
        request(
            turn_id="task:two",
            observations=[
                observe([new], validator="scope:2"),
            ],
        )
    )
    assert changed["status"] == "needs_interpretation"
    assert {old, new} <= set(changed["review_evidence_refs"])
    bad = BackgroundIntentService(runtime, clock=lambda: NOW).submit(
        submission(
            turn_id="review:bad",
            nodes=[],
        )
    )["evidence_ref"]
    with pytest.raises(ValueError, match="interpretation"):
        service.refresh(request(turn_id="task:two", reconciliation_ref=bad))
    review = reconcile(runtime, changed, turn="review:two")
    assert (
        service.refresh(request(turn_id="task:two", reconciliation_ref=review))["status"] == "ready"
    )


def test_deleted_collection_members_need_interpretation_and_preserve_old_evidence(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    service.sources(selection(collection=True, scope="Privacy topic in this workspace"))
    service.refresh(request(turn_id="task:two"))
    deleted = service.refresh(
        request(
            turn_id="task:two",
            observations=[
                observe([], generation=3, validator="empty:2"),
            ],
        )
    )
    assert deleted["status"] == "needs_interpretation"
    assert captured(runtime) in deleted["review_evidence_refs"]
    review = reconcile(runtime, deleted, turn="review:deleted")
    assert (
        service.refresh(request(turn_id="task:two", reconciliation_ref=review))["status"] == "ready"
    )
    assert (
        runtime.evidence_store.get(captured(runtime)).payload["content"] == "Keep reports private."
    )


def test_missing_foreign_and_mismatched_evidence_is_rejected_atomically(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    service.sources(selection())
    service.refresh(request())
    wrong = capture_source(
        runtime,
        BackgroundSource(
            provider="slack",
            object_id="other",
            version="1",
            locator="https://slack.com/other",
            observed_at=NOW,
            content="Other content",
        ),
    )["evidence_ref"]
    for ref in ("evidence:missing", wrong):
        before = durable(runtime.root / ".intent")
        with pytest.raises(ValueError, match="evidence"):
            service.refresh(request(observations=[observe([ref], generation=1)]))
        assert durable(runtime.root / ".intent") == before


def test_checks_must_follow_task_start_and_cannot_come_from_the_future(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    service.sources(selection())
    service.refresh(request())
    for at in (NOW - timedelta(seconds=1), NOW + timedelta(minutes=2)):
        with pytest.raises(ValueError, match="time"):
            service.refresh(
                request(observations=[observe([captured(runtime)], generation=1, checked_at=at)])
            )


def test_source_reconfiguration_invalidates_readiness(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    service.sources(selection(scope="page:another", locator="https://notion.so/another"))
    result = service.refresh(request())
    assert result["status"] == "refresh_required"
    assert result["sources"][0]["generation"] == 3


def test_open_conflict_blocks_until_exact_user_decision(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    background = BackgroundIntentService(runtime, clock=lambda: NOW)
    background.submit(submission(turn_id="seed:graph"))
    pending = service.refresh(request())
    asked = background.submit(
        submission(
            turn_id="conflict:one",
            graph_version=1,
            nodes=[],
            classification="conflicting",
            relevant_node_ids=["intent:local"],
            evidence_refs=pending["required_evidence_refs"],
            conflict="The sources disagree on local storage.",
            question="Keep data local? Recommendation: retain local storage until resolved.",
        )
    )
    assert service.refresh(request())["status"] == "blocked"
    decided = background.submit(
        submission(
            turn_id="decision:one",
            graph_version=2,
            nodes=[],
            relevant_node_ids=["intent:local"],
            evidence_refs=pending["required_evidence_refs"],
            answers=[
                {
                    "question_ref": asked["evidence_ref"],
                    "quote": "Keep all project data local.",
                    "resolution": "retain_existing",
                }
            ],
        )
    )
    assert service.refresh(request(reconciliation_ref=decided["evidence_ref"]))["status"] == "ready"


def test_late_content_cannot_undo_a_newer_task_refresh(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    service.refresh(request(turn_id="task:two"))
    old = captured(runtime)
    new = captured(runtime, "2", "Share reports.")
    service.refresh(request(turn_id="task:two", observations=[observe([new], validator="2")]))
    late = service.refresh(request(observations=[observe([old], validator="old-verified")]))
    assert late["status"] == "needs_interpretation"
    assert late["sources"][0]["cached"] is True
    assert late["sources"][0]["evidence_refs"] == [new]


def test_actor_cannot_see_another_actors_selections_or_reuse_evidence(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    ref = captured(runtime)
    path = runtime.root / ".intent/config.yaml"
    config = yaml.safe_load(path.read_text())
    config["local_actor"] = "other"
    path.write_text(yaml.safe_dump(config))
    other = load_runtime(runtime.root)
    try:
        isolated = SourceRefreshService(other, clock=lambda: NOW)
        assert isolated.sources()["sources"] == []
        assert isolated.refresh(request())["status"] == "not_configured"
        isolated.sources(selection())
        isolated.refresh(request())
        with pytest.raises(ValueError, match="evidence"):
            isolated.refresh(request(observations=[observe([ref], generation=1)]))
    finally:
        other.close()


def test_initial_reads_cannot_overtake_each_other(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    service.sources(selection())
    service.refresh(request())
    service.refresh(request(turn_id="task:two"))
    old = captured(runtime)
    new = captured(runtime, "2", "Share reports.")
    service.refresh(
        request(turn_id="task:two", observations=[observe([new], generation=1, validator="2")])
    )
    late = service.refresh(request(observations=[observe([old], generation=1)]))
    assert late["status"] == "needs_interpretation"
    assert late["sources"][0]["cached"] is True
    assert late["sources"][0]["evidence_refs"] == [new]


def test_new_task_rechecks_interpretation_after_graph_change(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    ready(runtime, service)
    BackgroundIntentService(runtime, clock=lambda: NOW).submit(submission(turn_id="graph:change"))
    service.refresh(request(turn_id="task:two"))
    result = service.refresh(request(turn_id="task:two", observations=[observe(kind="metadata")]))
    assert result["status"] == "needs_interpretation"
    assert result["sources"][0]["action"] == "reuse"


def test_source_only_conflict_follows_source_across_chats_until_answered(runtime):
    service = SourceRefreshService(runtime, clock=lambda: NOW)
    service.sources(selection())
    service.refresh(request())
    pending = service.refresh(request(observations=[observe([captured(runtime)], generation=1)]))
    background = BackgroundIntentService(runtime, clock=lambda: NOW)
    question = background.submit(
        submission(
            turn_id="source:question",
            nodes=[],
            classification="needs_clarification",
            evidence_refs=pending["required_evidence_refs"],
            question="Sources disagree on visibility. Keep reports private?",
        )
    )
    service.refresh(request(session_id="chat:two"))
    checked = service.refresh(
        request(session_id="chat:two", observations=[observe(kind="metadata")])
    )
    assert checked["status"] == "blocked"
    assert checked["questions"][0]["question_ref"] == question["evidence_ref"]
    answer = background.submit(
        submission(
            session_id="chat:two",
            turn_id="source:decision",
            nodes=[],
            evidence_refs=checked["required_evidence_refs"],
            answers=[
                {"question_ref": question["evidence_ref"], "quote": "Keep all project data local."}
            ],
        )
    )
    assert (
        service.refresh(request(session_id="chat:two", reconciliation_ref=answer["evidence_ref"]))[
            "status"
        ]
        == "ready"
    )
