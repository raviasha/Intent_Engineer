"""Real canonical baselines and proposals for developer readiness contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.cli.runtime import Runtime
from intent_engineering.core.models import ChangeSet, Graph
from intent_engineering.intent_workflow.models import IntentProposal, ProposalKind

NOW = datetime(2026, 8, 28, tzinfo=UTC)


def apply_baseline(runtime: Runtime, graph: Graph) -> None:
    """Build fixture graph state through the same evidence and ChangeSet stores as production."""
    content = "Approved developer readiness baseline"
    content_hash = "sha256:" + sha256(content.encode()).hexdigest()
    evidence = normalize_raw_source(
        RawSourceObject(
            connector_type="markdown",
            external_object_id="path:baseline.md",
            external_version=content_hash,
            author="local:owner",
            observed_at=NOW,
            source_locator="baseline.md",
            content_hash=content_hash,
            payload={"content": content},
        )
    )
    runtime.evidence_store.associate("markdown", evidence)
    runtime.graph_store.initialize(
        graph.model_copy(update={"version": 0, "nodes": (), "edges": ()})
    )
    runtime.graph_store.apply(
        ChangeSet(
            id="changeset:readiness-baseline",
            actor="local:owner",
            timestamp=NOW,
            baseline_graph_version=0,
            evidence_refs=(evidence.id,),
            nodes_added=graph.nodes,
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=graph.edges,
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )
    )


def put_pending_proposal(runtime: Runtime) -> IntentProposal:
    """Persist an undecided requirement proposal against an existing approved graph."""
    graph = runtime.graph_store.load()
    changeset = ChangeSet(
        id="changeset:pending-readiness",
        actor="agent:codex",
        timestamp=NOW,
        baseline_graph_version=graph.version,
        evidence_refs=tuple(record.id for record in runtime.evidence()),
        nodes_added=(),
        nodes_updated=(),
        nodes_superseded=(),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="validated",
    )
    values = {
        "kind": ProposalKind.REQUIREMENT,
        "proposed_by": "agent:codex",
        "proposed_at": NOW,
        "baseline_graph_version": graph.version,
        "evidence_refs": changeset.evidence_refs,
        "source_roles": (),
        "changeset": changeset,
        "assumptions": ("PRIVATE-PENDING-PROPOSAL-8197",),
    }
    draft = IntentProposal.model_construct(id="draft", **values)
    proposal = IntentProposal(id=f"proposal:{draft.digest}", **values)
    runtime.intent_proposals.put(proposal)
    return proposal
