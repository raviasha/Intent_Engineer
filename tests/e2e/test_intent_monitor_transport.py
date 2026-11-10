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
