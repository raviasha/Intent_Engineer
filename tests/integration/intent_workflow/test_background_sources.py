"""Authorized host connector reads retain provider provenance in local intent evidence."""

from datetime import UTC, datetime

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_sources import BackgroundSource, capture_source
from intent_engineering.validation import validate_project_directory
from tests.integration.intent_workflow.test_background_intent import submission


def test_source_capture_is_versioned_idempotent_and_drives_graph_without_approval(tmp_path):
    initialize_project(tmp_path)
    runtime = load_runtime(tmp_path)
    try:
        draft = BackgroundSource(
            provider="notion",
            object_id="page:privacy",
            version="rev:1",
            locator="https://notion.so/privacy",
            author="notion:alice",
            observed_at=datetime(2026, 11, 8, tzinfo=UTC),
            content="All reports must remain private.",
            source_acl=("notion:workspace-members",),
        )
        first = capture_source(runtime, draft)
        assert capture_source(runtime, draft) == first
        assert (
            capture_source(
                runtime,
                draft.model_copy(
                    update={
                        "observed_at": datetime(2026, 11, 9, tzinfo=UTC),
                    }
                ),
            )
            == first
        )
        record = runtime.evidence_store.get(first["evidence_ref"])
        assert record.author == "notion:alice"
        assert record.external_version == "rev:1"
        assert record.source_locator == "https://notion.so/privacy"
        assert record.payload["source_acl"] == ("notion:workspace-members",)
        assert record.acl == ("local",)
        BackgroundIntentService(runtime).submit(
            submission(
                nodes=[
                    {
                        "id": "requirement:private",
                        "type": "REQUIREMENT",
                        "label": "All reports must remain private.",
                        "source_mode": "explicit",
                        "confidence": 0.9,
                        "basis": "Exact source quotation",
                        "evidence_refs": [record.id],
                    }
                ]
            )
        )
        next_version = draft.model_copy(
            update={"version": "rev:2", "content": "Reports may be shared privately."}
        )
        newer = capture_source(runtime, next_version)
        assert newer["evidence_ref"] != first["evidence_ref"]
        assert runtime.evidence_store.get(first["evidence_ref"]) == record
        BackgroundIntentService(runtime).submit(
            submission(
                turn_id="turn:source-update",
                graph_version=1,
                user_statement="Refresh intent from the selected source.",
                nodes=[
                    {
                        "id": "requirement:private",
                        "type": "REQUIREMENT",
                        "label": next_version.content,
                        "source_mode": "explicit",
                        "confidence": 0.9,
                        "basis": "New revision from the same source and author",
                        "revision_quote": next_version.content,
                        "evidence_refs": [newer["evidence_ref"]],
                    }
                ],
            )
        )
        updated = runtime.graph_store.load().nodes[0]
        assert updated.label == next_version.content
        assert record.id in updated.evidence_refs
        assert newer["evidence_ref"] in updated.evidence_refs
        assert runtime.intent_proposals.list() == ()
        assert validate_project_directory(runtime.project_directory).valid
    finally:
        runtime.close()


def test_source_revision_by_another_author_requires_clarification(tmp_path):
    initialize_project(tmp_path)
    runtime = load_runtime(tmp_path)
    try:
        draft = BackgroundSource(
            provider="notion",
            object_id="page:privacy",
            version="1",
            author="notion:alice",
            locator="https://notion.so/privacy",
            observed_at=datetime(2026, 11, 8, tzinfo=UTC),
            content="Private reports",
        )
        first = capture_source(runtime, draft)
        BackgroundIntentService(runtime).submit(
            submission(
                nodes=[
                    {
                        "id": "requirement:private",
                        "type": "REQUIREMENT",
                        "label": draft.content,
                        "source_mode": "explicit",
                        "confidence": 0.9,
                        "basis": "Source quotation",
                        "evidence_refs": [first["evidence_ref"]],
                    }
                ]
            )
        )
        changed = capture_source(
            runtime,
            draft.model_copy(
                update={
                    "version": "2",
                    "author": "notion:bob",
                    "content": "Public reports",
                }
            ),
        )
        with pytest.raises(ValueError, match="revision"):
            BackgroundIntentService(runtime).submit(
                submission(
                    turn_id="turn:source-update",
                    graph_version=1,
                    user_statement="Refresh intent from the selected source.",
                    nodes=[
                        {
                            "id": "requirement:private",
                            "type": "REQUIREMENT",
                            "label": "Public reports",
                            "source_mode": "explicit",
                            "confidence": 0.9,
                            "basis": "New source version",
                            "revision_quote": "Public reports",
                            "evidence_refs": [changed["evidence_ref"]],
                        }
                    ],
                )
            )
        assert runtime.graph_store.load().nodes[0].label == "Private reports"
    finally:
        runtime.close()


def test_changed_content_cannot_reuse_a_provider_revision(tmp_path):
    initialize_project(tmp_path)
    runtime = load_runtime(tmp_path)
    try:
        draft = BackgroundSource(
            provider="slack",
            object_id="workspace:channel:message",
            version="1",
            locator="https://slack.com/message",
            observed_at=datetime(2026, 11, 8, tzinfo=UTC),
            content="Private reports",
        )
        capture_source(runtime, draft)
        before = runtime.evidence_store.ledger("background:slack")
        with pytest.raises(ValueError):
            capture_source(runtime, draft.model_copy(update={"content": "Public reports"}))
        assert runtime.evidence_store.ledger("background:slack") == before
        assert validate_project_directory(runtime.project_directory).valid
    finally:
        runtime.close()


def test_source_content_tampering_is_detected(tmp_path):
    initialize_project(tmp_path)
    runtime = load_runtime(tmp_path)
    try:
        draft = BackgroundSource(
            provider="confluence",
            object_id="site:page",
            version="1",
            locator="https://wiki.example/page",
            content="Private reports",
            observed_at=datetime(2026, 11, 8, tzinfo=UTC),
        )
        capture_source(runtime, draft)
    finally:
        runtime.close()
    evidence = tmp_path / ".intent" / "evidence" / "evidence.jsonl"
    evidence.write_text(evidence.read_text().replace("Private reports", "Changed reports"))
    runtime = load_runtime(tmp_path)
    try:
        report = validate_project_directory(runtime.project_directory)
        assert any(d.code == "evidence.content_hash_mismatch" for d in report.diagnostics)
    finally:
        runtime.close()
