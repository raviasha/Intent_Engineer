"""Official-SDK assembly and stdio entry point for the Intent MCP server."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from mcp import MCPError
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import (
    ResourceError,
    ResourceNotFoundError,
    ToolError,
)
from mcp.types import (
    INVALID_PARAMS,
    CallToolResult,
    GetPromptResult,
    InputRequiredResult,
    ReadResourceRequestParams,
    ReadResourceResult,
)
from pydantic import AnyUrl

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.integrations.mcp_server.prompts import register_read_prompts
from intent_engineering.integrations.mcp_server.resources import register_read_resources
from intent_engineering.integrations.mcp_server.tools import McpReadServices, register_read_tools


class _IntentMCPServer(MCPServer[Any]):
    """Apply fixed, non-retaining boundaries to public read operations."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        failed = False
        response: CallToolResult | InputRequiredResult | None = None
        try:
            response = await super().call_tool(name, arguments, context)
        except ToolError:
            failed = True
        finally:
            del name, arguments, context
        if failed:
            raise ToolError("invalid intent tool arguments") from None
        return cast(CallToolResult | InputRequiredResult, response)

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        context: Context[Any, Any] | None = None,
    ) -> GetPromptResult | InputRequiredResult:
        failed = False
        response: GetPromptResult | InputRequiredResult | None = None
        try:
            response = await super().get_prompt(name, arguments, context)
        except ValueError:
            failed = True
        finally:
            del name, arguments, context
        if failed:
            raise MCPError(INVALID_PARAMS, "invalid intent prompt arguments") from None
        return cast(GetPromptResult | InputRequiredResult, response)

    async def read_resource(
        self,
        uri: AnyUrl | str,
        context: Context[Any, Any] | None = None,
    ) -> Iterable[ReadResourceContents] | InputRequiredResult:
        failed = False
        response: Iterable[ReadResourceContents] | InputRequiredResult | None = None
        try:
            response = await super().read_resource(uri, context)
        except ResourceError:
            failed = True
        del uri, context
        if failed:
            raise ResourceNotFoundError("intent resource was not found") from None
        return cast(Iterable[ReadResourceContents] | InputRequiredResult, response)

    async def _handle_read_resource(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: ReadResourceRequestParams,
    ) -> ReadResourceResult | InputRequiredResult:
        failed = False
        response: ReadResourceResult | InputRequiredResult | None = None
        try:
            response = await super()._handle_read_resource(ctx, params)
        except MCPError:
            failed = True
        finally:
            del ctx, params
        if failed:
            raise MCPError(INVALID_PARAMS, "intent resource was not found") from None
        return cast(ReadResourceResult | InputRequiredResult, response)


def build_server(services: McpReadServices) -> MCPServer:
    """Build the exact version-1 read-only MCP protocol surface."""
    server = _IntentMCPServer(
        name="intent-engineering",
        title="Intent Engineering",
        description="Read-only evidence-backed intent, context, drift, and reconciliation.",
        instructions=(
            "Use read tools and resources to inspect authorized local intent. "
            "This server cannot create approvals or execute writes."
        ),
        version="0.1.0",
        log_level="ERROR",
    )
    register_read_tools(server, services)
    register_read_resources(server, services)
    register_read_prompts(server)
    return server


def load_mcp_services(project: Path) -> McpReadServices:
    """Load the reviewed local runtime once for a long-lived stdio session."""
    return McpReadServices(load_runtime(project))


def run_stdio(project: Path) -> None:
    """Run the official MCP v2 stdio transport without writing to stdout."""
    build_server(load_mcp_services(project)).run(transport="stdio")
