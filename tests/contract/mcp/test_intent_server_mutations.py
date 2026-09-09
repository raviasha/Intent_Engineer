"""Contracts for proposal-only and independently approved MCP mutations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]
from mcp.server.mcpserver.exceptions import ToolError

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import (
    ChangeSet,
    EdgeUpdate,
    JsonValue,
    NodeType,
    NodeUpdate,
    ReconciliationStatus,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server import mutations as mutation_module
from intent_engineering.integrations.mcp_server.mutations import (
    ChangeSetProposal,
    ChangeSetProposalStore,
    McpMutationServices,
    MutationPort,
    load_mutation_services,
)
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.storage.secure import SecureFile, UnsafePathError
from tests.contract.mcp.test_intent_server_reads import _node, _runtime
from tests.unit.mutations.test_planner import base_plan

pytestmark = pytest.mark.anyio

_MUTATION_TOOLS = {
    "intent_changeset_propose",
    "intent_reconciliation_propose",
    "intent_write_preview",
    "intent_write_execute",
}
_READ_TOOLS = {
    "intent_context",
    "intent_explain",
    "intent_impact",
    "intent_drift",
    "intent_status",
    "intent_validate",
    "intent_assessment_summary",
    "intent_assessment_scorecard",
    "intent_assessment_gaps",
    "intent_enrichment_status",
    "intent_enrichment_next_question",
    "intent_reconcile_list",
    "intent_reconcile_show",
}
_MAX_MUTATION_JSON_BYTES = 1_048_576


@dataclass
class _FakeMutations(MutationPort):
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def propose_changeset(self, changeset: object) -> dict[str, object]:
        self.calls.append(("changeset", changeset))
        return {"schema_version": "1", "status": "proposed", "proposal_id": "proposal:1"}

    async def propose_reconciliation(
        self,
        case_id: object,
        action: object,
    ) -> dict[str, object]:
        self.calls.append(("reconciliation", (case_id, action)))
        return {"schema_version": "1", "status": "proposed", "proposal_id": "proposal:2"}

    async def preview_write(
        self,
        case_id: object,
        connector_id: object,
        operation: object,
        fields: object,
        action: object,
    ) -> dict[str, object]:
        self.calls.append(("preview", (case_id, connector_id, operation, fields, action)))
        return {
            "schema_version": "1",
            "status": "previewed",
            "plan_id": "write-plan:1",
            "plan_hash": "sha256:" + "1" * 64,
        }

    async def execute_write(
        self,
        plan_id: object,
        approval_id: object,
    ) -> dict[str, object]:
        self.calls.append(("execute", (plan_id, approval_id)))
        return {
            "schema_version": "1",
            "status": "rejected",
            "reason": "approval_not_found",
        }


def _server(tmp_path: Path, mutations: MutationPort):
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    return build_server(
        McpReadServices(load_runtime(project)),
        mutation_services=mutations,
    )


async def test_server_exports_exact_mutation_surface_without_approval_creation(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path, _FakeMutations())

    tools = await server.list_tools()
    by_name = {tool.name: tool for tool in tools}

    assert set(by_name) == _READ_TOOLS | _MUTATION_TOOLS
    for name in _MUTATION_TOOLS - {"intent_write_execute"}:
        annotations = by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint is (name != "intent_write_preview")
    execute = by_name["intent_write_execute"].annotations
    assert execute is not None
    assert execute.read_only_hint is False
    assert execute.destructive_hint is True
    assert execute.idempotent_hint is True
    execute_schema = by_name["intent_write_execute"].input_schema
    assert execute_schema["$defs"]["_PlanIdInput"]["pattern"] == (
        r"^write-plan:sha256:[0-9a-f]{64}$"
    )
    assert execute_schema["$defs"]["_ApprovalIdInput"]["pattern"] == (
        r"^approval:sha256:[0-9a-f]{64}$"
    )
    prompts = await server.list_prompts()
    resources = await server.list_resources()
    templates = await server.list_resource_templates()
    public_names = {
        *(prompt.name for prompt in prompts),
        *(str(resource.uri) for resource in resources),
        *(template.uri_template for template in templates),
    }
    assert not any("approv" in name.lower() for name in public_names)


async def test_proposal_and_preview_tools_never_imply_approval(tmp_path: Path) -> None:
    mutations = _FakeMutations()
    server = _server(tmp_path, mutations)

    changeset = await server.call_tool(
        "intent_changeset_propose",
        {"changeset": {"id": "candidate"}},
    )
    reconciliation = await server.call_tool(
        "intent_reconciliation_propose",
        {"case_id": "case:1", "action": "update_requirement"},
    )
    preview = await server.call_tool(
        "intent_write_preview",
        {
            "case_id": "case:1",
            "connector_id": "jira-local",
            "operation": "update_issue",
            "fields": {"summary": "reviewed"},
            "action": "update_requirement",
        },
    )

    assert changeset.structured_content["status"] == "proposed"
    assert reconciliation.structured_content["status"] == "proposed"
    assert preview.structured_content["status"] == "previewed"
    assert [kind for kind, _payload in mutations.calls] == [
        "changeset",
        "reconciliation",
        "preview",
    ]
    assert "approval" not in repr((changeset, reconciliation, preview)).lower()


async def test_execute_rejects_without_separately_persisted_approval(tmp_path: Path) -> None:
    mutations = _FakeMutations()
    server = _server(tmp_path, mutations)

    result = await server.call_tool(
        "intent_write_execute",
        {
            "plan_id": "write-plan:sha256:" + "1" * 64,
            "approval_id": "approval:sha256:" + "2" * 64,
        },
    )

    assert result.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "approval_not_found",
    }
    assert mutations.calls == [
        (
            "execute",
            (
                "write-plan:sha256:" + "1" * 64,
                "approval:sha256:" + "2" * 64,
            ),
        ),
    ]


async def test_malformed_secret_bearing_write_ids_are_fixed_before_service_call(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-MCP-WRITE-ID-" + "x" * 200
    mutations = _FakeMutations()
    server = _server(tmp_path, mutations)

    with pytest.raises(ToolError) as caught:
        await server.call_tool(
            "intent_write_execute",
            {"plan_id": secret, "approval_id": "approval:missing"},
        )

    assert caught.value.args == ("invalid intent tool arguments",)
    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert secret not in "".join(repository_locals)
    assert mutations.calls == []


def _changeset(runtime, *, actor: str = "local", evidence_ref: str = "evidence:public"):
    graph = runtime.graph_store.load()
    node = _node("proposal:new", NodeType.REQUIREMENT, evidence_ref).model_copy(
        update={"created_by": actor, "last_modified_by": actor}
    )
    return ChangeSet(
        id="changeset:proposal:new",
        actor=actor,
        timestamp=node.created_at,
        baseline_graph_version=graph.version,
        evidence_refs=(evidence_ref,),
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


def _proposal_for(changeset: ChangeSet) -> ChangeSetProposal:
    material = changeset.model_dump(mode="json", by_alias=True)
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return ChangeSetProposal(
        id=f"changeset-proposal:sha256:{hashlib.sha256(encoded).hexdigest()}",
        proposed_by=changeset.actor,
        proposed_at=changeset.timestamp,
        changeset=changeset,
    )


async def test_changeset_proposal_is_durable_but_never_applies_canonical_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(
        McpReadServices(runtime),
        mutation_services=load_mutation_services(runtime),
    )
    candidate = _changeset(runtime)
    before = runtime.graph_store.load().model_dump_json()

    first = await server.call_tool(
        "intent_changeset_propose",
        {"changeset": candidate.model_dump(mode="json")},
    )
    second = await server.call_tool(
        "intent_changeset_propose",
        {"changeset": candidate.model_dump(mode="json")},
    )

    assert first.structured_content == second.structured_content
    assert first.structured_content["status"] == "proposed"
    assert first.structured_content["proposal"]["changeset"]["actor"] == "local"
    assert runtime.graph_store.load().model_dump_json() == before
    proposal_source = runtime.workspace_directory.read_relative(
        "approvals/changeset-proposals.jsonl",
        nonblocking=True,
    )
    assert proposal_source.content.count(b"\n") == 1


def test_changeset_proposal_store_deduplicates_concurrent_identical_puts(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    changeset = _changeset(runtime)
    proposal = _proposal_for(changeset)
    ledger_path = runtime.root / ".intent/approvals/changeset-proposals.jsonl"

    def put_once() -> bool:
        source = SecureFile.from_path(ledger_path)
        try:
            return ChangeSetProposalStore(source).put(proposal)
        finally:
            source.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(lambda _index: put_once(), range(4)))

    assert sorted(outcomes) == [False, False, False, True]
    assert ledger_path.read_bytes().count(b"\n") == 1


def test_changeset_proposal_store_interrupt_drops_proposal_content_locals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "PRIVATE-MCP-PROPOSAL-APPEND-8197"
    runtime = _runtime(tmp_path)
    changeset = _changeset(runtime)
    changeset = changeset.model_copy(
        update={"nodes_added": (changeset.nodes_added[0].model_copy(update={"label": secret}),)}
    )
    proposal = _proposal_for(changeset)
    interruption = KeyboardInterrupt("append interrupted")

    def interrupt_append(_source: object, _line: bytes) -> None:
        raise interruption

    monkeypatch.setattr(mutation_module, "append_durable_line", interrupt_append)
    source = SecureFile.from_path(runtime.root / ".intent/approvals/changeset-proposals.jsonl")
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            ChangeSetProposalStore(source).put(proposal)
    finally:
        source.close()

    assert caught.value is interruption
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert secret not in "".join(repository_locals)


def test_changeset_proposal_store_rejects_unterminated_jsonl_without_rewrite(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    first = _proposal_for(_changeset(runtime))
    second_changeset = _changeset(runtime).model_copy(
        update={
            "id": "changeset:proposal:second",
            "nodes_added": (
                _changeset(runtime).nodes_added[0].model_copy(update={"id": "proposal:second"}),
            ),
        }
    )
    second = _proposal_for(second_changeset)
    source = SecureFile.from_path(runtime.root / ".intent/approvals/changeset-proposals.jsonl")
    raw = json.dumps(
        first.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    try:
        source.atomic_write(raw)
        store = ChangeSetProposalStore(source)
        with pytest.raises(ValueError, match="changeset proposal unavailable"):
            store.list()
        with pytest.raises(ValueError, match="changeset proposal unavailable"):
            store.put(second)
        assert source.read_bytes_nonblocking() == raw
    finally:
        source.close()


@pytest.mark.parametrize("entrypoint", ("list", "proposal"))
def test_changeset_proposal_fifo_is_rejected_without_blocking(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    runtime = _runtime(tmp_path)
    candidate = json.dumps(_changeset(runtime).model_dump(mode="json"))
    fifo = runtime.root / ".intent/approvals/changeset-proposals.jsonl"
    os.mkfifo(fifo)
    program = """
import asyncio
import json
import sys
from pathlib import Path
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.integrations.mcp_server.mutations import load_mutation_services

runtime = load_runtime(Path(sys.argv[1]))
services = load_mutation_services(runtime)
if sys.argv[2] == "list":
    try:
        services.proposals.list()
    except ValueError:
        raise SystemExit(0)
    raise SystemExit(2)

async def run() -> None:
    result = await services.propose_changeset(json.loads(sys.argv[3]))
    if result != {"schema_version": "1", "status": "rejected", "reason": "invalid_proposal"}:
        raise SystemExit(3)

asyncio.run(run())
"""

    completed = subprocess.run(
        [sys.executable, "-c", program, str(runtime.root), entrypoint, candidate],
        check=False,
        capture_output=True,
        timeout=2,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


async def test_corrupt_secret_bearing_proposal_ledger_fails_fixed_without_rewrite(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-MCP-PROPOSAL-LEDGER-8197"
    runtime = _runtime(tmp_path)
    ledger = runtime.workspace_directory.file("approvals/changeset-proposals.jsonl")
    corrupt = json.dumps({"secret": secret}, separators=(",", ":")).encode() + b"\n"
    try:
        ledger.atomic_write(corrupt)
    finally:
        ledger.close()
    server = build_server(
        McpReadServices(runtime),
        mutation_services=load_mutation_services(runtime),
    )

    result = await server.call_tool(
        "intent_changeset_propose",
        {"changeset": _changeset(runtime).model_dump(mode="json")},
    )

    assert result.structured_content == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "invalid_proposal",
    }
    assert secret not in repr(result)
    persisted = runtime.workspace_directory.read_relative(
        "approvals/changeset-proposals.jsonl",
        nonblocking=True,
    )
    assert persisted.content == corrupt


async def test_changeset_proposal_rejects_forged_actor_or_hidden_evidence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(
        McpReadServices(runtime),
        mutation_services=load_mutation_services(runtime),
    )

    forged = await server.call_tool(
        "intent_changeset_propose",
        {"changeset": _changeset(runtime, actor="other").model_dump(mode="json")},
    )
    hidden = await server.call_tool(
        "intent_changeset_propose",
        {
            "changeset": _changeset(
                runtime,
                evidence_ref="evidence:private",
            ).model_dump(mode="json")
        },
    )

    assert forged.structured_content["status"] == "rejected"
    assert hidden.structured_content["status"] == "rejected"
    assert runtime.graph_store.load().version == 0
    with pytest.raises(UnsafePathError):
        runtime.workspace_directory.read_relative(
            "approvals/changeset-proposals.jsonl",
            nonblocking=True,
        )


async def test_changeset_proposal_rejects_nested_evidence_outside_direct_scope(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    public = runtime.evidence_store.get("evidence:public")
    second = public.model_copy(
        update={
            "id": "evidence:second-public",
            "external_object_id": "second-public",
            "content_hash": "sha256:" + "2" * 64,
        }
    )
    runtime.evidence_store.put(second)
    candidate = _changeset(runtime).model_copy(update={"evidence_refs": (second.id,)})
    services = load_mutation_services(runtime)

    result = await services.propose_changeset(candidate.model_dump(mode="json"))

    assert result == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "invalid_proposal",
    }
    with pytest.raises(UnsafePathError):
        runtime.workspace_directory.read_relative(
            "approvals/changeset-proposals.jsonl",
            nonblocking=True,
        )


@pytest.mark.parametrize("kind", ("node", "edge"))
async def test_changeset_proposal_preserves_creation_provenance(
    tmp_path: Path,
    kind: str,
) -> None:
    runtime = _runtime(tmp_path)
    graph = runtime.graph_store.load()
    candidate = _changeset(runtime)
    if kind == "node":
        current = next(node for node in graph.nodes if node.id == "req-local-export")
        replacement = current.model_copy(
            update={
                "created_at": datetime(2000, 1, 1, tzinfo=UTC),
                "last_modified_by": "local",
                "last_modified_at": candidate.timestamp,
            }
        )
        candidate = candidate.model_copy(
            update={
                "nodes_added": (),
                "nodes_updated": (NodeUpdate(node_id=current.id, replacement=replacement),),
            }
        )
    else:
        current_edge = graph.edges[0]
        replacement_edge = current_edge.model_copy(
            update={
                "created_at": datetime(2000, 1, 1, tzinfo=UTC),
                "last_modified_by": "local",
                "last_modified_at": candidate.timestamp,
            }
        )
        candidate = candidate.model_copy(
            update={
                "nodes_added": (),
                "edges_updated": (
                    EdgeUpdate(edge_id=current_edge.id, replacement=replacement_edge),
                ),
            }
        )

    result = await load_mutation_services(runtime).propose_changeset(
        candidate.model_dump(mode="json")
    )

    assert result == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "invalid_proposal",
    }


async def test_changeset_proposal_rejects_oversized_structured_input_before_persistence(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    candidate = _changeset(runtime)
    candidate = candidate.model_copy(
        update={
            "nodes_added": (
                candidate.nodes_added[0].model_copy(
                    update={"label": "x" * _MAX_MUTATION_JSON_BYTES}
                ),
            )
        }
    )
    services = load_mutation_services(runtime)

    result = await services.propose_changeset(candidate.model_dump(mode="json"))

    assert result == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "invalid_proposal",
    }
    with pytest.raises(UnsafePathError):
        runtime.workspace_directory.read_relative(
            "approvals/changeset-proposals.jsonl",
            nonblocking=True,
        )


async def test_changeset_proposal_cannot_launder_an_acl_hidden_existing_node(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(
        McpReadServices(runtime),
        mutation_services=load_mutation_services(runtime),
    )
    candidate = _changeset(runtime)
    private = next(node for node in runtime.graph_store.load().nodes if node.id == "req-private")
    replacement = private.model_copy(
        update={
            "label": "laundered private requirement",
            "last_modified_by": "local",
            "last_modified_at": candidate.timestamp,
            "evidence_refs": ("evidence:public",),
        }
    )
    candidate = candidate.model_copy(
        update={
            "nodes_added": (),
            "nodes_updated": (NodeUpdate(node_id=private.id, replacement=replacement),),
        }
    )

    result = await server.call_tool(
        "intent_changeset_propose",
        {"changeset": candidate.model_dump(mode="json")},
    )

    assert result.structured_content["status"] == "rejected"
    persisted = next(node for node in runtime.graph_store.load().nodes if node.id == "req-private")
    assert persisted.label != "laundered private requirement"


async def test_mutation_requests_fail_closed_after_local_actor_configuration_changes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(
        McpReadServices(runtime),
        mutation_services=load_mutation_services(runtime),
    )
    config_file = runtime.workspace_directory.file("config.yaml")
    try:
        changed = runtime.config.model_copy(update={"local_actor": "other"})
        config_file.atomic_write(
            yaml.safe_dump(changed.model_dump(mode="json"), sort_keys=True).encode()
        )
    finally:
        config_file.close()

    result = await server.call_tool(
        "intent_changeset_propose",
        {"changeset": _changeset(runtime).model_dump(mode="json")},
    )

    assert result.structured_content["status"] == "rejected"
    assert runtime.graph_store.load().version == 0


async def test_reconciliation_proposal_persists_review_state_without_graph_application(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    server = build_server(
        McpReadServices(runtime),
        mutation_services=load_mutation_services(runtime),
    )
    before = runtime.graph_store.load().model_dump_json()

    result = await server.call_tool(
        "intent_reconciliation_propose",
        {"case_id": "case:public", "action": "update_requirement"},
    )

    assert result.structured_content["status"] == "proposed"
    assert result.structured_content["case"]["status"] == "needs_human"
    assert "approval" not in result.structured_content
    assert runtime.case_store.get("case:public").status is ReconciliationStatus.NEEDS_HUMAN
    assert runtime.graph_store.load().model_dump_json() == before


class _ApprovalLookup:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def get(self, approval_id: str) -> object:
        self.lookups.append(approval_id)
        return object()


class _WorkflowPort:
    actor = "local"

    def __init__(self) -> None:
        self.approvals = _ApprovalLookup()
        self.preview_calls: list[tuple[object, ...]] = []
        self.execute_calls: list[tuple[str, str]] = []

    async def create_preview(
        self,
        case_id: str,
        *,
        connector_id: str,
        operation: str,
        requested_fields: object,
        resolution_action: object,
    ):
        self.preview_calls.append(
            (case_id, connector_id, operation, requested_fields, resolution_action)
        )
        return base_plan()

    async def execute(self, plan_id: str, approval_id: str):
        from intent_engineering.mutations.models import ExecutionReceipt, receipt_id

        self.execute_calls.append((plan_id, approval_id))
        timestamp = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
        identity_material: dict[str, JsonValue] = {
            "schema_version": 1,
            "plan_id": base_plan().id,
            "plan_hash": base_plan().canonical_hash,
            "approval_id": approval_id,
            "target_version": base_plan().before_version,
            "executed_by": "local:reviewer",
            "status": "succeeded",
            "attempted_at": "2026-08-26T12:00:00Z",
            "completed_at": "2026-08-26T12:00:00Z",
            "resulting_version": "v2",
            "evidence_ref": "evidence:mcp-write:" + "1" * 64,
            "redacted_error": None,
        }
        return ExecutionReceipt(
            id=receipt_id(identity_material),
            plan_id=base_plan().id,
            plan_hash=base_plan().canonical_hash,
            approval_id=approval_id,
            target_version=base_plan().before_version,
            executed_by="local:reviewer",
            status="succeeded",
            attempted_at=timestamp,
            completed_at=timestamp,
            resulting_version="v2",
            evidence_ref="evidence:mcp-write:" + "1" * 64,
            redacted_error=None,
        )

    def approve(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("MCP mutation service must never create approval")


class _CancellingWorkflow(_WorkflowPort):
    def __init__(self, cancellation: asyncio.CancelledError) -> None:
        super().__init__()
        self.cancellation = cancellation

    async def create_preview(
        self,
        case_id: str,
        *,
        connector_id: str,
        operation: str,
        requested_fields: object,
        resolution_action: object,
    ):
        del case_id, connector_id, operation, requested_fields, resolution_action
        await asyncio.sleep(0)
        raise self.cancellation


class _InterruptingProposalStore:
    def __init__(self, interruption: KeyboardInterrupt) -> None:
        self.interruption = interruption

    def put(self, _proposal: ChangeSetProposal) -> bool:
        raise self.interruption


def _mutation_services(runtime, workflow: _WorkflowPort) -> McpMutationServices:
    approvals = runtime.workspace_directory.subdirectory("approvals")
    try:
        proposals = ChangeSetProposalStore(approvals.file("changeset-proposals.jsonl"))
    finally:
        approvals.close()
    return McpMutationServices(runtime, cast(object, workflow), proposals)  # type: ignore[arg-type]


async def test_write_preview_persists_only_through_the_reviewed_workflow(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workflow = _WorkflowPort()
    server = build_server(
        McpReadServices(runtime),
        mutation_services=_mutation_services(runtime, workflow),
    )

    result = await server.call_tool(
        "intent_write_preview",
        {
            "case_id": "case:public",
            "connector_id": "jira-local",
            "operation": "update_issue",
            "fields": {"summary": "reviewed"},
            "action": "update_requirement",
        },
    )

    assert result.structured_content["status"] == "previewed"
    assert result.structured_content["plan_id"] == base_plan().id
    assert result.structured_content["plan_hash"] == base_plan().canonical_hash
    assert len(workflow.preview_calls) == 1


async def test_write_preview_rejects_oversized_fields_before_workflow_access(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workflow = _WorkflowPort()
    services = _mutation_services(runtime, workflow)

    result = await services.preview_write(
        "case:public",
        "jira-local",
        "update_issue",
        {"summary": "x" * _MAX_MUTATION_JSON_BYTES},
        "update_requirement",
    )

    assert result == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "preview_unavailable",
    }
    assert workflow.preview_calls == []


async def test_write_execute_delegates_once_only_after_existing_approval_lookup(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workflow = _WorkflowPort()
    server = build_server(
        McpReadServices(runtime),
        mutation_services=_mutation_services(runtime, workflow),
    )
    approval_id = "approval:sha256:" + "2" * 64

    result = await server.call_tool(
        "intent_write_execute",
        {"plan_id": base_plan().id, "approval_id": approval_id},
    )

    assert result.structured_content["status"] == "succeeded"
    assert result.structured_content["receipt"]["approval_id"] == approval_id
    assert workflow.approvals.lookups == [approval_id]
    assert workflow.execute_calls == [(base_plan().id, approval_id)]


async def test_write_execute_rejects_noncanonical_ids_before_approval_lookup(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    workflow = _WorkflowPort()
    services = _mutation_services(runtime, workflow)

    result = await services.execute_write(
        "write-plan:not-canonical",
        "approval:sha256:" + "2" * 64,
    )

    assert result == {
        "schema_version": "1",
        "status": "rejected",
        "reason": "invalid_arguments",
    }
    assert workflow.approvals.lookups == []
    assert workflow.execute_calls == []


def test_write_execute_rejects_an_approval_fifo_without_blocking(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    fifo = runtime.root / ".intent/approvals/approvals.jsonl"
    os.mkfifo(fifo)
    program = """
import asyncio
import sys
from pathlib import Path
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.integrations.mcp_server.mutations import load_mutation_services

async def run() -> None:
    runtime = load_runtime(Path(sys.argv[1]))
    services = load_mutation_services(runtime)
    result = await services.execute_write(
        "write-plan:sha256:" + "a" * 64,
        "approval:sha256:" + "b" * 64,
    )
    expected = {
        "schema_version": "1",
        "status": "rejected",
        "reason": "approval_not_found",
    }
    if result != expected:
        raise SystemExit(2)

asyncio.run(run())
"""

    completed = subprocess.run(
        [sys.executable, "-c", program, str(runtime.root)],
        check=False,
        capture_output=True,
        timeout=2,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


async def test_preview_cancellation_is_exact_and_drops_secret_request_locals(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-MCP-PREVIEW-CANCEL-8197"
    cancellation = asyncio.CancelledError("preview cancelled")
    runtime = _runtime(tmp_path)
    services = _mutation_services(runtime, _CancellingWorkflow(cancellation))

    with pytest.raises(asyncio.CancelledError) as caught:
        await services.preview_write(
            "case:public",
            "jira-local",
            "update_issue",
            {"summary": secret},
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


async def test_changeset_proposal_interrupt_is_exact_and_drops_candidate_locals(
    tmp_path: Path,
) -> None:
    secret = "PRIVATE-MCP-CHANGESET-INTERRUPT-8197"
    interruption = KeyboardInterrupt("proposal interrupted")
    runtime = _runtime(tmp_path)
    candidate = _changeset(runtime)
    node = candidate.nodes_added[0].model_copy(update={"label": secret})
    candidate = candidate.model_copy(update={"nodes_added": (node,)})
    services = McpMutationServices(
        runtime,
        None,
        cast(ChangeSetProposalStore, _InterruptingProposalStore(interruption)),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        await services.propose_changeset(candidate.model_dump(mode="json"))

    assert caught.value is interruption
    traceback = caught.value.__traceback__
    repository_locals: list[str] = []
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    assert secret not in "".join(repository_locals)
