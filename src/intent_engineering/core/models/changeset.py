"""Validated, immutable descriptions of semantic graph mutations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from intent_engineering.core.models.enums import (
    ChangeKind,
    ImplementationStatus,
    NodeType,
    SourceMode,
)
from intent_engineering.core.models.epistemic import ConfidenceChange
from intent_engineering.core.models.evidence import JsonValue, freeze_json, thaw_json
from intent_engineering.core.models.graph import Confidence, Edge, Node


class NodeUpdate(BaseModel):
    """Replace a graph node while retaining its stable identity."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    node_id: str
    replacement: Node


class EdgeUpdate(BaseModel):
    """Replace a graph edge while retaining its stable identity."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    edge_id: str
    replacement: Edge


class ImplementationStatusChange(BaseModel):
    """An evidence-backed transition of a separately versioned implementation claim."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    claim_id: str
    prior: ImplementationStatus
    new: ImplementationStatus
    evidence_refs: tuple[str, ...]


class CandidateAssertion(BaseModel):
    """A deterministic reasoner's typed, evidence-backed proposal boundary."""

    model_config = ConfigDict(frozen=True, validate_default=True)

    id: str
    subject_id: str
    change_kind: ChangeKind
    node_type: NodeType | str
    label: str
    source_mode: SourceMode
    evidence_refs: tuple[str, ...]
    confidence: Confidence
    attributes: Mapping[str, JsonValue] = Field(default_factory=dict)

    @field_validator("attributes")
    @classmethod
    def freeze_attributes(cls, attributes: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        return freeze_json(dict(attributes))  # type: ignore[return-value]

    @field_serializer("attributes")
    def serialize_attributes(self, attributes: Mapping[str, JsonValue]) -> JsonValue:
        return thaw_json(attributes)


class ChangeSet(BaseModel):
    """The complete, evidence-backed set of changes proposed for one graph baseline."""

    model_config = ConfigDict(frozen=True)

    id: str
    actor: str
    timestamp: datetime
    baseline_graph_version: int = Field(ge=0)
    evidence_refs: tuple[str, ...]
    nodes_added: tuple[Node, ...]
    nodes_updated: tuple[NodeUpdate, ...]
    nodes_superseded: tuple[str, ...]
    edges_added: tuple[Edge, ...]
    edges_updated: tuple[EdgeUpdate, ...]
    edges_superseded: tuple[str, ...]
    confidence_changes: tuple[ConfidenceChange, ...]
    implementation_status_changes: tuple[ImplementationStatusChange, ...]
    reconciliation_cases_created: tuple[str, ...]
    reconciliation_cases_resolved: tuple[str, ...]
    validation_status: str

    @property
    def is_empty(self) -> bool:
        """Whether this ChangeSet makes no mutation or reconciliation change."""
        return not self.is_semantic

    @property
    def is_semantic(self) -> bool:
        """Whether this ChangeSet contains a graph or reconciliation mutation."""
        return any(
            (
                self.nodes_added,
                self.nodes_updated,
                self.nodes_superseded,
                self.edges_added,
                self.edges_updated,
                self.edges_superseded,
                self.confidence_changes,
                self.implementation_status_changes,
                self.reconciliation_cases_created,
                self.reconciliation_cases_resolved,
            )
        )

    @model_validator(mode="after")
    def validate_mutations(self) -> ChangeSet:
        mutation_subjects = {
            "nodes_added": tuple(item.id for item in self.nodes_added),
            "nodes_updated": tuple(item.node_id for item in self.nodes_updated),
            "nodes_superseded": self.nodes_superseded,
            "edges_added": tuple(item.id for item in self.edges_added),
            "edges_updated": tuple(item.edge_id for item in self.edges_updated),
            "edges_superseded": self.edges_superseded,
            "confidence_changes": tuple(item.subject_ref for item in self.confidence_changes),
            "implementation_status_changes": tuple(
                item.claim_id for item in self.implementation_status_changes
            ),
            "reconciliation_cases_created": self.reconciliation_cases_created,
            "reconciliation_cases_resolved": self.reconciliation_cases_resolved,
        }
        for group, subjects in mutation_subjects.items():
            if len(subjects) != len(set(subjects)):
                raise ValueError(f"duplicate subject in {group}")

        if {item.node_id for item in self.nodes_updated} & set(self.nodes_superseded):
            raise ValueError("cannot both update and supersede node")
        if {item.edge_id for item in self.edges_updated} & set(self.edges_superseded):
            raise ValueError("cannot both update and supersede edge")
        if self.is_semantic and not self.evidence_refs:
            raise ValueError("semantic ChangeSet requires evidence")
        return self
