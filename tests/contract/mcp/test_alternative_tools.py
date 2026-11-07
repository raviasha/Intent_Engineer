"""Read-only MCP contracts for host-generated requirement alternatives."""

from types import SimpleNamespace

import pytest

from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from tests.integration.alternatives.test_service import RecordingReasoner
from tests.unit.alternatives.fixtures import assessment_pair
from tests.unit.alternatives.test_validation import _candidate

pytestmark = pytest.mark.anyio

_TOOLS = {
    "intent_alternative_context",
    "intent_alternative_validate",
    "intent_alternatives",
}


class AlternativeRuntime:
    def __init__(self) -> None:
        self.config = SimpleNamespace(local_actor="actor:owner")
        self.snapshot, _report = assessment_pair()

    def assessment_snapshot(self, actor: str):
        assert actor == "actor:owner"
        return self.snapshot


async def test_alternative_tools_are_read_only_and_never_expose_submission() -> None:
    runtime = AlternativeRuntime()
    services = McpReadServices(runtime, alternative_reasoner=RecordingReasoner())
    server = build_server(services)

    by_name = {tool.name: tool for tool in await server.list_tools()}

    assert _TOOLS <= set(by_name)
    assert "intent_alternative_submit" not in by_name
    for name in _TOOLS:
        annotations = by_name[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False


async def test_host_context_validation_and_generation_are_token_free_and_read_only() -> None:
    runtime = AlternativeRuntime()
    before = runtime.snapshot.graph.model_dump_json()
    server = build_server(McpReadServices(runtime, alternative_reasoner=RecordingReasoner()))

    context = (
        await server.call_tool(
            "intent_alternative_context",
            {"subject_ref": "req:export", "modes": ["change_approach"]},
        )
    ).structured_content
    request = context["request"]
    validated = (
        await server.call_tool(
            "intent_alternative_validate",
            {"request_id": request["id"], "candidate": _candidate().model_dump(mode="json")},
        )
    ).structured_content
    generated = (
        await server.call_tool(
            "intent_alternatives",
            {"subject_ref": "req:export", "modes": ["change_approach"]},
        )
    ).structured_content

    encoded = str((context, validated, generated)).casefold()
    assert validated["comparison"]["projected_state"] == "projected"
    assert generated["result"]["status"] == "available"
    assert "capability_token" not in encoded
    assert "authorization" not in encoded
    assert runtime.snapshot.graph.model_dump_json() == before


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("intent_alternative_context", {"subject_ref": "req:export", "modes": []}),
        (
            "intent_alternative_validate",
            {"request_id": "alternative-request:bad", "candidate": {"private": "SECRET"}},
        ),
        ("intent_alternatives", {"subject_ref": True}),
    ],
)
async def test_alternative_tools_reject_malformed_input_with_one_fixed_error(
    name: str,
    arguments: dict[str, object],
) -> None:
    server = build_server(
        McpReadServices(AlternativeRuntime(), alternative_reasoner=RecordingReasoner())
    )

    with pytest.raises(Exception, match="invalid intent tool arguments") as caught:
        await server.call_tool(name, arguments)

    assert "SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
