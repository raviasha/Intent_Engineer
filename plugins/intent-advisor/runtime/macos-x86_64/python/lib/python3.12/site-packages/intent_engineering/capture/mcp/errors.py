"""Redacted public failures for the provider-neutral MCP runtime."""

from __future__ import annotations


class McpError(RuntimeError):
    """Base class for failures that may safely cross the MCP boundary."""


class McpTransportError(McpError):
    """The configured transport could not complete an MCP operation."""

    def __init__(self) -> None:
        super().__init__("MCP transport failure")


class McpProtocolError(McpError):
    """The peer did not provide the expected MCP protocol response."""

    def __init__(self) -> None:
        super().__init__("MCP protocol failure")


class McpCapabilityError(McpError):
    """A locally bound capability is not available from the configured peer."""


class McpPermissionError(McpError):
    """The peer denied the requested MCP operation."""

    def __init__(self) -> None:
        super().__init__("MCP permission denied")


class McpTimeoutError(McpError):
    """The configured operation deadline elapsed."""

    def __init__(self) -> None:
        super().__init__("MCP operation timed out")


class McpSchemaError(McpError):
    """An MCP input or output is outside the strict JSON boundary."""

    def __init__(self) -> None:
        super().__init__("invalid MCP JSON result")


class McpClosedSessionError(McpError):
    """The session cannot accept another operation."""

    def __init__(self) -> None:
        super().__init__("MCP session is closed")
