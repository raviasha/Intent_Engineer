"""Exercise the package smoke probe against the pinned MCP result model."""

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult


@pytest.mark.anyio
async def test_mcp_smoke_reads_typed_structured_result(monkeypatch, tmp_path):
    path = Path(__file__).parents[2] / "scripts" / "smoke_plugin.py"
    spec = importlib.util.spec_from_file_location("smoke_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Session:
        async def initialize(self):
            return SimpleNamespace(server_info=SimpleNamespace(name="fixture"))

        async def list_tools(self):
            return SimpleNamespace(tools=["intent_initial_reconciliation"])

        async def call_tool(self, name, args):
            assert name == "intent_initial_reconciliation"
            assert args["draft"]["action"] == "status"
            return CallToolResult(
                content=[],
                structured_content={
                    "chat_update": {
                        "kind": "complete",
                        "findings": [{"recommendation": "Restore local storage."}],
                    }
                },
            )

    @asynccontextmanager
    async def transport(*args):
        yield (None, None)

    @asynccontextmanager
    async def session(*args):
        yield Session()

    monkeypatch.setattr(module, "stdio_client", transport)
    monkeypatch.setattr(module, "ClientSession", session)
    await module.check_mcp(tmp_path / "intent", tmp_path, {})
