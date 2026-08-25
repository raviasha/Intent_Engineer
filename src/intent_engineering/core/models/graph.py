"""Immutable semantic graph records and their structural invariants."""

import re
from datetime import datetime
from typing import Annotated

from pydantic import ConfigDict, Field, field_validator

from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.models.enums import NodeType, RelationType, SourceMode

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

_NAMESPACED_TYPE = re.compile(r"^[^\s:]+:[^\s:]+$")


class TypeRegistry(StrictModel):
    """Project-approved namespaced node-type extensions."""

    model_config = ConfigDict(frozen=True)

    extensions: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("extensions")
    @classmethod
    def validate_extensions(cls, extensions: frozenset[str]) -> frozenset[str]:
        for extension in extensions:
            if not _NAMESPACED_TYPE.fullmatch(extension):
                raise ValueError(f"extension type must be namespaced: {extension}")
        return extensions

    def assert_registered(self, node_type: NodeType | str) -> None:
        """Reject node types outside the built-in vocabulary and registry."""
        value = node_type.value if isinstance(node_type, NodeType) else node_type
        if value in NodeType._value2member_map_:
            return
        if not _NAMESPACED_TYPE.fullmatch(value):
            raise ValueError(f"unknown node type must be namespaced: {value}")
        if value not in self.extensions:
            raise ValueError(f"unregistered node type: {value}")


class Node(StrictModel):
    """A typed assertion in the canonical intent graph."""

    model_config = ConfigDict(frozen=True)

    id: str
    type: NodeType | str
    label: str
    status: str
    created_by: str
    created_at: datetime
    last_modified_by: str
    last_modified_at: datetime
    source_mode: SourceMode | None = None
    intent_fidelity_confidence: Confidence | None = None
    confidence_basis: str | None = None
    last_reassessed_at: datetime | None = None
    evidence_refs: tuple[str, ...] = ()

    @field_validator("type", mode="before")
    @classmethod
    def normalize_known_type(cls, node_type: NodeType | str) -> NodeType | str:
        if isinstance(node_type, str) and node_type in NodeType._value2member_map_:
            return NodeType(node_type)
        return node_type


class Edge(StrictModel):
    """A directed relationship between graph nodes."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str
    from_id: str = Field(alias="from")
    relation: RelationType
    to_id: str = Field(alias="to")
    status: str
    created_by: str
    created_at: datetime
    last_modified_by: str
    last_modified_at: datetime
    external: bool = False


class Graph(StrictModel):
    """Canonical graph state, validated whenever it is constructed."""

    model_config = ConfigDict(frozen=True)

    id: str
    version: int = Field(ge=0)
    schema_version: str = "0.1.0"
    name: str | None = None
    purpose: str | None = None
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    type_registry: TypeRegistry = Field(default_factory=TypeRegistry)

    def model_post_init(self, __context: object, /) -> None:
        self.assert_invariants()

    def assert_invariants(self) -> None:
        """Raise when canonical graph references or provenance are inconsistent."""
        node_ids = [item.id for item in self.nodes]
        edge_ids = [item.id for item in self.edges]
        duplicate_nodes = sorted({item for item in node_ids if node_ids.count(item) > 1})
        duplicate_edges = sorted({item for item in edge_ids if edge_ids.count(item) > 1})
        if duplicate_nodes:
            raise ValueError(f"duplicate node id: {duplicate_nodes[0]}")
        if duplicate_edges:
            raise ValueError(f"duplicate edge id: {duplicate_edges[0]}")

        known_node_ids = set(node_ids)
        for item in self.nodes:
            self.type_registry.assert_registered(item.type)
            if item.source_mode is not None and not item.evidence_refs:
                raise ValueError(f"provenance-backed node requires evidence: {item.id}")
        for edge in self.edges:
            if edge.from_id not in known_node_ids:
                raise ValueError(f"missing node: {edge.from_id}")
            if not edge.external and edge.to_id not in known_node_ids:
                raise ValueError(f"missing node: {edge.to_id}")
