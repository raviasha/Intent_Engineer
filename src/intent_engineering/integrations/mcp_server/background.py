"""Explicit MCP surface for local conversational intent maintenance."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from intent_engineering.cli.runtime import Runtime
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_models import BackgroundSubmission
from intent_engineering.intent_workflow.background_sources import BackgroundSource, capture_source


def register_background_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    service = BackgroundIntentService(runtime)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_capture_source(draft: BackgroundSource) -> dict[str, str]:
        """Preserve a real authorized connector read with source provenance, locally.

        This records host-reported evidence. It does not connect or authenticate a provider.
        """
        try:
            return capture_source(runtime, draft)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("background source capture unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_context(task: str, session_id: str) -> dict[str, Any]:
        """Read relevant intent, prior chat answers and pending data without a review gate."""
        try:
            return service.context(task=task, session_id=session_id)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("background intent context unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_submit(draft: BackgroundSubmission) -> dict[str, Any]:
        """Record a chat question/answer or apply clear intent with evidence and ChangeSet history.

        This local maintenance path creates no approvals and leaves legacy proposals intact.
        User words are agent-reported statements; agent inferences remain inferred. Ask only
        consequential missing-meaning or conflict questions, reusing available answers.
        """
        try:
            return service.submit(draft)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("background intent update unavailable") from None
