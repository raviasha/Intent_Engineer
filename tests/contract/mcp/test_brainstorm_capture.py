"""Brainstorming captures settled meaning while clarification stays in its chat.

The host's semantic choices are explicit fixtures, not a simulated LLM guarantee.
Real MCP calls verify persistence, provenance, retries and conversation boundaries.
"""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.agent_host.advisory import background_prompt_route
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from tests.integration.intent_workflow.test_background_intent import submission


@pytest.mark.anyio
async def test_brainstorm_captures_clear_need_then_clarifies_and_reuses_answer(tmp_path):
    initialize_project(tmp_path)
    runtime = load_runtime(tmp_path)
    server = build_server(McpReadServices(runtime), background=True)

    async def call(name, **arguments):
        result = await server.call_tool(name, arguments)
        assert not result.is_error, result
        return result.structured_content

    try:
        # Existing graph and source say nothing about the new reports feature.
        await call("intent_background_submit", draft=submission().model_dump(mode="json"))
        source = await call(
            "intent_background_capture_source",
            draft={
                "provider": "notion",
                "object_id": "workspace:privacy",
                "version": "1",
                "author": "notion:owner",
                "locator": "https://notion.so/privacy",
                "observed_at": "2026-11-09T00:00:00Z",
                "content": "Keep project data local.",
            },
        )
        route = background_prompt_route("brainstorm-chat", "brainstorm-turn")
        session = route.arguments["session_id"]
        turn = route.arguments["turn_id"]
        statement = "Customers need offline reports; not sure CSV or PDF."
        context = await call("intent_background_context", task=statement, session_id=session)
        draft = {
            "session_id": session,
            "turn_id": f"{turn}:capture:1",
            "graph_version": context["graph_version"],
            "user_statement": statement,
            "classification": "clear",
            "understanding": "Customers need reports offline.",
            "nodes": [
                {
                    "id": "need:offline-reports",
                    "type": "NEED",
                    "label": "Use reports without connectivity",
                    "source_mode": "inferred",
                    "confidence": 0.8,
                    "basis": "Interpretation of the user's offline-report need",
                }
            ],
        }
        captured = await call("intent_background_submit", draft=draft)
        assert captured["status"] == "maintained"
        assert await call("intent_background_submit", draft=draft) == captured
        question = "Will customers analyze data in a spreadsheet or read a formatted report?"
        asked = await call(
            "intent_background_submit",
            draft={
                "session_id": session,
                "turn_id": f"{turn}:question:1",
                "graph_version": captured["graph_version"],
                "user_statement": statement,
                "classification": "needs_clarification",
                "understanding": "Format remains undecided.",
                "relevant_node_ids": ["need:offline-reports"],
                "evidence_refs": [captured["evidence_ref"]],
                "question": question,
            },
        )
        assert asked["question"] == question
        node = next(n for n in runtime.graph_store.load().nodes if n.id == "need:offline-reports")
        assert node.source_mode.value == "inferred"
        assert node.evidence_refs == (captured["evidence_ref"],)
        assert source["evidence_ref"] not in node.evidence_refs
        assert len(runtime.graph_store.history(node.id)) == 1
        evidence = runtime.evidence_store.get(captured["evidence_ref"])
        assert evidence.payload["content"]["user_statement"] == statement
        assert evidence.payload["content"]["human_approval"] is False

        # Restart the tool surface: the user need not restate or approve the graph.
        server = build_server(McpReadServices(runtime), background=True)
        current = await call(
            "intent_background_context", task="Offline reports", session_id=session
        )
        assert current["questions"][0]["question_ref"] == asked["evidence_ref"]
        # An ambiguous reply records no answer and leaves the original question pending.
        await call(
            "intent_background_submit",
            draft={
                "session_id": session,
                "turn_id": "ambiguous:1",
                "graph_version": current["graph_version"],
                "user_statement": "Maybe.",
                "classification": "clear",
                "understanding": "No format has been selected; keep the existing question open.",
            },
        )
        pending = await call(
            "intent_background_context", task="Offline reports", session_id=session
        )
        assert pending["questions"][0]["question_ref"] == asked["evidence_ref"]
        answer = "CSV, so customers can analyze reports offline in spreadsheets."
        answer_draft = {
            "session_id": session,
            "turn_id": "answer:capture:1",
            "graph_version": current["graph_version"],
            "user_statement": answer,
            "classification": "clear",
            "understanding": "CSV supports offline analysis.",
            "answers": [{"question_ref": asked["evidence_ref"], "quote": answer}],
            "nodes": [
                {
                    "id": "req:csv-export",
                    "type": "REQUIREMENT",
                    "label": "Export reports as CSV",
                    "source_mode": "inferred",
                    "confidence": 0.8,
                    "basis": "User selected CSV for offline spreadsheet analysis",
                    "evidence_refs": [captured["evidence_ref"], asked["evidence_ref"]],
                }
            ],
            "edges": [
                {
                    "id": "edge:offline-csv",
                    "from_id": "need:offline-reports",
                    "relation": "SPECIFIED_BY",
                    "to_id": "req:csv-export",
                }
            ],
        }
        with pytest.raises(ToolError):
            await server.call_tool(
                "intent_background_submit",
                {
                    "draft": {
                        **answer_draft,
                        "session_id": "other-chat",
                    }
                },
            )
        answered = await call("intent_background_submit", draft=answer_draft)
        assert await call("intent_background_submit", draft=answer_draft) == answered
        final = await call("intent_background_context", task="Offline reports", session_id=session)
        assert final["questions"] == []
        assert final["answers"][0]["quote"] == answer
        assert len(runtime.graph_store.load().edges) == 1
        assert runtime.intent_proposals.list() == ()
    finally:
        runtime.close()
