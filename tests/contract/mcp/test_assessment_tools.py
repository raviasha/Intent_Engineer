"""Contracts for deterministic, read-only graph assessment over MCP."""

from __future__ import annotations

import json
import shutil
import traceback
from collections.abc import ItemsView, Iterator
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from mcp import MCPError
from mcp.server.mcpserver.exceptions import ToolError

from intent_engineering.integrations.mcp_server import tools as tools_module
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.storage.yaml.graph_store import parse_graph, serialize_graph
from tests.contract.mcp.test_intent_server_reads import _runtime
from tests.e2e.test_cli_assessment import _leave_transaction_journal

pytestmark = pytest.mark.anyio

_ASSESSMENT_TOOLS = {
    "intent_assessment_summary",
    "intent_assessment_scorecard",
    "intent_assessment_gaps",
}


def _durable_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _repository_traceback_locals(error: BaseException) -> str:
    return "\n".join(
        repr(frame.f_locals)
        for frame, _line in traceback.walk_tb(error.__traceback__)
        if "/src/intent_engineering/" in frame.f_code.co_filename
    )


async def test_assessment_tools_are_exactly_read_only_and_structured(tmp_path: Path) -> None:
    """Catches a missing tool, an accidental mutation hint, or an unstructured response."""
    runtime = _runtime(tmp_path)
    server = build_server(McpReadServices(runtime))

    by_name = {tool.name: tool for tool in await server.list_tools()}

    assert _ASSESSMENT_TOOLS <= set(by_name)
    for name in _ASSESSMENT_TOOLS:
        annotations = by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert by_name[name].output_schema is not None


async def test_production_assessment_tools_are_read_only_token_free_and_bound(
    tmp_path: Path,
) -> None:
    """Catches assessment output writing state, leaking authority, or losing snapshot identity."""
    runtime = _runtime(tmp_path)
    project = runtime.root
    before = _durable_bytes(project)
    server = build_server(McpReadServices(runtime))

    summary = (await server.call_tool("intent_assessment_summary", {})).structured_content
    scorecard = (
        await server.call_tool("intent_assessment_scorecard", {"reference": "req-local-export"})
    ).structured_content
    gaps = (
        await server.call_tool("intent_assessment_gaps", {"limit": 2, "health": "red"})
    ).structured_content

    assert summary["schema_version"] == "1"
    assert summary["assessment"]["project"]["health"] in {
        "green",
        "orange",
        "red",
        "unassessed",
    }
    assert summary["semantic_digest"].startswith("sha256:")
    assert scorecard["semantic_digest"] == gaps["semantic_digest"] == summary["semantic_digest"]
    assert scorecard["reference"] == "req-local-export"
    assert scorecard["node"]["node_id"] == "req-local-export"
    assert scorecard["branch"] is None
    assert len(gaps["gaps"]) == 2
    assert all(gap["severity"] == "red" for gap in gaps["gaps"])
    assert "token" not in json.dumps((summary, scorecard, gaps)).casefold()
    assert _durable_bytes(project) == before


@pytest.mark.parametrize("stage", ("target:graph", "journal_committed"))
async def test_assessment_tool_never_recovers_an_incomplete_transaction(
    tmp_path: Path,
    stage: str,
) -> None:
    """Catches an MCP assessment recovering graph state or deleting its transaction journal."""
    runtime = _runtime(tmp_path)
    project = runtime.root
    _leave_transaction_journal(project, stage)
    before = _durable_bytes(project)
    server = build_server(McpReadServices(runtime))

    with pytest.raises(MCPError) as unavailable:
        await server.call_tool("intent_assessment_summary", {})

    assert unavailable.value.message == "intent read is unavailable"
    assert _durable_bytes(project) == before


async def test_assessment_uses_held_graph_and_acl_after_project_directory_replacement(
    tmp_path: Path,
) -> None:
    """Catches assessment reopening a replaced root instead of its authenticated descriptors."""
    runtime = _runtime(tmp_path)
    project = runtime.root
    held_location = project.with_name("held-project")
    project.rename(held_location)
    shutil.copytree(held_location, project)
    replacement_graph = parse_graph((project / ".intent/graph.yaml").read_bytes()).model_copy(
        update={"id": "graph:replacement", "name": "PRIVATE-REPLACEMENT-GRAPH"}
    )
    (project / ".intent/graph.yaml").write_bytes(serialize_graph(replacement_graph))
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local"],
                "approvers": ["local"],
                "executors": ["local"],
                "identities": {"local": ["local", "other"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    server = build_server(McpReadServices(runtime))

    summary = (await server.call_tool("intent_assessment_summary", {})).structured_content

    assert summary["assessment"]["graph_id"] == "graph:test"
    assert {item["node_id"] for item in summary["assessment"]["nodes"]} == {
        "cap-local-export",
        "req-local-export",
    }
    assert "PRIVATE-REPLACEMENT-GRAPH" not in repr(summary)


async def test_all_assessment_responses_enforce_one_canonical_byte_cap_without_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches any assessment tool returning a response beyond the common encoded bound."""
    services = McpReadServices(_runtime(tmp_path))
    report = services._assessment_report()
    assert report is not None
    secret = "PRIVATE-OVERSIZED-ASSESSMENT-RESPONSE"
    selected = report.node("req-local-export").model_copy(
        update={"recommended_next_action": secret * 20}
    )
    nodes = tuple(selected if item.node_id == selected.node_id else item for item in report.nodes)
    gaps = tuple(item.model_copy(update={"explanation": secret * 20}) for item in report.gaps)
    oversized = report.model_copy(update={"nodes": nodes, "gaps": gaps, "warnings": (secret * 20,)})
    monkeypatch.setattr(tools_module, "_MAX_ASSESSMENT_RESPONSE_BYTES", 128, raising=False)
    monkeypatch.setattr(services, "_assessment_report", lambda: oversized)
    server = build_server(services)

    for tool, arguments in (
        ("intent_assessment_summary", {}),
        ("intent_assessment_scorecard", {"reference": "req-local-export"}),
        ("intent_assessment_gaps", {}),
    ):
        with pytest.raises(MCPError) as caught:
            await server.call_tool(tool, arguments)
        assert secret not in repr(caught.value)
        assert secret not in _repository_traceback_locals(caught.value)


async def test_scorecard_hides_unknown_and_inaccessible_references_identically(
    tmp_path: Path,
) -> None:
    """Catches scorecard lookup disclosing whether an omitted node exists."""
    server = build_server(McpReadServices(_runtime(tmp_path)))

    failures = []
    for reference in ("req-private", "missing"):
        with pytest.raises(MCPError) as caught:
            await server.call_tool("intent_assessment_scorecard", {"reference": reference})
        failures.append((caught.value.message, caught.value.error.data))

    assert failures == [
        ("intent object was not found", None),
        ("intent object was not found", None),
    ]


@pytest.mark.parametrize(
    "arguments",
    (
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"limit": 20.0},
        {"health": "unassessed"},
        {"health": "RED"},
        {"health": None, "extra": "PRIVATE-EXTRA"},
    ),
)
async def test_gap_arguments_are_exact_and_bounded_before_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    """Catches SDK coercion or default handling reaching the assessment service."""
    services = McpReadServices(_runtime(tmp_path))
    called = False

    def should_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(services, "assessment_gaps", should_not_run)
    server = build_server(services)

    with pytest.raises(ToolError, match="invalid intent tool arguments"):
        await server.call_tool("intent_assessment_gaps", arguments)

    assert called is False


@pytest.mark.parametrize("kind", ("arguments", "string", "list", "dict"))
async def test_assessment_raw_boundary_rejects_container_and_scalar_subclasses_without_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Catches hostile Python subclasses executing before exact raw JSON validation."""
    accessed = False

    class HostileString(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            nonlocal accessed
            accessed = True
            return super().encode(*args, **kwargs)  # type: ignore[arg-type]

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

    services = McpReadServices(_runtime(tmp_path))
    called = False

    def should_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(services, "assessment_scorecard", should_not_run)
    if kind == "arguments":
        arguments: dict[str, object] = HostileDict({"reference": "req-local-export"})
    elif kind == "string":
        arguments = {"reference": HostileString("req-local-export")}
    elif kind == "list":
        arguments = {"reference": "req-local-export", "extra": HostileList([])}
    else:
        arguments = {"reference": "req-local-export", "extra": HostileDict({})}
    server = build_server(services)

    with pytest.raises(ToolError, match="invalid intent tool arguments"):
        await server.call_tool("intent_assessment_scorecard", arguments)

    assert accessed is False
    assert called is False


@pytest.mark.parametrize("kind", ("cycle", "alias", "utf8"))
async def test_assessment_raw_boundary_rejects_non_json_trees_and_utf8_oversize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """Catches cycles, shared containers, or oversized UTF-8 reaching a tool handler."""
    services = McpReadServices(_runtime(tmp_path))
    called = False

    def should_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(services, "assessment_scorecard", should_not_run)
    if kind == "cycle":
        nested: list[object] = []
        nested.append(nested)
        arguments: dict[str, object] = {"reference": "req-local-export", "extra": nested}
    elif kind == "alias":
        shared: list[object] = []
        arguments = {"reference": "req-local-export", "first": shared, "second": shared}
    else:
        arguments = {"reference": "req-local-export", "extra": "U0001f9e8" * 300_000}
    server = build_server(services)

    with pytest.raises(ToolError, match="invalid intent tool arguments"):
        await server.call_tool("intent_assessment_scorecard", arguments)

    assert called is False


class _CancellationSignal(BaseException):
    """Test-only cancellation whose exact identity must cross the MCP boundary."""


async def test_raw_assessment_cancellation_is_identity_preserving_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches raw-boundary cancellation retaining argument or nested exception material."""
    import intent_engineering.integrations.mcp_server.intent_workflow as workflow_module

    secret = "PRIVATE-RAW-ASSESSMENT-CANCELLATION"
    signal = _CancellationSignal(secret)
    server = build_server(McpReadServices(_runtime(tmp_path)))

    def cancel(_arguments: object) -> None:
        try:
            raise ValueError(secret)
        except ValueError:
            raise signal

    monkeypatch.setattr(workflow_module, "_require_exact_json", cancel)
    with pytest.raises(_CancellationSignal) as caught:
        await server.call_tool("intent_assessment_summary", {})

    assert caught.value is signal
    assert caught.value.args == ()
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)


async def test_hostile_tool_name_is_rejected_before_subclass_behavior_or_sdk_lookup(
    tmp_path: Path,
) -> None:
    """Catches tool-name membership checks executing a hostile string subclass."""
    accessed = False

    class HostileName(str):
        def __hash__(self) -> int:
            nonlocal accessed
            accessed = True
            return super().__hash__()

        def __eq__(self, other: object) -> bool:
            nonlocal accessed
            accessed = True
            return super().__eq__(other)

    server = build_server(McpReadServices(_runtime(tmp_path)))

    with pytest.raises(ToolError, match="invalid intent tool arguments"):
        await server.call_tool(HostileName("intent_assessment_summary"), {})

    assert accessed is False


async def test_assessment_failure_is_fixed_and_cancellation_identity_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches private loader failures escaping or cancellation being wrapped."""
    services = McpReadServices(_runtime(tmp_path))
    server = build_server(services)
    secret = "PRIVATE-ASSESSMENT-LOAD-FAILURE"

    def fail(_runtime: object, _actor: str) -> None:
        raise ValueError(secret)

    monkeypatch.setattr(type(services.runtime), "assessment_snapshot", fail)
    with pytest.raises(MCPError) as unavailable:
        await server.call_tool("intent_assessment_summary", {})
    assert unavailable.value.message == "intent read is unavailable"
    assert secret not in repr(unavailable.value)
    assert secret not in _repository_traceback_locals(unavailable.value)

    signal = _CancellationSignal(secret)

    def cancel(_runtime: object, _actor: str) -> None:
        try:
            raise ValueError(secret)
        except ValueError:
            raise signal

    monkeypatch.setattr(type(services.runtime), "assessment_snapshot", cancel)
    with pytest.raises(_CancellationSignal) as cancelled:
        await server.call_tool("intent_assessment_summary", {})
    assert cancelled.value is signal
    assert cancelled.value.args == ()
    assert cancelled.value.__cause__ is None
    assert cancelled.value.__context__ is None
    assert secret not in _repository_traceback_locals(cancelled.value)
