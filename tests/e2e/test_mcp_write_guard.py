"""Black-box stdio guard for the production MCP mutation surface."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from intent_engineering.cli.connectors import ConnectorCatalog
from intent_engineering.cli.writes import WriteWorkflow, write_workflow
from intent_engineering.core.models import ResolutionAction
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server import mutations as mutation_module
from intent_engineering.integrations.mcp_server.mutations import (
    ChangeSetProposalStore,
    McpMutationServices,
)
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from tests.e2e.test_cli_write_approval import (
    _approval_project,
    _FakeTerminal,
    _seed_review_state,
    _set_actor,
    _workflow_with_runtime,
    _WriteRuntime,
)

pytestmark = pytest.mark.anyio


async def test_production_stdio_exposes_no_approval_creation_and_rejects_missing_approval(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    executable = Path(sys.executable).with_name("intent")
    parameters = StdioServerParameters(
        command=str(executable),
        args=["mcp", "--project", str(project)],
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    )

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with (
            stdio_client(parameters, errlog=errlog) as streams,
            ClientSession(*streams) as client,
        ):
            initialized = await client.initialize()
            tools = await client.list_tools()
            prompts = await client.list_prompts()
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            rejected = await client.call_tool(
                "intent_write_execute",
                {
                    "plan_id": "write-plan:sha256:" + "a" * 64,
                    "approval_id": "approval:sha256:" + "b" * 64,
                },
            )
        errlog.seek(0)
        diagnostics = errlog.read()

    names = {tool.name for tool in tools.tools}
    assert {
        "intent_changeset_propose",
        "intent_reconciliation_propose",
        "intent_write_preview",
        "intent_write_execute",
    }.issubset(names)
    assert not {"intent_approve", "intent_write_approve"}.intersection(names)
    assert initialized.instructions is not None
    assert "cannot create approvals" in initialized.instructions
    public_names = {
        *(prompt.name for prompt in prompts.prompts),
        *(str(resource.uri) for resource in resources.resources),
        *(template.uri_template for template in templates.resource_templates),
    }
    assert not any("approv" in name.lower() for name in public_names)
    assert rejected.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "approval_not_found",
    }
    assert "Traceback" not in diagnostics


def _mutation_services(workflow) -> McpMutationServices:
    runtime = workflow.catalog.runtime
    approvals = runtime.workspace_directory.subdirectory("approvals")
    try:
        proposals = ChangeSetProposalStore(approvals.file("changeset-proposals.jsonl"))
    finally:
        approvals.close()
    return McpMutationServices(runtime, workflow, proposals)


def _live_fake_workflow(runtime, provider: _WriteRuntime) -> WriteWorkflow:
    loaded = write_workflow(runtime)
    catalog = ConnectorCatalog(
        runtime,
        loaded.catalog.configured,
        mcp_runtime=provider,  # type: ignore[arg-type]
    )
    return WriteWorkflow(catalog, loaded.plans, loaded.approvals, loaded.policy, loaded.actor)


class _CancellingWriteRuntime(_WriteRuntime):
    def __init__(self, cancellation: asyncio.CancelledError) -> None:
        super().__init__()
        self.cancellation = cancellation

    async def call(self, server: object, name: str, arguments: dict) -> object:
        if name == "get_issue":
            raise self.cancellation
        return await super().call(server, name, arguments)  # type: ignore[arg-type]


def _invalidate_live_write_configuration(
    project: Path,
    mode: str,
    *,
    actor: str,
    operation: str,
) -> None:
    policy_path = project / ".intent/approvals/policy.yaml"
    connector_path = project / ".intent/connectors/jira.yaml"
    if mode == "policy_deleted":
        policy_path.unlink()
        return
    if mode == "policy_corrupt":
        policy_path.write_text("policy: [unterminated", encoding="utf-8")
        return
    if mode == "role_revoked":
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        role = "contributors" if operation == "preview" else "executors"
        replacement = "local:security" if actor != "local:security" else "local:proposer"
        policy[role] = [replacement]
        policy_path.write_text(yaml.safe_dump(policy, sort_keys=True), encoding="utf-8")
        return
    if mode == "connector_deleted":
        connector_path.unlink()
        return
    if mode == "connector_drift":
        connector = yaml.safe_load(connector_path.read_text(encoding="utf-8"))
        semantic = "fetch_issue" if operation == "preview" else "update_issue"
        connector["binding"]["tools"][semantic] += "_changed"
        connector_path.write_text(
            yaml.safe_dump(connector, sort_keys=True),
            encoding="utf-8",
        )
        return
    raise AssertionError(mode)


async def test_production_preview_then_separate_human_approval_executes_exactly_once(
    tmp_path: Path,
) -> None:
    project = _approval_project(tmp_path)
    provider = _WriteRuntime()
    _set_actor(project, "local:proposer")
    proposer = _workflow_with_runtime(project, provider)
    _seed_review_state(proposer)
    preview_server = build_server(
        McpReadServices(proposer.catalog.runtime),
        mutation_services=_mutation_services(proposer),
    )

    previewed = await preview_server.call_tool(
        "intent_write_preview",
        {
            "case_id": "case-write-1",
            "connector_id": "jira-local",
            "operation": "update_issue",
            "fields": {
                "summary": "Keep exports local by default",
                "description": "Enable centralized export only after independent approval.",
                "status": "Approved",
            },
            "action": "update_requirement",
        },
    )
    plan_id = previewed.structured_content["plan_id"]

    assert previewed.structured_content["status"] == "previewed"
    assert proposer.approvals.list() == ()
    assert [name for name, _arguments in provider.calls] == ["get_issue"]

    rejected = await preview_server.call_tool(
        "intent_write_execute",
        {
            "plan_id": plan_id,
            "approval_id": "approval:sha256:" + "f" * 64,
        },
    )

    assert rejected.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "approval_not_found",
    }
    assert [name for name, _arguments in provider.calls] == ["get_issue"]

    _set_actor(project, "local:reviewer")
    reviewer = _workflow_with_runtime(project, provider)
    approval = reviewer.approve(
        plan_id,
        _FakeTerminal(True, f"approve {plan_id}"),
        now=datetime.now(UTC),
    )
    execute_server = build_server(
        McpReadServices(reviewer.catalog.runtime),
        mutation_services=_mutation_services(reviewer),
    )

    executed = await execute_server.call_tool(
        "intent_write_execute",
        {"plan_id": plan_id, "approval_id": approval.id},
    )

    assert executed.structured_content["status"] == "succeeded"
    assert executed.structured_content["receipt"]["approval_id"] == approval.id
    assert [name for name, _arguments in provider.calls].count("update_issue") == 1


async def test_production_preview_cancellation_drops_requested_fields_from_traceback(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-PRODUCTION-PREVIEW-CANCEL-9137"
    cancellation = asyncio.CancelledError("preview cancelled")
    project = _approval_project(tmp_path)
    _set_actor(project, "local:proposer")
    provider = _CancellingWriteRuntime(cancellation)
    workflow = _workflow_with_runtime(project, provider)
    _seed_review_state(workflow)

    with pytest.raises(asyncio.CancelledError) as caught:
        await _mutation_services(workflow).preview_write(
            "case-write-1",
            "jira-local",
            "update_issue",
            {
                "summary": secret,
                "description": "Independent review remains mandatory.",
                "status": "Approved",
            },
            "update_requirement",
        )

    assert caught.value is cancellation
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert secret not in "".join(repository_locals)


@pytest.mark.parametrize(
    "mode",
    (
        "policy_deleted",
        "policy_corrupt",
        "role_revoked",
        "connector_deleted",
        "connector_drift",
    ),
)
async def test_preview_reloads_live_policy_and_connector_before_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    project = _approval_project(tmp_path)
    provider = _WriteRuntime()
    _set_actor(project, "local:proposer")
    initial = _workflow_with_runtime(project, provider)
    _seed_review_state(initial)

    def fresh(runtime):
        return _live_fake_workflow(runtime, provider)

    monkeypatch.setattr(mutation_module, "write_workflow", fresh)
    server = build_server(
        McpReadServices(initial.catalog.runtime),
        mutation_services=mutation_module.load_mutation_services(initial.catalog.runtime),
    )
    _invalidate_live_write_configuration(
        project,
        mode,
        actor="local:proposer",
        operation="preview",
    )

    result = await server.call_tool(
        "intent_write_preview",
        {
            "case_id": "case-write-1",
            "connector_id": "jira-local",
            "operation": "update_issue",
            "fields": {
                "summary": "Keep exports local by default",
                "description": "Independent review remains mandatory.",
                "status": "Approved",
            },
            "action": "update_requirement",
        },
    )

    assert result.structured_content["status"] == "rejected"
    assert provider.calls == []


@pytest.mark.parametrize(
    "mode",
    (
        "policy_deleted",
        "policy_corrupt",
        "role_revoked",
        "connector_deleted",
        "connector_drift",
    ),
)
async def test_execute_reloads_live_policy_and_connector_before_provider_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    project = _approval_project(tmp_path)
    provider = _WriteRuntime()
    _set_actor(project, "local:proposer")
    proposer = _workflow_with_runtime(project, provider)
    _seed_review_state(proposer)
    preview = await proposer.create_preview(
        "case-write-1",
        connector_id="jira-local",
        operation="update_issue",
        requested_fields={
            "summary": "Keep exports local by default",
            "description": "Independent review remains mandatory.",
            "status": "Approved",
        },
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
    )
    _set_actor(project, "local:reviewer")
    reviewer = _workflow_with_runtime(project, provider)
    approval = reviewer.approve(
        preview.id,
        _FakeTerminal(True, f"approve {preview.id}"),
        now=datetime.now(UTC),
    )

    def fresh(runtime):
        return _live_fake_workflow(runtime, provider)

    monkeypatch.setattr(mutation_module, "write_workflow", fresh)
    server = build_server(
        McpReadServices(reviewer.catalog.runtime),
        mutation_services=mutation_module.load_mutation_services(reviewer.catalog.runtime),
    )
    _invalidate_live_write_configuration(
        project,
        mode,
        actor="local:reviewer",
        operation="execute",
    )

    result = await server.call_tool(
        "intent_write_execute",
        {"plan_id": preview.id, "approval_id": approval.id},
    )

    assert result.structured_content["status"] == "rejected"
    assert [name for name, _arguments in provider.calls].count("update_issue") == 0
