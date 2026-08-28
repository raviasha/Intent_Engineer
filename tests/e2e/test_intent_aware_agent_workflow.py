"""Complete offline proof of the intent-aware coding-agent operating model."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import structlog

from intent_engineering.core.models import ChangeSet, Node, NodeType, SourceMode
from intent_engineering.integrations.mcp_server.intent_workflow import (
    load_intent_workflow_services,
)
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.models import ClarificationProposalSubmission
from tests.e2e.intent_aware_agent_harness import (
    CONTRIBUTOR,
    IntentAwareAgentHarness,
)
from tests.helpers.cli import run_intent

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_HOOK = ROOT / "plugins/intent-advisor/scripts/prompt-hook"
_CONVERSATION_REF = re.compile(r'conversation_ref="([^"]+)"')


@pytest.fixture
def intent_agent_harness(tmp_path: Path):
    structlog.reset_defaults()
    harness = IntentAwareAgentHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()
        structlog.reset_defaults()


def _plugin_prompt(
    harness: IntentAwareAgentHarness,
    prompt: str,
    *,
    turn_id: str,
) -> tuple[str, str]:
    event = {
        "session_id": "codex:guided-release-proof",
        "transcript_path": None,
        "cwd": str(harness.project),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5.6-sol",
        "turn_id": turn_id,
        "permission_mode": "default",
        "prompt": prompt,
    }
    environment = os.environ.copy()
    environment["PATH"] = f"{ROOT / '.venv/bin'}:/usr/bin:/bin"
    completed = subprocess.run(
        [str(PLUGIN_HOOK)],
        cwd=harness.project,
        env=environment,
        input=json.dumps(event, separators=(",", ":")).encode(),
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0
    assert completed.stderr == b""
    output = json.loads(completed.stdout)
    assert set(output) == {"hookSpecificOutput"}
    context = output["hookSpecificOutput"]["additionalContext"]
    assert type(context) is str
    return context, completed.stdout.decode()


def _conversation_ref(context: str) -> str:
    matched = _CONVERSATION_REF.search(context)
    assert matched is not None
    return matched.group(1)


def _clarification_answer_arguments(context: str, answer: str) -> dict[str, object]:
    encoded = context.partition("with ")[2].partition(" and the current")[0]
    arguments = cast(dict[str, object], json.loads(encoded))
    arguments["answer"] = answer
    return arguments


def test_guided_onboarding_plugin_and_assurance_share_one_project(
    intent_agent_harness: IntentAwareAgentHarness,
) -> None:
    harness = intent_agent_harness
    transcript: list[str] = []
    wire: list[object] = []

    offer, offer_wire = _plugin_prompt(
        harness,
        "Implement CSV export",
        turn_id="turn-1",
    )
    transcript.extend(("human: Implement CSV export", offer_wire))
    assert offer == (
        "This repository has not been onboarded into Intent Engineering. "
        "Start guided onboarding now?"
    )
    assert harness.runtime.graph_store.load().version == 0

    onboarded = run_intent(
        harness.project,
        "onboard",
        "--project",
        ".",
        "--prd",
        "docs/prd.md",
        "--yes",
        "--format",
        "json",
    )
    assert onboarded.returncode == 0, onboarded.stderr
    assert onboarded.json()["state"] == "proposal_required"
    assert onboarded.json()["next_action"] == "intent_bootstrap_propose"
    assert onboarded.json()["authorization_issued"] is False
    baseline = harness.bootstrap("docs/prd.md")
    assert baseline.core_confirmed
    assert baseline.graph_version == 1

    aligned_route, aligned_hook_wire = _plugin_prompt(
        harness,
        "Implement CSV export",
        turn_id="turn-2",
    )
    transcript.extend(("human: Implement CSV export", aligned_hook_wire))
    assert "action=classify" in aligned_route
    assert "intent_advisory_preflight" in aligned_route

    workflow = load_intent_workflow_services(harness.runtime, clock=lambda: harness._tick())
    server = build_server(
        McpReadServices(harness.runtime),
        intent_workflow_services=workflow,
    )
    context = asyncio.run(
        server.call_tool(
            "intent_context",
            {"task": "Implement CSV export", "format": "json"},
        )
    )
    assert context.structured_content is not None
    assert context.structured_content["relevant_requirements"][0]["id"] == (
        "requirement:csv-export"
    )
    aligned = asyncio.run(
        server.call_tool(
            "intent_advisory_preflight",
            {
                "conversation_ref": _conversation_ref(aligned_route),
                "request": "Implement CSV export",
                "draft": {
                    "classification": "aligned",
                    "basis": "Matches the confirmed CSV export requirement",
                    "relevant_node_ids": ["requirement:csv-export"],
                    "evidence_refs": [harness._prd_evidence_id],
                    "semantic_effects": ["Implement the confirmed CSV export requirement"],
                    "uncertainties": [],
                    "questions": [],
                    "conflict_claims": [],
                    "requested_scope": ["src/export.py", "tests/test_export.py"],
                },
            },
        )
    )
    assert aligned.structured_content is not None
    assert aligned.structured_content["classification"] == "aligned"
    assert aligned.structured_content["authorized"] is True
    (harness.project / "src").mkdir(exist_ok=True)
    (harness.project / "tests").mkdir(exist_ok=True)
    (harness.project / "src/export.py").write_text(
        "def export_csv(value: str) -> bytes:\n    return (value + '\\n').encode('utf-8')\n",
        encoding="utf-8",
    )
    (harness.project / "tests/test_export.py").write_text(
        "from export import export_csv\n\n"
        "def test_export_is_utf8() -> None:\n"
        "    assert export_csv('München').decode('utf-8') == 'München\\n'\n",
        encoding="utf-8",
    )
    test_environment = os.environ.copy()
    test_environment["PYTHONPATH"] = str(harness.project / "src")
    tested = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "tests/test_export.py",
            "-q",
        ],
        cwd=harness.project,
        env=test_environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert tested.returncode == 0, tested.stderr
    harness._git("add", "src/export.py", "tests/test_export.py")
    implementation_revision = harness._git_commit(
        "Implement confirmed CSV export",
        harness._tick(seconds=1),
    )

    ambiguous_route, ambiguous_hook_wire = _plugin_prompt(
        harness,
        "Add team sharing",
        turn_id="turn-3",
    )
    transcript.extend(("human: Add team sharing", ambiguous_hook_wire))
    ambiguous_context = asyncio.run(
        server.call_tool(
            "intent_context",
            {"task": "Add team sharing", "format": "json"},
        )
    )
    assert ambiguous_context.structured_content is not None
    ambiguous = asyncio.run(
        server.call_tool(
            "intent_advisory_preflight",
            {
                "conversation_ref": _conversation_ref(ambiguous_route),
                "request": "Add team sharing",
                "draft": {
                    "classification": "new_or_ambiguous",
                    "basis": "Active-agent classification grounded in the current intent graph",
                    "relevant_node_ids": [],
                    "evidence_refs": [],
                    "semantic_effects": [],
                    "uncertainties": [],
                    "questions": [
                        "Which roles may share an exported report?",
                        "When should a sharing grant expire?",
                    ],
                    "conflict_claims": [],
                    "requested_scope": ["src/sharing.py"],
                },
            },
        )
    )
    assert ambiguous.structured_content is not None
    assert ambiguous.structured_content["classification"] == "new_or_ambiguous"
    assert ambiguous.structured_content["questions"]
    session_payload = ambiguous.structured_content["context"]["clarification_session"]

    for turn_id, answer in (
        ("turn-4", "Workspace admins may share read-only reports."),
        ("turn-5", "Sharing grants expire after seven days."),
    ):
        answer_route, answer_hook_wire = _plugin_prompt(
            harness,
            answer,
            turn_id=turn_id,
        )
        transcript.extend((f"human: {answer}", answer_hook_wire))
        assert "action=answer_clarification" in answer_route
        answered = asyncio.run(
            server.call_tool(
                "intent_clarification_answer",
                _clarification_answer_arguments(answer_route, answer),
            )
        )
        assert answered.structured_content is not None
        assert answered.structured_content["status"] == "open"
        wire.append(answered.structured_content)

    session = harness.runtime.intent_proposals.session(session_payload["id"])
    evidence_refs = (
        session.request_evidence_ref,
        session.classification_evidence_ref,
        *(item.evidence_ref for item in session.questions),
        *(item.evidence_ref for item in session.answers),
    )
    last_answered_at = datetime.fromisoformat(
        cast(str, answered.structured_content["session"]["answers"][-1]["answered_at"])
    )
    proposal_at = last_answered_at + timedelta(microseconds=1)
    node = Node(
        id="requirement:team-sharing",
        type=NodeType.REQUIREMENT,
        label="Workspace admins may share read-only reports for seven days",
        status="proposed",
        created_by=CONTRIBUTOR,
        created_at=proposal_at,
        last_modified_by=CONTRIBUTOR,
        last_modified_at=proposal_at,
        source_mode=SourceMode.INFERRED,
        intent_fidelity_confidence=0.86,
        confidence_basis="Required questions answered by the contributor",
        last_reassessed_at=proposal_at,
        evidence_refs=evidence_refs,
    )
    changeset = ChangeSet(
        id="",
        actor=CONTRIBUTOR,
        timestamp=proposal_at,
        baseline_graph_version=session.baseline_graph_version,
        evidence_refs=evidence_refs,
        nodes_added=(node,),
        nodes_updated=(),
        nodes_superseded=(),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="validated",
    )
    proposed = asyncio.run(
        server.call_tool(
            "intent_clarification_propose",
            {
                "submission": ClarificationProposalSubmission(
                    session_id=session.id,
                    task_id=session.task_id,
                    baseline_graph_version=session.baseline_graph_version,
                    actor=CONTRIBUTOR,
                    timestamp=proposal_at,
                    evidence_refs=evidence_refs,
                    changeset=changeset,
                    core_node_ids=(node.id,),
                ).model_dump(mode="json")
            },
        )
    )
    assert proposed.structured_content is not None
    assert proposed.structured_content["status"] == "proposed"
    confirmed = asyncio.run(
        server.call_tool(
            "intent_clarification_confirm",
            {
                "proposal_id": proposed.structured_content["proposal_id"],
                "actor": CONTRIBUTOR,
                "at": (proposal_at + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z"),
                "selected_node_ids": [node.id],
            },
        )
    )
    assert confirmed.structured_content is not None
    assert confirmed.structured_content["status"] == "applied"
    assert node.id in {item.id for item in harness.runtime.graph_store.load().nodes}

    wire.extend(
        (
            context.structured_content,
            aligned.structured_content,
            ambiguous_context.structured_content,
            ambiguous.structured_content,
            proposed.structured_content,
            confirmed.structured_content,
        )
    )
    public_wire = json.dumps(wire, sort_keys=True).casefold()
    public_transcript = "\n".join(transcript).casefold()
    for public in (public_wire, public_transcript):
        assert "authorization_token" not in public
        assert "capability" not in public

    assert not (harness.project / "plugins/intent-advisor").exists()
    validated = run_intent(harness.project, "validate", "--project", ".", "--format", "json")
    first_sync = run_intent(
        harness.project,
        "sync",
        "--project",
        ".",
        "--sources",
        "markdown,git",
        "--format",
        "json",
    )
    second_sync = run_intent(
        harness.project,
        "sync",
        "--project",
        ".",
        "--sources",
        "markdown,git",
        "--format",
        "json",
    )
    drift = run_intent(
        harness.project,
        "drift",
        "--project",
        ".",
        "--format",
        "markdown",
        "--output",
        "intent-drift.md",
    )
    assert validated.returncode == 0, validated.stderr
    assert first_sync.returncode == 0, first_sync.stderr
    assert first_sync.json()["evidence_added"] > 0
    assert second_sync.returncode == 0, second_sync.stderr
    assert second_sync.json()["evidence_added"] == 0
    assert second_sync.json()["changes_applied"] == 0
    assert second_sync.json()["cases_created"] == 0
    assert drift.returncode == 0, drift.stderr
    assert (harness.project / "intent-drift.md").is_file()
    implementation_evidence = next(
        record
        for record in harness.runtime.evidence()
        if record.connector_type == "git" and record.external_version == implementation_revision
    )
    assert set(implementation_evidence.payload["changed_paths"]) >= {
        "src/export.py",
        "tests/test_export.py",
    }
    assurance_wire = "\n".join(
        result.stdout for result in (validated, first_sync, second_sync, drift)
    ).casefold()
    assert "intent-advisor" not in assurance_wire
    assert "authorization_token" not in assurance_wire
    assert "capability" not in assurance_wire
    assert harness.secret_leaks() == ()


def test_existing_repo_prd_to_agent_to_scheduled_assurance(
    intent_agent_harness: IntentAwareAgentHarness,
) -> None:
    bootstrap = intent_agent_harness.bootstrap("docs/prd.md")
    assert bootstrap.core_confirmed
    assert bootstrap.provisional_ids
    assert bootstrap.graph_version == 1
    assert bootstrap.evidence_author == "local:asha"
    assert bootstrap.evidence_version
    assert bootstrap.evidence_acl == ()
    assert bootstrap.replay_byte_stable

    teammate = intent_agent_harness.ingest_teammate_revision()
    assert teammate.authors == ("U123", "U456")
    assert teammate.versions[0] != teammate.versions[1]
    assert teammate.predecessors == (None, teammate.evidence_ids[0])
    assert teammate.graph_version == 2

    aligned = intent_agent_harness.run_aligned_task()
    assert aligned.classification == "aligned"
    assert aligned.mutation.allowed
    assert aligned.mutation.reason == "authorized"
    assert aligned.post_task.status == "recorded"
    assert aligned.post_task.claim is not None
    assert aligned.post_task.claim.requirement_refs == ("requirement:csv-export",)
    assert aligned.post_task.claim.code_evidence
    assert aligned.post_task.claim.test_evidence
    assert aligned.post_task.claim.verified_commit == aligned.final_revision
    assert aligned.graph_version == 3
    assert aligned.detached

    clarified = intent_agent_harness.clarify_new_requirement()
    assert clarified.classification == "new_or_ambiguous"
    assert clarified.question_count == 2
    assert clarified.proposal_id.startswith("proposal:sha256:")
    assert clarified.decision_id is not None
    assert clarified.graph_version == 4
    assert clarified.chronology == ("opened", "answered", "answered", "proposed", "closed")

    conflict = intent_agent_harness.review_conflicting_request()
    assert conflict.classification == "conflicting"
    assert conflict.preflight_case_id.startswith("case:preflight:")
    assert not conflict.mutation.allowed
    assert conflict.mutation.reason == "intent_preflight_required"
    assert conflict.self_review_status == "review_required"
    assert conflict.independent_review_status == "applied"
    assert conflict.review_case_id.startswith("case:sha256:")
    assert "local:ben" in conflict.reviewer_aliases
    assert "local:asha" not in conflict.reviewer_aliases
    assert conflict.graph_version == 5

    host_modes = intent_agent_harness.exercise_host_modes()
    assert host_modes.mandatory_error == "Codex mandatory mutation hook is unavailable"
    assert host_modes.disabled_reason == "plugin_disabled"
    assert host_modes.disabled_workflow_calls == 0
    assert not host_modes.plugin_directory_exists

    assurance = intent_agent_harness.scheduled_assurance()
    assert assurance.first_evidence >= 5
    assert assurance.first_cases >= 1
    assert assurance.fingerprints
    assert assurance.second_evidence == 0
    assert assurance.second_changes == 0
    assert assurance.second_cases == 0
    assert assurance.checkpoint_byte_stable

    views = intent_agent_harness.shared_views()
    assert views.valid
    assert views.cli_graph_version == views.graph_version
    assert views.mcp_graph_version == views.graph_version
    assert "requirement:csv-export" in views.context_requirement_ids

    writes = intent_agent_harness.external_write_governance(conflict.preflight_case_id)
    assert writes.missing_status == "rejected"
    assert writes.missing_reason == "approval_not_found"
    assert writes.missing_provider_calls == 0
    assert writes.changed_status == "rejected"
    assert writes.changed_reason == "target_changed"
    assert writes.changed_provider_mutations == 0
    assert writes.changed_fresh_reads == 1
    assert writes.success_status == "succeeded"
    assert writes.provider_mutations == 1
    assert writes.plan_id.startswith("write-plan:sha256:")
    assert writes.approval_id.startswith("approval:sha256:")
    assert writes.receipt_id.startswith("receipt:sha256:")
    assert writes.resulting_version == "2026-08-20T13:00:00Z"
    assert writes.evidence_author == "local:ben"
    assert writes.evidence_plan_id == writes.plan_id
    assert writes.evidence_approval_id == writes.approval_id
    assert writes.graph_version == views.graph_version + 1
    assert writes.shared_transaction_coordinator
    assert writes.all_services_hold_runtime

    final_views = intent_agent_harness.shared_views()
    assert final_views.valid
    assert final_views.graph_version == writes.graph_version
    assert final_views.cli_graph_version == final_views.graph_version
    assert final_views.mcp_graph_version == final_views.graph_version
    assert intent_agent_harness.secret_leaks() == ()
