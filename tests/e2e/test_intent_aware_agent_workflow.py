"""Complete offline proof of the intent-aware coding-agent operating model."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import structlog
import yaml  # type: ignore[import-untyped]
from structlog.testing import capture_logs

from intent_engineering.capture.git.connector import GitConnector
from intent_engineering.cli.writes import MutationPolicy
from intent_engineering.core.models import ChangeSet, Node, NodeType, SourceMode
from intent_engineering.integrations.mcp_server.intent_workflow import (
    load_intent_workflow_services,
)
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.authorization import AuthorizationIssuer
from intent_engineering.intent_workflow.models import (
    ClarificationProposalSubmission,
    PreflightResult,
    ProposalDecisionV2,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.post_task import PostTaskSubmission
from tests.e2e.intent_aware_agent_harness import (
    GuidedOnboardingHarness,
    IntentAwareAgentHarness,
)
from tests.helpers.cli import run_intent

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_HOOK = ROOT / "plugins/intent-advisor/scripts/prompt-hook"
_CONVERSATION_REF = re.compile(r'conversation_ref="([^"]+)"')
_REQUEST_EVIDENCE_REF = re.compile(r'request_evidence_ref="([^"]+)"')


@pytest.fixture
def intent_agent_harness(tmp_path: Path):
    structlog.reset_defaults()
    harness = IntentAwareAgentHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()
        structlog.reset_defaults()


@pytest.fixture
def guided_onboarding_harness(tmp_path: Path):
    structlog.reset_defaults()
    harness = GuidedOnboardingHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()
        structlog.reset_defaults()


def _plugin_prompt(
    project: Path,
    prompt: str,
    *,
    turn_id: str,
) -> tuple[str, str, str]:
    event = {
        "session_id": "codex:guided-release-proof",
        "transcript_path": None,
        "cwd": str(project),
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
        cwd=project,
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
    return context, completed.stdout.decode(), completed.stderr.decode()


def _conversation_ref(context: str) -> str:
    matched = _CONVERSATION_REF.search(context)
    assert matched is not None
    return matched.group(1)


def _request_evidence_ref(context: str) -> str:
    matched = _REQUEST_EVIDENCE_REF.search(context)
    assert matched is not None
    return matched.group(1)


def _clarification_answer_arguments(context: str) -> dict[str, object]:
    encoded = context.partition("with ")[2].partition(". The hook")[0]
    return cast(dict[str, object], json.loads(encoded))


def test_guided_onboarding_plugin_and_assurance_share_one_project(
    guided_onboarding_harness: GuidedOnboardingHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = guided_onboarding_harness
    project = harness.project
    transcript: list[object] = []
    mcp_wire: list[object] = []
    captured_outputs: list[object] = []
    authorization_sentinel = "intent-advisory-capability-sentinel-round-1"
    test_credential = "intent-test-credential-sentinel-round-1"
    test_credential_name = "INTENT_RELEASE_TEST_CREDENTIAL"

    assert not (project / ".intent").exists()
    assert not (project / ".intent/graph.yaml").exists()
    assert not (project / ".intent/evidence/evidence.jsonl").exists()
    offer, offer_stdout, offer_stderr = _plugin_prompt(
        project,
        "Implement CSV export",
        turn_id="turn-1",
    )
    transcript.extend(("human: Implement CSV export", offer_stdout, offer_stderr))
    assert offer == (
        "This repository has not been onboarded into Intent Engineering. "
        "Start guided onboarding now?"
    )
    assert offer_stderr == ""
    assert not (project / ".intent").exists()

    onboarded = run_intent(
        project,
        "onboard",
        "--project",
        ".",
        "--prd",
        "docs/prd.md",
        "--yes",
        "--format",
        "json",
    )
    captured_outputs.extend((onboarded.stdout, onboarded.stderr))
    assert onboarded.returncode == 0, onboarded.stderr
    assert onboarded.json()["state"] == "proposal_required"
    assert onboarded.json()["next_action"] == "intent_bootstrap_propose"
    assert onboarded.json()["authorization_issued"] is False
    assert (project / ".intent").is_dir()

    runtime = harness.bind_runtime()
    assert harness.runtime_loads == 1
    actor = runtime.config.local_actor
    assert MutationPolicy.model_validate(
        yaml.safe_load((project / ".intent/approvals/policy.yaml").read_bytes())
    ) == MutationPolicy.model_validate(
        {
            "contributors": [actor],
            "approvers": [actor],
            "executors": [actor],
            "identities": {actor: [actor]},
        }
    )
    (project / ".intent/repository.id").write_text(
        f"{runtime.config.project_id}\n",
        encoding="utf-8",
    )
    assert runtime.graph_store.load().version == 0
    assert runtime.intent_proposals.list() == ()
    evidence = runtime.evidence()
    assert len(evidence) == 1
    assert evidence[0].source_locator == "docs/prd.md"

    workflow = load_intent_workflow_services(runtime, clock=lambda: harness.tick())
    assert workflow.runtime is runtime
    assert workflow.runtime.transactions is runtime.transactions
    plugin_mcp = json.loads((ROOT / "plugins/intent-advisor/.mcp.json").read_bytes())
    assert plugin_mcp["mcpServers"]["intent_advisor"] == {
        "command": "intent",
        "args": ["mcp", "--project", "."],
    }

    class AdvisoryIssuerSpy:
        def __init__(self) -> None:
            self.issue_calls = 0
            self.revoke_calls = 0

        def issue(self, *_args: object, **_kwargs: object) -> str:
            self.issue_calls += 1
            return authorization_sentinel

        def revoke(self, _token: str) -> None:
            self.revoke_calls += 1

    advisory_issuer = AdvisoryIssuerSpy()
    monkeypatch.setattr(workflow, "_issuer", cast(AuthorizationIssuer, advisory_issuer))
    server = build_server(
        McpReadServices(runtime),
        intent_workflow_services=workflow,
    )

    def call_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
        result = asyncio.run(server.call_tool(name, arguments))
        assert result.structured_content is not None
        payload = cast(dict[str, object], result.structured_content)
        mcp_wire.append(result.model_dump(mode="json", by_alias=True))
        return payload

    bootstrap = harness.bootstrap_submission(evidence[0].id)
    proposed = call_tool(
        "intent_bootstrap_propose",
        {"submission": bootstrap.model_dump(mode="json")},
    )
    assert proposed["status"] == "proposed"
    shown = call_tool(
        "intent_proposal_show",
        {"proposal_id": proposed["proposal_id"]},
    )
    preview = cast(dict[str, object], shown["proposal"])
    assert preview["proposal_id"] == proposed["proposal_id"]
    assert preview["proposal_digest"] == proposed["proposal_digest"]
    assert preview["core_node_ids"] == list(bootstrap.core_node_ids)
    assert preview["provisional_node_ids"] == []
    assert preview["evidence_refs"] == [evidence[0].id]
    confirmed = call_tool(
        "intent_proposal_confirm",
        {
            "proposal_id": proposed["proposal_id"],
            "proposal_digest": preview["proposal_digest"],
            "confirmed_node_ids": preview["core_node_ids"],
        },
    )
    assert confirmed["status"] == "activated"
    assert confirmed["graph_version"] == 1
    decision = runtime.intent_proposals.decision_for(cast(str, proposed["proposal_id"]))
    assert isinstance(decision, ProposalDecisionV2)
    assert decision.actor == runtime.config.local_actor
    assert decision.actor_aliases == (runtime.config.local_actor,)
    assert decision.proposal_digest == preview["proposal_digest"]
    assert decision.confirmed_node_ids == bootstrap.core_node_ids
    assert runtime.graph_store.load().version == 1

    aligned_route, aligned_hook_stdout, aligned_hook_stderr = _plugin_prompt(
        project,
        "Implement CSV export",
        turn_id="turn-2",
    )
    transcript.extend(
        (
            "human: Implement CSV export",
            aligned_hook_stdout,
            aligned_hook_stderr,
        )
    )
    assert "action=classify" in aligned_route
    assert "intent_advisory_preflight" in aligned_route

    aligned = call_tool(
        "intent_advisory_preflight",
        {
            "conversation_ref": _conversation_ref(aligned_route),
            "request_evidence_ref": _request_evidence_ref(aligned_route),
            "draft": {
                "classification": "aligned",
                "basis": "Matches the confirmed CSV export requirement",
                "relevant_node_ids": ["requirement:csv-export"],
                "evidence_refs": [evidence[0].id],
                "semantic_effects": ["Implement the confirmed CSV export requirement"],
                "uncertainties": [],
                "questions": [],
                "conflict_claims": [],
                "requested_scope": ["src/export.py", "tests/test_export.py"],
            },
        },
    )
    assert aligned["classification"] == "aligned"
    assert aligned["authorized"] is True
    assert advisory_issuer.issue_calls == advisory_issuer.revoke_calls == 0

    human_request = next(
        record
        for record in runtime.evidence()
        if record.connector_type == "conversation"
        and record.external_object_id == _conversation_ref(aligned_route)
        and record.payload.get("role") == "human"
        and record.payload.get("content") == "Implement CSV export"
    )
    envelope = TaskEnvelope(
        repository_id=runtime.config.project_id,
        actor=actor,
        conversation_ref=_conversation_ref(aligned_route),
        request="Implement CSV export",
        request_evidence_ref=human_request.id,
        graph_version=1,
        created_at=human_request.observed_at,
        requested_scope=("src/export.py", "tests/test_export.py"),
    )
    preflight = PreflightResult.model_validate_json(json.dumps(aligned))
    assert envelope.id == preflight.task_id
    post_issuer = AuthorizationIssuer()
    post_issued_at = max(harness.tick(), human_request.observed_at)
    post_token = post_issuer.issue(
        envelope,
        preflight,
        graph_content=(project / ".intent/graph.yaml").read_bytes(),
        now=post_issued_at,
    )

    base_revision = harness.git("rev-parse", "HEAD")
    (project / "src").mkdir(exist_ok=True)
    (project / "tests").mkdir(exist_ok=True)
    (project / "src/export.py").write_text(
        "def export_csv(value: str) -> bytes:\n    return (value + '\\n').encode('utf-8')\n",
        encoding="utf-8",
    )
    (project / "tests/test_export.py").write_text(
        "import os\n\n"
        "from export import export_csv\n\n"
        "def test_export_is_utf8() -> None:\n"
        f"    assert os.environ['{test_credential_name}']\n"
        "    assert export_csv('München').decode('utf-8') == 'München\\n'\n",
        encoding="utf-8",
    )
    assert test_credential_name not in os.environ
    test_environment = os.environ.copy()
    test_environment["PYTHONPATH"] = str(project / "src")
    test_environment[test_credential_name] = test_credential
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
        cwd=project,
        env=test_environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert tested.returncode == 0, tested.stderr
    test_environment.pop(test_credential_name)
    assert test_credential_name not in os.environ
    captured_outputs.extend((tested.stdout, tested.stderr))
    harness.git("add", "src/export.py", "tests/test_export.py")
    implementation_at = post_issued_at + timedelta(seconds=1)
    implementation_revision = harness.git_commit(
        "Implement confirmed CSV export",
        implementation_at,
    )

    git_connector = GitConnector(project, repository_id=runtime.config.project_id)
    with capture_logs() as structured_logs:
        discovered = asyncio.run(git_connector.discover(None))
        implementation_source = next(
            source for source in discovered if source.external_version == implementation_revision
        )
        raw_implementation = asyncio.run(
            git_connector.fetch(
                implementation_source.external_object_id,
                implementation_source.external_version,
            )
        )
        implementation_evidence = git_connector.normalize(raw_implementation)
        runtime.evidence_store.associate(
            git_connector.connector_id,
            implementation_evidence,
        )
    captured_outputs.append(implementation_evidence.model_dump(mode="json"))
    test_record = harness.test_evidence(
        implementation_revision,
        post_issued_at + timedelta(seconds=2),
    )
    runtime.evidence_store.associate("test-results", test_record)
    completed_at = post_issued_at + timedelta(seconds=3)
    verification = post_issuer.verify(
        post_token,
        actor=actor,
        repository_id=runtime.config.project_id,
        task_id=envelope.id,
        graph_version=1,
        graph_content=(project / ".intent/graph.yaml").read_bytes(),
        requested_paths=("src/export.py", "tests/test_export.py"),
        now=completed_at,
        request_digest=("sha256:" + hashlib.sha256(envelope.request.encode("utf-8")).hexdigest()),
    )
    assert verification.authorized, verification
    harness_now = harness.tick()
    if harness_now < completed_at:
        harness.tick(
            seconds=int((completed_at - harness_now).total_seconds()) + 1,
            microseconds=0,
        )
    post_task = harness.post_task_service(post_issuer).evaluate(
        PostTaskSubmission(
            repository_id=runtime.config.project_id,
            task_id=envelope.id,
            actor=actor,
            request_digest=(
                "sha256:" + hashlib.sha256(envelope.request.encode("utf-8")).hexdigest()
            ),
            graph_version=1,
            base_revision=base_revision,
            final_revision=implementation_revision,
            changed_paths=("src/export.py", "tests/test_export.py"),
            requirement_ids=("requirement:csv-export",),
            code_refs=("file:csv-export",),
            test_refs=("test:csv-export",),
            git_evidence_refs=(implementation_evidence.id,),
            test_evidence_refs=(test_record.id,),
            completed_at=completed_at,
        ),
        token=post_token,
    )
    captured_outputs.append(post_task.model_dump(mode="json"))
    assert post_task.status == "recorded", post_task
    assert post_task.claim is not None
    assert post_task.claim.requirement_refs == ("requirement:csv-export",)
    assert post_task.claim.code_evidence == (implementation_evidence.id,)
    assert post_task.claim.test_evidence == (test_record.id,)
    assert post_task.claim.verified_commit == implementation_revision
    assert runtime.graph_store.load().version == 2

    ambiguous_route, ambiguous_hook_stdout, ambiguous_hook_stderr = _plugin_prompt(
        project,
        "Add team sharing",
        turn_id="turn-3",
    )
    transcript.extend(("human: Add team sharing", ambiguous_hook_stdout, ambiguous_hook_stderr))
    ambiguous = call_tool(
        "intent_advisory_preflight",
        {
            "conversation_ref": _conversation_ref(ambiguous_route),
            "request_evidence_ref": _request_evidence_ref(ambiguous_route),
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
    assert ambiguous["classification"] == "new_or_ambiguous"
    assert ambiguous["questions"]
    session_payload = cast(dict[str, object], ambiguous["context"])["clarification_session"]

    for turn_id, answer in (
        ("turn-4", "Workspace admins may share read-only reports."),
        ("turn-5", "Sharing grants expire after seven days."),
    ):
        answer_route, answer_hook_stdout, answer_hook_stderr = _plugin_prompt(
            project,
            answer,
            turn_id=turn_id,
        )
        transcript.extend((f"human: {answer}", answer_hook_stdout, answer_hook_stderr))
        assert "action=answer_clarification" in answer_route
        answered = call_tool(
            "intent_clarification_answer",
            _clarification_answer_arguments(answer_route),
        )
        assert answered["status"] == "open"

    session = runtime.intent_proposals.session(cast(dict[str, object], session_payload)["id"])
    evidence_refs = (
        session.request_evidence_ref,
        session.classification_evidence_ref,
        *(item.evidence_ref for item in session.questions),
        *(item.evidence_ref for item in session.answers),
    )
    last_answered_at = datetime.fromisoformat(
        cast(
            str,
            cast(dict[str, object], answered["session"])["answers"][-1]["answered_at"],  # type: ignore[index]
        )
    )
    proposal_at = last_answered_at + timedelta(microseconds=1)
    node = Node(
        id="requirement:team-sharing",
        type=NodeType.REQUIREMENT,
        label="Workspace admins may share read-only reports for seven days",
        status="proposed",
        created_by=actor,
        created_at=proposal_at,
        last_modified_by=actor,
        last_modified_at=proposal_at,
        source_mode=SourceMode.INFERRED,
        intent_fidelity_confidence=0.86,
        confidence_basis="Required questions answered by the contributor",
        last_reassessed_at=proposal_at,
        evidence_refs=evidence_refs,
    )
    changeset = ChangeSet(
        id="",
        actor=actor,
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
    clarification_proposed = call_tool(
        "intent_clarification_propose",
        {
            "submission": ClarificationProposalSubmission(
                session_id=session.id,
                task_id=session.task_id,
                baseline_graph_version=session.baseline_graph_version,
                actor=actor,
                timestamp=proposal_at,
                evidence_refs=evidence_refs,
                changeset=changeset,
                core_node_ids=(node.id,),
            ).model_dump(mode="json")
        },
    )
    assert clarification_proposed["status"] == "proposed"
    review_route, review_hook_stdout, review_hook_stderr = _plugin_prompt(
        project,
        f"confirm {clarification_proposed['proposal_digest']}",
        turn_id="turn-6",
    )
    transcript.extend(
        (
            f"human: confirm {clarification_proposed['proposal_digest']}",
            review_hook_stdout,
            review_hook_stderr,
        )
    )
    assert "action=review_clarification_proposal" in review_route
    assert "intent_clarification_show" in review_route
    clarification_shown = call_tool(
        "intent_clarification_show",
        {"proposal_id": clarification_proposed["proposal_id"]},
    )
    clarification_preview = cast(dict[str, object], clarification_shown["proposal"])
    assert clarification_preview["proposal_digest"] == clarification_proposed["proposal_digest"]
    assert clarification_preview["core_node_ids"] == [node.id]
    assert clarification_preview["evidence_refs"] == list(evidence_refs)
    clarification_confirmed = call_tool(
        "intent_clarification_confirm",
        {
            "proposal_id": clarification_proposed["proposal_id"],
            "proposal_digest": clarification_preview["proposal_digest"],
            "actor": actor,
            "at": (proposal_at + timedelta(microseconds=1)).isoformat().replace("+00:00", "Z"),
            "selected_node_ids": [node.id],
        },
    )
    assert clarification_confirmed["status"] == "applied"
    assert node.id in {item.id for item in runtime.graph_store.load().nodes}

    assert not (project / "plugins/intent-advisor").exists()
    baseline_status = run_intent(
        project,
        "status",
        "--project",
        ".",
        "--format",
        "json",
        "--require-baseline",
    )
    validated = run_intent(project, "validate", "--project", ".", "--format", "json")
    first_sync = run_intent(
        project,
        "sync",
        "--project",
        ".",
        "--sources",
        "markdown,git",
        "--format",
        "json",
    )
    second_sync = run_intent(
        project,
        "sync",
        "--project",
        ".",
        "--sources",
        "markdown,git",
        "--format",
        "json",
    )
    drift = run_intent(
        project,
        "drift",
        "--project",
        ".",
        "--format",
        "markdown",
        "--output",
        "intent-drift.md",
    )
    for result in (baseline_status, validated, first_sync, second_sync, drift):
        captured_outputs.extend((result.stdout, result.stderr))
    assert baseline_status.returncode == 0, baseline_status.stderr
    assert baseline_status.json()["status"] == "ready"
    assert validated.returncode == 0, validated.stderr
    assert first_sync.returncode == 0, first_sync.stderr
    assert second_sync.returncode == 0, second_sync.stderr
    assert second_sync.json()["evidence_added"] == 0
    assert second_sync.json()["changes_applied"] == 0
    assert second_sync.json()["cases_created"] == 0
    assert drift.returncode == 0, drift.stderr
    assert (project / "intent-drift.md").is_file()
    assert {implementation_evidence.id, test_record.id}.issubset(
        {record.id for record in runtime.evidence()}
    )
    assert set(implementation_evidence.payload["changed_paths"]) >= {
        "src/export.py",
        "tests/test_export.py",
    }
    assurance_wire = "\n".join(
        result.stdout for result in (baseline_status, validated, first_sync, second_sync, drift)
    ).casefold()
    assert "intent-advisor" not in assurance_wire
    assert "authorization_token" not in assurance_wire
    assert "capability" not in assurance_wire
    assert advisory_issuer.issue_calls == advisory_issuer.revoke_calls == 0
    assert harness.runtime_loads == 1
    assert test_credential_name not in os.environ

    public_wire = json.dumps(mcp_wire, sort_keys=True).casefold()
    complete_transcript = json.dumps(
        {
            "hook_transcript": transcript,
            "mcp_wire": mcp_wire,
            "captured_outputs": captured_outputs,
            "structured_logs": structured_logs,
        },
        default=str,
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "authorization_token" not in public_wire
    assert "capability" not in public_wire
    assert "authorization_token" not in complete_transcript.casefold()
    assert "capability" not in complete_transcript.casefold()
    for secret in (authorization_sentinel, post_token, test_credential):
        assert secret not in complete_transcript
    for path in project.rglob("*"):
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            continue
        content = path.read_bytes()
        for secret in (authorization_sentinel, post_token, test_credential):
            assert secret.encode() not in content, path


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
