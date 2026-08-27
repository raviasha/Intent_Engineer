"""Black-box stdio coverage for the production Intent MCP command."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import anyio
import pytest
from mcp import MCPError
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from intent_engineering.core.policy.project import initialize_project

pytestmark = pytest.mark.anyio


async def test_intent_mcp_startup_failure_is_fixed_and_stdout_clean(tmp_path: Path) -> None:
    project = tmp_path / "uninitialized"
    project.mkdir()
    executable = Path(sys.executable).with_name("intent")

    completed = await anyio.run_process(
        [str(executable), "mcp", "--project", str(project)],
        cwd=project,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b"intent error: MCP server failed\n"


async def test_intent_mcp_stdio_is_protocol_clean_and_read_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    executable = Path(sys.executable).with_name("intent")
    parameters = StdioServerParameters(
        command=str(executable),
        args=["mcp", "--project", str(project)],
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
    )

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        async with (
            stdio_client(parameters, errlog=errlog) as streams,
            ClientSession(*streams) as client,
        ):
            initialized = await client.initialize()
            tools = await client.list_tools()
            status = await client.call_tool("intent_status", {})
            invalid = await client.call_tool(
                "intent_context",
                {"format": "PRIVATE-MISSING-MCP-WIRE-ARG-" + "x" * 200},
            )
            invalid_workflow = await client.call_tool(
                "intent_bootstrap_propose",
                {"submission": {"private": "PRIVATE-WORKFLOW-WIRE-" + "x" * 1_100_000}},
            )
            prompts = await client.list_prompts()
            prepared = await client.get_prompt("prepare_task", {"task": "implement local export"})
            with pytest.raises(MCPError, match="not found"):
                await client.read_resource("intent://evidence/evidence:missing")
            for secret_uri in (
                "intent://unknown/PRIVATE-MCP-WIRE-RESOURCE-URI",
                "intent://unknown/" + "PRIVATE-MCP-WIRE-RESOURCE-URI-" * 300,
            ):
                with pytest.raises(MCPError) as caught:
                    await client.read_resource(secret_uri)
                assert caught.value.message == "intent resource was not found"
                assert caught.value.error.data is None
                assert "PRIVATE-MCP-WIRE-RESOURCE-URI" not in repr(caught.value.error)
            for prompt_name, arguments in (
                ("PRIVATE-UNKNOWN-MCP-WIRE-PROMPT", {}),
                ("prepare_task", {"other": "PRIVATE-MISSING-MCP-WIRE-PROMPT-ARG"}),
            ):
                with pytest.raises(MCPError) as caught:
                    await client.get_prompt(prompt_name, arguments)
                assert caught.value.message == "invalid intent prompt arguments"
                assert "PRIVATE-" not in repr(caught.value.error)

        errlog.seek(0)
        diagnostics = errlog.read()

    assert initialized.server_info.name == "intent-engineering"
    assert {tool.name for tool in tools.tools} == {
        "intent_context",
        "intent_explain",
        "intent_impact",
        "intent_drift",
        "intent_status",
        "intent_validate",
        "intent_reconcile_list",
        "intent_reconcile_show",
        "intent_changeset_propose",
        "intent_reconciliation_propose",
        "intent_write_preview",
        "intent_write_execute",
        "intent_bootstrap_propose",
        "intent_proposal_show",
        "intent_proposal_confirm",
    }
    assert status.structured_content["schema_version"] == "1"
    assert invalid.is_error is True
    assert "invalid intent tool arguments" in repr(invalid.content)
    assert "PRIVATE-MISSING-MCP-WIRE-ARG" not in repr(invalid)
    assert invalid_workflow.is_error is True
    assert "invalid intent workflow arguments" in repr(invalid_workflow.content)
    assert "PRIVATE-WORKFLOW-WIRE" not in repr(invalid_workflow)
    assert {prompt.name for prompt in prompts.prompts} == {
        "prepare_task",
        "review_reconciliation",
    }
    assert "intent_context" in prepared.messages[0].content.text
    assert "Traceback" not in diagnostics
    assert "PRIVATE-" not in diagnostics
    assert str(Path.cwd()) not in diagnostics
