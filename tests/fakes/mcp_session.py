"""Deterministic MCP session fake used by runtime contract tests."""

from __future__ import annotations

from collections.abc import Callable

import anyio

from intent_engineering.core.models import JsonValue


class FakeMcpSession:
    """A local session double with observable lifecycle and no provider traffic."""

    def __init__(self) -> None:
        self.tools: frozenset[str] = frozenset({"search_messages", "get_message"})
        self.resources: frozenset[str] = frozenset({"resource://message"})
        self.resource_templates: frozenset[str] = frozenset()
        self.tool_result: object = {"items": [{"id": "one"}]}
        self.resource_result: object = {"id": "one"}
        self.failure: BaseException | None = None
        self.block: anyio.Event | None = None
        self.closed = 0
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []

    async def list_tools(self) -> frozenset[str]:
        await self._wait_or_fail()
        return self.tools

    async def list_resources(self) -> frozenset[str]:
        await self._wait_or_fail()
        return self.resources

    async def list_resource_templates(self) -> frozenset[str]:
        await self._wait_or_fail()
        return self.resource_templates

    async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> object:
        self.calls.append((name, arguments))
        await self._wait_or_fail()
        return self.tool_result

    async def read_resource(self, uri: str) -> object:
        await self._wait_or_fail()
        return self.resource_result

    async def close(self) -> None:
        self.closed += 1

    async def _wait_or_fail(self) -> None:
        if self.block is not None:
            await self.block.wait()
        if self.failure is not None:
            raise self.failure


type SessionFactory = Callable[[], FakeMcpSession]
