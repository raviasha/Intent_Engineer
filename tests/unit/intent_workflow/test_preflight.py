"""Unit contracts for attributed conversation capture and preflight submissions."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import TaskClassification
from intent_engineering.intent_workflow.preflight import AgentClassificationSubmission
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _submission(**updates: object) -> AgentClassificationSubmission:
    values: dict[str, object] = {
        "task_id": "task:sha256:" + "a" * 64,
        "task_digest": "sha256:" + "a" * 64,
        "graph_version": 3,
        "classification": TaskClassification.ALIGNED,
        "basis": "The request implements the active export requirement.",
        "relevant_node_ids": ("req-export",),
        "evidence_refs": ("ev-requirement",),
        "agent_evidence_ref": "evidence:conversation:" + "b" * 64,
        "requested_scope": ("src/export.py",),
    }
    values.update(updates)
    return AgentClassificationSubmission.model_validate(values)


def test_agent_submission_is_strict_frozen_bounded_and_canonical() -> None:
    """Fails if untrusted classifier material can remain mutable, duplicated, or unordered."""
    submission = _submission(
        relevant_node_ids=("req-z", "req-a"),
        evidence_refs=("ev-z", "ev-a"),
        semantic_effects=("Adds export behavior", "Changes report behavior"),
        questions=("Which report?", "What encoding?"),
        requested_scope=("tests/test_export.py", "src/export.py"),
    )

    assert submission.relevant_node_ids == ("req-a", "req-z")
    assert submission.evidence_refs == ("ev-a", "ev-z")
    assert submission.requested_scope == ("src/export.py", "tests/test_export.py")
    with pytest.raises(ValidationError):
        _submission(relevant_node_ids=("req-export", "req-export"))
    with pytest.raises(ValidationError):
        _submission(questions=("x" * 4097,))
    with pytest.raises(ValidationError):
        _submission(relevant_node_ids=tuple(f"req-{index}" for index in range(257)))
    with pytest.raises(ValidationError):
        _submission(questions=tuple(f"question-{index}" for index in range(65)))
    with pytest.raises(ValidationError):
        AgentClassificationSubmission.model_validate(
            {**submission.model_dump(), "unexpected": True}
        )
    with pytest.raises(ValidationError):
        submission.basis = "changed"  # type: ignore[misc]


def test_agent_submission_detaches_input_collections() -> None:
    """Fails if caller-owned collections can change a validated classification."""
    nodes = ["req-export"]
    with pytest.raises(ValidationError):
        _submission(relevant_node_ids=nodes)

    submission = _submission(relevant_node_ids=tuple(nodes))
    nodes[0] = "req-foreign"

    assert submission.relevant_node_ids == ("req-export",)


def test_conversation_capture_records_exact_authors_and_predecessor_chain(
    tmp_path: Path,
) -> None:
    """Fails if human and agent turns collapse authorship or fork one conversation history."""
    store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")
    capture = ConversationCapture(store, connector_id="conversation:codex")
    human = capture.record_turn(
        conversation_ref="codex:thread-1",
        role="human",
        author="local:asha",
        content="Add CSV export",
        captured_at=NOW,
        acl=("local:asha",),
    )
    agent = capture.record_turn(
        conversation_ref="codex:thread-1",
        role="agent",
        author="agent:codex",
        content={"classification": "aligned", "basis": "Matches req-export"},
        captured_at=NOW + timedelta(microseconds=1),
        acl=("local:asha",),
    )

    chain = store.chain("conversation:codex", "conversation", "codex:thread-1")
    assert [entry.evidence.author for entry in chain] == ["local:asha", "agent:codex"]
    assert [entry.predecessor_id for entry in chain] == [None, human.id]
    assert tuple(entry.evidence.id for entry in chain) == (human.id, agent.id)
    before = (tmp_path / "evidence.jsonl").read_bytes()
    assert capture.record_turn(
        conversation_ref="codex:thread-1",
        role="agent",
        author="agent:codex",
        content={"basis": "Matches req-export", "classification": "aligned"},
        captured_at=NOW + timedelta(microseconds=1),
        acl=("local:asha",),
    ) == agent
    assert (tmp_path / "evidence.jsonl").read_bytes() == before


def test_concurrent_conversation_appends_form_one_unforked_chain(tmp_path: Path) -> None:
    """Fails if concurrent different turns can share a predecessor or overwrite evidence."""
    store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")
    capture = ConversationCapture(store, connector_id="conversation:codex")
    capture.record_turn(
        conversation_ref="codex:thread-race",
        role="human",
        author="local:asha",
        content="Start",
        captured_at=NOW,
        acl=("local:asha",),
    )
    barrier = threading.Barrier(2)

    def append(index: int) -> None:
        barrier.wait(timeout=5)
        capture.record_turn(
            conversation_ref="codex:thread-race",
            role="agent",
            author="agent:codex",
            content={"basis": f"classification-{index}"},
            captured_at=NOW + timedelta(microseconds=index + 1),
            acl=("local:asha",),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(append, index) for index in range(2)]
        for future in futures:
            future.result(timeout=5)

    chain = store.chain("conversation:codex", "conversation", "codex:thread-race")
    assert len(chain) == 3
    assert chain[0].predecessor_id is None
    assert tuple(entry.predecessor_id for entry in chain[1:]) == (
        chain[0].evidence.id,
        chain[1].evidence.id,
    )
    assert len({entry.evidence.id for entry in chain}) == 3


def test_concurrent_identical_turn_replay_is_one_evidence_version(tmp_path: Path) -> None:
    """Fails if simultaneous replay duplicates one immutable conversation turn."""
    store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")
    capture = ConversationCapture(store, connector_id="conversation:codex")
    barrier = threading.Barrier(2)

    def append() -> str:
        barrier.wait(timeout=5)
        return capture.record_turn(
            conversation_ref="codex:thread-identical",
            role="human",
            author="local:asha",
            content="Same request",
            captured_at=NOW,
            acl=("local:asha",),
        ).id

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(append), executor.submit(append))
        identifiers = tuple(future.result(timeout=5) for future in futures)

    chain = store.chain("conversation:codex", "conversation", "codex:thread-identical")
    assert identifiers[0] == identifiers[1]
    assert len(chain) == 1
    assert chain[0].predecessor_id is None


@pytest.mark.parametrize(
    "timestamp",
    [NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))],
)
def test_conversation_capture_requires_exact_utc(timestamp: datetime, tmp_path: Path) -> None:
    """Fails if local or naive times can make conversation ordering environment-dependent."""
    capture = ConversationCapture(JsonlEvidenceStore(tmp_path / "evidence.jsonl"))

    with pytest.raises(ValueError, match="conversation capture unavailable"):
        capture.record_turn(
            conversation_ref="codex:thread-1",
            role="human",
            author="local:asha",
            content="request",
            captured_at=timestamp,
            acl=("local:asha",),
        )
