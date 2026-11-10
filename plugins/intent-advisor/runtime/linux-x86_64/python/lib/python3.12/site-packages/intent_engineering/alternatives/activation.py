"""Materialize one validated semantic alternative into a deterministic ChangeSet."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from intent_engineering.alternatives.models import GraphEffectKind, RequirementAlternative
from intent_engineering.core.graph import apply_changeset
from intent_engineering.core.models import ChangeSet, Graph, NodeUpdate


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_alternative_changeset(
    graph: Graph,
    alternative: RequirementAlternative,
    *,
    actor: str,
    timestamp: datetime,
) -> ChangeSet:
    """Build the only activation shape used for both projection and review."""
    if (
        type(graph) is not Graph
        or type(alternative) is not RequirementAlternative
        or type(actor) is not str
        or not actor
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
        or alternative.graph_effect_digest != alternative.candidate.activation_effect_digest
    ):
        raise ValueError("invalid alternative activation")
    nodes = {node.id: node for node in graph.nodes}
    updates: list[NodeUpdate] = []
    superseded: list[str] = []
    evidence_refs = alternative.candidate.supporting_evidence_refs
    for effect in alternative.candidate.graph_effects:
        node = nodes.get(effect.target_ref)
        if node is None:
            raise ValueError("alternative target unavailable")
        if effect.effect is GraphEffectKind.UPDATE_NODE:
            if (
                effect.field not in {"label", "statement"}
                or effect.before != node.label
                or effect.after is None
            ):
                raise ValueError("alternative update changed")
            replacement = node.model_copy(
                update={
                    "label": effect.after,
                    "last_modified_by": actor,
                    "last_modified_at": timestamp,
                    "evidence_refs": tuple(sorted(set(node.evidence_refs) | set(evidence_refs))),
                }
            )
            updates.append(NodeUpdate(node_id=node.id, replacement=replacement))
        elif effect.effect is GraphEffectKind.REMOVE_NODE:
            if effect.before != node.label:
                raise ValueError("alternative removal changed")
            superseded.append(node.id)
        else:
            raise ValueError("unsupported alternative effect")
    changeset = ChangeSet(
        id="",
        actor=actor,
        timestamp=timestamp,
        baseline_graph_version=graph.version,
        evidence_refs=evidence_refs,
        nodes_added=(),
        nodes_updated=tuple(sorted(updates, key=lambda item: item.node_id)),
        nodes_superseded=tuple(sorted(superseded)),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="validated",
    )
    identity = _digest(changeset.model_dump(mode="json", exclude={"id"}))
    bound = changeset.model_copy(update={"id": f"changeset:alternative:{identity}"})
    apply_changeset(graph, bound)
    return bound


__all__ = ["build_alternative_changeset"]
