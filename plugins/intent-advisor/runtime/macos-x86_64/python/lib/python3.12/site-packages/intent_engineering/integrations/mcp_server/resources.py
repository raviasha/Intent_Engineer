"""Versioned ACL-filtered Intent MCP resources."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError

from intent_engineering.integrations.mcp_server.tools import McpReadServices


def _found(content: str | None) -> str:
    if content is None:
        raise ResourceNotFoundError("intent resource was not found") from None
    return content


def register_read_resources(server: MCPServer, services: McpReadServices) -> None:
    """Register graph, evidence, schema, case, and report resources."""

    @server.resource(
        "intent://graph/nodes/{node_id}",
        name="intent_graph_node",
        mime_type="application/json",
    )
    def graph_node(node_id: str) -> str:
        return _found(services.node_resource(node_id))

    @server.resource(
        "intent://evidence/{evidence_id}",
        name="intent_evidence_chain",
        mime_type="application/json",
    )
    def evidence_chain(evidence_id: str) -> str:
        return _found(services.evidence_resource(evidence_id))

    @server.resource(
        "intent://schemas/{model_name}",
        name="intent_public_schema",
        mime_type="application/schema+json",
    )
    def public_schema(model_name: str) -> str:
        return _found(services.schema_resource(model_name))

    @server.resource(
        "intent://cases/{case_id}",
        name="intent_reconciliation_case",
        mime_type="application/json",
    )
    def reconciliation_case(case_id: str) -> str:
        return _found(services.case_resource(case_id))

    @server.resource(
        "intent://reports/drift",
        name="intent_drift_report",
        mime_type="text/markdown",
    )
    def drift_report() -> str:
        return _found(services.drift_report_resource())
