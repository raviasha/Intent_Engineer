"""Small MCP prompts that delegate context retrieval to versioned tools."""

from __future__ import annotations

import json
from typing import Never

from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from mcp.types import INVALID_PARAMS


def _prompt_argument(value: object, *, maximum: int, identifier: bool = False) -> str | None:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or (identifier and value != value.strip())
    ):
        return None
    return value


def _invalid_prompt() -> Never:
    raise MCPError(INVALID_PARAMS, "invalid intent prompt arguments") from None


def register_read_prompts(server: MCPServer) -> None:
    """Register prompts without embedding graph or evidence dumps."""

    @server.prompt(name="prepare_task")
    def prepare_task(task: object) -> str:
        """Ask the client to retrieve the relevant Intent context before coding."""
        selected = _prompt_argument(task, maximum=4096)
        del task
        if selected is None:
            _invalid_prompt()
        encoded = json.dumps(selected, ensure_ascii=False)
        del selected
        return (
            f"Call intent_context with task={encoded} and format=json. "
            "Follow its requirements, constraints, warnings, evidence refs, and open cases."
        )

    @server.prompt(name="review_reconciliation")
    def review_reconciliation(case_id: object) -> str:
        """Ask the client to retrieve and review one reconciliation packet."""
        selected = _prompt_argument(case_id, maximum=512, identifier=True)
        del case_id
        if selected is None:
            _invalid_prompt()
        encoded = json.dumps(selected, ensure_ascii=False)
        del selected
        return (
            f"Call intent_reconcile_show with case_id={encoded}. Compare every attributed side "
            "and evidence reference; do not approve or execute any write."
        )
