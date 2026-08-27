"""Official-SDK contracts for reviewed intent-workflow onboarding tools."""

from __future__ import annotations

import json
import logging
import multiprocessing
from collections.abc import ItemsView, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Never

import anyio
import pytest
import yaml  # type: ignore[import-untyped]
from mcp.server.mcpserver.exceptions import ToolError

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server.intent_workflow import (
    AuthorizationVerifyRequest,
    validate_intent_workflow_call,
)
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.authorization import AuthorizationVerification
from intent_engineering.intent_workflow.bootstrap import BootstrapSubmission
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import TaskClassification, TaskEnvelope
from intent_engineering.intent_workflow.preflight import (
    AgentClassificationSubmission,
    classification_evidence_content,
)
from tests.e2e.test_cli_intent_bootstrap import _configured_project

pytestmark = pytest.mark.anyio

_READ_TOOLS = {
    "intent_context",
    "intent_explain",
    "intent_impact",
    "intent_drift",
    "intent_status",
    "intent_validate",
    "intent_reconcile_list",
    "intent_reconcile_show",
}
_WORKFLOW_TOOLS = {
    "intent_bootstrap_propose",
    "intent_proposal_show",
    "intent_proposal_confirm",
    "intent_preflight",
    "intent_authorization_verify",
}


@dataclass
class _FakeWorkflow:
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def bootstrap_propose(self, submission: BootstrapSubmission) -> dict[str, object]:
        self.calls.append(("propose", submission))
        return {
            "schema_version": "1",
            "status": "proposed",
            "proposal_id": "proposal:sha256:" + "1" * 64,
        }

    async def proposal_show(self, proposal_id: object) -> dict[str, object]:
        self.calls.append(("show", proposal_id))
        return {
            "schema_version": "1",
            "status": "proposed",
            "proposal_id": proposal_id,
        }

    async def proposal_confirm(
        self,
        proposal_id: object,
        proposal_digest: object,
        confirmed_node_ids: object,
    ) -> dict[str, object]:
        self.calls.append(
            ("confirm", (proposal_id, proposal_digest, confirmed_node_ids))
        )
        return {
            "schema_version": "1",
            "status": "activated",
            "graph_version": 1,
        }

    async def preflight(
        self,
        envelope: TaskEnvelope,
        submission: AgentClassificationSubmission,
    ) -> dict[str, object]:
        self.calls.append(("preflight", (envelope, submission)))
        return {
            "schema_version": 1,
            "task_id": envelope.id,
            "graph_version": envelope.graph_version,
            "classification": submission.classification.value,
            "authorized": True,
            "basis": "validated",
            "relevant_node_ids": [],
            "evidence_refs": [],
            "questions": [],
            "review_case_id": None,
            "permitted_scope": list(envelope.requested_scope),
            "context": {},
            "authorization_token": "opaque-token",
        }

    async def authorization_verify(
        self,
        token: str,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        requested_paths: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append(
            (
                "verify",
                (token, actor, repository_id, task_id, graph_version, requested_paths),
            )
        )
        return {
            "schema_version": 1,
            "authorized": True,
            "classification": "no_semantic_impact",
            "relevant_node_ids": [],
            "expires_at": "2026-08-26T12:05:00Z",
        }


class _CancellationSignal(BaseException):
    pass


@dataclass
class _FailingWorkflow(_FakeWorkflow):
    failure: BaseException | None = None

    async def bootstrap_propose(self, submission: object) -> dict[str, object]:
        del submission
        if self.failure is not None:
            raise self.failure
        raise RuntimeError("PRIVATE-WORKFLOW-HANDLER")

    async def authorization_verify(
        self,
        token: str,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        requested_paths: tuple[str, ...],
    ) -> dict[str, object]:
        del token, actor, repository_id, task_id, graph_version, requested_paths
        if self.failure is not None:
            raise self.failure
        raise RuntimeError("PRIVATE-WORKFLOW-HANDLER")


def _services(tmp_path: Path) -> McpReadServices:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    initialize_project(project)
    return McpReadServices(load_runtime(project))


def _raw_mechanical_preflight() -> dict[str, object]:
    envelope = TaskEnvelope(
        repository_id="project",
        actor="local",
        conversation_ref="codex:strict-input",
        request="Format README",
        request_evidence_ref="evidence:conversation:" + "3" * 64,
        graph_version=0,
        created_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        requested_scope=("README.md",),
    )
    submission = AgentClassificationSubmission(
        task_id=envelope.id,
        task_digest=envelope.digest,
        graph_version=0,
        classification=TaskClassification.NO_SEMANTIC_IMPACT,
        basis="Formatting only",
        agent_evidence_ref="evidence:conversation:" + "4" * 64,
        requested_scope=("README.md",),
    )
    return {
        "envelope": envelope.model_dump(mode="json"),
        "submission": submission.model_dump(mode="json"),
    }


def _valid_raw_workflow_arguments(tool_name: str) -> dict[str, object]:
    if tool_name == "intent_bootstrap_propose":
        return {
            "submission": {
                "schema_version": 1,
                "baseline_graph_version": 0,
                "actor": "agent:codex",
                "timestamp": "2026-08-26T12:00:00Z",
                "evidence_refs": [],
                "source_roles": [],
                "candidate_nodes": [],
                "candidate_edges": [],
                "core_node_ids": [],
                "provisional_node_ids": [],
                "assumptions": [],
                "unanswered_questions": [],
                "conflicting_authors": [],
                "destructive": False,
            }
        }
    if tool_name == "intent_proposal_show":
        return {"proposal_id": "proposal:sha256:" + "1" * 64}
    if tool_name == "intent_proposal_confirm":
        return {
            "proposal_id": "proposal:sha256:" + "1" * 64,
            "proposal_digest": "sha256:" + "2" * 64,
            "confirmed_node_ids": ["requirement:csv-export"],
        }
    if tool_name == "intent_preflight":
        return _raw_mechanical_preflight()
    return {
        "token": "bounded-token",
        "actor": "local",
        "repository_id": "project",
        "task_id": "task:sha256:" + "1" * 64,
        "graph_version": 0,
        "requested_paths": [],
    }


async def test_workflow_registration_is_optional_additive_and_has_truthful_annotations(
    tmp_path: Path,
) -> None:
    services = _services(tmp_path)
    without = build_server(services)
    workflow = _FakeWorkflow()
    with_workflow = build_server(services, intent_workflow_services=workflow)

    assert {tool.name for tool in await without.list_tools()} == _READ_TOOLS
    by_name = {tool.name: tool for tool in await with_workflow.list_tools()}
    assert set(by_name) == _READ_TOOLS | _WORKFLOW_TOOLS
    proposed_annotations = by_name["intent_bootstrap_propose"].annotations
    shown_annotations = by_name["intent_proposal_show"].annotations
    confirmed_annotations = by_name["intent_proposal_confirm"].annotations
    preflight_annotations = by_name["intent_preflight"].annotations
    verification_annotations = by_name["intent_authorization_verify"].annotations
    assert proposed_annotations is not None
    assert shown_annotations is not None
    assert confirmed_annotations is not None
    assert preflight_annotations is not None
    assert verification_annotations is not None
    assert (
        proposed_annotations.read_only_hint,
        proposed_annotations.destructive_hint,
        proposed_annotations.idempotent_hint,
        proposed_annotations.open_world_hint,
    ) == (False, False, True, False)
    assert (
        shown_annotations.read_only_hint,
        shown_annotations.destructive_hint,
        shown_annotations.idempotent_hint,
        shown_annotations.open_world_hint,
    ) == (True, False, True, False)
    assert (
        confirmed_annotations.read_only_hint,
        confirmed_annotations.destructive_hint,
        confirmed_annotations.idempotent_hint,
        confirmed_annotations.open_world_hint,
    ) == (False, False, True, False)
    assert (
        preflight_annotations.read_only_hint,
        preflight_annotations.destructive_hint,
        preflight_annotations.idempotent_hint,
        preflight_annotations.open_world_hint,
    ) == (False, False, False, False)
    assert (
        verification_annotations.read_only_hint,
        verification_annotations.destructive_hint,
        verification_annotations.idempotent_hint,
        verification_annotations.open_world_hint,
    ) == (True, False, True, False)
    all_public_names = {
        *by_name,
        *(prompt.name for prompt in await with_workflow.list_prompts()),
        *(str(resource.uri) for resource in await with_workflow.list_resources()),
        *(item.uri_template for item in await with_workflow.list_resource_templates()),
    }
    assert not any("approval" in name or "external_write" in name for name in all_public_names)
    propose_schema = by_name["intent_bootstrap_propose"].input_schema
    submission_schema = propose_schema["properties"]["submission"]
    assert submission_schema == {"$ref": "#/$defs/_BootstrapSubmissionInput"}
    assert propose_schema["$defs"]["_BootstrapSubmissionInput"] == {
        "$ref": "#/$defs/BootstrapSubmission"
    }
    typed_schema = propose_schema["$defs"]["BootstrapSubmission"]
    assert typed_schema["additionalProperties"] is False
    assert set(typed_schema["required"]) == {
        "baseline_graph_version",
        "actor",
        "timestamp",
        "evidence_refs",
        "source_roles",
        "candidate_nodes",
        "candidate_edges",
        "core_node_ids",
        "provisional_node_ids",
    }
    preflight_schema = by_name["intent_preflight"].input_schema
    assert preflight_schema["properties"]["envelope"] == {
        "$ref": "#/$defs/_TaskEnvelopeInput"
    }
    assert preflight_schema["properties"]["submission"] == {
        "$ref": "#/$defs/_AgentClassificationInput"
    }
    assert preflight_schema["$defs"]["TaskEnvelope"]["additionalProperties"] is False
    assert (
        preflight_schema["$defs"]["AgentClassificationSubmission"]["additionalProperties"]
        is False
    )


async def test_workflow_tools_delegate_detached_typed_payloads(tmp_path: Path) -> None:
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    submission = {
        "schema_version": 1,
        "baseline_graph_version": 0,
        "actor": "agent:codex",
        "timestamp": "2026-08-26T12:00:00Z",
        "evidence_refs": ["evidence:sha256:" + "1" * 64],
        "source_roles": [
            {
                "connector_id": "markdown",
                "scope": "docs/prd.md",
                "role": "declared_intent",
                "inherited": False,
            }
        ],
        "candidate_nodes": [],
        "candidate_edges": [],
        "core_node_ids": [],
        "provisional_node_ids": [],
        "assumptions": [],
        "unanswered_questions": [],
        "conflicting_authors": [],
        "destructive": False,
    }

    proposed = await server.call_tool("intent_bootstrap_propose", {"submission": submission})
    shown = await server.call_tool(
        "intent_proposal_show", {"proposal_id": "proposal:sha256:" + "1" * 64}
    )
    confirmed = await server.call_tool(
        "intent_proposal_confirm",
        {
            "proposal_id": "proposal:sha256:" + "1" * 64,
            "proposal_digest": "sha256:" + "2" * 64,
            "confirmed_node_ids": ["requirement:csv-export"],
        },
    )
    envelope = TaskEnvelope(
        repository_id="project",
        actor="local",
        conversation_ref="codex:thread-1",
        request="Format README",
        request_evidence_ref="evidence:conversation:" + "3" * 64,
        graph_version=0,
        created_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        requested_scope=("README.md",),
    )
    classification = AgentClassificationSubmission(
        task_id=envelope.id,
        task_digest=envelope.digest,
        graph_version=0,
        classification=TaskClassification.NO_SEMANTIC_IMPACT,
        basis="Formatting only",
        agent_evidence_ref="evidence:conversation:" + "4" * 64,
        requested_scope=("README.md",),
    )
    preflight = await server.call_tool(
        "intent_preflight",
        {
            "envelope": envelope.model_dump(mode="json"),
            "submission": classification.model_dump(mode="json"),
        },
    )
    verified = await server.call_tool(
        "intent_authorization_verify",
        {
            "token": "opaque-token",
            "actor": "local",
            "repository_id": "project",
            "task_id": envelope.id,
            "graph_version": 0,
            "requested_paths": ["README.md"],
        },
    )

    assert proposed.structured_content["status"] == "proposed"
    assert shown.structured_content["status"] == "proposed"
    assert confirmed.structured_content["status"] == "activated"
    assert preflight.structured_content["authorization_token"] == "opaque-token"
    assert verified.structured_content == {
        "schema_version": 1,
        "authorized": True,
        "classification": "no_semantic_impact",
        "relevant_node_ids": [],
        "expires_at": "2026-08-26T12:05:00Z",
    }
    assert isinstance(workflow.calls[0][1], BootstrapSubmission)
    assert workflow.calls == [
        (
            "propose",
            BootstrapSubmission.model_validate_json(json.dumps(submission)),
        ),
        ("show", "proposal:sha256:" + "1" * 64),
        (
            "confirm",
            (
                "proposal:sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                ("requirement:csv-export",),
            ),
        ),
        ("preflight", (envelope, classification)),
        (
            "verify",
            (
                "opaque-token",
                "local",
                "project",
                envelope.id,
                0,
                ("README.md",),
            ),
        ),
    ]


def _preflight_inputs(
    runtime,
    *,
    classification: TaskClassification,
    conversation_ref: str,
    request: str,
    relevant_node_ids: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    questions: tuple[str, ...] = (),
    conflict_claims: tuple[str, ...] = (),
) -> tuple[TaskEnvelope, AgentClassificationSubmission]:
    at = datetime(2026, 8, 26, 12, 1, tzinfo=UTC)
    capture = ConversationCapture(runtime.evidence_store, connector_id="conversation:codex")
    human = capture.record_turn(
        conversation_ref=conversation_ref,
        role="human",
        author=runtime.config.local_actor,
        content=request,
        captured_at=at,
        acl=(runtime.config.local_actor,),
    )
    envelope = TaskEnvelope(
        repository_id=runtime.config.project_id,
        actor=runtime.config.local_actor,
        conversation_ref=conversation_ref,
        request=request,
        request_evidence_ref=human.id,
        graph_version=runtime.graph_store.load().version,
        created_at=at,
        requested_scope=("src/export.py",),
    )
    material = {
        "task_id": envelope.id,
        "task_digest": envelope.digest,
        "graph_version": envelope.graph_version,
        "classification": classification,
        "basis": "Bounded classification",
        "relevant_node_ids": relevant_node_ids,
        "evidence_refs": evidence_refs,
        "semantic_effects": (),
        "uncertainties": (),
        "questions": questions,
        "conflict_claims": conflict_claims,
        "requested_scope": envelope.requested_scope,
    }
    agent = capture.record_turn(
        conversation_ref=conversation_ref,
        role="agent",
        author="agent:codex",
        content=classification_evidence_content(**material),
        captured_at=at + timedelta(microseconds=1),
        acl=(runtime.config.local_actor,),
    )
    return envelope, AgentClassificationSubmission(
        **material,
        agent_evidence_ref=agent.id,
    )


async def test_production_preflight_issues_only_authorized_results_and_verifies_live_scope(
    tmp_path: Path,
) -> None:
    """Fails if the held production runtime can mint for ambiguity/conflict or trust stale scope."""
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project, bootstrap = await anyio.to_thread.run_sync(_configured_project, tmp_path)
    runtime = load_runtime(project)
    current = [datetime(2026, 8, 26, 12, 1, 1, tzinfo=UTC)]
    workflow = load_intent_workflow_services(
        runtime,
        clock=lambda: current[0],
    )
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    proposed = await server.call_tool(
        "intent_bootstrap_propose",
        {"submission": bootstrap.model_dump(mode="json")},
    )
    await server.call_tool(
        "intent_proposal_confirm",
        {
            "proposal_id": proposed.structured_content["proposal_id"],
            "proposal_digest": proposed.structured_content["proposal_digest"],
            "confirmed_node_ids": ["intent:local-export", "requirement:csv-export"],
        },
    )
    evidence_ref = runtime.graph_store.load().nodes[0].evidence_refs[0]
    cases_before = len(runtime.case_store.list())
    fixtures = (
        _preflight_inputs(
            runtime,
            classification=TaskClassification.NO_SEMANTIC_IMPACT,
            conversation_ref="codex:mechanical",
            request="Format export module",
        ),
        _preflight_inputs(
            runtime,
            classification=TaskClassification.ALIGNED,
            conversation_ref="codex:aligned",
            request="Implement CSV export",
            relevant_node_ids=("requirement:csv-export",),
            evidence_refs=(evidence_ref,),
        ),
        _preflight_inputs(
            runtime,
            classification=TaskClassification.NEW_OR_AMBIGUOUS,
            conversation_ref="codex:ambiguous",
            request="Add sharing",
            questions=("Who may share reports?",),
        ),
        _preflight_inputs(
            runtime,
            classification=TaskClassification.CONFLICTING,
            conversation_ref="codex:conflict",
            request="Remove CSV export",
            relevant_node_ids=("requirement:csv-export",),
            evidence_refs=(evidence_ref,),
            conflict_claims=("The request removes the active export behavior.",),
        ),
    )
    outputs: list[dict[str, object]] = []
    for envelope, classification in fixtures:
        response = await server.call_tool(
            "intent_preflight",
            {
                "envelope": envelope.model_dump(mode="json"),
                "submission": classification.model_dump(mode="json"),
            },
        )
        outputs.append(response.structured_content)

    assert [item.get("classification") for item in outputs] == [
        "no_semantic_impact",
        "aligned",
        "new_or_ambiguous",
        "conflicting",
    ], outputs
    assert ["authorization_token" in item for item in outputs] == [True, True, False, False]
    assert outputs[2]["questions"] == ["Who may share reports?"]
    assert outputs[3]["review_case_id"] is not None
    assert len(runtime.case_store.list()) == cases_before + 1

    aligned_envelope = fixtures[1][0]
    token = outputs[1]["authorization_token"]
    accepted = await server.call_tool(
        "intent_authorization_verify",
        {
            "token": token,
            "actor": "local",
            "repository_id": "project",
            "task_id": aligned_envelope.id,
            "graph_version": 1,
            "requested_paths": ["src/export.py"],
        },
    )
    expanded = await server.call_tool(
        "intent_authorization_verify",
        {
            "token": token,
            "actor": "local",
            "repository_id": "project",
            "task_id": aligned_envelope.id,
            "graph_version": 1,
            "requested_paths": ["src/unrelated.py"],
        },
    )
    mismatch_arguments = (
        {"actor": "local:other"},
        {"repository_id": "other"},
        {"graph_version": 2},
        {"task_id": "task:sha256:" + "9" * 64},
    )
    denials = []
    base_verification = {
        "token": token,
        "actor": "local",
        "repository_id": "project",
        "task_id": aligned_envelope.id,
        "graph_version": 1,
        "requested_paths": ["src/export.py"],
    }
    for updates in mismatch_arguments:
        denied = await server.call_tool(
            "intent_authorization_verify",
            {**base_verification, **updates},
        )
        denials.append(denied.structured_content)
    unknown = await server.call_tool(
        "intent_authorization_verify",
        {**base_verification, "token": "not-a-capability"},
    )
    restarted = build_server(
        McpReadServices(runtime),
        intent_workflow_services=load_intent_workflow_services(
            runtime,
            clock=lambda: current[0],
        ),
    )
    after_restart = await restarted.call_tool(
        "intent_authorization_verify", base_verification
    )
    current[0] = datetime(2026, 8, 26, 12, 6, 1, tzinfo=UTC)
    expired = await server.call_tool("intent_authorization_verify", base_verification)
    assert accepted.structured_content == {
        "schema_version": 1,
        "authorized": True,
        "classification": "aligned",
        "relevant_node_ids": ["requirement:csv-export"],
        "expires_at": "2026-08-26T12:06:01Z",
    }
    assert expanded.structured_content == {
        "schema_version": 1,
        "authorized": False,
        "classification": None,
        "relevant_node_ids": [],
        "expires_at": None,
    }
    denied_shape = expanded.structured_content
    assert all(item == denied_shape for item in denials)
    assert unknown.structured_content == denied_shape
    assert after_restart.structured_content == denied_shape
    assert expired.structured_content == denied_shape
    assert set(denied_shape) == {
        "schema_version",
        "authorized",
        "classification",
        "relevant_node_ids",
        "expires_at",
    }
    assert "reason" not in denied_shape


async def test_production_verification_reauthenticates_live_graph_and_config(
    tmp_path: Path,
) -> None:
    """Fails if request fields can mask descriptor-held graph or actor replacement."""
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    at = datetime(2026, 8, 26, 12, 1, tzinfo=UTC)
    envelope, submission = _preflight_inputs(
        runtime,
        classification=TaskClassification.NO_SEMANTIC_IMPACT,
        conversation_ref="codex:live-binding",
        request="Format README",
    )
    workflow = load_intent_workflow_services(runtime, clock=lambda: at)
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    issued = await server.call_tool(
        "intent_preflight",
        {
            "envelope": envelope.model_dump(mode="json"),
            "submission": submission.model_dump(mode="json"),
        },
    )
    token = issued.structured_content["authorization_token"]
    arguments = {
        "token": token,
        "actor": "local",
        "repository_id": "project",
        "task_id": envelope.id,
        "graph_version": 0,
        "requested_paths": ["src/export.py"],
    }
    graph = runtime.graph_store.load()
    runtime.graph_store.initialize(graph.model_copy(update={"version": 1}))
    graph_denied = await server.call_tool("intent_authorization_verify", arguments)
    runtime.graph_store.initialize(graph)
    runtime.graph_store.initialize(
        graph.model_copy(update={"purpose": "PRIVATE-SAME-VERSION-REPLACEMENT"})
    )
    same_version_denied = await server.call_tool(
        "intent_authorization_verify", arguments
    )
    runtime.graph_store.initialize(graph)
    config_path = project / ".intent/config.yaml"
    config = runtime.config.model_copy(update={"local_actor": "local:other"})
    config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    config_denied = await server.call_tool("intent_authorization_verify", arguments)

    assert graph_denied.structured_content["authorized"] is False
    assert same_version_denied.structured_content == graph_denied.structured_content
    assert config_denied.structured_content == graph_denied.structured_content
    assert token not in repr(graph_denied)
    assert token not in repr(config_denied)


async def test_preflight_graph_swap_during_issue_returns_no_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if graph identity can change after Task 5 authentication but before minting."""
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    at = datetime(2026, 8, 26, 12, 1, tzinfo=UTC)
    envelope, submission = _preflight_inputs(
        runtime,
        classification=TaskClassification.NO_SEMANTIC_IMPACT,
        conversation_ref="codex:issue-race",
        request="Format README",
    )
    workflow = load_intent_workflow_services(runtime, clock=lambda: at)
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    original_graph = runtime.graph_store.load()
    original_issue = workflow._issuer.issue

    def swap_then_issue(*args: object, **kwargs: object) -> str:
        runtime.graph_store.initialize(
            original_graph.model_copy(update={"purpose": "PRIVATE-ISSUE-RACE"})
        )
        return original_issue(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workflow._issuer, "issue", swap_then_issue)
    response = await server.call_tool(
        "intent_preflight",
        {
            "envelope": envelope.model_dump(mode="json"),
            "submission": submission.model_dump(mode="json"),
        },
    )

    assert response.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert workflow._issuer.__dict__["_grants"] == {}
    assert "PRIVATE-ISSUE-RACE" not in repr(response)


async def test_authorization_verify_denies_graph_swap_during_issuer_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if verification can authorize a graph replaced after its live snapshot read."""
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    at = datetime(2026, 8, 26, 12, 1, tzinfo=UTC)
    envelope, submission = _preflight_inputs(
        runtime,
        classification=TaskClassification.NO_SEMANTIC_IMPACT,
        conversation_ref="codex:verify-race",
        request="Format README",
    )
    workflow = load_intent_workflow_services(runtime, clock=lambda: at)
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    issued = await server.call_tool(
        "intent_preflight",
        {
            "envelope": envelope.model_dump(mode="json"),
            "submission": submission.model_dump(mode="json"),
        },
    )
    token = issued.structured_content["authorization_token"]
    original_graph = runtime.graph_store.load()
    original_verify = workflow._issuer.verify

    def swap_then_verify(
        *args: object, **kwargs: object
    ) -> AuthorizationVerification:
        runtime.graph_store.initialize(
            original_graph.model_copy(update={"purpose": "PRIVATE-VERIFY-RACE"})
        )
        return original_verify(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workflow._issuer, "verify", swap_then_verify)
    response = await server.call_tool(
        "intent_authorization_verify",
        {
            "token": token,
            "actor": "local",
            "repository_id": "project",
            "task_id": envelope.id,
            "graph_version": 0,
            "requested_paths": ["src/export.py"],
        },
    )

    assert response.structured_content == {
        "schema_version": 1,
        "authorized": False,
        "classification": None,
        "relevant_node_ids": [],
        "expires_at": None,
    }
    assert token not in repr(response)


async def test_token_sentinel_is_absent_from_project_logs_denials_and_tracebacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fails if success, denial, fixed failure, or cancellation retains capability text."""
    import intent_engineering.intent_workflow.authorization as authorization_module
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    sentinel = "PRIVATE_CAPABILITY_" + "x" * 24
    monkeypatch.setattr(authorization_module.secrets, "token_urlsafe", lambda size: sentinel)
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    at = datetime(2026, 8, 26, 12, 1, tzinfo=UTC)
    envelope, submission = _preflight_inputs(
        runtime,
        classification=TaskClassification.NO_SEMANTIC_IMPACT,
        conversation_ref="codex:sentinel",
        request="Format README",
    )
    workflow = load_intent_workflow_services(runtime, clock=lambda: at)
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    caplog.set_level(logging.DEBUG)
    issued = await server.call_tool(
        "intent_preflight",
        {
            "envelope": envelope.model_dump(mode="json"),
            "submission": submission.model_dump(mode="json"),
        },
    )
    assert issued.structured_content["authorization_token"] == sentinel
    denied = await server.call_tool(
        "intent_authorization_verify",
        {
            "token": sentinel,
            "actor": "local",
            "repository_id": "project",
            "task_id": envelope.id,
            "graph_version": 0,
            "requested_paths": ["src/unrelated.py"],
        },
    )
    assert denied.structured_content["authorized"] is False
    assert sentinel not in repr(denied)
    project_text = "".join(
        path.read_text(encoding="utf-8")
        for path in project.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    captured = capsys.readouterr()
    assert sentinel not in project_text
    assert sentinel not in captured.out + captured.err + caplog.text

    failed = build_server(
        _services(tmp_path / "failure"), intent_workflow_services=_FailingWorkflow()
    )
    with pytest.raises(ToolError) as fixed:
        await failed.call_tool(
            "intent_authorization_verify",
            {
                "token": sentinel,
                "actor": "local",
                "repository_id": "project",
                "task_id": envelope.id,
                "graph_version": 0,
                "requested_paths": ["README.md"],
            },
        )
    assert fixed.value.args == ("invalid intent workflow arguments",)
    assert sentinel not in repr(fixed.value)
    assert sentinel not in _repository_traceback_values(fixed.value)

    signal = _CancellationSignal("cancel authorization verify")
    cancelled = build_server(
        _services(tmp_path / "cancellation"),
        intent_workflow_services=_FailingWorkflow(failure=signal),
    )
    with pytest.raises(_CancellationSignal) as caught:
        await cancelled.call_tool(
            "intent_authorization_verify",
            {
                "token": sentinel,
                "actor": "local",
                "repository_id": "project",
                "task_id": envelope.id,
                "graph_version": 0,
                "requested_paths": ["README.md"],
            },
        )
    assert caught.value is signal
    assert sentinel not in _repository_traceback_values(caught.value)


def _repository_traceback_values(error: BaseException) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return " ".join(values)


def _cycle_validation_child(kind: str, connection: Connection) -> None:
    marker = "PRIVATE_CYCLE_" + "x" * 29
    arguments: dict[str, object] = {
        "token": marker,
        "actor": "local",
        "repository_id": "project",
        "task_id": "task:sha256:" + "1" * 64,
        "graph_version": 0,
        "requested_paths": [],
    }
    if kind == "self_list":
        cycle: list[object] = []
        cycle.append(cycle)
        arguments["requested_paths"] = cycle
    elif kind == "self_dict":
        cycle_dict: dict[str, object] = {}
        cycle_dict["self"] = cycle_dict
        arguments["requested_paths"] = [cycle_dict]
    else:
        left: list[object] = []
        right: list[object] = [left]
        left.append(right)
        arguments["requested_paths"] = left
    try:
        validate_intent_workflow_call("intent_authorization_verify", arguments)
        connection.send(("accepted", False))
    except ValueError as error:
        connection.send(
            (
                error.args,
                marker not in repr(error)
                and marker not in _repository_traceback_values(error),
            )
        )
    except BaseException as error:  # noqa: BLE001 - child reports exact boundary outcome
        connection.send(((type(error).__name__,), False))
    finally:
        arguments = {}
        connection.close()


async def test_production_port_proposes_shows_and_activates_without_propose_time_graph_write(
    tmp_path: Path,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project, submission = await anyio.to_thread.run_sync(_configured_project, tmp_path)
    runtime = load_runtime(project)
    workflow = load_intent_workflow_services(
        runtime,
        clock=lambda: datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )
    server = build_server(
        McpReadServices(runtime),
        intent_workflow_services=workflow,
    )

    proposed = await server.call_tool(
        "intent_bootstrap_propose",
        {"submission": submission.model_dump(mode="json")},
    )
    proposal_id = proposed.structured_content["proposal_id"]
    digest = proposed.structured_content["proposal_digest"]
    assert runtime.graph_store.load().version == 0
    shown = await server.call_tool("intent_proposal_show", {"proposal_id": proposal_id})
    confirmed = await server.call_tool(
        "intent_proposal_confirm",
        {
            "proposal_id": proposal_id,
            "proposal_digest": digest,
            "confirmed_node_ids": ["intent:local-export", "requirement:csv-export"],
        },
    )

    assert proposed.structured_content["status"] == "proposed"
    assert shown.structured_content["proposal"]["proposal_digest"] == digest
    assert confirmed.structured_content == {
        "schema_version": "1",
        "status": "activated",
        "proposal_id": proposal_id,
        "graph_version": 1,
    }
    assert runtime.graph_store.load().version == 1


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("intent_bootstrap_propose", {}),
        ("intent_bootstrap_propose", {"submission": {}}),
        ("intent_bootstrap_propose", {"submission": {"private": "x" * 1_100_000}}),
        (
            "intent_bootstrap_propose",
            {
                "submission": {
                    "schema_version": 1,
                    "baseline_graph_version": 0,
                    "actor": "agent:codex",
                    "timestamp": "2026-08-26T12:00:00Z",
                    "evidence_refs": [],
                    "source_roles": [],
                    "candidate_nodes": [],
                    "candidate_edges": [],
                    "core_node_ids": [],
                    "provisional_node_ids": [],
                    "unknown": "PRIVATE-UNKNOWN",
                }
            },
        ),
        ("intent_proposal_show", {"proposal_id": "PRIVATE-" + "x" * 600}),
        (
            "intent_proposal_confirm",
            {
                "proposal_id": "proposal:sha256:" + "1" * 64,
                "proposal_digest": "sha256:" + "2" * 64,
                "confirmed_node_ids": ["PRIVATE-" + "x" * 600],
                "unknown": "PRIVATE-UNKNOWN",
            },
        ),
        (
            "intent_preflight",
            {
                "envelope": {"unknown": "PRIVATE-UNKNOWN"},
                "submission": {},
            },
        ),
        (
            "intent_authorization_verify",
            {
                "token": "PRIVATE-" + "x" * 300,
                "actor": "local",
                "repository_id": "project",
                "task_id": "task:sha256:" + "1" * 64,
                "graph_version": 0,
                "requested_paths": [],
            },
        ),
        (
            "intent_authorization_verify",
            {
                "token": "bounded-token",
                "actor": "local",
                "repository_id": "project",
                "task_id": "task:sha256:" + "1" * 64,
                "graph_version": 0,
                "requested_paths": ["PRIVATE-duplicate", "PRIVATE-duplicate"],
            },
        ),
        (
            "intent_authorization_verify",
            {
                "token": "bounded-token",
                "actor": "local",
                "repository_id": "project",
                "task_id": "task:sha256:" + "1" * 64,
                "graph_version": 0,
                "requested_paths": ["/PRIVATE-absolute"],
            },
        ),
        (
            "intent_authorization_verify",
            {
                "token": "bounded-token",
                "actor": "local",
                "repository_id": "project",
                "task_id": "task:sha256:" + "1" * 64,
                "graph_version": "0",
                "requested_paths": [],
                "unknown": "PRIVATE-UNKNOWN",
            },
        ),
        (
            "intent_authorization_verify",
            {
                "token": "PRIVATE-TOKEN",
                "actor": "local",
                "repository_id": "project",
                "task_id": "task:sha256:" + "1" * 64,
                "graph_version": "0",
                "requested_paths": [],
            },
        ),
        (
            "intent_authorization_verify",
            {
                "token": "é" * 129,
                "actor": "local",
                "repository_id": "project",
                "task_id": "task:sha256:" + "1" * 64,
                "graph_version": 0,
                "requested_paths": [],
            },
        ),
        *(
            (
                "intent_authorization_verify",
                {
                    "token": "bounded-token",
                    "actor": "local",
                    "repository_id": "project",
                    "task_id": "task:sha256:" + "1" * 64,
                    "graph_version": 0,
                    "requested_paths": [path],
                },
            )
            for path in (
                "C:/src/export.py",
                "C:\\src\\export.py",
                "C:src/export.py",
                "//server/share/export.py",
                "\\\\server\\share\\export.py",
                "//?/C:/src/export.py",
                "\\\\?\\C:\\src\\export.py",
                "//./PIPE/name",
                "src/export:alternate.py",
                "NUL",
                "src/NUL.txt",
                "src/con/config.py",
                "src/AuX.log",
                "src/prn.",
                "src/CLOCK$",
                "src/clock$.txt",
                "src/COM1",
                "src/com9.log",
                "src/LPT1",
                "src/lpt9.txt",
                "src/NUL ",
            )
        ),
    ],
)
async def test_invalid_workflow_arguments_fail_before_port_with_fixed_secret_free_error(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)

    with pytest.raises(ToolError) as caught:
        await server.call_tool(tool_name, arguments)

    assert caught.value.args == ("invalid intent workflow arguments",)
    assert "PRIVATE-" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    repository_locals: list[str] = []
    traceback = caught.value.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert "PRIVATE-" not in "".join(repository_locals)
    assert workflow.calls == []


@pytest.mark.parametrize(
    "created_at",
    ["2026-08-26T12:00:00+00:00", "2026-08-26T17:30:00+05:30"],
)
async def test_preflight_requires_canonical_utc_z_roundtrip_before_port(
    tmp_path: Path,
    created_at: str,
) -> None:
    """Fails if an equivalent or non-UTC timestamp bypasses the canonical wire spelling."""
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    arguments = _raw_mechanical_preflight()
    envelope = arguments["envelope"]
    assert type(envelope) is dict
    envelope["created_at"] = created_at

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool("intent_preflight", arguments)

    assert workflow.calls == []


@pytest.mark.parametrize("mutation", ["missing", "unknown", "coerced"])
async def test_preflight_rejects_missing_unknown_and_coerced_fields_before_port(
    tmp_path: Path,
    mutation: str,
) -> None:
    """Fails if raw envelope structure is defaulted, discarded, or coerced by the SDK."""
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    arguments = _raw_mechanical_preflight()
    envelope = arguments["envelope"]
    assert type(envelope) is dict
    if mutation == "missing":
        envelope.pop("actor")
    elif mutation == "unknown":
        envelope["unknown"] = "PRIVATE-UNKNOWN"
    else:
        envelope["graph_version"] = "0"

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool("intent_preflight", arguments)

    assert workflow.calls == []


@pytest.mark.parametrize("tool_name", sorted(_WORKFLOW_TOOLS))
@pytest.mark.parametrize("attack", ["key", "scalar"])
async def test_raw_workflow_tree_is_validated_before_hostile_key_or_scalar_behavior(
    tmp_path: Path,
    tool_name: str,
    attack: str,
) -> None:
    """The recursive exact-tree guard must be the first raw-argument operation."""
    accessed: list[str] = []
    armed = False

    class HostileString(str):
        def _trip(self, behavior: str) -> Never:
            accessed.append(behavior)
            raise RuntimeError("hostile string behavior ran")

        def __hash__(self) -> int:
            if armed:
                self._trip("hash")
            return str.__hash__(self)

        def __eq__(self, other: object) -> bool:
            if armed:
                self._trip("eq")
            return str.__eq__(self, other)

        def __repr__(self) -> str:
            if armed:
                self._trip("repr")
            return str.__repr__(self)

        def __str__(self) -> str:
            if armed:
                self._trip("str")
            return str.__str__(self)

        def encode(self, *args: object, **kwargs: object) -> bytes:
            if armed:
                self._trip("encode")
            return str.encode(self, *args, **kwargs)  # type: ignore[arg-type]

    arguments = _valid_raw_workflow_arguments(tool_name)
    if attack == "key":
        first_key, first_value = next(iter(dict.items(arguments)))
        arguments = {
            HostileString(first_key): first_value,
            **{
                key: value
                for key, value in dict.items(arguments)
                if key != first_key
            },
        }
    elif tool_name == "intent_bootstrap_propose":
        submission = arguments["submission"]
        assert type(submission) is dict
        submission["actor"] = HostileString("agent:codex")
    elif tool_name == "intent_preflight":
        envelope = arguments["envelope"]
        assert type(envelope) is dict
        envelope["request"] = HostileString("Format README")
    else:
        first_key = next(iter(dict.keys(arguments)))
        arguments[first_key] = HostileString(str(arguments[first_key]))
    armed = True

    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    with pytest.raises(ToolError) as caught:
        await server.call_tool(tool_name, arguments)

    assert caught.value.args == ("invalid intent workflow arguments",)
    assert accessed == []
    assert workflow.calls == []


@pytest.mark.parametrize("kind", ["string", "list", "dict"])
async def test_nested_json_subclasses_are_rejected_before_overridden_access(
    tmp_path: Path,
    kind: str,
) -> None:
    """Fails if direct API input executes hostile nested container or scalar behavior."""
    accessed = False

    class HostileString(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            nonlocal accessed
            accessed = True
            return super().encode(*args, **kwargs)  # type: ignore[arg-type]

        def __str__(self) -> str:
            nonlocal accessed
            accessed = True
            return super().__str__()

    class HostileList(list[object]):
        def __iter__(self) -> Iterator[object]:
            nonlocal accessed
            accessed = True
            return super().__iter__()

    class HostileDict(dict[str, object]):
        def items(self) -> ItemsView[str, object]:
            nonlocal accessed
            accessed = True
            return super().items()

    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    if kind == "string":
        envelope = TaskEnvelope(
            repository_id="project",
            actor="local",
            conversation_ref="codex:strict-subclass",
            request="Format README",
            request_evidence_ref="evidence:conversation:" + "3" * 64,
            graph_version=0,
            created_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
            requested_scope=(),
        )
        arguments: dict[str, object] = {
            "token": HostileString("bounded-token"),
            "actor": "local",
            "repository_id": "project",
            "task_id": envelope.id,
            "graph_version": 0,
            "requested_paths": [],
        }
        tool = "intent_authorization_verify"
    else:
        arguments = _raw_mechanical_preflight()
        submission = arguments["submission"]
        assert type(submission) is dict
        submission["requested_scope"] = (
            HostileList(["README.md"])
            if kind == "list"
            else [HostileDict({"private": "PRIVATE-NESTED"})]
        )
        tool = "intent_preflight"

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool(tool, arguments)

    assert accessed is False
    assert workflow.calls == []


@pytest.mark.parametrize("kind", ["self_list", "self_dict", "mutual_lists"])
async def test_cyclic_exact_json_is_rejected_within_a_bounded_child(
    kind: str,
) -> None:
    """Fails if exact built-in cycles hang the raw pre-handler or leak marker material."""
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=_cycle_validation_child, args=(kind, sending))
    process.start()
    sending.close()
    process.join(timeout=1.0)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(timeout=1.0)
    try:
        assert timed_out is False
        assert process.exitcode == 0
        assert receiving.poll(timeout=0.1)
        assert receiving.recv() == (("invalid intent workflow arguments",), True)
    finally:
        receiving.close()


async def test_shared_exact_container_alias_is_rejected_before_port(
    tmp_path: Path,
) -> None:
    """Fails if an object graph rather than a JSON tree can reach typed validation."""
    shared: list[object] = []
    arguments = _raw_mechanical_preflight()
    submission = arguments["submission"]
    assert type(submission) is dict
    submission["semantic_effects"] = shared
    submission["uncertainties"] = shared
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)

    with pytest.raises(ToolError, match="invalid intent workflow arguments") as caught:
        await server.call_tool("intent_preflight", arguments)

    assert "PRIVATE-" not in repr(caught.value)
    assert workflow.calls == []


@pytest.mark.parametrize("shape", ["deep", "wide"])
async def test_excessive_exact_json_shape_fails_before_typed_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """Fails if depth/node bounds are deferred until typed validation or serialization."""
    marker = "PRIVATE-RAW-BOUNDARY"
    arguments: dict[str, object] = {
        "token": marker,
        "actor": "local",
        "repository_id": "project",
        "task_id": "task:sha256:" + "1" * 64,
        "graph_version": 0,
        "requested_paths": [],
    }
    if shape == "deep":
        root: list[object] = []
        cursor = root
        for _index in range(160):
            child: list[object] = []
            cursor.append(child)
            cursor = child
        arguments["requested_paths"] = root
    else:
        arguments["requested_paths"] = ["src/file.py"] * 70_000
    signal = _CancellationSignal("typed validation must not run")

    def typed_validation_reached(*_args: object, **_kwargs: object) -> object:
        raise signal

    monkeypatch.setattr(
        AuthorizationVerifyRequest,
        "model_validate",
        typed_validation_reached,
    )
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)

    with pytest.raises(ToolError, match="invalid intent workflow arguments") as caught:
        await server.call_tool("intent_authorization_verify", arguments)

    assert marker not in repr(caught.value)
    assert marker not in _repository_traceback_values(caught.value)
    assert workflow.calls == []


@pytest.mark.parametrize(
    "path",
    ["src/com10.py", "src/lpt10.txt", "src/auxiliary.py", "src/null-device.py"],
)
async def test_mcp_preserves_valid_posix_names_near_windows_devices(
    tmp_path: Path,
    path: str,
) -> None:
    """Fails if the raw Windows-device check rejects ordinary POSIX repository paths."""
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    envelope = TaskEnvelope(
        repository_id="project",
        actor="local",
        conversation_ref="codex:valid-posix",
        request="Format README",
        request_evidence_ref="evidence:conversation:" + "3" * 64,
        graph_version=0,
        created_at=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
        requested_scope=(path,),
    )

    response = await server.call_tool(
        "intent_authorization_verify",
        {
            "token": "bounded-token",
            "actor": envelope.actor,
            "repository_id": envelope.repository_id,
            "task_id": envelope.id,
            "graph_version": envelope.graph_version,
            "requested_paths": [path],
        },
    )

    assert response.structured_content["authorized"] is True
    assert workflow.calls[-1][0] == "verify"
    assert workflow.calls[-1][1][-1] == (path,)


async def test_raw_preflight_cancellation_preserves_identity_without_secret_repo_locals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails if pre-handler cancellation changes identity or retains raw task material."""
    import intent_engineering.integrations.mcp_server.intent_workflow as workflow_module

    signal = _CancellationSignal("cancel raw preflight")
    arguments = _raw_mechanical_preflight()
    envelope = arguments["envelope"]
    assert type(envelope) is dict
    envelope["request"] = "PRIVATE-RAW-PREFLIGHT-CANCELLATION"
    server = build_server(
        _services(tmp_path), intent_workflow_services=_FakeWorkflow()
    )

    def cancel(*_args: object, **_kwargs: object) -> str:
        raise signal

    monkeypatch.setattr(workflow_module.json, "dumps", cancel)
    with pytest.raises(_CancellationSignal) as caught:
        await server.call_tool("intent_preflight", arguments)

    assert caught.value is signal
    assert "PRIVATE-RAW-PREFLIGHT-CANCELLATION" not in _repository_traceback_values(
        caught.value
    )


async def test_authorization_verify_rejects_dict_subclass_before_member_access(
    tmp_path: Path,
) -> None:
    """Fails if hostile mapping behavior can run before the exact-container check."""
    accessed = False

    class HostileArguments(dict[str, object]):
        def get(self, key: str, default: object = None) -> object:
            nonlocal accessed
            accessed = True
            return super().get(key, default)

    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    arguments = HostileArguments(
        {
            "token": "bounded-token",
            "actor": "local",
            "repository_id": "project",
            "task_id": "task:sha256:" + "1" * 64,
            "graph_version": 0,
            "requested_paths": [],
        }
    )

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool("intent_authorization_verify", arguments)

    assert accessed is False
    assert workflow.calls == []


async def test_handler_failure_is_fixed_and_cancellation_preserves_signal_without_repo_locals(
    tmp_path: Path,
) -> None:
    submission = {
        "schema_version": 1,
        "baseline_graph_version": 0,
        "actor": "PRIVATE-WORKFLOW-ACTOR",
        "timestamp": "2026-08-26T12:00:00Z",
        "evidence_refs": [],
        "source_roles": [],
        "candidate_nodes": [],
        "candidate_edges": [],
        "core_node_ids": [],
        "provisional_node_ids": [],
    }
    failed = build_server(
        _services(tmp_path), intent_workflow_services=_FailingWorkflow()
    )
    with pytest.raises(ToolError) as caught:
        await failed.call_tool("intent_bootstrap_propose", {"submission": submission})
    assert caught.value.args == ("invalid intent workflow arguments",)
    assert "PRIVATE-" not in repr(caught.value)

    signal = _CancellationSignal("cancel")
    cancelled = build_server(
        _services(tmp_path / "cancelled"),
        intent_workflow_services=_FailingWorkflow(failure=signal),
    )
    with pytest.raises(_CancellationSignal) as raised:
        await cancelled.call_tool("intent_bootstrap_propose", {"submission": submission})
    assert raised.value is signal
    repository_locals: list[str] = []
    traceback = raised.value.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert "PRIVATE-" not in "".join(repository_locals)
