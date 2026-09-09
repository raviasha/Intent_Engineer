"""Contracts for bounded read-only enrichment projections over MCP."""

from __future__ import annotations

import json
import traceback
from collections.abc import ItemsView
from pathlib import Path

import pytest
from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from intent_engineering.integrations.mcp_server import tools as tools_module
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.enrichment import GraphEnrichmentService
from intent_engineering.storage.yaml.graph_store import serialize_graph
from tests.contract.mcp.test_intent_server_reads import _runtime
from tests.integration.intent_workflow.test_enrichment import Clock

pytestmark = pytest.mark.anyio

_ENRICHMENT_TOOLS = {
    "intent_enrichment_status",
    "intent_enrichment_next_question",
}
_FORBIDDEN_ENRICHMENT_TOOLS = {
    "intent_enrichment_answer",
    "intent_enrichment_skip",
    "intent_enrichment_pause",
    "intent_enrichment_resume",
    "intent_enrichment_propose",
    "intent_enrichment_mutate",
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


async def test_enrichment_mcp_registers_only_two_read_only_structured_tools(
    tmp_path: Path,
) -> None:
    """Catches a missing read or any agent-callable enrichment mutation surface."""
    runtime = _runtime(tmp_path)
    try:
        tools = {
            tool.name: tool for tool in await build_server(McpReadServices(runtime)).list_tools()
        }
    finally:
        runtime.close()

    assert _ENRICHMENT_TOOLS <= set(tools)
    assert _FORBIDDEN_ENRICHMENT_TOOLS.isdisjoint(tools)
    for name in _ENRICHMENT_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert tools[name].output_schema is not None


async def test_enrichment_mcp_reads_exact_status_and_question_without_durable_writes(
    tmp_path: Path,
) -> None:
    """Catches either read tool mutating state or returning an unbound question."""
    runtime = _runtime(tmp_path)
    try:
        opened = GraphEnrichmentService(
            runtime,
            actor=runtime.config.local_actor,
        ).start(minutes=5)
        before = _durable_bytes(runtime.root)
        server = build_server(McpReadServices(runtime))

        status = (
            await server.call_tool("intent_enrichment_status", {"session_id": opened.id})
        ).structured_content
        question = (
            await server.call_tool(
                "intent_enrichment_next_question",
                {"session_id": opened.id},
            )
        ).structured_content

        assert status == {
            "schema_version": "1",
            "session": opened.model_dump(mode="json"),
        }
        assert question["schema_version"] == "1"
        assert question["session_id"] == opened.id
        assert question["question"]["gap_id"] == opened.current_gap_id
        assert _durable_bytes(runtime.root) == before
    finally:
        runtime.close()


async def test_enrichment_mcp_never_exposes_plaintext_answer_or_hidden_evidence(
    tmp_path: Path,
) -> None:
    """Catches status/question projections copying raw answers or unrelated private evidence."""
    runtime = _runtime(tmp_path)
    secret_answer = "PRIVATE HUMAN ANSWER MATERIAL"
    try:
        service = GraphEnrichmentService(runtime, actor=runtime.config.local_actor)
        opened = service.start(minutes=5)
        assert opened.current_gap_id is not None
        answered = service.answer(opened.id, opened.current_gap_id, secret_answer)
        before = _durable_bytes(runtime.root)
        server = build_server(McpReadServices(runtime))

        status = (
            await server.call_tool("intent_enrichment_status", {"session_id": answered.id})
        ).structured_content
        rendered = json.dumps(status, sort_keys=True)
        if answered.status == "open":
            question = (
                await server.call_tool(
                    "intent_enrichment_next_question",
                    {"session_id": answered.id},
                )
            ).structured_content
            rendered += json.dumps(question, sort_keys=True)

        assert secret_answer not in rendered
        assert "payload for evidence:private" not in rendered
        assert "evidence:private" not in rendered
        assert _durable_bytes(runtime.root) == before
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"session_id": ""},
        {"session_id": "bogus"},
        {"session_id": " refine:session"},
        {"session_id": "refine:session\ncontrol"},
        {"session_id": "x" * 257},
        {"session_id": "é" * 256},
        {"session_id": 1},
        {"session_id": "refine:session", "extra": "PRIVATE-EXTRA"},
    ),
)
async def test_enrichment_raw_arguments_are_exact_before_handler_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
) -> None:
    """Catches SDK coercion, extras, or unbounded IDs reaching the enrichment service."""
    services = McpReadServices(_runtime(tmp_path))
    called = False

    def should_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(services, "enrichment_status", should_not_run)
    try:
        with pytest.raises(ToolError, match="invalid intent tool arguments"):
            await build_server(services).call_tool("intent_enrichment_status", arguments)
        assert called is False
    finally:
        services.runtime.close()


async def test_enrichment_raw_boundary_rejects_subclasses_without_behavior(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches hostile containers or scalar subclasses executing before raw validation."""
    accessed = False

    class HostileString(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            nonlocal accessed
            accessed = True
            return super().strip(*args, **kwargs)

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

    monkeypatch.setattr(services, "enrichment_status", should_not_run)
    server = build_server(services)
    try:
        for arguments in (
            HostileDict({"session_id": "refine:session"}),
            {"session_id": HostileString("refine:session")},
        ):
            with pytest.raises(ToolError, match="invalid intent tool arguments"):
                await server.call_tool("intent_enrichment_status", arguments)
        assert accessed is False
        assert called is False
    finally:
        services.runtime.close()


@pytest.mark.parametrize(
    ("name", "method"),
    (
        ("intent_enrichment_status", "enrichment_status"),
        ("intent_enrichment_next_question", "enrichment_next_question"),
    ),
)
@pytest.mark.parametrize(
    "invalid_session_id",
    ("bogus", "refine:session\ncontrol", "é" * 256),
)
async def test_enrichment_registered_handler_rejects_noncanonical_id_before_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    method: str,
    invalid_session_id: str,
) -> None:
    """Catches direct SDK calls bypassing the canonical ID and UTF-8 byte bounds."""
    services = McpReadServices(_runtime(tmp_path))
    called = False

    def should_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(services, method, should_not_run)
    server = build_server(services)
    try:
        with pytest.raises(MCPError):
            await MCPServer.call_tool(server, name, {"session_id": invalid_session_id})
        assert called is False
    finally:
        services.runtime.close()


@pytest.mark.parametrize("state", ("missing", "expired", "stale", "corrupt"))
async def test_enrichment_mcp_edge_failures_are_fixed_and_byte_preserving(
    tmp_path: Path,
    state: str,
) -> None:
    """Catches read tools recovering or advancing unavailable enrichment state."""
    runtime = _runtime(tmp_path)
    try:
        clock = Clock()
        service = GraphEnrichmentService(runtime, actor=runtime.config.local_actor, clock=clock)
        opened = service.start(minutes=5)
        session_id = opened.id
        if state == "missing":
            session_id = "refine:" + "f" * 64
        elif state == "expired":
            clock.advance(seconds=300)
        elif state == "stale":
            changed = runtime.graph_store.load().model_copy(update={"version": 1})
            with runtime.transactions.transaction() as transaction:
                transaction.write("graph", serialize_graph(changed))
        else:
            target = runtime.transactions.target_file("enrichment_sessions")
            try:
                target.atomic_write(b'{"corrupt":')
            finally:
                target.close()
        before = _durable_bytes(runtime.root)
        services = McpReadServices(runtime)
        services._enrichment_service = service
        server = build_server(services)

        for name in _ENRICHMENT_TOOLS:
            with pytest.raises(MCPError) as caught:
                await server.call_tool(name, {"session_id": session_id})
            assert caught.value.message in {
                "intent read is unavailable",
                "intent object was not found",
            }
        assert _durable_bytes(runtime.root) == before
    finally:
        runtime.close()


class _CancellationSignal(BaseException):
    """Test-only cancellation whose exact identity must cross the MCP boundary."""


async def test_enrichment_failure_and_cancellation_are_fixed_secret_free_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches dependency details or cancellation traceback material crossing MCP."""
    runtime = _runtime(tmp_path)
    services = McpReadServices(runtime)
    services._enrichment_service = GraphEnrichmentService(
        runtime,
        actor=runtime.config.local_actor,
    )
    server = build_server(services)
    secret = "PRIVATE-ENRICHMENT-READ-MATERIAL"
    before = _durable_bytes(runtime.root)

    def fail(_session_id: str) -> object:
        marker = secret
        raise ValueError(marker)

    assert services._enrichment_service is not None
    monkeypatch.setattr(services._enrichment_service, "read_status", fail)
    try:
        with pytest.raises(MCPError) as unavailable:
            await server.call_tool(
                "intent_enrichment_status",
                {"session_id": "refine:" + "a" * 64},
            )
        assert unavailable.value.message == "intent read is unavailable"
        assert secret not in repr(unavailable.value)
        assert secret not in _repository_traceback_locals(unavailable.value)

        signal = _CancellationSignal(secret)
        retained: list[object] = []

        def cancel(_session_id: str) -> object:
            marker = secret
            try:
                if marker:
                    raise signal
                raise AssertionError("test requires retained material")
            except BaseException as caught:
                retained.append(caught.__traceback__)
                raise

        monkeypatch.setattr(services._enrichment_service, "read_status", cancel)
        with pytest.raises(_CancellationSignal) as cancelled:
            await server.call_tool(
                "intent_enrichment_status",
                {"session_id": "refine:" + "a" * 64},
            )
        assert cancelled.value is signal
        assert signal.args == ()
        assert signal.__dict__ == {}
        assert signal.__cause__ is None
        assert signal.__context__ is None
        assert retained
        assert secret not in "\n".join(
            repr(frame.f_locals)
            for old_traceback in retained
            if old_traceback is not None
            for frame, _line in traceback.walk_tb(old_traceback)  # type: ignore[arg-type]
        )
        assert _durable_bytes(runtime.root) == before
    finally:
        runtime.close()


async def test_enrichment_response_uses_one_fixed_canonical_byte_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches oversized enrichment projections escaping the public response boundary."""
    runtime = _runtime(tmp_path)
    services = McpReadServices(runtime)
    monkeypatch.setattr(tools_module, "_MAX_ENRICHMENT_RESPONSE_BYTES", 32, raising=False)
    try:
        opened = GraphEnrichmentService(runtime, actor=runtime.config.local_actor).start(minutes=5)
        before = _durable_bytes(runtime.root)
        with pytest.raises(MCPError) as unavailable:
            await build_server(services).call_tool(
                "intent_enrichment_status",
                {"session_id": opened.id},
            )
        assert unavailable.value.message == "intent read is unavailable"
        assert _durable_bytes(runtime.root) == before
    finally:
        runtime.close()
