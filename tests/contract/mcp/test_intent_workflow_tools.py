"""Official-SDK contracts for reviewed intent-workflow onboarding tools."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.bootstrap import BootstrapSubmission
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


def _services(tmp_path: Path) -> McpReadServices:
    project = tmp_path / "project"
    project.mkdir(parents=True)
    initialize_project(project)
    return McpReadServices(load_runtime(project))


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
    assert proposed_annotations is not None
    assert shown_annotations is not None
    assert confirmed_annotations is not None
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

    assert proposed.structured_content["status"] == "proposed"
    assert shown.structured_content["status"] == "proposed"
    assert confirmed.structured_content["status"] == "activated"
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
    ]


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
