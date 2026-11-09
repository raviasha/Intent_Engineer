"""Public background tools commit typed interpretations without approval records."""

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from tests.integration.intent_workflow.test_background_intent import submission


@pytest.mark.anyio
async def test_background_mcp_applies_and_reuses_conversational_evidence(tmp_path):
    initialize_project(tmp_path)
    runtime = load_runtime(tmp_path)
    try:
        server = build_server(McpReadServices(runtime), background=True)
        tools = {tool.name: tool for tool in await server.list_tools()}
        assert tools["intent_background_context"].annotations.read_only_hint
        assert not tools["intent_background_submit"].annotations.read_only_hint
        assert not tools["intent_background_capture_source"].annotations.read_only_hint
        captured = await server.call_tool(
            "intent_background_capture_source",
            {
                "draft": {
                    "provider": "notion",
                    "object_id": "page:privacy",
                    "version": "1",
                    "author": "notion:alice",
                    "locator": "https://notion.so/privacy",
                    "observed_at": "2026-11-08T00:00:00Z",
                    "content": "Private reports",
                }
            },
        )
        assert not captured.is_error
        assert captured.structured_content["status"] == "captured"
        result = await server.call_tool(
            "intent_background_submit",
            {
                "draft": submission().model_dump(mode="json"),
            },
        )
        assert not result.is_error
        assert result.structured_content["status"] == "maintained"
        context = await server.call_tool(
            "intent_background_context",
            {
                "task": "project data storage",
                "session_id": "chat:one",
            },
        )
        assert context.structured_content["graph_version"] == 1
        assert runtime.intent_proposals.list() == ()
    finally:
        runtime.close()
