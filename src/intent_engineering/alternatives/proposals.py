"""Convert selected alternatives into ordinary independently reviewed proposals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ConfigDict, field_validator

from intent_engineering.alternatives.models import AlternativeComparison, GraphEffectKind
from intent_engineering.alternatives.service import RequirementAlternativeService
from intent_engineering.assessment import AssessmentSnapshot
from intent_engineering.core.graph import apply_changeset
from intent_engineering.core.models import ChangeSet, NodeUpdate
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow import IntentProposal, ProposalKind
from intent_engineering.intent_workflow.proposal_store import IntentProposalStore


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AlternativeActor(StrictModel):
    """Exact currently authenticated proposal author identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    actor: str
    aliases: tuple[str, ...]

    @field_validator("actor", mode="before")
    @classmethod
    def validate_actor(cls, value: object) -> str:
        if type(value) is not str or not value or len(value.encode("utf-8")) > 256:
            raise ValueError("invalid alternative actor")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(
            type(item) is not str or not item or len(item.encode("utf-8")) > 256 for item in value
        ):
            raise ValueError("invalid alternative actor aliases")
        return tuple(sorted(set(value)))


class AlternativeProposalConflictError(ValueError):
    """One fixed failure for stale, substituted, or unavailable selections."""

    def __init__(self) -> None:
        super().__init__("alternative proposal unavailable")


class AlternativeProposalService:
    """Create pending proposals while preserving existing confirmation authority."""

    def __init__(
        self,
        *,
        alternatives: RequirementAlternativeService,
        snapshot_loader: Callable[[], AssessmentSnapshot],
        proposal_store: IntentProposalStore,
        actor_resolver: Callable[[], AlternativeActor],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._alternatives = alternatives
        self._snapshot_loader = snapshot_loader
        self._store = proposal_store
        self._actor_resolver = actor_resolver
        self._clock = clock or _utc_now

    def submit(
        self,
        alternative_id: str,
        *,
        expected_request_digest: str,
        expected_effect_digest: str,
    ) -> IntentProposal:
        proposal: IntentProposal | None = None
        failed = False
        try:
            proposal = self._submit(
                alternative_id,
                expected_request_digest=expected_request_digest,
                expected_effect_digest=expected_effect_digest,
            )
        except Exception:  # noqa: BLE001 - public boundary exposes only a fixed conflict
            failed = True
        if failed or proposal is None:
            raise AlternativeProposalConflictError()
        return proposal

    def _submit(
        self,
        alternative_id: str,
        *,
        expected_request_digest: str,
        expected_effect_digest: str,
    ) -> IntentProposal:
        comparison = self._alternatives.get_comparison(
            alternative_id,
            expected_request_digest=expected_request_digest,
            expected_effect_digest=expected_effect_digest,
        )
        if comparison is None:
            raise ValueError("comparison unavailable")
        snapshot = self._snapshot_loader()
        actor = self._actor_resolver()
        if type(snapshot) is not AssessmentSnapshot or type(actor) is not AlternativeActor:
            raise ValueError("proposal authority unavailable")
        if (
            snapshot.aggregate_digest != comparison.snapshot_digest
            or snapshot.principal_projection_digest != comparison.principal_projection_digest
        ):
            raise ValueError("proposal snapshot changed")
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("proposal clock must be timezone-aware")
        timestamp = timestamp.astimezone(UTC)
        changeset = self._changeset(comparison, snapshot, actor.actor, timestamp)
        apply_changeset(snapshot.graph, changeset)
        proposal = self._proposal(comparison, changeset, actor.actor, timestamp)
        added = self._store.put(proposal)
        if added:
            return proposal
        stored = self._store.get(proposal.id)
        if stored != proposal:
            raise ValueError("conflicting proposal replay")
        return stored

    @staticmethod
    def _changeset(
        comparison: AlternativeComparison,
        snapshot: AssessmentSnapshot,
        actor: str,
        timestamp: datetime,
    ) -> ChangeSet:
        nodes = {node.id: node for node in snapshot.graph.nodes}
        updates: list[NodeUpdate] = []
        superseded: list[str] = []
        for effect in comparison.alternative.candidate.graph_effects:
            node = nodes.get(effect.target_ref)
            if node is None:
                raise ValueError("proposal target unavailable")
            if effect.effect is GraphEffectKind.UPDATE_NODE:
                if effect.after is None or effect.before != node.label:
                    raise ValueError("proposal target changed")
                replacement = node.model_copy(
                    update={
                        "label": effect.after,
                        "last_modified_by": actor,
                        "last_modified_at": timestamp,
                        "evidence_refs": tuple(
                            sorted(
                                set(node.evidence_refs)
                                | set(comparison.alternative.candidate.supporting_evidence_refs)
                            )
                        ),
                    }
                )
                updates.append(NodeUpdate(node_id=node.id, replacement=replacement))
            elif effect.effect is GraphEffectKind.REMOVE_NODE:
                superseded.append(node.id)
            else:
                raise ValueError("unsupported proposal effect")
        changeset = ChangeSet(
            id="",
            actor=actor,
            timestamp=timestamp,
            baseline_graph_version=snapshot.graph.version,
            evidence_refs=comparison.alternative.candidate.supporting_evidence_refs,
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
        return changeset.model_copy(update={"id": f"changeset:alternative:{identity}"})

    @staticmethod
    def _proposal(
        comparison: AlternativeComparison,
        changeset: ChangeSet,
        actor: str,
        timestamp: datetime,
    ) -> IntentProposal:
        candidate = comparison.alternative.candidate
        review_items = tuple(
            sorted(
                {
                    *(
                        f"Review weakened outcome {item.outcome_ref}: {item.rationale}"
                        for item in candidate.weakened_outcomes
                    ),
                    *(
                        f"Review constraint {item.constraint_ref} ({item.effect.value}): "
                        f"{item.rationale}"
                        for item in candidate.constraint_effects
                    ),
                }
            )
        )
        destructive = bool(
            changeset.nodes_superseded
            or candidate.weakened_outcomes
            or any(item.effect.value == "remove" for item in candidate.constraint_effects)
        )
        values: dict[str, object] = {
            "schema_version": 1,
            "kind": ProposalKind.REQUIREMENT.value,
            "proposed_by": actor,
            "proposed_at": timestamp.isoformat().replace("+00:00", "Z"),
            "baseline_graph_version": changeset.baseline_graph_version,
            "evidence_refs": list(changeset.evidence_refs),
            "source_roles": [],
            "changeset": changeset.model_dump(mode="json"),
            "core_node_ids": [],
            "provisional_node_ids": [],
            "assumptions": list(candidate.assumptions),
            "unanswered_questions": list(review_items),
            "conflicting_authors": [],
            "destructive": destructive,
        }
        return IntentProposal(
            id=f"proposal:{_digest(values)}",
            kind=ProposalKind.REQUIREMENT,
            proposed_by=actor,
            proposed_at=timestamp,
            baseline_graph_version=changeset.baseline_graph_version,
            evidence_refs=changeset.evidence_refs,
            source_roles=(),
            changeset=changeset,
            assumptions=candidate.assumptions,
            unanswered_questions=review_items,
            destructive=destructive,
        )


__all__ = [
    "AlternativeActor",
    "AlternativeProposalConflictError",
    "AlternativeProposalService",
]
