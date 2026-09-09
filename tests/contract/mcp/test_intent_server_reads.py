"""Contract tests for the versioned, ACL-filtered Intent MCP read surface."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from mcp import MCPError
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import INTERNAL_ERROR

from intent_engineering.cli.runtime import Runtime, load_runtime
from intent_engineering.core.models import (
    ClassificationEvent,
    Edge,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    Node,
    NodeType,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    RelationType,
    SourceMode,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices

pytestmark = pytest.mark.anyio

_AT = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
_TOOLS = {
    "intent_context",
    "intent_explain",
    "intent_impact",
    "intent_drift",
    "intent_status",
    "intent_validate",
    "intent_assessment_summary",
    "intent_assessment_scorecard",
    "intent_assessment_gaps",
    "intent_reconcile_list",
    "intent_reconcile_show",
}


def _evidence(evidence_id: str, *, acl: tuple[str, ...]) -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence_id,
        connector_type="fixture",
        external_object_id=evidence_id.removeprefix("evidence:"),
        external_version="1",
        author="provider:asha",
        observed_at=_AT,
        source_locator=f"fixture://{evidence_id}",
        content_hash="sha256:" + evidence_id[-1] * 64,
        payload={"content": f"payload for {evidence_id}"},
        acl=acl,
    )


def _node(node_id: str, node_type: NodeType, evidence_id: str) -> Node:
    labels = {
        "req-local-export": "local export",
        "cap-local-export": "export capability",
    }
    return Node(
        id=node_id,
        type=node_type,
        label=labels.get(node_id, node_id.replace("-", " ")),
        status="active",
        created_by="provider:asha",
        created_at=_AT,
        last_modified_by="provider:asha",
        last_modified_at=_AT,
        source_mode=SourceMode.EXPLICIT,
        intent_fidelity_confidence=0.9,
        confidence_basis="fixture",
        last_reassessed_at=_AT,
        evidence_refs=(evidence_id,),
    )


def _case(case_id: str, evidence_id: str, subject: str) -> ReconciliationCase:
    return ReconciliationCase(
        id=case_id,
        subject_ref=subject,
        case_type=ReconciliationCaseType.CONFLICTING_SOURCES,
        affected_refs=(subject,),
        evidence_sides=(
            EvidenceSide(
                label="conversation",
                claim="requested behavior",
                evidence_refs=(evidence_id,),
                observed_at=_AT,
                authors=("provider:asha",),
                confidence=0.9,
            ),
        ),
        detector_id="fixture",
        fingerprint=("1" if case_id.endswith("public") else "2") * 64,
        created_at=_AT,
        created_by="detector:fixture",
    )


def _runtime(tmp_path: Path) -> Runtime:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    public = _evidence("evidence:public", acl=("local",))
    private = _evidence("evidence:private", acl=("other",))
    runtime.evidence_store.put(public)
    runtime.evidence_store.put(private)
    requirement = _node("req-local-export", NodeType.REQUIREMENT, public.id)
    capability = _node("cap-local-export", NodeType.CAPABILITY, public.id)
    hidden = _node("req-private", NodeType.REQUIREMENT, private.id)
    runtime.graph_store.initialize(
        Graph(
            id="graph:test",
            version=0,
            name="test",
            nodes=(requirement, capability, hidden),
            edges=(
                Edge(
                    id="edge:req-cap",
                    **{"from": requirement.id, "to": capability.id},
                    relation=RelationType.AFFECTS,
                    status="active",
                    created_by="provider:asha",
                    created_at=_AT,
                    last_modified_by="provider:asha",
                    last_modified_at=_AT,
                ),
            ),
        )
    )
    runtime.case_store.put(_case("case:public", public.id, requirement.id))
    runtime.case_store.put(_case("case:private", private.id, hidden.id))
    return runtime


async def test_server_exports_exact_read_only_tool_and_prompt_contract(tmp_path: Path) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    tools = await server.list_tools()
    assert {tool.name for tool in tools} == _TOOLS
    assert all(tool.annotations is not None and tool.annotations.read_only_hint for tool in tools)
    context_schema = next(tool for tool in tools if tool.name == "intent_context").input_schema
    task_ref = context_schema["properties"]["task"]["$ref"].rsplit("/", 1)[-1]
    task_schema = context_schema["$defs"][task_ref]
    assert task_schema["type"] == "string"
    assert task_schema["minLength"] == 1
    assert task_schema["maxLength"] == 4096
    prompts = await server.list_prompts()
    assert {prompt.name for prompt in prompts} == {"prepare_task", "review_reconciliation"}
    resources = await server.list_resources()
    templates = await server.list_resource_templates()
    assert {str(resource.uri) for resource in resources} == {"intent://reports/drift"}
    assert {template.uri_template for template in templates} == {
        "intent://graph/nodes/{node_id}",
        "intent://evidence/{evidence_id}",
        "intent://schemas/{model_name}",
        "intent://cases/{case_id}",
    }
    assert not any("approve" in tool.name or "execute" in tool.name for tool in tools)


@pytest.mark.parametrize(
    ("prompt", "argument", "value"),
    (
        ("prepare_task", "task", ""),
        ("prepare_task", "task", "x" * 4097),
        ("review_reconciliation", "case_id", " "),
        ("review_reconciliation", "case_id", "x" * 513),
    ),
)
async def test_prompt_arguments_are_bounded_before_rendering(
    tmp_path: Path,
    prompt: str,
    argument: str,
    value: str,
) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    with pytest.raises(MCPError, match="invalid intent prompt arguments") as caught:
        await server.get_prompt(prompt, {argument: value})

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("prompt", "arguments"),
    (
        ("PRIVATE-UNKNOWN-MCP-PROMPT", {}),
        ("prepare_task", {"other": "PRIVATE-MISSING-MCP-PROMPT-ARG"}),
    ),
)
async def test_unknown_or_missing_prompt_input_has_one_fixed_error(
    tmp_path: Path,
    prompt: str,
    arguments: dict[str, object],
) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    with pytest.raises(MCPError) as caught:
        await server.get_prompt(prompt, arguments)

    assert caught.value.message == "invalid intent prompt arguments"
    assert "PRIVATE-" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert "PRIVATE-" not in "".join(repository_locals)


async def test_invalid_tool_input_is_fixed_without_echo_or_retained_repository_local(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-MCP-TOOL-ARG-" + "x" * 4097
    server = build_server(McpReadServices(_runtime(tmp_path)))

    with pytest.raises(MCPError) as caught:
        await server.call_tool("intent_context", {"task": secret})

    assert caught.value.message == "invalid intent tool arguments"
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert secret not in "".join(repository_locals)


async def test_missing_required_tool_input_is_fixed_before_sdk_validation_echoes_values(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-MISSING-MCP-ARG-" + "x" * 200
    server = build_server(McpReadServices(_runtime(tmp_path)))

    with pytest.raises(ToolError) as caught:
        await server.call_tool("intent_context", {"format": secret})

    assert caught.value.args == ("invalid intent tool arguments",)
    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert secret not in "".join(repository_locals)


async def test_context_tool_returns_versioned_authorized_pack(tmp_path: Path) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    result = await server.call_tool("intent_context", {"task": "local export", "format": "json"})

    payload = result.structured_content
    assert payload["schema_version"] == "1"
    assert payload["task"] == "local export"
    assert payload["relevant_requirements"][0]["id"] == "req-local-export"
    assert "req-private" not in repr(payload)


async def test_status_explain_impact_and_drift_are_versioned_and_acl_filtered(
    tmp_path: Path,
) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    status = (await server.call_tool("intent_status", {})).structured_content
    explain = (
        await server.call_tool("intent_explain", {"reference": "req-local-export"})
    ).structured_content
    impact = (
        await server.call_tool("intent_impact", {"reference": "req-local-export"})
    ).structured_content
    drift = (await server.call_tool("intent_drift", {"format": "json"})).structured_content

    assert status == {
        "schema_version": "1",
        "project_id": "project",
        "graph_version": 0,
        "node_count": 2,
        "edge_count": 1,
        "evidence_count": 1,
        "open_case_count": 1,
    }
    assert explain["schema_version"] == impact["schema_version"] == "1"
    assert explain["nodes"][0]["id"] == "req-local-export"
    assert impact["dependents"][0]["id"] == "cap-local-export"
    assert [case["id"] for case in drift["cases"]] == ["case:public"]
    assert "case:private" not in repr((explain, impact, drift))


async def test_reconcile_and_validation_tools_use_public_versioned_envelopes(
    tmp_path: Path,
) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    listed = (await server.call_tool("intent_reconcile_list", {})).structured_content
    shown = (
        await server.call_tool("intent_reconcile_show", {"case_id": "case:public"})
    ).structured_content
    validation = (await server.call_tool("intent_validate", {})).structured_content

    assert listed["schema_version"] == shown["schema_version"] == "1"
    assert [case["id"] for case in listed["cases"]] == ["case:public"]
    assert shown["case"]["id"] == "case:public"
    assert validation["schema_version"] == "1"
    assert type(validation["valid"]) is bool


async def test_unauthorized_or_missing_items_are_indistinguishable(tmp_path: Path) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    for reference in ("req-private", "missing"):
        with pytest.raises(MCPError, match="not found"):
            await server.call_tool("intent_explain", {"reference": reference})
    for evidence_id in ("evidence:private", "evidence:missing"):
        with pytest.raises(ResourceNotFoundError, match="not found"):
            await server.read_resource(f"intent://evidence/{evidence_id}")


@pytest.mark.parametrize(
    "uri",
    (
        "intent://unknown/PRIVATE-MCP-RESOURCE-URI",
        "intent://evidence/PRIVATE-MCP-RESOURCE-URI/extra",
        "intent://evidence/../PRIVATE-MCP-RESOURCE-URI",
        "intent://evidence/" + "PRIVATE-MCP-RESOURCE-URI-" * 300,
    ),
)
async def test_every_unknown_or_malformed_resource_uri_has_one_fixed_error(
    tmp_path: Path,
    uri: str,
) -> None:
    server = build_server(McpReadServices(_runtime(tmp_path)))

    with pytest.raises(ResourceNotFoundError) as caught:
        await server.read_resource(uri)

    assert caught.value.args == ("intent resource was not found",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert "PRIVATE-MCP-RESOURCE-URI" not in "".join(repository_locals)


async def test_evidence_resource_preserves_append_history_order(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    first = _evidence("evidence:z-first", acl=("local",)).model_copy(
        update={"external_object_id": "shared-object", "external_version": "1"}
    )
    second = _evidence("evidence:a-second", acl=("local",)).model_copy(
        update={"external_object_id": "shared-object", "external_version": "2"}
    )
    runtime.evidence_store.put(first)
    runtime.evidence_store.put(second)
    server = build_server(McpReadServices(runtime))

    content = next(iter(await server.read_resource("intent://evidence/evidence:z-first")))
    payload = json.loads(content.content)

    assert [version["id"] for version in payload["versions"]] == [
        "evidence:z-first",
        "evidence:a-second",
    ]


async def test_terminal_cases_are_hidden_as_not_found_on_every_surface(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    current = runtime.case_store.get("case:public")
    deferred = current.model_copy(
        update={
            "status": ReconciliationStatus.DEFERRED,
            "history": (
                ClassificationEvent(
                    actor="local",
                    at=_AT,
                    prior=ReconciliationStatus.OPEN,
                    new=ReconciliationStatus.DEFERRED,
                ),
            ),
        }
    )
    runtime.case_store.put(deferred)
    server = build_server(McpReadServices(runtime))

    listed = (
        await server.call_tool("intent_reconcile_list", {"status": "deferred"})
    ).structured_content

    assert listed["cases"] == []
    for tool, arguments in (
        ("intent_reconcile_show", {"case_id": "case:public"}),
        ("intent_explain", {"reference": "case:public"}),
    ):
        with pytest.raises(MCPError, match="not found"):
            await server.call_tool(tool, arguments)
    with pytest.raises(ResourceNotFoundError, match="not found"):
        await server.read_resource("intent://cases/case:public")


async def test_cases_referencing_acl_hidden_graph_objects_are_not_discoverable(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    mixed = _case("case:mixed", "evidence:public", "req-private").model_copy(
        update={"fingerprint": "3" * 64}
    )
    runtime.case_store.put(mixed)
    server = build_server(McpReadServices(runtime))

    listed = (await server.call_tool("intent_reconcile_list", {})).structured_content

    assert [case["id"] for case in listed["cases"]] == ["case:public"]
    for tool, arguments in (
        ("intent_reconcile_show", {"case_id": "case:mixed"}),
        ("intent_explain", {"reference": "case:mixed"}),
    ):
        with pytest.raises(MCPError, match="not found"):
            await server.call_tool(tool, arguments)
    with pytest.raises(ResourceNotFoundError, match="not found"):
        await server.read_resource("intent://cases/case:mixed")


async def test_each_request_reloads_and_resolves_the_configured_local_actor(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(McpReadServices(runtime))
    assert (
        await server.call_tool("intent_explain", {"reference": "req-local-export"})
    ).is_error is False
    config_file = runtime.workspace_directory.file("config.yaml")
    try:
        changed = runtime.config.model_copy(update={"local_actor": "other"})
        config_file.atomic_write(
            yaml.safe_dump(changed.model_dump(mode="json"), sort_keys=True).encode()
        )
    finally:
        config_file.close()

    private = await server.call_tool("intent_explain", {"reference": "req-private"})

    assert private.structured_content["nodes"][0]["id"] == "req-private"
    with pytest.raises(MCPError, match="not found"):
        await server.call_tool("intent_explain", {"reference": "req-local-export"})

    config_file = runtime.workspace_directory.file("config.yaml")
    try:
        mismatched = changed.model_copy(update={"graph_path": ".intent/other.yaml"})
        config_file.atomic_write(
            yaml.safe_dump(mismatched.model_dump(mode="json"), sort_keys=True).encode()
        )
    finally:
        config_file.close()
    with pytest.raises(MCPError, match="unavailable"):
        await server.call_tool("intent_status", {})


async def test_long_lived_server_never_switches_to_a_replacement_project_path(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(McpReadServices(runtime))
    before = (await server.call_tool("intent_validate", {})).structured_content
    moved = tmp_path / "original-project"
    runtime.root.rename(moved)
    runtime.root.mkdir()
    initialize_project(runtime.root)
    (runtime.root / ".intent/graph.yaml").write_text("not: [valid", encoding="utf-8")

    status = (await server.call_tool("intent_status", {})).structured_content
    validation = (await server.call_tool("intent_validate", {})).structured_content

    assert status["node_count"] == 2
    assert validation == before


async def test_resources_return_only_authorized_versioned_payloads(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(McpReadServices(runtime))
    before = {
        path.relative_to(runtime.workspace): path.read_bytes()
        for path in runtime.workspace.rglob("*")
        if path.is_file()
    }

    evidence = next(iter(await server.read_resource("intent://evidence/evidence:public")))
    node = next(iter(await server.read_resource("intent://graph/nodes/req-local-export")))
    case = next(iter(await server.read_resource("intent://cases/case:public")))
    schema = next(iter(await server.read_resource("intent://schemas/Graph")))
    report = next(iter(await server.read_resource("intent://reports/drift")))

    assert '"schema_version":"1"' in evidence.content
    assert '"id":"req-local-export"' in node.content
    assert '"id":"case:public"' in case.content
    assert '"title":"Graph"' in schema.content
    assert '"additionalProperties":false' in schema.content
    assert "case:public" in report.content
    contents = (evidence.content, node.content, case.content, schema.content, report.content)
    assert not any("private" in content for content in contents)
    after = {
        path.relative_to(runtime.workspace): path.read_bytes()
        for path in runtime.workspace.rglob("*")
        if path.is_file()
    }
    assert after == before


async def test_corrupt_workspace_failure_is_fixed_and_drops_private_traceback_locals(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-MCP-READ-TRACE-8197"
    runtime = _runtime(tmp_path)
    evidence_file = runtime.transactions.target_file("evidence")
    try:
        evidence_file.atomic_write(f'{{"payload":"{secret}"'.encode())
    finally:
        evidence_file.close()
    server = build_server(McpReadServices(runtime))

    with pytest.raises(MCPError) as caught:
        await server.call_tool("intent_status", {})

    assert caught.value.code == INTERNAL_ERROR
    assert caught.value.message == "intent read is unavailable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert secret not in "".join(repository_locals)
