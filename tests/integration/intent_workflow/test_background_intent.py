"""Conversational intent uses real local stores without manufacturing approvals."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_models import BackgroundSubmission
from intent_engineering.validation import validate_project_directory
from tests.helpers.readiness import put_pending_proposal

NOW = datetime(2026, 11, 9, tzinfo=UTC)


@pytest.fixture
def runtime(tmp_path):
    initialize_project(tmp_path)
    result = load_runtime(tmp_path)
    yield result
    result.close()


def submission(**changes):
    data = {
        "session_id": "chat:one",
        "turn_id": "turn:one",
        "graph_version": 0,
        "user_statement": "Keep all project data local.",
        "classification": "clear",
        "understanding": "The user needs local project storage.",
        "nodes": [
            {
                "id": "intent:local",
                "type": "PRODUCT_INTENT",
                "label": "Keep all project data local.",
                "source_mode": "explicit",
                "confidence": 0.95,
                "basis": "Exact statement in the current chat",
            }
        ],
    }
    data.update(changes)
    return BackgroundSubmission.model_validate(data)


def durable(root: Path):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and not p.name.endswith(".lock")
    }


def test_clear_intent_applies_with_history_and_no_approval(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    result = service.submit(submission())
    assert result["status"] == "maintained"
    assert result["question"] is None
    node = runtime.graph_store.load().nodes[0]
    assert node.source_mode.value == "explicit"
    assert node.created_by == "agent:intent-background"
    assert len(runtime.graph_store.history(node.id)) == 1
    assert runtime.intent_proposals.list() == ()
    evidence = runtime.evidence_store.get(result["evidence_ref"])
    assert evidence.payload["content"]["user_statement"] == "Keep all project data local."
    assert evidence.payload["content"]["attribution"] == "agent_reported_user_statement"
    assert evidence.payload["role"] == "agent"
    assert evidence.payload["content"]["human_approval"] is False
    assert validate_project_directory(runtime.project_directory).valid


def test_replay_is_exact_and_survives_service_restart(runtime):
    first = BackgroundIntentService(runtime, clock=lambda: NOW).submit(submission())
    before = durable(runtime.root / ".intent")
    assert BackgroundIntentService(runtime).submit(submission()) == first
    assert durable(runtime.root / ".intent") == before
    with pytest.raises(ValueError):
        BackgroundIntentService(runtime).submit(submission(user_statement="Changed replay"))
    assert durable(runtime.root / ".intent") == before


def test_unrelated_and_aligned_work_is_silent_despite_pending_proposal(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    pending = put_pending_proposal(runtime)
    before = runtime.graph_store.load()
    for i, classification in enumerate(("unrelated", "aligned")):
        result = service.submit(
            submission(
                turn_id=f"silent:{i}",
                graph_version=1,
                nodes=[],
                classification=classification,
                relevant_node_ids=[] if i == 0 else ["intent:local"],
            )
        )
        assert result["status"] == "continue"
        assert result["question"] is None
    assert runtime.graph_store.load() == before
    assert runtime.intent_proposals.decision_for(pending.id) is None


def test_clarification_and_answer_are_reused_without_authentication(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    asked = service.submit(
        submission(
            nodes=[],
            classification="needs_clarification",
            question="Must storage stay local?",
        )
    )
    assert asked["status"] == "question"
    assert runtime.graph_store.load().version == 0
    assert (
        service.context(task="storage", session_id="chat:one")["questions"][0]["question_ref"]
        == asked["evidence_ref"]
    )
    answered = service.submit(
        submission(
            turn_id="answer:one",
            answers=[
                {"question_ref": asked["evidence_ref"], "quote": "Keep all project data local."}
            ],
        )
    )
    assert answered["status"] == "maintained"
    context = service.context(task="storage", session_id="chat:one")
    assert context["questions"] == []
    assert context["answers"][0]["quote"] == "Keep all project data local."
    assert service.context(task="storage", session_id="another-chat")["answers"] == []
    with pytest.raises(ValueError):
        service.submit(
            submission(
                session_id="another-chat",
                turn_id="foreign-answer",
                graph_version=1,
                nodes=[],
                answers=[
                    {"question_ref": asked["evidence_ref"], "quote": "Keep all project data local."}
                ],
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"graph_version": 12},
        {"evidence_refs": ["evidence:missing"]},
        {
            "edges": [
                {
                    "id": "edge:bad",
                    "from_id": "intent:local",
                    "relation": "SPECIFIED_BY",
                    "to_id": "missing",
                }
            ]
        },
        {
            "nodes": [
                {
                    "id": "code:fake",
                    "type": "FILE",
                    "label": "Keep all project data local.",
                    "source_mode": "explicit",
                    "confidence": 0.9,
                    "basis": "Current chat",
                }
            ]
        },
        {
            "nodes": [
                {
                    "id": "intent:fake",
                    "type": "PRODUCT_INTENT",
                    "label": "Invented human intent",
                    "source_mode": "explicit",
                    "confidence": 0.9,
                    "basis": "Current chat",
                }
            ]
        },
    ],
)
def test_invalid_submissions_are_atomic(runtime, changes):
    before = durable(runtime.root / ".intent")
    with pytest.raises(ValueError):
        BackgroundIntentService(runtime, clock=lambda: NOW).submit(submission(**changes))
    assert durable(runtime.root / ".intent") == before


def test_inference_is_distinct_and_cannot_overwrite_explicit_intent(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    infer = {
        "id": "need:privacy",
        "type": "NEED",
        "label": "Protect private project information",
        "source_mode": "inferred",
        "confidence": 0.7,
        "basis": "Interpretation of local storage",
    }
    service.submit(submission(turn_id="infer", graph_version=1, nodes=[infer]))
    assert runtime.graph_store.load().nodes[1].source_mode.value == "inferred"
    with pytest.raises(ValueError):
        service.submit(
            submission(
                turn_id="overwrite", graph_version=2, nodes=[{**infer, "id": "intent:local"}]
            )
        )


def test_explicit_revision_preserves_identity_and_previous_evidence(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    prior = runtime.graph_store.load().nodes[0]
    new_text = "Allow encrypted cloud backups."
    service.submit(
        submission(
            turn_id="revision",
            graph_version=1,
            user_statement=new_text,
            nodes=[
                {
                    "id": prior.id,
                    "type": "PRODUCT_INTENT",
                    "label": new_text,
                    "source_mode": "explicit",
                    "confidence": 0.95,
                    "basis": "User revised storage preference",
                    "revision_quote": new_text,
                }
            ],
        )
    )
    updated = runtime.graph_store.load().nodes[0]
    assert updated.id == prior.id
    assert set(prior.evidence_refs) < set(updated.evidence_refs)
    assert len(runtime.graph_store.history(prior.id)) == 2


def test_conflict_keeps_existing_position_and_asks_specific_question(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    original = runtime.graph_store.load().nodes
    result = service.submit(
        submission(
            turn_id="conflict",
            graph_version=1,
            user_statement="Upload project data publicly.",
            classification="conflicting",
            relevant_node_ids=["intent:local"],
            nodes=[],
            question="Should local-only storage change to public uploads?",
            conflict="Public uploads conflict with keeping all project data local.",
        )
    )
    assert result["status"] == "question"
    assert result["existing_intent"][0]["label"] == "Keep all project data local."
    assert runtime.graph_store.load().nodes == original
    assert len(runtime.cases()) == 1
    case = runtime.cases()[0]
    assert case.status.value == "open"
    assert len(case.evidence_sides) == 2
    with pytest.raises(ValueError):
        service.submit(
            submission(
                turn_id="erase-conflict",
                graph_version=2,
                nodes=[
                    {
                        "id": "intent:local",
                        "type": "PRODUCT_INTENT",
                        "label": "New position",
                        "source_mode": "inferred",
                        "confidence": 0.7,
                        "basis": "Automatic merge",
                    }
                ],
            )
        )
    assert runtime.cases()[0] == case


def test_chat_decision_resolves_its_conflict_without_a_second_approval(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    asked = service.submit(
        submission(
            turn_id="conflict",
            graph_version=1,
            nodes=[],
            classification="conflicting",
            relevant_node_ids=["intent:local"],
            user_statement="Upload publicly.",
            conflict="Uploads conflict with local storage.",
            question="Keep local storage?",
        )
    )
    result = service.submit(
        submission(
            turn_id="decision",
            graph_version=2,
            nodes=[],
            user_statement="Yes, keep local storage.",
            answers=[
                {
                    "question_ref": asked["evidence_ref"],
                    "quote": "Yes, keep local storage.",
                    "resolution": "retain_existing",
                }
            ],
        )
    )
    assert result["graph_version"] == 3
    assert runtime.cases()[0].status.value == "resolved"
    assert runtime.graph_store.load().nodes[0].label == "Keep all project data local."
    assert (
        service.submit(
            submission(
                turn_id="aligned",
                graph_version=3,
                nodes=[],
                classification="aligned",
                relevant_node_ids=["intent:local"],
            )
        )["status"]
        == "continue"
    )
    assert runtime.intent_proposals.list() == ()
    assert validate_project_directory(runtime.project_directory).valid


def test_agent_inference_evidence_cannot_be_relabelled_as_explicit(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    record = service.submit(
        submission(
            nodes=[
                {
                    "id": "need:privacy",
                    "type": "NEED",
                    "label": "Protect private information",
                    "source_mode": "inferred",
                    "confidence": 0.7,
                    "basis": "Inferred from local storage",
                }
            ]
        )
    )
    with pytest.raises(ValueError):
        service.submit(
            submission(
                turn_id="promote",
                graph_version=1,
                nodes=[
                    {
                        "id": "need:claimed",
                        "type": "NEED",
                        "label": "Protect private information",
                        "source_mode": "explicit",
                        "confidence": 0.95,
                        "basis": "Copy of agent evidence",
                        "evidence_refs": [record["evidence_ref"]],
                    }
                ],
            )
        )


def test_background_refuses_corrupt_history_before_writing(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    history = runtime.root / ".intent/history/changesets.jsonl"
    history.write_text("invalid history\n")
    before = durable(runtime.root / ".intent")
    with pytest.raises(ValueError):
        service.submit(submission(turn_id="after-corruption", graph_version=1, nodes=[]))
    assert durable(runtime.root / ".intent") == before
    with pytest.raises(ValueError):
        service.context(task="storage", session_id="chat:one")


def test_conflict_uses_source_time_not_the_later_detection_time(runtime):
    from datetime import timedelta

    BackgroundIntentService(runtime, clock=lambda: NOW).submit(submission())
    BackgroundIntentService(runtime, clock=lambda: NOW + timedelta(days=1)).submit(
        submission(
            turn_id="later-conflict",
            graph_version=1,
            nodes=[],
            classification="conflicting",
            relevant_node_ids=["intent:local"],
            conflict="Public upload conflicts with local storage",
            question="Should storage change?",
        )
    )
    assert validate_project_directory(runtime.project_directory).valid
    assert runtime.cases()[0].evidence_sides[0].observed_at == NOW


def test_copied_private_intent_does_not_become_visible_to_readers_team(runtime, monkeypatch):
    from intent_engineering.core.policy.access import evidence_allowed

    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    monkeypatch.setattr(
        "intent_engineering.cli.intent_workflow._principals",
        lambda *_: frozenset({"local", "team:all"}),
    )
    result = service.submit(
        submission(
            turn_id="private-copy",
            graph_version=1,
            nodes=[],
            classification="aligned",
            relevant_node_ids=["intent:local"],
        )
    )
    copied = runtime.evidence_store.get(result["evidence_ref"])
    assert not evidence_allowed(copied, frozenset({"other-user", "team:all"}))


def test_preserved_disagreement_can_later_receive_a_conversational_decision(runtime):
    service = BackgroundIntentService(runtime, clock=lambda: NOW)
    service.submit(submission())
    question = service.submit(
        submission(
            turn_id="conflict",
            graph_version=1,
            nodes=[],
            classification="conflicting",
            relevant_node_ids=["intent:local"],
            conflict="Public upload conflicts with local storage",
            question="Should storage change?",
        )
    )
    service.submit(
        submission(
            turn_id="defer-conflict",
            graph_version=2,
            nodes=[],
            user_statement="Decide later.",
            answers=[
                {
                    "question_ref": question["evidence_ref"],
                    "quote": "Decide later.",
                    "resolution": "preserve_disagreement",
                }
            ],
        )
    )
    context = service.context(task="storage", session_id="chat:one")
    assert context["disagreements"][0]["question_ref"] == question["evidence_ref"]
    service.submit(
        submission(
            turn_id="resolve-conflict",
            graph_version=2,
            nodes=[],
            user_statement="Keep local storage.",
            answers=[
                {
                    "question_ref": question["evidence_ref"],
                    "quote": "Keep local storage.",
                    "resolution": "retain_existing",
                }
            ],
        )
    )
    assert runtime.cases()[0].status.value == "resolved"
    assert len(service.context(task="storage", session_id="chat:one")["answers"]) == 2


def test_inferred_revision_cannot_overwrite_another_authors_position(runtime):
    from intent_engineering.core.models import Graph, Node, NodeType
    from tests.helpers.readiness import apply_baseline

    apply_baseline(
        runtime,
        Graph(
            id="test",
            version=1,
            nodes=(
                Node(
                    id="intent:external",
                    type=NodeType.PRODUCT_INTENT,
                    label="Private storage",
                    status="active",
                    created_by="contributor:alice",
                    last_modified_by="contributor:alice",
                    created_at=NOW,
                    last_modified_at=NOW,
                ),
            ),
            edges=(),
        ),
    )
    with pytest.raises(ValueError):
        BackgroundIntentService(runtime, clock=lambda: NOW).submit(
            submission(
                turn_id="overwrite-alice",
                graph_version=1,
                nodes=[
                    {
                        "id": "intent:external",
                        "type": "PRODUCT_INTENT",
                        "label": "Keep all project data local.",
                        "source_mode": "explicit",
                        "confidence": 0.95,
                        "basis": "Current chat",
                        "revision_quote": "Keep all project data local.",
                    }
                ],
            )
        )
