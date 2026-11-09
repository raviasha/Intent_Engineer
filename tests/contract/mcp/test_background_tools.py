"""Public background tools commit typed interpretations without approval records."""

from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from tests.integration.intent_workflow.test_background_intent import submission
from tests.integration.intent_workflow.test_source_refresh import request, selection


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


@pytest.mark.anyio
async def test_public_refresh_reuses_metadata_and_waits_for_user_conflict_resolution(tmp_path):
    initialize_project(tmp_path)
    runtime = load_runtime(tmp_path)
    try:
        server = build_server(McpReadServices(runtime), background=True)

        async def call(name, **arguments):
            result = await server.call_tool(name, arguments)
            assert not result.is_error, result
            return result.structured_content

        await call("intent_background_sources", selection=selection().model_dump(mode="json"))
        await call("intent_background_refresh", draft=request().model_dump(mode="json"))
        captured = await call(
            "intent_background_capture_source",
            draft={
                "provider": "notion",
                "object_id": "page:privacy",
                "version": "1",
                "locator": "https://notion.so/privacy",
                "observed_at": datetime.now(UTC).isoformat(),
                "author": "notion:alice",
                "content": "Keep all project data local.",
            },
        )
        checked = await call(
            "intent_background_refresh",
            draft=request(
                observations=[
                    {
                        "source_id": "source:privacy",
                        "generation": 1,
                        "kind": "content",
                        "complete": True,
                        "validator": "scope:1",
                        "checked_at": datetime.now(UTC).isoformat(),
                        "evidence_refs": [captured["evidence_ref"]],
                    }
                ]
            ).model_dump(mode="json"),
        )
        assert checked["status"] == "needs_interpretation"
        interpreted = await call(
            "intent_background_submit",
            draft=submission(
                evidence_refs=checked["required_evidence_refs"],
            ).model_dump(mode="json"),
        )
        assert (
            await call(
                "intent_background_refresh",
                draft=request(
                    reconciliation_ref=interpreted["evidence_ref"],
                ).model_dump(mode="json"),
            )
        )["status"] == "ready"
        await call(
            "intent_background_refresh", draft=request(turn_id="task:two").model_dump(mode="json")
        )
        unchanged = await call(
            "intent_background_refresh",
            draft=request(
                turn_id="task:two",
                observations=[
                    {
                        "source_id": "source:privacy",
                        "generation": checked["sources"][0]["generation"],
                        "kind": "metadata",
                        "complete": True,
                        "validator": "scope:1",
                        "checked_at": datetime.now(UTC).isoformat(),
                    }
                ],
            ).model_dump(mode="json"),
        )
        assert unchanged["status"] == "ready"
        assert unchanged["sources"][0]["action"] == "reuse"
        asked = await call(
            "intent_background_submit",
            draft=submission(
                turn_id="conflict",
                graph_version=1,
                classification="conflicting",
                nodes=[],
                relevant_node_ids=["intent:local"],
                evidence_refs=unchanged["required_evidence_refs"],
                conflict="Leadership asks for public access; the source requires local storage.",
                question="Retain local storage? I recommend retaining it until leadership clarifies.",
            ).model_dump(mode="json"),
        )
        blocked = await call(
            "intent_background_refresh", draft=request(turn_id="task:two").model_dump(mode="json")
        )
        assert blocked["status"] == "blocked"
        assert blocked["blocking_case_ids"]
        answered = await call(
            "intent_background_submit",
            draft=submission(
                turn_id="answer",
                graph_version=2,
                nodes=[],
                relevant_node_ids=["intent:local"],
                evidence_refs=unchanged["required_evidence_refs"],
                answers=[
                    {
                        "question_ref": asked["evidence_ref"],
                        "quote": "Keep all project data local.",
                        "resolution": "retain_existing",
                    }
                ],
            ).model_dump(mode="json"),
        )
        result = await call(
            "intent_background_refresh",
            draft=request(
                turn_id="task:two",
                reconciliation_ref=answered["evidence_ref"],
            ).model_dump(mode="json"),
        )
        assert result["status"] == "ready"
        with pytest.raises(ToolError, match="invalid intent tool arguments"):
            await server.call_tool(
                "intent_background_refresh",
                {
                    "draft": request(
                        task="Changed task",
                        turn_id="task:two",
                    ).model_dump(mode="json")
                },
            )
    finally:
        runtime.close()
