"""Official-SDK contracts for reviewed intent-workflow onboarding tools."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
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

from intent_engineering.cli.intent_workflow import _principals
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import ChangeSet, Graph, Node, NodeType, SourceMode
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.agent_host.advisory import (
    AdvisoryPromptError,
    AdvisoryPromptRouter,
    PromptEvent,
    codex_conversation_ref,
)
from intent_engineering.integrations.mcp_server.intent_workflow import (
    AuthorizationVerifyRequest,
    ClarificationConfirmRequest,
    validate_intent_workflow_call,
)
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.authorization import AuthorizationVerification
from intent_engineering.intent_workflow.bootstrap import BootstrapSubmission
from intent_engineering.intent_workflow.clarification import (
    ClarificationCoordinator,
    ProposalConfirmationService,
)
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    ClarificationSession,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.preflight import (
    AgentClassificationSubmission,
    classification_evidence_content,
)
from intent_engineering.storage import secure
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
    "intent_advisory_preflight",
    "intent_authorization_verify",
    "intent_clarification_open",
    "intent_clarification_answer",
    "intent_clarification_propose",
    "intent_clarification_show",
    "intent_clarification_confirm",
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
        self.calls.append(("confirm", (proposal_id, proposal_digest, confirmed_node_ids)))
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

    async def advisory_preflight(
        self,
        conversation_ref: str,
        request_evidence_ref: str,
        draft: object,
    ) -> dict[str, object]:
        self.calls.append(("advisory_preflight", (conversation_ref, request_evidence_ref, draft)))
        return {
            "schema_version": 1,
            "task_id": "task:sha256:" + "9" * 64,
            "graph_version": 0,
            "classification": "no_semantic_impact",
            "authorized": True,
            "basis": "validated",
            "relevant_node_ids": [],
            "evidence_refs": [],
            "questions": [],
            "review_case_id": None,
            "permitted_scope": ["README.md"],
            "context": {},
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

    async def clarification_open(
        self,
        envelope: TaskEnvelope,
        classification_evidence_ref: str,
        questions: tuple[ClarificationQuestionInput, ...],
        opened_by: str,
        opened_at: datetime,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "clarification_open",
                (envelope, classification_evidence_ref, questions, opened_by, opened_at),
            )
        )
        return {
            "schema_version": 1,
            "status": "open",
            "session_id": "clarification:sha256:" + "1" * 64,
        }

    async def clarification_answer(
        self,
        session_id: str,
        question_id: str,
        answer_evidence_ref: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "clarification_answer",
                (session_id, question_id, answer_evidence_ref),
            )
        )
        return {"schema_version": 1, "status": "open", "session_id": session_id}

    async def clarification_propose(
        self,
        submission: ClarificationProposalSubmission,
    ) -> dict[str, object]:
        self.calls.append(("clarification_propose", submission))
        return {
            "schema_version": 1,
            "status": "proposed",
            "proposal_id": "proposal:sha256:" + "2" * 64,
        }

    async def clarification_show(self, proposal_id: str) -> dict[str, object]:
        self.calls.append(("clarification_show", proposal_id))
        return {
            "schema_version": 1,
            "status": "proposed",
            "proposal": {
                "proposal_id": proposal_id,
                "proposal_digest": "sha256:" + "4" * 64,
            },
        }

    async def clarification_confirm(
        self,
        proposal_id: str,
        proposal_digest: str,
        actor: str,
        at: datetime,
        selected_node_ids: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append(
            (
                "clarification_confirm",
                (proposal_id, proposal_digest, actor, at, selected_node_ids),
            )
        )
        return {
            "schema_version": 1,
            "status": "applied",
            "proposal_id": proposal_id,
            "graph_version": 1,
            "decision_id": "proposal-decision:sha256:" + "3" * 64,
            "case_id": None,
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
    if tool_name == "intent_advisory_preflight":
        request = "Format README\nwithout changing semantics"
        return {
            "conversation_ref": codex_conversation_ref("codex:thread-3", "turn-7", request),
            "request_evidence_ref": "evidence:conversation:" + "7" * 64,
            "draft": {
                "classification": "no_semantic_impact",
                "basis": "Formatting only",
                "relevant_node_ids": [],
                "evidence_refs": [],
                "semantic_effects": [],
                "uncertainties": [],
                "questions": [],
                "conflict_claims": [],
                "requested_scope": ["README.md"],
            },
        }
    if tool_name == "intent_clarification_open":
        return {
            "envelope": _raw_mechanical_preflight()["envelope"],
            "classification_evidence_ref": "evidence:conversation:" + "4" * 64,
            "questions": [{"id": "audience", "prompt": "Who may share reports?", "required": True}],
            "opened_by": "agent:codex",
            "opened_at": "2026-08-26T12:00:01Z",
        }
    if tool_name == "intent_clarification_answer":
        return {
            "session_id": "clarification:sha256:" + "1" * 64,
            "question_id": "audience",
            "answer_evidence_ref": "evidence:conversation:" + "8" * 64,
        }
    if tool_name == "intent_clarification_propose":
        return {"submission": _clarification_submission().model_dump(mode="json")}
    if tool_name == "intent_clarification_show":
        return {"proposal_id": "proposal:sha256:" + "2" * 64}
    if tool_name == "intent_clarification_confirm":
        return {
            "proposal_id": "proposal:sha256:" + "2" * 64,
            "proposal_digest": "sha256:" + "4" * 64,
            "actor": "local",
            "at": "2026-08-26T12:00:04Z",
            "selected_node_ids": ["requirement:sharing"],
        }
    return {
        "token": "bounded-token",
        "actor": "local",
        "repository_id": "project",
        "task_id": "task:sha256:" + "1" * 64,
        "graph_version": 0,
        "requested_paths": [],
    }


def _clarification_submission() -> ClarificationProposalSubmission:
    at = datetime(2026, 8, 26, 12, 0, 3, tzinfo=UTC)
    evidence_refs = ("evidence:conversation:" + "5" * 64,)
    changeset = ChangeSet(
        id="changeset:clarification-test",
        actor="local",
        timestamp=at,
        baseline_graph_version=0,
        evidence_refs=evidence_refs,
        nodes_added=(),
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
    return ClarificationProposalSubmission(
        session_id="clarification:sha256:" + "1" * 64,
        task_id="task:sha256:" + "2" * 64,
        baseline_graph_version=0,
        actor="local",
        timestamp=at,
        evidence_refs=evidence_refs,
        changeset=changeset,
    )


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
    advisory_annotations = by_name["intent_advisory_preflight"].annotations
    verification_annotations = by_name["intent_authorization_verify"].annotations
    clarification_annotations = tuple(
        by_name[name].annotations
        for name in (
            "intent_clarification_open",
            "intent_clarification_answer",
            "intent_clarification_propose",
            "intent_clarification_show",
            "intent_clarification_confirm",
        )
    )
    assert proposed_annotations is not None
    assert shown_annotations is not None
    assert confirmed_annotations is not None
    assert preflight_annotations is not None
    assert advisory_annotations is not None
    assert verification_annotations is not None
    assert all(item is not None for item in clarification_annotations)
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
        advisory_annotations.read_only_hint,
        advisory_annotations.destructive_hint,
        advisory_annotations.idempotent_hint,
        advisory_annotations.open_world_hint,
    ) == (False, False, True, False)
    assert (
        verification_annotations.read_only_hint,
        verification_annotations.destructive_hint,
        verification_annotations.idempotent_hint,
        verification_annotations.open_world_hint,
    ) == (True, False, True, False)
    assert all(
        (
            item.read_only_hint,
            item.destructive_hint,
            item.idempotent_hint,
            item.open_world_hint,
        )
        == (False, False, False, False)
        for item in clarification_annotations[:3]
        if item is not None
    )
    clarification_show_annotations = clarification_annotations[3]
    assert clarification_show_annotations is not None
    assert (
        clarification_show_annotations.read_only_hint,
        clarification_show_annotations.destructive_hint,
        clarification_show_annotations.idempotent_hint,
        clarification_show_annotations.open_world_hint,
    ) == (True, False, True, False)
    clarification_confirm_annotations = clarification_annotations[4]
    assert clarification_confirm_annotations is not None
    assert (
        clarification_confirm_annotations.read_only_hint,
        clarification_confirm_annotations.destructive_hint,
        clarification_confirm_annotations.idempotent_hint,
        clarification_confirm_annotations.open_world_hint,
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
    assert preflight_schema["properties"]["envelope"] == {"$ref": "#/$defs/_TaskEnvelopeInput"}
    assert preflight_schema["properties"]["submission"] == {
        "$ref": "#/$defs/_AgentClassificationInput"
    }
    assert preflight_schema["$defs"]["TaskEnvelope"]["additionalProperties"] is False
    assert (
        preflight_schema["$defs"]["AgentClassificationSubmission"]["additionalProperties"] is False
    )
    advisory_schema = by_name["intent_advisory_preflight"].input_schema
    assert set(advisory_schema["properties"]) == {
        "conversation_ref",
        "request_evidence_ref",
        "draft",
    }
    assert set(advisory_schema["required"]) == {
        "conversation_ref",
        "request_evidence_ref",
        "draft",
    }
    assert advisory_schema["properties"]["conversation_ref"] == {
        "maxLength": 210,
        "minLength": 210,
        "pattern": r"^codex-prompt:v1:[0-9a-f]{64}:[0-9a-f]{64}:[0-9a-f]{64}$",
        "title": "Conversation Ref",
        "type": "string",
    }
    assert "actor" not in advisory_schema["properties"]
    assert "authorization_token" not in json.dumps(advisory_schema)
    assert advisory_schema["$defs"]["AdvisoryClassificationDraft"]["additionalProperties"] is False
    assert "actor" not in advisory_schema["$defs"]["AdvisoryClassificationDraft"]["properties"]
    for name in (
        "intent_clarification_open",
        "intent_clarification_answer",
        "intent_clarification_propose",
        "intent_clarification_show",
        "intent_clarification_confirm",
    ):
        assert (
            set(by_name[name].input_schema["properties"])
            == {
                "intent_clarification_open": {
                    "envelope",
                    "classification_evidence_ref",
                    "questions",
                    "opened_by",
                    "opened_at",
                },
                "intent_clarification_answer": {
                    "session_id",
                    "question_id",
                    "answer_evidence_ref",
                },
                "intent_clarification_propose": {"submission"},
                "intent_clarification_show": {"proposal_id"},
                "intent_clarification_confirm": {
                    "proposal_id",
                    "proposal_digest",
                    "actor",
                    "at",
                    "selected_node_ids",
                },
            }[name]
        )


async def test_clarification_tools_delegate_strict_detached_typed_payloads(
    tmp_path: Path,
) -> None:
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    raw = _raw_mechanical_preflight()
    envelope = TaskEnvelope.model_validate_json(json.dumps(raw["envelope"]))
    question = ClarificationQuestionInput(
        id="audience", prompt="Who may share reports?", required=True
    )
    opened_at = datetime(2026, 8, 26, 12, 0, 1, tzinfo=UTC)
    confirmed_at = datetime(2026, 8, 26, 12, 0, 4, tzinfo=UTC)

    opened = await server.call_tool(
        "intent_clarification_open",
        {
            "envelope": envelope.model_dump(mode="json"),
            "classification_evidence_ref": "evidence:conversation:" + "4" * 64,
            "questions": [question.model_dump(mode="json")],
            "opened_by": "agent:codex",
            "opened_at": "2026-08-26T12:00:01Z",
        },
    )
    answered = await server.call_tool(
        "intent_clarification_answer",
        {
            "session_id": "clarification:sha256:" + "1" * 64,
            "question_id": "audience",
            "answer_evidence_ref": "evidence:conversation:" + "8" * 64,
        },
    )
    submission = _clarification_submission()
    proposed = await server.call_tool(
        "intent_clarification_propose",
        {"submission": submission.model_dump(mode="json")},
    )
    shown = await server.call_tool(
        "intent_clarification_show",
        {"proposal_id": "proposal:sha256:" + "2" * 64},
    )
    confirmed = await server.call_tool(
        "intent_clarification_confirm",
        {
            "proposal_id": "proposal:sha256:" + "2" * 64,
            "proposal_digest": "sha256:" + "4" * 64,
            "actor": "local",
            "at": "2026-08-26T12:00:04Z",
            "selected_node_ids": ["requirement:sharing"],
        },
    )

    assert opened.structured_content["status"] == "open"
    assert answered.structured_content["status"] == "open"
    assert proposed.structured_content["status"] == "proposed"
    assert shown.structured_content["status"] == "proposed"
    assert confirmed.structured_content["status"] == "applied"
    assert workflow.calls == [
        (
            "clarification_open",
            (
                envelope,
                "evidence:conversation:" + "4" * 64,
                (question,),
                "agent:codex",
                opened_at,
            ),
        ),
        (
            "clarification_answer",
            (
                "clarification:sha256:" + "1" * 64,
                "audience",
                "evidence:conversation:" + "8" * 64,
            ),
        ),
        ("clarification_propose", submission),
        ("clarification_show", "proposal:sha256:" + "2" * 64),
        (
            "clarification_confirm",
            (
                "proposal:sha256:" + "2" * 64,
                "sha256:" + "4" * 64,
                "local",
                confirmed_at,
                ("requirement:sharing",),
            ),
        ),
    ]
    assert (
        "authorization"
        not in json.dumps(
            [
                opened.structured_content,
                answered.structured_content,
                proposed.structured_content,
                shown.structured_content,
                confirmed.structured_content,
            ]
        ).casefold()
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


async def test_advisory_preflight_delegates_exact_detached_draft(tmp_path: Path) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        AdvisoryClassificationDraft,
    )

    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    arguments = _valid_raw_workflow_arguments("intent_advisory_preflight")

    response = await server.call_tool("intent_advisory_preflight", arguments)

    assert response.structured_content["authorized"] is True
    assert "authorization_token" not in response.structured_content
    call = workflow.calls[-1]
    assert call[0] == "advisory_preflight"
    conversation_ref, request_evidence_ref, draft = call[1]
    assert conversation_ref == codex_conversation_ref(
        "codex:thread-3", "turn-7", "Format README\nwithout changing semantics"
    )
    assert request_evidence_ref == "evidence:conversation:" + "7" * 64
    assert type(draft) is AdvisoryClassificationDraft
    assert draft.requested_scope == ("README.md",)


async def test_production_advisory_preflight_is_durable_idempotent_and_token_free(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local"],
                "approvers": ["local"],
                "executors": ["local"],
                "identities": {"local": ["local", "local:advisory-alias"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id="graph:advisory-evidence",
            version=1,
            name="Advisory evidence",
            nodes=(
                Node(
                    id="intent:advisory-evidence",
                    type=NodeType.PRODUCT_INTENT,
                    label="Preserve attributed advisory evidence",
                    status="active",
                    created_by="local",
                    created_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
                    last_modified_by="local",
                    last_modified_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
                ),
            ),
            edges=(),
        )
    )
    workflow = load_intent_workflow_services(
        runtime, clock=lambda: datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    )
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    arguments = _valid_raw_workflow_arguments("intent_advisory_preflight")
    request = "Format README\nwithout changing semantics"
    route = AdvisoryPromptRouter(runtime).route(
        PromptEvent(
            session_id="codex:thread-3",
            turn_id="turn-7",
            repository=str(project),
            actor="local",
            prompt=request,
            created_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        )
    )
    arguments.update(route.arguments)

    first = await server.call_tool("intent_advisory_preflight", arguments)
    durable = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    second = await server.call_tool("intent_advisory_preflight", arguments)
    replayed = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    }

    assert first.structured_content == second.structured_content
    assert first.structured_content["classification"] == "no_semantic_impact"
    assert first.structured_content["authorized"] is True
    assert durable == replayed
    wire_and_durable = json.dumps(first.structured_content).encode() + b"".join(durable.values())
    assert b"authorization_token" not in wire_and_durable
    assert b"capability" not in wire_and_durable.lower()
    chain = runtime.evidence_store.chain(
        "conversation:codex", "conversation", arguments["conversation_ref"]
    )
    assert tuple(item.evidence.payload["role"] for item in chain) == ("agent", "agent")
    assert chain[0].evidence.author == "agent:codex"
    assert chain[1].evidence.author == "agent:codex"
    assert chain[1].predecessor_id == chain[0].evidence.id
    assert "authorization_token" not in caplog.text
    assert "capability" not in caplog.text.casefold()

    divergent = json.loads(json.dumps(arguments))
    divergent["draft"]["basis"] = "PRIVATE-DIVERGENT"
    rejected = await server.call_tool("intent_advisory_preflight", divergent)
    assert rejected.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    } == replayed
    altered_same_turn = json.loads(json.dumps(arguments))
    altered_same_turn["conversation_ref"] = codex_conversation_ref(
        "codex:thread-3",
        "turn-7",
        "Format CONTRIBUTING without changing semantics",
    )
    forged_ref = json.loads(json.dumps(arguments))
    forged_ref["conversation_ref"] = arguments["conversation_ref"][:-1] + (
        "0" if arguments["conversation_ref"][-1] != "0" else "1"
    )
    for rejected_arguments in (altered_same_turn, forged_ref):
        rejected_turn = await server.call_tool("intent_advisory_preflight", rejected_arguments)
        assert rejected_turn.structured_content == rejected.structured_content
        assert {
            path.relative_to(project).as_posix(): path.read_bytes()
            for path in sorted((project / ".intent").rglob("*"))
            if path.is_file() and not path.is_symlink()
        } == replayed
    policy_path = project / ".intent/approvals/policy.yaml"
    policy_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local"],
                "approvers": ["local"],
                "executors": ["local"],
                "identities": {"local": ["local"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    before_revocation = tuple(runtime.evidence_store.ledger("conversation:codex"))
    revoked = await server.call_tool("intent_advisory_preflight", arguments)
    assert revoked.structured_content == rejected.structured_content
    assert tuple(runtime.evidence_store.ledger("conversation:codex")) == before_revocation


async def test_advisory_conflict_attributes_request_side_as_agent_inference(
    tmp_path: Path,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project, bootstrap = await anyio.to_thread.run_sync(_configured_project, tmp_path)
    runtime = load_runtime(project)
    server = build_server(
        McpReadServices(runtime),
        intent_workflow_services=load_intent_workflow_services(runtime),
    )
    proposed = await server.call_tool(
        "intent_bootstrap_propose", {"submission": bootstrap.model_dump(mode="json")}
    )
    await server.call_tool(
        "intent_proposal_confirm",
        {
            "proposal_id": proposed.structured_content["proposal_id"],
            "proposal_digest": proposed.structured_content["proposal_digest"],
            "confirmed_node_ids": ["intent:local-export", "requirement:csv-export"],
        },
    )
    graph = runtime.graph_store.load()
    node = next(item for item in graph.nodes if item.id == "requirement:csv-export")
    request = "Remove CSV export"
    route = AdvisoryPromptRouter(runtime).route(
        PromptEvent(
            session_id="codex:advisory-conflict",
            turn_id="turn-1",
            repository=str(project),
            actor="local",
            prompt=request,
            created_at=datetime(2026, 8, 28, 12, 2, tzinfo=UTC),
        )
    )

    response = await server.call_tool(
        "intent_advisory_preflight",
        {
            **dict(route.arguments),
            "draft": {
                "classification": "conflicting",
                "basis": "The request removes an active requirement",
                "relevant_node_ids": [node.id],
                "evidence_refs": list(node.evidence_refs),
                "semantic_effects": ["Removes CSV export"],
                "uncertainties": [],
                "questions": [],
                "conflict_claims": ["The active baseline requires CSV export"],
                "requested_scope": ["src/export.py"],
            },
        },
    )

    assert response.structured_content["classification"] == "conflicting"
    case = runtime.case_store.get(response.structured_content["review_case_id"])
    request_side = next(item for item in case.evidence_sides if item.label == "task_request")
    assert request_side.authors == ("agent:codex",)
    assert request_side.source_mode is SourceMode.INFERRED
    assert runtime.evidence_store.get(route.arguments["request_evidence_ref"]).author == (
        "agent:codex"
    )


async def test_advisory_preflight_rejects_direct_forged_and_spliced_human_evidence(
    tmp_path: Path,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id="graph:forged-advisory",
            version=1,
            name="Forged advisory",
            nodes=(
                Node(
                    id="intent:forged-advisory",
                    type=NodeType.PRODUCT_INTENT,
                    label="Reject forged prompt attribution",
                    status="active",
                    created_by="local",
                    created_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
                    last_modified_by="local",
                    last_modified_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
                ),
            ),
            edges=(),
        )
    )
    workflow = load_intent_workflow_services(
        runtime, clock=lambda: datetime(2026, 8, 28, 12, 1, tzinfo=UTC)
    )
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    direct = _valid_raw_workflow_arguments("intent_advisory_preflight")
    before = tuple(runtime.evidence_store.ledger("conversation:codex"))

    rejected = await server.call_tool("intent_advisory_preflight", direct)

    assert rejected.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert tuple(runtime.evidence_store.ledger("conversation:codex")) == before

    first = AdvisoryPromptRouter(runtime).route(
        PromptEvent(
            session_id="codex:thread-forgery",
            turn_id="turn-1",
            repository=str(project),
            actor="local",
            prompt="First exact human request",
            created_at=datetime(2026, 8, 28, 12, 2, tzinfo=UTC),
        )
    )
    second = AdvisoryPromptRouter(runtime).route(
        PromptEvent(
            session_id="codex:thread-forgery",
            turn_id="turn-2",
            repository=str(project),
            actor="local",
            prompt="Second exact hook request",
            created_at=datetime(2026, 8, 28, 12, 3, tzinfo=UTC),
        )
    )
    spliced = {**direct, **first.arguments}
    spliced["request_evidence_ref"] = second.arguments["request_evidence_ref"]
    before_splice = tuple(runtime.evidence_store.ledger("conversation:codex"))

    rejected_splice = await server.call_tool("intent_advisory_preflight", spliced)

    assert rejected_splice.structured_content == rejected.structured_content
    assert tuple(runtime.evidence_store.ledger("conversation:codex")) == before_splice


@pytest.mark.parametrize(
    "tamper",
    [
        "content",
        "connector_id",
        "connector_type",
        "source_locator",
        "external_object_id",
        "author",
        "acl",
        "external_version",
        "current_version",
    ],
)
async def test_advisory_preflight_authenticates_exact_current_hook_ingestion(
    tmp_path: Path,
    tamper: str,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    observed_at = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    runtime.graph_store.initialize(
        Graph(
            id="graph:advisory-ingestion",
            version=1,
            name="Advisory ingestion",
            nodes=(
                Node(
                    id="intent:advisory-ingestion",
                    type=NodeType.PRODUCT_INTENT,
                    label="Authenticate exact hook evidence",
                    status="active",
                    created_by="local",
                    created_at=observed_at,
                    last_modified_by="local",
                    last_modified_at=observed_at,
                ),
            ),
            edges=(),
        )
    )
    request = "PRIVATE-HOOK-REQUEST-MUST-NOT-CROSS-MCP"
    route = AdvisoryPromptRouter(runtime).route(
        PromptEvent(
            session_id="codex:advisory-ingestion",
            turn_id="turn-1",
            repository=str(project),
            actor="local",
            prompt=request,
            created_at=observed_at,
        )
    )
    if tamper == "current_version":
        ConversationCapture(
            runtime.evidence_store,
            connector_id="conversation:codex",
        ).record_turn(
            conversation_ref=str(route.arguments["conversation_ref"]),
            role="agent",
            author="agent:codex",
            content=request,
            captured_at=observed_at + timedelta(microseconds=1),
            acl=("local",),
        )
    else:
        evidence_path = project / ".intent/evidence/evidence.jsonl"
        lines = evidence_path.read_bytes().splitlines()
        row = json.loads(lines[-1])
        record = row["evidence"]
        if tamper == "content":
            record["payload"]["content"] = "PRIVATE-ALTERED-HOOK-TEXT"
        elif tamper == "connector_id":
            row["connector_id"] = "conversation:other"
        elif tamper == "connector_type":
            record["connector_type"] = "conversation-other"
        elif tamper == "source_locator":
            record["source_locator"] += ":altered"
        elif tamper == "external_object_id":
            record["external_object_id"] = codex_conversation_ref(
                "codex:other-session", "turn-1", request
            )
        elif tamper == "author":
            record["author"] = "agent:other"
        elif tamper == "acl":
            record["acl"] = ["local:other"]
        else:
            record["external_version"] = "sha256:" + "f" * 64
        lines[-1] = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        evidence_path.write_bytes(b"\n".join(lines) + b"\n")

    before = (project / ".intent/evidence/evidence.jsonl").read_bytes()
    arguments = _valid_raw_workflow_arguments("intent_advisory_preflight")
    arguments.update(route.arguments)
    server = build_server(
        McpReadServices(runtime),
        intent_workflow_services=load_intent_workflow_services(runtime),
    )
    rejected = await server.call_tool("intent_advisory_preflight", arguments)

    assert rejected.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert (project / ".intent/evidence/evidence.jsonl").read_bytes() == before
    assert request not in json.dumps(rejected.model_dump(mode="json"))


async def test_official_turn_refs_drive_retry_and_distinct_evidence_pairs(
    tmp_path: Path,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id="graph:turn-ref",
            version=1,
            name="Turn ref",
            nodes=(
                Node(
                    id="intent:turn-ref",
                    type=NodeType.PRODUCT_INTENT,
                    label="Preserve turn evidence",
                    status="active",
                    created_by="local",
                    created_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
                    last_modified_by="local",
                    last_modified_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
                ),
            ),
            edges=(),
        )
    )
    router = AdvisoryPromptRouter(runtime)

    def route_arguments(turn_id: str, request: str) -> dict[str, object]:
        route = router.route(
            PromptEvent(
                session_id="codex:thread-3",
                turn_id=turn_id,
                repository=str(project),
                actor="local",
                prompt=request,
                created_at=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
            )
        )
        conversation_ref = route.arguments["conversation_ref"]
        assert type(conversation_ref) is str
        return dict(route.arguments)

    first_request = "Format README\nwithout changing semantics"
    first_arguments = route_arguments("turn-7", first_request)
    first_ref = str(first_arguments["conversation_ref"])
    workflow = load_intent_workflow_services(
        runtime, clock=lambda: datetime(2026, 8, 28, 12, 1, tzinfo=UTC)
    )
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    base = _valid_raw_workflow_arguments("intent_advisory_preflight")
    base.update(first_arguments)

    first = await server.call_tool("intent_advisory_preflight", base)
    retry = await server.call_tool("intent_advisory_preflight", base)
    identical_next_arguments = route_arguments("turn-8", first_request)
    identical_next_ref = str(identical_next_arguments["conversation_ref"])
    identical_next = json.loads(json.dumps(base))
    identical_next.update(identical_next_arguments)
    second = await server.call_tool("intent_advisory_preflight", identical_next)
    changed_request = "Format CONTRIBUTING without changing semantics"
    changed_next_arguments = route_arguments("turn-9", changed_request)
    changed_next_ref = str(changed_next_arguments["conversation_ref"])
    changed_next = json.loads(json.dumps(base))
    changed_next.update(changed_next_arguments)
    third = await server.call_tool("intent_advisory_preflight", changed_next)

    assert first.structured_content == retry.structured_content
    assert [
        result.structured_content.get("authorized") for result in (first, retry, second, third)
    ] == [True, True, True, True]
    assert first_ref != identical_next_ref != changed_next_ref
    assert tuple(
        len(runtime.evidence_store.chain("conversation:codex", "conversation", ref))
        for ref in (first_ref, identical_next_ref, changed_next_ref)
    ) == (2, 2, 2)
    wire_and_durable = json.dumps(
        [
            first.structured_content,
            retry.structured_content,
            second.structured_content,
            third.structured_content,
        ]
    ).encode() + b"".join(
        path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    )
    assert b"authorization_token" not in wire_and_durable
    assert b"capability" not in wire_and_durable.lower()


async def test_production_advisory_ambiguity_opens_exact_persisted_session(
    tmp_path: Path,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local"],
                "approvers": ["local"],
                "executors": ["local"],
                "identities": {"local": ["local"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id="graph:clarification-restart",
            version=1,
            name="Clarification restart",
            nodes=(
                Node(
                    id="intent:clarification-restart",
                    type=NodeType.PRODUCT_INTENT,
                    label="Persist clarification continuity",
                    status="active",
                    created_by="local",
                    created_at=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
                    last_modified_by="local",
                    last_modified_at=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
                ),
            ),
            edges=(),
        )
    )
    workflow = load_intent_workflow_services(
        runtime, clock=lambda: datetime(2026, 8, 28, 13, 0, tzinfo=UTC)
    )
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    arguments = _valid_raw_workflow_arguments("intent_advisory_preflight")
    request = "Format README\nwithout changing semantics"
    first_event = PromptEvent(
        session_id="codex:thread-3",
        turn_id="turn-7",
        repository=str(project),
        actor="local",
        prompt=request,
        created_at=datetime(2026, 8, 28, 13, 0, tzinfo=UTC),
    )
    first_route = AdvisoryPromptRouter(runtime).route(first_event)
    arguments.update(first_route.arguments)
    draft = arguments["draft"]
    assert type(draft) is dict
    draft.update(
        {
            "classification": "new_or_ambiguous",
            "basis": "Audience is missing",
            "uncertainties": ["Sharing audience"],
            "questions": ["Who may share reports?", "When should sharing expire?"],
            "requested_scope": [],
        }
    )

    response = await server.call_tool("intent_advisory_preflight", arguments)

    payload = response.structured_content
    assert payload["authorized"] is False
    assert payload["classification"] == "new_or_ambiguous"
    assert payload["questions"] == [
        "When should sharing expire?",
        "Who may share reports?",
    ]
    session_payload = payload["context"]["clarification_session"]
    session = runtime.intent_proposals.session(session_payload["id"])
    assert session.model_dump(mode="json") == session_payload
    assert session.conversation_ref == first_route.arguments["conversation_ref"]
    assert session.questions[0].prompt_digest.startswith("sha256:")
    assert "Who may share reports?" not in json.dumps(session_payload)
    assert "authorization_token" not in json.dumps(payload)
    durable = {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    replay = await server.call_tool("intent_advisory_preflight", arguments)
    assert replay.structured_content == payload
    assert {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    } == durable
    hook_retry = AdvisoryPromptRouter(runtime).route(first_event)
    assert hook_retry == first_route
    assert {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.is_symlink()
    } == durable
    restarted = load_runtime(project)
    answer = AdvisoryPromptRouter(restarted).route(
        PromptEvent(
            session_id="codex:thread-3",
            turn_id="turn-8",
            repository=str(project),
            actor="local",
            prompt="Workspace administrators only",
            created_at=datetime(2026, 8, 28, 13, 1, tzinfo=UTC),
        )
    )
    assert answer.action == "human_confirmation_required"
    assert answer.mcp_tool is None
    assert answer.arguments == {}
    answer_ingestion = restarted.evidence_store.ledger("conversation:codex")[-1]
    assert answer_ingestion.evidence.author == "agent:codex"
    assert answer_ingestion.evidence.payload == {
        "role": "agent",
        "content": "Workspace administrators only",
    }
    restarted_server = build_server(
        McpReadServices(restarted),
        intent_workflow_services=load_intent_workflow_services(restarted),
    )
    answer_arguments = {
        "session_id": session.id,
        "question_id": session.questions[0].id,
        "answer_evidence_ref": answer_ingestion.evidence.id,
    }
    before_answer = _transaction_bytes(restarted)
    first_answered = await restarted_server.call_tool(
        "intent_clarification_answer", answer_arguments
    )
    assert first_answered.structured_content == {
        "schema_version": "1",
        "status": "human_confirmation_required",
        "reason": "authenticated_local_human_evidence_required",
    }
    retried_answer = await restarted_server.call_tool(
        "intent_clarification_answer", answer_arguments
    )
    assert retried_answer.structured_content == first_answered.structured_content
    assert _transaction_bytes(restarted) == before_answer
    assert restarted.intent_proposals.session(session.id).answers == ()

    conflicting_route = AdvisoryPromptRouter(restarted).route(
        PromptEvent(
            session_id="codex:thread-3",
            turn_id="turn-9",
            repository=str(project),
            actor="local",
            prompt="Sharing never expires",
            created_at=datetime(2026, 8, 28, 13, 2, tzinfo=UTC),
        )
    )
    assert conflicting_route.action == "human_confirmation_required"
    assert conflicting_route.arguments == {}
    pending = restarted.intent_proposals.session(session.id)
    assert pending.answers == ()
    assert pending.conflicts == ()


async def test_advisory_replay_fixed_fails_after_graph_change_without_new_evidence(
    tmp_path: Path,
) -> None:
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id="graph:advisory-replay",
            version=1,
            name="Advisory replay",
            nodes=(
                Node(
                    id="intent:advisory-replay",
                    type=NodeType.PRODUCT_INTENT,
                    label="Reject stale advisory replays",
                    status="active",
                    created_by="local",
                    created_at=datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
                    last_modified_by="local",
                    last_modified_at=datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
                ),
            ),
            edges=(),
        )
    )
    server = build_server(
        McpReadServices(runtime),
        intent_workflow_services=load_intent_workflow_services(
            runtime, clock=lambda: datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
        ),
    )
    arguments = _valid_raw_workflow_arguments("intent_advisory_preflight")
    route = AdvisoryPromptRouter(runtime).route(
        PromptEvent(
            session_id="codex:advisory-replay",
            turn_id="turn-1",
            repository=str(project),
            actor="local",
            prompt="Format README without changing semantics",
            created_at=datetime(2026, 8, 28, 14, 0, tzinfo=UTC),
        )
    )
    arguments.update(route.arguments)
    accepted = await server.call_tool("intent_advisory_preflight", arguments)
    graph = runtime.graph_store.load()
    runtime.graph_store.initialize(graph.model_copy(update={"version": graph.version + 1}))
    before = tuple(runtime.evidence_store.ledger("conversation:codex"))

    stale = await server.call_tool("intent_advisory_preflight", arguments)

    assert accepted.structured_content["authorized"] is True
    assert stale.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert tuple(runtime.evidence_store.ledger("conversation:codex")) == before


async def test_advisory_authority_race_rolls_back_conversation_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    policy_path = project / ".intent/approvals/policy.yaml"
    policy = {
        "schema_version": 1,
        "contributors": ["local"],
        "approvers": ["local"],
        "executors": ["local"],
        "identities": {"local": ["local"]},
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=True), encoding="utf-8")
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id="graph:advisory-authority-race",
            version=1,
            name="Advisory authority race",
            nodes=(
                Node(
                    id="intent:advisory-authority-race",
                    type=NodeType.PRODUCT_INTENT,
                    label="Bind advisory authority",
                    status="active",
                    created_by="local",
                    created_at=datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
                    last_modified_by="local",
                    last_modified_at=datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
                ),
            ),
            edges=(),
        )
    )
    original = ConversationCapture.record_turn
    raced = False

    def race(self: ConversationCapture, **kwargs: object):
        nonlocal raced
        result = original(self, **kwargs)
        if kwargs.get("role") == "agent" and not raced:
            raced = True
            changed = {**policy, "identities": {"local": ["local", "local:changed"]}}
            policy_path.write_text(yaml.safe_dump(changed, sort_keys=True), encoding="utf-8")
        return result

    monkeypatch.setattr(ConversationCapture, "record_turn", race)
    router = AdvisoryPromptRouter(runtime)

    with pytest.raises(AdvisoryPromptError):
        router.route(
            PromptEvent(
                session_id="codex:authority-race",
                turn_id="turn-1",
                repository=str(project),
                actor="local",
                prompt="Format README without semantic changes",
                created_at=datetime(2026, 8, 28, 15, 0, tzinfo=UTC),
            )
        )

    assert raced is True
    assert tuple(runtime.evidence_store.ledger("conversation:codex")) == ()


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


def _transaction_bytes(runtime) -> dict[str, bytes | None]:
    content: dict[str, bytes | None] = {}
    for name in runtime.transactions.target_names:
        target = runtime.transactions.target_file(name)
        try:
            content[name] = target.read_optional()
        finally:
            target.close()
    content["journal"] = (
        runtime.transactions.journal_path.read_bytes()
        if (runtime.transactions.journal_path.exists())
        else None
    )
    return content


def _proposal_submission_for_session(
    session: ClarificationSession,
) -> ClarificationProposalSubmission:
    timestamp = session.opened_at + timedelta(microseconds=4)
    evidence_refs = (
        session.request_evidence_ref,
        session.classification_evidence_ref,
        *(item.evidence_ref for item in session.questions),
        *(item.evidence_ref for item in session.answers),
    )
    node = Node(
        id="requirement:authority-race",
        type=NodeType.REQUIREMENT,
        label="Authority-bound clarification",
        status="proposed",
        created_by="local",
        created_at=timestamp,
        last_modified_by="local",
        last_modified_at=timestamp,
        source_mode=SourceMode.INFERRED,
        intent_fidelity_confidence=0.8,
        confidence_basis="Clarified conversation",
        last_reassessed_at=timestamp,
        evidence_refs=evidence_refs,
    )
    changeset = ChangeSet(
        id="",
        actor="local",
        timestamp=timestamp,
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
    return ClarificationProposalSubmission(
        session_id=session.id,
        task_id=session.task_id,
        baseline_graph_version=session.baseline_graph_version,
        actor="local",
        timestamp=timestamp,
        evidence_refs=evidence_refs,
        changeset=changeset,
        core_node_ids=(node.id,),
    )


def _write_test_connector_binding(
    project: Path,
    name: str,
    *,
    principal: str = "local:connector-authority-unregistered",
) -> Path:
    source = Path(__file__).resolve().parents[3] / "profiles/mcp/example-bindings/jira.yaml"
    binding = yaml.safe_load(source.read_text(encoding="utf-8"))
    binding["binding"]["actor_principals"] = {"local": [principal]}
    path = project / ".intent/connectors" / name
    path.write_text(yaml.safe_dump(binding, sort_keys=True), encoding="utf-8")
    return path


def _pad_test_connector_binding(path: Path, size: int) -> None:
    content = path.read_bytes()
    padding = size - len(content) - 2
    assert padding >= 0
    path.write_bytes(b"#" + b"x" * padding + b"\n" + content)
    assert path.stat().st_size == size


def _clarification_fifo_snapshot_child(
    workflow: object,
    binding_path: str,
    sending: Connection,
) -> None:
    """Swap one scanned binding before its locked snapshot and report bounded completion."""
    target = Path(binding_path)
    transactions = workflow.runtime.transactions  # type: ignore[attr-defined]
    original_snapshot = transactions.snapshot
    authority_files: dict[str, object] = {}

    def snapshot_after_swap(extras: object = None, **kwargs: object):
        target.unlink()
        os.mkfifo(target)
        return original_snapshot(extras, **kwargs)

    transactions.snapshot = snapshot_after_swap  # type: ignore[method-assign]
    try:
        result = workflow._clarification_authority()  # type: ignore[attr-defined]
        authority_files = result[2]
    except Exception:  # noqa: BLE001 - the child reports only the fixed outcome class
        sending.send("rejected")
    else:
        sending.send("accepted")
    finally:
        for file in authority_files.values():
            file.close()  # type: ignore[attr-defined]
        sending.close()


def _clarification_confirm_fifo_child(
    workflow: object,
    binding_path: str,
    arguments: dict[str, object],
    stage: str,
    sending: Connection,
) -> None:
    """Swap one binding after adapter authentication and report bounded confirmation."""
    target = Path(binding_path)

    def swap_to_fifo() -> None:
        target.unlink()
        os.mkfifo(target)

    if stage == "service_authentication":
        original_authority = workflow._clarification_authority  # type: ignore[attr-defined]

        def authority_then_swap():
            authority = original_authority()
            swap_to_fifo()
            return authority

        workflow._clarification_authority = authority_then_swap  # type: ignore[attr-defined]
    else:
        original_confirm = ProposalConfirmationService._confirm

        def confirm_after_swap(self, *args: object, **kwargs: object):
            swap_to_fifo()
            return original_confirm(self, *args, **kwargs)  # type: ignore[arg-type]

        ProposalConfirmationService._confirm = confirm_after_swap  # type: ignore[method-assign]
    try:
        sending.send(_authenticated_local_confirmation(workflow, arguments))
    except BaseException:  # noqa: BLE001 - the child reports only completion class
        sending.send("raised")
    finally:
        sending.close()


async def _production_clarification_server(
    tmp_path: Path,
    *,
    initial_binding: bool = False,
    initial_binding_names: tuple[str, ...] = (),
):
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project, bootstrap = await anyio.to_thread.run_sync(_configured_project, tmp_path)
    policy = {
        "schema_version": 1,
        "contributors": ["local"],
        "approvers": ["local"],
        "executors": ["local"],
        "identities": {
            "local": [
                "local",
                "local:authority-before",
                "local:connector-authority",
            ]
        },
    }
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
    )
    if initial_binding and initial_binding_names:
        raise ValueError("choose one initial binding fixture")
    binding_names = ("authority.yaml",) if initial_binding else initial_binding_names
    for binding_name in binding_names:
        _write_test_connector_binding(
            project,
            binding_name,
            principal="local:connector-authority",
        )
    runtime = load_runtime(project)
    workflow = load_intent_workflow_services(runtime)
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    proposed = await server.call_tool(
        "intent_bootstrap_propose", {"submission": bootstrap.model_dump(mode="json")}
    )
    await server.call_tool(
        "intent_proposal_confirm",
        {
            "proposal_id": proposed.structured_content["proposal_id"],
            "proposal_digest": proposed.structured_content["proposal_digest"],
            "confirmed_node_ids": ["intent:local-export", "requirement:csv-export"],
        },
    )
    request = "Add authority-bound sharing"
    envelope, classification = _preflight_inputs(
        runtime,
        classification=TaskClassification.NEW_OR_AMBIGUOUS,
        conversation_ref=codex_conversation_ref("codex:authority-race", "turn-1", request),
        request=request,
        questions=("Who may share?",),
    )
    return project, runtime, workflow, server, envelope, classification


def _open_arguments(
    envelope: TaskEnvelope,
    classification: AgentClassificationSubmission,
) -> dict[str, object]:
    return {
        "envelope": envelope.model_dump(mode="json"),
        "classification_evidence_ref": classification.agent_evidence_ref,
        "questions": [{"id": "audience", "prompt": "Who may share?", "required": True}],
        "opened_by": "agent:codex",
        "opened_at": (envelope.created_at + timedelta(microseconds=2))
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _authenticated_answer_arguments(
    runtime,
    session: ClarificationSession,
    *,
    host_session_id: str,
    turn_id: str,
    answer: str,
    captured_at: datetime,
) -> dict[str, object]:
    conversation_ref = codex_conversation_ref(host_session_id, turn_id, answer)
    record = ConversationCapture(
        runtime.evidence_store,
        connector_id="conversation:codex",
    ).record_turn(
        conversation_ref=conversation_ref,
        role="human",
        author=runtime.config.local_actor,
        content=answer,
        captured_at=captured_at,
        acl=tuple(sorted(_principals(runtime, runtime.config))),
    )
    return {
        "session_id": session.id,
        "question_id": session.questions[0].id,
        "answer_evidence_ref": record.id,
    }


def _authenticated_local_confirmation(
    workflow,
    arguments: dict[str, object],
) -> dict[str, object]:
    authority_files: dict[str, object] = {}
    confirmation = None
    try:
        request = ClarificationConfirmRequest.model_validate_json(
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )
        (
            _config,
            _principals_live,
            authority_files,
            authority_preimages,
            authority_membership_digest,
        ) = workflow._clarification_authority()
        confirmation = workflow._clarification_confirmation_service(
            authority_files,
            authority_preimages,
            authority_membership_digest,
        )
        result = confirmation.confirm(
            request.proposal_id,
            proposal_digest=request.proposal_digest,
            actor=request.actor,
            at=request.at,
            selected_node_ids=request.selected_node_ids,
        )
        return result.model_dump(mode="json")
    except Exception:  # noqa: BLE001 - test adapter mirrors the fixed local boundary
        return {
            "schema_version": "1",
            "status": "rejected",
            "reason": "intent_workflow_unavailable",
        }
    finally:
        if confirmation is not None:
            confirmation.close()
        workflow._close_clarification_authority(authority_files)


async def _clarification_operation_arguments(
    runtime,
    server,
    envelope: TaskEnvelope,
    classification: AgentClassificationSubmission,
    operation: str,
) -> dict[str, object]:
    opened = None
    if operation in {"answer", "propose"}:
        opened = await server.call_tool(
            "intent_clarification_open", _open_arguments(envelope, classification)
        )
    if operation == "propose":
        assert opened is not None
        session = runtime.intent_proposals.session(opened.structured_content["session"]["id"])
        answer_arguments = _authenticated_answer_arguments(
            runtime,
            session,
            host_session_id="codex:authority-race",
            turn_id="turn-2",
            answer="Workspace administrators",
            captured_at=envelope.created_at + timedelta(microseconds=5),
        )
        await server.call_tool(
            "intent_clarification_answer",
            answer_arguments,
        )
    session = (
        None
        if opened is None
        else runtime.intent_proposals.session(opened.structured_content["session"]["id"])
    )
    if operation == "open":
        return _open_arguments(envelope, classification)
    if operation == "answer":
        assert session is not None
        return _authenticated_answer_arguments(
            runtime,
            session,
            host_session_id="codex:authority-race",
            turn_id="turn-2",
            answer="Workspace administrators",
            captured_at=envelope.created_at + timedelta(microseconds=5),
        )
    assert session is not None
    return {"submission": _proposal_submission_for_session(session).model_dump(mode="json")}


@pytest.mark.parametrize(
    "tamper",
    [
        "swapped_ref",
        "connector_id",
        "connector_type",
        "source_locator",
        "external_object_id",
        "author",
        "acl",
        "external_version",
        "current_version",
    ],
)
async def test_clarification_answer_authenticates_exact_independent_human_ingestion(
    tmp_path: Path,
    tamper: str,
) -> None:
    (
        project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path)
    opened = await server.call_tool(
        "intent_clarification_open", _open_arguments(envelope, classification)
    )
    session = runtime.intent_proposals.session(opened.structured_content["session"]["id"])
    answer_text = "PRIVATE-EXACT-CLARIFICATION-ANSWER"
    arguments = _authenticated_answer_arguments(
        runtime,
        session,
        host_session_id="codex:authority-race",
        turn_id="turn-2",
        answer=answer_text,
        captured_at=envelope.created_at + timedelta(microseconds=5),
    )
    answer_ref = str(arguments["answer_evidence_ref"])
    if tamper == "swapped_ref":
        arguments["answer_evidence_ref"] = session.request_evidence_ref
    elif tamper == "current_version":
        original = runtime.evidence_store.get(answer_ref)
        ConversationCapture(
            runtime.evidence_store,
            connector_id="conversation:codex",
        ).record_turn(
            conversation_ref=original.external_object_id,
            role="human",
            author=runtime.config.local_actor,
            content=answer_text,
            captured_at=original.observed_at + timedelta(microseconds=1),
            acl=original.acl,
        )
    else:
        evidence_path = project / ".intent/evidence/evidence.jsonl"
        lines = evidence_path.read_bytes().splitlines()
        selected = next(
            index
            for index, line in enumerate(lines)
            if json.loads(line)["evidence"]["id"] == answer_ref
        )
        row = json.loads(lines[selected])
        record = row["evidence"]
        if tamper == "connector_id":
            row["connector_id"] = "conversation:other"
            row["sequence"] = 1
        elif tamper == "connector_type":
            record["connector_type"] = "conversation-other"
        elif tamper == "source_locator":
            record["source_locator"] += ":altered"
        elif tamper == "external_object_id":
            record["external_object_id"] = codex_conversation_ref(
                "codex:other-session", "turn-2", answer_text
            )
        elif tamper == "author":
            record["author"] = "local:other"
        elif tamper == "acl":
            record["acl"] = ["local:other"]
        else:
            record["external_version"] = "sha256:" + "f" * 64
        lines[selected] = json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        evidence_path.write_bytes(b"\n".join(lines) + b"\n")

    before = _transaction_bytes(runtime)
    rejected = await server.call_tool("intent_clarification_answer", arguments)

    assert rejected.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before
    assert answer_text not in json.dumps(rejected.model_dump(mode="json"))


async def _clarification_confirmation_arguments(
    runtime,
    server,
    envelope: TaskEnvelope,
    classification: AgentClassificationSubmission,
) -> dict[str, object]:
    proposed = await server.call_tool(
        "intent_clarification_propose",
        await _clarification_operation_arguments(
            runtime,
            server,
            envelope,
            classification,
            "propose",
        ),
    )
    assert proposed.structured_content["status"] == "proposed"
    return {
        "proposal_id": proposed.structured_content["proposal_id"],
        "proposal_digest": proposed.structured_content["proposal_digest"],
        "actor": "local",
        "at": (envelope.created_at + timedelta(microseconds=7)).isoformat().replace("+00:00", "Z"),
        "selected_node_ids": ["requirement:authority-race"],
    }


async def test_clarification_show_checks_live_authority_and_races_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path, initial_binding=True)
    arguments = await _clarification_confirmation_arguments(
        runtime, server, envelope, classification
    )
    proposal_id = arguments["proposal_id"]
    before = _transaction_bytes(runtime)
    original_get = runtime.intent_proposals.get
    raced = False

    def get_then_change_authority(selected_id: str):
        nonlocal raced
        proposal = original_get(selected_id)
        if not raced:
            raced = True
            (project / ".intent/approvals/policy.yaml").write_text(
                yaml.safe_dump(
                    {
                        "schema_version": 1,
                        "contributors": ["local"],
                        "approvers": ["local"],
                        "executors": ["local"],
                        "identities": {"local": ["local", "local:authority-after"]},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return proposal

    monkeypatch.setattr(runtime.intent_proposals, "get", get_then_change_authority)
    response = await server.call_tool("intent_clarification_show", {"proposal_id": proposal_id})

    assert raced is True
    assert response.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before


async def test_clarification_show_tamper_is_hidden_and_cancellation_identity_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path)
    arguments = await _clarification_confirmation_arguments(
        runtime, server, envelope, classification
    )
    proposal_id = arguments["proposal_id"]
    ledger = project / ".intent/history/intent-proposals.jsonl"
    original_ledger = ledger.read_bytes()
    ledger.write_bytes(original_ledger.replace(b'"status":"proposed"', b'"status":"open"'))

    tampered = await server.call_tool("intent_clarification_show", {"proposal_id": proposal_id})
    assert tampered.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    ledger.write_bytes(original_ledger)

    signal = _CancellationSignal()
    monkeypatch.setattr(
        runtime.intent_proposals,
        "get",
        lambda _proposal_id: (_ for _ in ()).throw(signal),
    )
    with pytest.raises(_CancellationSignal) as caught:
        await server.call_tool("intent_clarification_show", {"proposal_id": proposal_id})
    assert caught.value is signal


@pytest.mark.parametrize("operation", ["open", "answer", "propose"])
async def test_clarification_authority_change_before_each_write_is_an_exact_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    (
        project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path)
    arguments = await _clarification_operation_arguments(
        runtime,
        server,
        envelope,
        classification,
        operation,
    )
    tool_name = f"intent_clarification_{operation}"
    before = _transaction_bytes(runtime)
    raced_policy = {
        "schema_version": 1,
        "contributors": ["local"],
        "approvers": ["local"],
        "executors": ["local"],
        "identities": {"local": ["local", "local:authority-after"]},
    }

    raced = False

    def change_authority() -> None:
        nonlocal raced
        if raced:
            return
        raced = True
        (project / ".intent/approvals/policy.yaml").write_text(
            yaml.safe_dump(raced_policy, sort_keys=True), encoding="utf-8"
        )

    if operation == "open":
        original_record_turn = ConversationCapture.record_turn

        def race_evidence(self, **kwargs: object):
            change_authority()
            return original_record_turn(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ConversationCapture, "record_turn", race_evidence)
    elif operation == "answer":
        original_answer_evidence = ClarificationCoordinator.answer_evidence

        def race_answer(self, session_id: str, **kwargs: object):
            change_authority()
            return original_answer_evidence(self, session_id, **kwargs)

        monkeypatch.setattr(ClarificationCoordinator, "answer_evidence", race_answer)
    else:
        original_ledger_bytes = runtime.intent_proposals.bytes

        def race_proposal_ledger() -> bytes:
            change_authority()
            return original_ledger_bytes()

        monkeypatch.setattr(runtime.intent_proposals, "bytes", race_proposal_ledger)
    response = await server.call_tool(tool_name, arguments)

    assert raced is True
    assert response.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before


@pytest.mark.parametrize("stage", ["service_authentication", "executor_commit"])
async def test_clarification_confirm_rejects_fifo_after_adapter_check_without_blocking(
    tmp_path: Path,
    stage: str,
) -> None:
    (
        project,
        runtime,
        workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path, initial_binding=True)
    arguments = await _clarification_confirmation_arguments(
        runtime,
        server,
        envelope,
        classification,
    )
    before = _transaction_bytes(runtime)
    binding = project / ".intent/connectors/authority.yaml"
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_clarification_confirm_fifo_child,
        args=(workflow, str(binding), arguments, stage, sending),
    )
    process.start()
    sending.close()
    process.join(timeout=2.0)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(timeout=1.0)
    try:
        assert timed_out is False
        assert process.exitcode == 0
        assert receiving.poll(timeout=0.1)
        assert receiving.recv() == {
            "schema_version": "1",
            "status": "rejected",
            "reason": "intent_workflow_unavailable",
        }
    finally:
        receiving.close()
        binding.unlink(missing_ok=True)

    assert _transaction_bytes(runtime) == before


@pytest.mark.parametrize("stage", ["service_authentication", "executor_commit"])
async def test_clarification_confirm_per_file_limit_reaches_every_authority_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    (
        project,
        runtime,
        workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path, initial_binding=True)
    arguments = await _clarification_confirmation_arguments(
        runtime,
        server,
        envelope,
        classification,
    )
    binding = project / ".intent/connectors/authority.yaml"
    before = _transaction_bytes(runtime)
    raced = False
    binding_read_limits: list[int | None] = []
    original_read_optional_nonblocking = secure.SecureFile.read_optional_nonblocking

    def grow_binding() -> None:
        nonlocal raced
        if raced:
            return
        raced = True
        _pad_test_connector_binding(binding, 1_048_577)

    if stage == "service_authentication":
        original_authority = workflow._clarification_authority

        def authority_then_grow():
            authority = original_authority()
            grow_binding()
            return authority

        monkeypatch.setattr(workflow, "_clarification_authority", authority_then_grow)
    else:
        original_confirm = ProposalConfirmationService._confirm

        def confirm_after_growth(self, *args: object, **kwargs: object):
            grow_binding()
            return original_confirm(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ProposalConfirmationService, "_confirm", confirm_after_growth)

    def observe_read_limit(
        self: secure.SecureFile,
        *,
        max_bytes: int | None = None,
    ) -> bytes | None:
        if raced and self.name == binding.name:
            binding_read_limits.append(max_bytes)
        return original_read_optional_nonblocking(self, max_bytes=max_bytes)

    monkeypatch.setattr(secure.SecureFile, "read_optional_nonblocking", observe_read_limit)
    response = _authenticated_local_confirmation(workflow, arguments)

    assert raced is True
    assert binding_read_limits
    assert None not in binding_read_limits
    assert all(limit is not None and limit <= 1_048_576 for limit in binding_read_limits)
    assert response == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before


@pytest.mark.parametrize("stage", ["service_authentication", "executor_commit"])
async def test_clarification_confirm_rejects_aggregate_authority_over_exact_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    binding_names = tuple(f"authority-{index:02d}.yaml" for index in range(9))
    (
        project,
        runtime,
        workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(
        tmp_path,
        initial_binding_names=binding_names,
    )
    arguments = await _clarification_confirmation_arguments(
        runtime,
        server,
        envelope,
        classification,
    )
    bindings = tuple(project / ".intent/connectors" / name for name in binding_names)
    before = _transaction_bytes(runtime)
    raced = False

    def grow_bindings() -> None:
        nonlocal raced
        for binding in bindings:
            _pad_test_connector_binding(binding, 1_000_000)
        raced = True

    if stage == "service_authentication":
        original_authority = workflow._clarification_authority

        def authority_then_grow():
            authority = original_authority()
            grow_bindings()
            return authority

        monkeypatch.setattr(workflow, "_clarification_authority", authority_then_grow)
    else:
        original_confirm = ProposalConfirmationService._confirm

        def confirm_after_growth(self, *args: object, **kwargs: object):
            grow_bindings()
            return original_confirm(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ProposalConfirmationService, "_confirm", confirm_after_growth)
    response = _authenticated_local_confirmation(workflow, arguments)

    assert raced is True
    assert sum(binding.stat().st_size for binding in bindings) == 9_000_000
    assert response == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before


@pytest.mark.parametrize(
    "membership_change",
    ["create", "delete", "rename", "content", "fifo", "oversize"],
)
async def test_clarification_confirm_binds_current_membership_through_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    membership_change: str,
) -> None:
    initial_binding = membership_change != "create"
    (
        project,
        runtime,
        workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(
        tmp_path,
        initial_binding=initial_binding,
    )
    arguments = await _clarification_confirmation_arguments(
        runtime, server, envelope, classification
    )
    before = _transaction_bytes(runtime)
    connector_directory = project / ".intent/connectors"
    binding = connector_directory / "authority.yaml"
    original_confirm = ProposalConfirmationService._confirm
    raced = False

    def change_membership() -> None:
        nonlocal raced
        if raced:
            return
        raced = True
        if membership_change == "create":
            _write_test_connector_binding(
                project,
                "authority-added.yaml",
                principal="local:connector-authority",
            )
        elif membership_change == "delete":
            binding.unlink()
        elif membership_change == "rename":
            binding.rename(connector_directory / "authority-renamed.yaml")
        elif membership_change == "content":
            _write_test_connector_binding(
                project,
                "authority.yaml",
                principal="local:authority-before",
            )
        elif membership_change == "fifo":
            binding.unlink()
            os.mkfifo(binding)
        else:
            _pad_test_connector_binding(binding, 1_048_577)

    def confirm_after_change(self, *args: object, **kwargs: object):
        change_membership()
        return original_confirm(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ProposalConfirmationService, "_confirm", confirm_after_change)
    response = _authenticated_local_confirmation(workflow, arguments)

    assert raced is True
    assert response == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before


async def test_clarification_scan_to_snapshot_fifo_swap_fails_without_blocking(
    tmp_path: Path,
) -> None:
    (
        project,
        runtime,
        workflow,
        _server,
        _envelope,
        _classification,
    ) = await _production_clarification_server(tmp_path, initial_binding=True)
    before = _transaction_bytes(runtime)
    binding = project / ".intent/connectors/authority.yaml"
    context = multiprocessing.get_context("fork")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_clarification_fifo_snapshot_child,
        args=(workflow, str(binding), sending),
    )
    process.start()
    sending.close()
    process.join(timeout=2.0)
    timed_out = process.is_alive()
    if timed_out:
        process.terminate()
        process.join(timeout=1.0)
    try:
        assert timed_out is False
        assert process.exitcode == 0
        assert receiving.poll(timeout=0.1)
        assert receiving.recv() == "rejected"
    finally:
        receiving.close()
        binding.unlink(missing_ok=True)

    assert _transaction_bytes(runtime) == before


@pytest.mark.parametrize("operation", ["open", "answer", "propose"])
async def test_clarification_scan_to_snapshot_growth_never_uses_an_unbounded_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    (
        project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path, initial_binding=True)
    arguments = await _clarification_operation_arguments(
        runtime,
        server,
        envelope,
        classification,
        operation,
    )
    binding = project / ".intent/connectors/authority.yaml"
    before = _transaction_bytes(runtime)
    original_snapshot = runtime.transactions.snapshot
    original_read_descriptor = secure._read_descriptor
    raced = False
    binding_read_limits: list[int | None] = []

    def grow_after_scan(extras: object = None, **kwargs: object):
        nonlocal raced
        if extras is not None and not raced:
            raced = True
            _pad_test_connector_binding(binding, 2_097_152)
        return original_snapshot(extras, **kwargs)

    def observe_read_limit(
        descriptor: int,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        opened = os.fstat(descriptor)
        current = os.stat(binding, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
            binding_read_limits.append(max_bytes)
        return original_read_descriptor(descriptor, max_bytes=max_bytes)

    monkeypatch.setattr(runtime.transactions, "snapshot", grow_after_scan)
    monkeypatch.setattr(secure, "_read_descriptor", observe_read_limit)

    response = await server.call_tool(f"intent_clarification_{operation}", arguments)

    assert raced is True
    assert None not in binding_read_limits
    assert all(limit is not None and limit <= 1_048_576 for limit in binding_read_limits)
    assert response.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before


@pytest.mark.parametrize("operation", ["open", "answer", "propose"])
@pytest.mark.parametrize("membership_change", ["create", "rename", "delete"])
async def test_clarification_binding_membership_change_before_each_write_is_an_exact_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    membership_change: str,
) -> None:
    initial_binding = membership_change == "delete"
    (
        project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(
        tmp_path,
        initial_binding=initial_binding,
    )
    arguments = await _clarification_operation_arguments(
        runtime,
        server,
        envelope,
        classification,
        operation,
    )
    before = _transaction_bytes(runtime)
    connector_directory = project / ".intent/connectors"
    if membership_change == "rename":
        ignored = _write_test_connector_binding(project, "authority.pending")

    raced = False

    def change_membership() -> None:
        nonlocal raced
        if raced:
            return
        raced = True
        if membership_change == "create":
            _write_test_connector_binding(project, "authority-added.yaml")
        elif membership_change == "rename":
            ignored.rename(connector_directory / "authority-renamed.yaml")
        else:
            (connector_directory / "authority.yaml").unlink()

    if operation == "open":
        original_record_turn = ConversationCapture.record_turn

        def race_evidence(self, **kwargs: object):
            change_membership()
            return original_record_turn(self, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ConversationCapture, "record_turn", race_evidence)
    elif operation == "answer":
        original_answer_evidence = ClarificationCoordinator.answer_evidence

        def race_answer(self, session_id: str, **kwargs: object):
            change_membership()
            return original_answer_evidence(self, session_id, **kwargs)

        monkeypatch.setattr(ClarificationCoordinator, "answer_evidence", race_answer)
    else:
        original_ledger_bytes = runtime.intent_proposals.bytes

        def race_proposal_ledger() -> bytes:
            change_membership()
            return original_ledger_bytes()

        monkeypatch.setattr(runtime.intent_proposals, "bytes", race_proposal_ledger)

    response = await server.call_tool(f"intent_clarification_{operation}", arguments)

    assert raced is True
    assert response.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before
    if membership_change in {"create", "rename"}:
        from intent_engineering.integrations.mcp_server.intent_workflow import (
            load_intent_workflow_services,
        )

        fresh_workflow = load_intent_workflow_services(runtime)
        fresh_server = build_server(
            McpReadServices(runtime),
            intent_workflow_services=fresh_workflow,
        )
        fresh = await fresh_server.call_tool(
            "intent_clarification_open",
            _open_arguments(envelope, classification),
        )
        assert fresh.structured_content == {
            "schema_version": "1",
            "status": "rejected",
            "reason": "intent_workflow_unavailable",
        }


@pytest.mark.parametrize("boundary", ["count", "depth", "per-file", "aggregate"])
async def test_clarification_binding_scan_bounds_fail_before_canonical_mutation(
    tmp_path: Path,
    boundary: str,
) -> None:
    (
        project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path)
    connector_directory = project / ".intent/connectors"
    if boundary == "count":
        for index in range(257):
            _write_test_connector_binding(
                project,
                f"binding-{index:03}.yaml",
                principal="local:connector-authority",
            )
    elif boundary == "depth":
        binding = _write_test_connector_binding(
            project,
            "too-deep.yaml",
            principal="local:connector-authority",
        )
        nested = connector_directory.joinpath(*(f"level-{index}" for index in range(9)))
        nested.mkdir(parents=True)
        binding.rename(nested / "binding.yaml")
    elif boundary == "per-file":
        binding = _write_test_connector_binding(
            project,
            "oversized.yaml",
            principal="local:connector-authority",
        )
        _pad_test_connector_binding(binding, 1_048_577)
    else:
        for index in range(9):
            binding = _write_test_connector_binding(
                project,
                f"aggregate-{index}.yaml",
                principal="local:connector-authority",
            )
            _pad_test_connector_binding(binding, 1_000_000)
    before = _transaction_bytes(runtime)

    response = await server.call_tool(
        "intent_clarification_open",
        _open_arguments(envelope, classification),
    )

    assert response.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "intent_workflow_unavailable",
    }
    assert _transaction_bytes(runtime) == before


@pytest.mark.parametrize(
    "question_id",
    ["audience\x00private", "x" * 257],
)
async def test_clarification_question_id_bounds_fail_before_production_persistence(
    tmp_path: Path,
    question_id: str,
) -> None:
    (
        _project,
        runtime,
        _workflow,
        server,
        envelope,
        classification,
    ) = await _production_clarification_server(tmp_path)
    arguments = _open_arguments(envelope, classification)
    arguments["questions"][0]["id"] = question_id
    before = _transaction_bytes(runtime)

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool("intent_clarification_open", arguments)

    assert _transaction_bytes(runtime) == before


async def test_clarification_question_id_subclass_is_rejected_without_behavior(
    tmp_path: Path,
) -> None:
    accessed: list[str] = []

    class HostileQuestionId(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            accessed.append("encode")
            raise RuntimeError("PRIVATE-HOSTILE-QUESTION-ID")

    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    arguments = _valid_raw_workflow_arguments("intent_clarification_open")
    arguments["questions"][0]["id"] = HostileQuestionId("audience")

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool("intent_clarification_open", arguments)

    assert accessed == []
    assert workflow.calls == []


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-26T12:00:03+00:00",
        "2026-08-26T17:30:03+05:30",
        "2026-08-26 12:00:03Z",
        "2026-08-26T12:00:03.000000Z",
    ],
)
@pytest.mark.parametrize("location", ["submission", "changeset"])
async def test_clarification_proposal_requires_canonical_nested_utc_z_timestamps(
    tmp_path: Path,
    timestamp: str,
    location: str,
) -> None:
    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    arguments = _valid_raw_workflow_arguments("intent_clarification_propose")
    submission = arguments["submission"]
    assert type(submission) is dict
    target = submission if location == "submission" else submission["changeset"]
    assert type(target) is dict
    target["timestamp"] = timestamp

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool("intent_clarification_propose", arguments)

    assert workflow.calls == []


async def test_nested_clarification_timestamp_subclass_never_executes_behavior(
    tmp_path: Path,
) -> None:
    accessed: list[str] = []

    class HostileTimestamp(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            accessed.append("encode")
            raise RuntimeError("PRIVATE-HOSTILE-TIMESTAMP")

        def endswith(self, *args: object, **kwargs: object) -> bool:
            accessed.append("endswith")
            raise RuntimeError("PRIVATE-HOSTILE-TIMESTAMP")

    workflow = _FakeWorkflow()
    server = build_server(_services(tmp_path), intent_workflow_services=workflow)
    arguments = _valid_raw_workflow_arguments("intent_clarification_propose")
    submission = arguments["submission"]
    assert type(submission) is dict
    changeset = submission["changeset"]
    assert type(changeset) is dict
    changeset["timestamp"] = HostileTimestamp("2026-08-26T12:00:03Z")

    with pytest.raises(ToolError, match="invalid intent workflow arguments"):
        await server.call_tool("intent_clarification_propose", arguments)

    assert accessed == []
    assert workflow.calls == []


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
    after_restart = await restarted.call_tool("intent_authorization_verify", base_verification)
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


async def test_production_clarification_lifecycle_preserves_evidence_and_exact_confirmation(
    tmp_path: Path,
) -> None:
    """The public ports reuse held production services without exposing conversation bodies."""
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        load_intent_workflow_services,
    )

    project, bootstrap = await anyio.to_thread.run_sync(_configured_project, tmp_path)
    policy = {
        "schema_version": 1,
        "contributors": ["local"],
        "approvers": ["local"],
        "executors": ["local"],
        "identities": {"local": ["local", "local:authoritative-alias"]},
    }
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
    )
    runtime = load_runtime(project)
    workflow = load_intent_workflow_services(runtime)
    server = build_server(McpReadServices(runtime), intent_workflow_services=workflow)
    proposed_baseline = await server.call_tool(
        "intent_bootstrap_propose",
        {"submission": bootstrap.model_dump(mode="json")},
    )
    await server.call_tool(
        "intent_proposal_confirm",
        {
            "proposal_id": proposed_baseline.structured_content["proposal_id"],
            "proposal_digest": proposed_baseline.structured_content["proposal_digest"],
            "confirmed_node_ids": ["intent:local-export", "requirement:csv-export"],
        },
    )
    request = "Add report sharing"
    envelope, classification = _preflight_inputs(
        runtime,
        classification=TaskClassification.NEW_OR_AMBIGUOUS,
        conversation_ref=codex_conversation_ref("codex:clarification-lifecycle", "turn-1", request),
        request=request,
        questions=("Who may share reports?",),
    )
    preflight = await server.call_tool(
        "intent_preflight",
        {
            "envelope": envelope.model_dump(mode="json"),
            "submission": classification.model_dump(mode="json"),
        },
    )
    assert "authorization_token" not in preflight.structured_content

    base = envelope.created_at
    question_text = "Who may share reports? [question-only marker]"
    answer_text = "Workspace administrators [answer-only marker]"
    opened = await server.call_tool(
        "intent_clarification_open",
        {
            "envelope": envelope.model_dump(mode="json"),
            "classification_evidence_ref": classification.agent_evidence_ref,
            "questions": [{"id": "audience", "prompt": question_text, "required": True}],
            "opened_by": "agent:codex",
            "opened_at": (base + timedelta(microseconds=2)).isoformat().replace("+00:00", "Z"),
        },
    )
    session_id = opened.structured_content["session"]["id"]
    session = runtime.intent_proposals.session(session_id)
    answer_arguments = _authenticated_answer_arguments(
        runtime,
        session,
        host_session_id="codex:clarification-lifecycle",
        turn_id="turn-2",
        answer=answer_text,
        captured_at=base + timedelta(microseconds=5),
    )
    answered = await server.call_tool(
        "intent_clarification_answer",
        answer_arguments,
    )
    session = runtime.intent_proposals.session(session_id)
    assert tuple(item.author for item in session.questions) == ("agent:codex",)
    assert tuple(item.actor for item in session.answers) == ("local",)
    assert (
        "local:authoritative-alias"
        in runtime.evidence_store.get(session.answers[0].evidence_ref).acl
    )
    evidence_refs = (
        session.request_evidence_ref,
        session.classification_evidence_ref,
        *(item.evidence_ref for item in session.questions),
        *(item.evidence_ref for item in session.answers),
    )
    proposal_at = base + timedelta(microseconds=6)
    node = Node(
        id="requirement:report-sharing",
        type=NodeType.REQUIREMENT,
        label="Workspace administrators may share reports",
        status="proposed",
        created_by="local",
        created_at=proposal_at,
        last_modified_by="local",
        last_modified_at=proposal_at,
        source_mode=SourceMode.INFERRED,
        intent_fidelity_confidence=0.8,
        confidence_basis="Clarified conversation",
        last_reassessed_at=proposal_at,
        evidence_refs=evidence_refs,
    )
    changeset = ChangeSet(
        id="",
        actor="local",
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
    submission = ClarificationProposalSubmission(
        session_id=session.id,
        task_id=session.task_id,
        baseline_graph_version=session.baseline_graph_version,
        actor="local",
        timestamp=proposal_at,
        evidence_refs=evidence_refs,
        changeset=changeset,
        core_node_ids=(node.id,),
    )
    proposed = await server.call_tool(
        "intent_clarification_propose",
        {"submission": submission.model_dump(mode="json")},
    )
    proposal_id = proposed.structured_content["proposal_id"]
    shown = await server.call_tool(
        "intent_clarification_show",
        {"proposal_id": proposal_id},
    )
    stored = runtime.intent_proposals.get(proposal_id)
    expected_preview = stored.model_dump(mode="json")
    expected_preview["proposal_id"] = expected_preview.pop("id")
    expected_preview["proposal_digest"] = stored.digest
    assert shown.structured_content == {
        "schema_version": 1,
        "status": "proposed",
        "proposal": expected_preview,
    }
    graph_before = runtime.graph_store.load()
    mismatched_digest = await server.call_tool(
        "intent_clarification_confirm",
        {
            "proposal_id": proposal_id,
            "proposal_digest": "sha256:" + "0" * 64,
            "actor": "local",
            "at": (base + timedelta(microseconds=7)).isoformat().replace("+00:00", "Z"),
            "selected_node_ids": [node.id],
        },
    )
    human_required = {
        "schema_version": "1",
        "status": "human_confirmation_required",
        "reason": "authenticated_local_human_evidence_required",
    }
    assert mismatched_digest.structured_content == human_required
    assert runtime.graph_store.load() == graph_before
    incorrect = await server.call_tool(
        "intent_clarification_confirm",
        {
            "proposal_id": proposal_id,
            "proposal_digest": stored.digest,
            "actor": "local",
            "at": (base + timedelta(microseconds=7)).isoformat().replace("+00:00", "Z"),
            "selected_node_ids": ["requirement:not-in-proposal"],
        },
    )
    assert incorrect.structured_content == human_required
    assert runtime.graph_store.load() == graph_before
    confirmed = await server.call_tool(
        "intent_clarification_confirm",
        {
            "proposal_id": proposal_id,
            "proposal_digest": stored.digest,
            "actor": "local",
            "at": (base + timedelta(microseconds=8)).isoformat().replace("+00:00", "Z"),
            "selected_node_ids": [node.id],
        },
    )

    assert confirmed.structured_content == human_required
    assert runtime.graph_store.load() == graph_before
    assert tuple(
        item.event_type for item in runtime.intent_proposals.clarification_events(session_id)
    ) == ("opened", "answered", "proposed")

    locally_confirmed = _authenticated_local_confirmation(
        workflow,
        {
            "proposal_id": proposal_id,
            "proposal_digest": stored.digest,
            "actor": "local",
            "at": (base + timedelta(microseconds=8)).isoformat().replace("+00:00", "Z"),
            "selected_node_ids": [node.id],
        },
    )
    events = runtime.intent_proposals.clarification_events(session_id)
    assert tuple(item.event_type for item in events) == (
        "opened",
        "answered",
        "proposed",
        "closed",
    )
    assert tuple(item.predecessor_event_id for item in events) == (
        None,
        events[0].id,
        events[1].id,
        events[2].id,
    )
    assert runtime.intent_proposals.get(proposal_id).proposed_by == "local"
    assert locally_confirmed["status"] == "applied"
    assert runtime.graph_store.load().nodes[-1].id == node.id
    hidden_after_decision = await server.call_tool(
        "intent_clarification_show", {"proposal_id": proposal_id}
    )
    missing = await server.call_tool(
        "intent_clarification_show", {"proposal_id": "proposal:sha256:" + "f" * 64}
    )
    assert (
        hidden_after_decision.structured_content
        == missing.structured_content
        == {
            "schema_version": "1",
            "status": "rejected",
            "reason": "intent_workflow_unavailable",
        }
    )
    public = json.dumps(
        [
            opened.structured_content,
            answered.structured_content,
            proposed.structured_content,
            shown.structured_content,
            mismatched_digest.structured_content,
            incorrect.structured_content,
            confirmed.structured_content,
        ]
    )
    assert question_text not in public
    assert answer_text not in public
    assert "authorization" not in public.casefold()
    durable_proposals = (project / ".intent/history/intent-proposals.jsonl").read_text(
        encoding="utf-8"
    )
    durable_evidence = (project / ".intent/evidence/evidence.jsonl").read_text(encoding="utf-8")
    assert question_text not in durable_proposals
    assert answer_text not in durable_proposals
    assert question_text in durable_evidence
    assert answer_text in durable_evidence


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
    same_version_denied = await server.call_tool("intent_authorization_verify", arguments)
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

    def swap_then_verify(*args: object, **kwargs: object) -> AuthorizationVerification:
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
                marker not in repr(error) and marker not in _repository_traceback_values(error),
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
            "intent_advisory_preflight",
            {
                **_valid_raw_workflow_arguments("intent_advisory_preflight"),
                "request": "PRIVATE-RAW-HUMAN-PROMPT",
            },
        ),
        (
            "intent_clarification_answer",
            {
                **_valid_raw_workflow_arguments("intent_clarification_answer"),
                "answer": "PRIVATE-RAW-HUMAN-ANSWER",
                "actor": "PRIVATE-CALLER-ACTOR",
                "answered_at": "2026-08-26T12:00:05Z",
                "acl": ["PRIVATE-CALLER-ACL"],
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
                "intent_clarification_open",
                {
                    **_valid_raw_workflow_arguments("intent_clarification_open"),
                    field_name: ["PRIVATE-CALLER-SUPERSET"],
                },
            )
            for field_name in ("principals", "actor_aliases", "acl")
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
            **{key: value for key, value in dict.items(arguments) if key != first_key},
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
    server = build_server(_services(tmp_path), intent_workflow_services=_FakeWorkflow())

    def cancel(*_args: object, **_kwargs: object) -> str:
        raise signal

    monkeypatch.setattr(workflow_module.json, "dumps", cancel)
    with pytest.raises(_CancellationSignal) as caught:
        await server.call_tool("intent_preflight", arguments)

    assert caught.value is signal
    assert "PRIVATE-RAW-PREFLIGHT-CANCELLATION" not in _repository_traceback_values(caught.value)


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
    failed = build_server(_services(tmp_path), intent_workflow_services=_FailingWorkflow())
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
