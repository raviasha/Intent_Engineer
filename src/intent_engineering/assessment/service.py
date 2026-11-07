"""Snapshot-bound service for deterministic detached graph assessment reports."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock

from intent_engineering.assessment.models import (
    AssessmentHealth,
    AssessmentReport,
    AssessmentSnapshot,
    DimensionApplicability,
    DimensionResult,
    NodeScorecard,
    RubricCheck,
)
from intent_engineering.assessment.policy import AssessmentPolicy
from intent_engineering.assessment.rollup import roll_up
from intent_engineering.assessment.rubric import assess_node
from intent_engineering.assessment.snapshot import AssessmentUnavailable
from intent_engineering.core.models import Graph

_MAX_CACHE_ENTRIES = 32
_SEVERITY = {
    AssessmentHealth.RED: 0,
    AssessmentHealth.ORANGE: 1,
    AssessmentHealth.GREEN: 2,
    AssessmentHealth.UNASSESSED: 3,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _projected_snapshot(current: AssessmentSnapshot, proposed_graph: Graph) -> AssessmentSnapshot:
    graph_digest = _digest(proposed_graph.model_dump(mode="json", by_alias=True))
    components = {
        "case": current.case_digest,
        "clarification": current.clarification_digest,
        "config": current.config_digest,
        "evidence": current.evidence_digest,
        "graph": graph_digest,
        "history": current.history_digest,
        "ingestion": current.ingestion_digest,
        "principal_projection": current.principal_projection_digest,
    }
    return current.model_copy(
        update={
            "graph": proposed_graph,
            "graph_digest": graph_digest,
            "aggregate_digest": _digest(components),
        }
    )


def _merge_gaps(reports: tuple[RubricCheck, ...]) -> tuple[RubricCheck, ...]:
    by_rule: dict[str, RubricCheck] = {}
    for check in reports:
        existing = by_rule.get(check.rule_id)
        if existing is None:
            by_rule[check.rule_id] = check
            continue
        chosen = min((existing, check), key=lambda item: (_SEVERITY[item.severity], -item.points))
        by_rule[check.rule_id] = chosen.model_copy(
            update={"references": tuple(sorted(set(existing.references) | set(check.references)))}
        )
    return tuple(by_rule[rule_id] for rule_id in sorted(by_rule))


def _failed_severity(result: DimensionResult) -> int:
    return min(
        (_SEVERITY[check.severity] for check in result.failed),
        default=_SEVERITY[result.health],
    )


def _canonical_scorecard(scorecard: NodeScorecard) -> NodeScorecard:
    contributors = tuple(
        result
        for result in scorecard.dimensions
        if result.applicability
        in {
            DimensionApplicability.REQUIRED,
            DimensionApplicability.INHERITED,
            DimensionApplicability.OPTIONAL,
        }
    )
    if not contributors:
        return scorecard
    worst = min(
        contributors,
        key=lambda result: (
            _SEVERITY[result.health],
            _failed_severity(result),
            result.dimension.value,
        ),
    )
    return scorecard.model_copy(update={"worst_dimension": worst.dimension})


@dataclass(frozen=True)
class AssessmentComparison:
    """One approved report and its exact in-memory projected counterpart."""

    current: AssessmentReport
    projected: AssessmentReport


class GraphAssessmentService:
    """Assess immutable ACL-closed snapshots with a small thread-safe LRU cache."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        max_cache_entries: int = _MAX_CACHE_ENTRIES,
    ) -> None:
        if type(max_cache_entries) is not int or not 1 <= max_cache_entries <= _MAX_CACHE_ENTRIES:
            raise ValueError("assessment cache bound must be between 1 and 32")
        self._clock = clock or _utc_now
        self._max_cache_entries = max_cache_entries
        self._cache: OrderedDict[tuple[str, str, str], AssessmentReport] = OrderedDict()
        self._lock = RLock()

    @property
    def cache_entry_count(self) -> int:
        """Return the bounded count of detached reports retained in memory."""
        with self._lock:
            return len(self._cache)

    def assess(
        self,
        snapshot: AssessmentSnapshot,
        policy: AssessmentPolicy | None = None,
    ) -> AssessmentReport:
        """Assess exactly one detached snapshot without acquiring or mutating canonical state."""
        active_policy = policy or AssessmentPolicy.v1()
        key = (
            snapshot.aggregate_digest,
            snapshot.principal_projection_digest,
            active_policy.digest,
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached

            nodes = tuple(
                _canonical_scorecard(assess_node(snapshot, node.id, active_policy))
                for node in snapshot.graph.nodes
            )
            project, branches = roll_up(snapshot, nodes, active_policy)
            gaps = _merge_gaps(
                tuple(
                    check
                    for scorecard in nodes
                    for result in scorecard.dimensions
                    for check in result.failed
                )
            )
            incomplete = any(node.health is AssessmentHealth.UNASSESSED for node in nodes)
            report = AssessmentReport(
                project_id=snapshot.project_id,
                graph_id=snapshot.graph.id,
                graph_version=snapshot.graph.version,
                graph_digest=snapshot.graph_digest,
                evidence_digest=snapshot.evidence_digest,
                ingestion_digest=snapshot.ingestion_digest,
                case_digest=snapshot.case_digest,
                clarification_digest=snapshot.clarification_digest,
                history_digest=snapshot.history_digest,
                policy_digest=active_policy.digest,
                snapshot_digest=snapshot.aggregate_digest,
                principal_projection_digest=snapshot.principal_projection_digest,
                generated_at=self._clock(),
                project=project,
                branches=branches,
                nodes=nodes,
                gaps=gaps,
                warnings=("unassessed visible nodes",) if incomplete else (),
                assessment_complete=not incomplete,
            )
            self._cache[key] = report
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)
            return report

    def projected(
        self,
        current: AssessmentSnapshot,
        proposed_graph: Graph,
        policy: AssessmentPolicy | None = None,
    ) -> AssessmentComparison:
        """Compare approved and proposed graph state entirely in memory or fail closed."""
        comparison: AssessmentComparison | None = None
        failed = False
        try:
            if type(current) is not AssessmentSnapshot or type(proposed_graph) is not Graph:
                raise ValueError("invalid assessment projection")
            validated_graph = Graph.model_validate(
                proposed_graph.model_dump(mode="python", by_alias=True), strict=True
            )
            if (
                validated_graph != proposed_graph
                or proposed_graph.id != current.graph.id
                or proposed_graph.version != current.graph.version
            ):
                raise ValueError("stale assessment projection")
            comparison = AssessmentComparison(
                current=self.assess(current, policy),
                projected=self.assess(_projected_snapshot(current, proposed_graph), policy),
            )
        except AssessmentUnavailable:
            raise
        except Exception:  # noqa: BLE001 - expose one fixed projection failure boundary
            failed = True
        if failed or comparison is None:
            raise AssessmentUnavailable() from None
        return comparison


__all__ = ["AssessmentComparison", "GraphAssessmentService"]
