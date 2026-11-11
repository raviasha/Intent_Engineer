"""The host can drive recurring intent/PRD checks through public transports."""

import json

import pytest
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from tests.integration.intent_workflow.test_initial_reconciliation import env as env  # noqa: PLC0414


def test_monitor_cli_exposes_schema_and_rejects_canonical_submissions(env):
    runner = CliRunner()
    schema = runner.invoke(app, ["monitor-intent", "--submission-schema"])
    assert schema.exit_code == 0, schema.output
    assert "begin" in json.loads(schema.output)["properties"]["action"]["enum"]
    rejected = runner.invoke(
        app, ["monitor-intent", "--project", str(env.root), "--submission", ".intent/config.yaml"]
    )
    assert rejected.exit_code != 0


@pytest.mark.anyio
async def test_monitor_public_status_does_not_start_code_review(env, monkeypatch):
    from intent_engineering.intent_workflow.code_reconciliation import CodeReconciliationService

    def forbidden(*args, **kwargs):
        raise AssertionError("source monitoring must not dispatch code review")

    monkeypatch.setattr(CodeReconciliationService, "run", forbidden)
    server = build_server(McpReadServices(env), background=True)
    result = await server.call_tool("intent_monitor", {"draft": {"action": "status"}})
    assert not result.is_error
    assert result.structured_content["status"] == "not_configured"


@pytest.mark.anyio
async def test_mcp_tasks_share_source_checks_without_skipping_task_context(env):
    from datetime import UTC, datetime

    from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
    from tests.integration.intent_workflow.test_source_refresh import captured, selection

    SourceRefreshService(env).sources(selection())
    ref = captured(env)
    server = build_server(McpReadServices(env), background=True)
    draft = {"task": "Check privacy intent", "session_id": "chat:first", "turn_id": "one"}
    started = await server.call_tool("intent_background_refresh", {"draft": draft})
    assert not started.is_error
    read = await server.call_tool(
        "intent_background_refresh",
        {
            "draft": {
                **draft,
                "observations": [
                    {
                        "source_id": "source:privacy",
                        "generation": 1,
                        "kind": "content",
                        "checked_at": datetime.now(UTC).isoformat(),
                        "complete": True,
                        "validator": "scope:1",
                        "evidence_refs": [ref],
                    }
                ],
            }
        },
    )
    assert not read.is_error
    other = await server.call_tool(
        "intent_background_refresh",
        {"draft": {**draft, "task": "Implement private exports", "session_id": "chat:second"}},
    )
    assert not other.is_error
    first = read.structured_content
    second = other.structured_content
    assert second["sources"][0]["cached"] is True
    assert second["required_evidence_refs"] == first["required_evidence_refs"]
    assert second["task_key"] != first["task_key"]
    assert second["status"] == "needs_interpretation"
