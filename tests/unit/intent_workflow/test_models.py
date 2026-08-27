"""Strict, immutable public contracts for intent-aware task workflow."""

import json
from collections import UserDict
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import ChangeSet, ProjectConfig
from intent_engineering.intent_workflow.models import (
    IntentProposal,
    PreflightResult,
    ProposalDecision,
    ProposalKind,
    SourceRole,
    SourceRoleAssignment,
    TaskClassification,
    TaskEnvelope,
)

NOW = datetime(2026, 8, 26, tzinfo=UTC)


def _changeset() -> ChangeSet:
    return ChangeSet(
        id="changeset:bootstrap",
        actor="local:asha",
        timestamp=NOW,
        baseline_graph_version=0,
        evidence_refs=(),
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


def _assignment() -> SourceRoleAssignment:
    return SourceRoleAssignment(
        connector_id="mcp:slack-product",
        scope="channel:C123",
        role=SourceRole.PROPOSED_INTENT,
        inherited=False,
    )


def _identity(prefix: str, material: dict[str, object]) -> str:
    encoded = json.dumps(
        material, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return f"{prefix}:sha256:{sha256(encoded).hexdigest()}"


def test_source_role_assignment_is_granular_strict_and_frozen() -> None:
    """Rejects invalid roles and undeclared fields rather than discarding authority metadata."""
    assignment = _assignment()

    assert assignment.role is SourceRole.PROPOSED_INTENT
    with pytest.raises(ValidationError):
        SourceRoleAssignment.model_validate(
            {**assignment.model_dump(), "role": "truth", "unexpected": True}
        )
    with pytest.raises(ValidationError):
        assignment.scope = "channel:C456"  # type: ignore[misc]


def test_project_config_round_trips_sorted_unique_source_roles_without_changing_old_configs() -> None:
    """Catches lost role configuration, nondeterministic ordering, and duplicate source scopes."""
    legacy = ProjectConfig(project_id="demo", local_actor="local:asha")
    first = SourceRoleAssignment(
        connector_id="markdown",
        scope="docs/prd.md",
        role=SourceRole.DECLARED_INTENT,
        inherited=False,
    )
    second = SourceRoleAssignment(
        connector_id="markdown",
        scope="docs/decisions.md",
        role=SourceRole.DECISION,
        inherited=False,
    )
    configured = ProjectConfig(
        project_id="demo", local_actor="local:asha", source_roles=(first, second)
    )

    assert legacy.source_roles == ()
    assert ProjectConfig.model_validate_json(configured.model_dump_json()) == configured
    assert configured.model_dump(mode="json")["source_roles"] == [
        {
            "connector_id": "markdown",
            "scope": "docs/decisions.md",
            "role": "decision",
            "inherited": False,
        },
        {
            "connector_id": "markdown",
            "scope": "docs/prd.md",
            "role": "declared_intent",
            "inherited": False,
        },
    ]
    with pytest.raises(ValidationError, match="duplicate source role"):
        ProjectConfig(
            project_id="demo",
            local_actor="local:asha",
            source_roles=(first, first),
        )


def test_task_envelope_has_canonical_bounded_identity_and_rejects_mismatch() -> None:
    """Catches requests that evade preflight identity by changing bounds or caller identity."""
    envelope = TaskEnvelope(
        repository_id="demo",
        actor="local:asha",
        conversation_ref="codex:thread-1:message-1",
        request="Add CSV export",
        request_evidence_ref="evidence:sha256:request-1",
        graph_version=3,
        created_at=NOW,
        requested_scope=("src/export.py",),
    )

    assert envelope.id.startswith("task:sha256:")
    assert TaskClassification.ALIGNED.value == "aligned"
    with pytest.raises(ValidationError, match="identifier does not match"):
        TaskEnvelope.model_validate({**envelope.model_dump(), "id": "task:sha256:bad"})
    with pytest.raises(ValidationError, match="invalid task text"):
        TaskEnvelope.model_validate({**envelope.model_dump(), "request": "Add\x00export"})
    with pytest.raises(ValidationError, match="request exceeds"):
        TaskEnvelope.model_validate({**envelope.model_dump(), "request": "x" * (16 * 1024 + 1)})


def test_proposal_and_decision_are_independent_content_addressed_append_records() -> None:
    """Catches a proposal or confirmation record that can be silently substituted."""
    changeset = _changeset()
    assignment = _assignment()
    proposal_material = {
        "schema_version": 1,
        "kind": "bootstrap",
        "proposed_by": "local:asha",
        "proposed_at": "2026-08-26T00:00:00Z",
        "baseline_graph_version": 0,
        "evidence_refs": ["evidence:prd:v1"],
        "source_roles": [assignment.model_dump(mode="json")],
        "changeset": changeset.model_dump(mode="json"),
        "core_node_ids": [],
        "provisional_node_ids": [],
        "assumptions": ["CSV supports UTF-8"],
        "unanswered_questions": ["Which locales are in scope?"],
        "conflicting_authors": [],
        "destructive": False,
    }
    proposal = IntentProposal(
        id=_identity("proposal", proposal_material),
        kind=ProposalKind.BOOTSTRAP,
        proposed_by="local:asha",
        proposed_at=NOW,
        baseline_graph_version=0,
        evidence_refs=("evidence:prd:v1",),
        source_roles=(assignment,),
        changeset=changeset,
        assumptions=("CSV supports UTF-8",),
        unanswered_questions=("Which locales are in scope?",),
    )
    decision_material = {
        "schema_version": 1,
        "proposal_id": proposal.id,
        "proposal_digest": proposal.digest,
        "actor": "local:ben",
        "actor_aliases": ["local:ben"],
        "decided_at": "2026-08-26T00:00:00Z",
        "action": "confirm",
        "baseline_graph_version": 0,
    }
    decision = ProposalDecision(
        id=_identity("proposal-decision", decision_material),
        proposal_id=proposal.id,
        proposal_digest=proposal.digest,
        actor="local:ben",
        actor_aliases=("local:ben",),
        decided_at=NOW,
        action="confirm",
        baseline_graph_version=0,
    )

    assert proposal.id.startswith("proposal:sha256:")
    assert decision.id.startswith("proposal-decision:sha256:")
    with pytest.raises(ValidationError, match="identifier does not match"):
        ProposalDecision.model_validate({**decision.model_dump(), "id": "proposal-decision:sha256:bad"})


def test_preflight_context_is_detached_strict_json_and_serializes_to_plain_containers() -> None:
    """Catches mutable or non-JSON context crossing the agent-to-core boundary."""
    context = {"constraints": {"names": ["local-first"]}}
    result = PreflightResult(
        task_id="task:sha256:1",
        graph_version=1,
        classification=TaskClassification.ALIGNED,
        authorized=True,
        basis="Matches active requirement.",
        context=context,
    )
    context["constraints"]["names"].append("offline")

    assert result.context["constraints"] == {"names": ("local-first",)}
    assert result.model_dump(mode="json")["context"] == {"constraints": {"names": ["local-first"]}}
    with pytest.raises(TypeError):
        result.context["new"] = "value"  # type: ignore[index]
    with pytest.raises(ValidationError, match="exact"):
        PreflightResult.model_validate(
            {**result.model_dump(), "context": UserDict({"constraint": "local-first"})}
        )
