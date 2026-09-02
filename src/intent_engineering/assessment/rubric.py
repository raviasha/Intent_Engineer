"""Pure, deterministic evaluation for the version-one graph assessment rubric."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from intent_engineering.assessment.models import (
    AssessmentDimension,
    AssessmentHealth,
    AssessmentSnapshot,
    DimensionApplicability,
    DimensionResult,
    NodeScorecard,
    RubricCheck,
)
from intent_engineering.assessment.policy import AssessmentPolicy
from intent_engineering.core.models import (
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    Node,
    NodeType,
    ReconciliationCase,
    RelationType,
    is_nonterminal_case_status,
)


@dataclass(frozen=True)
class InputCheck:
    """One declared set of visible inputs used to calculate confidence."""

    required_inputs: int
    resolved_inputs: int


@dataclass(frozen=True)
class RubricContext:
    """The complete, detached input available to one dimension's rule set."""

    dimension: AssessmentDimension
    applicability: DimensionApplicability
    policy: AssessmentPolicy
    node: Node
    snapshot: AssessmentSnapshot
    related_nodes: tuple[Node, ...]
    related_edges: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    blocking_cases: tuple[ReconciliationCase, ...]
    input_checks: tuple[InputCheck, ...]


Rule = Callable[[RubricContext], RubricCheck | None]

_SEMANTIC_TYPES = frozenset(
    {
        NodeType.PRODUCT_INTENT,
        NodeType.DESIRED_OUTCOME,
        NodeType.CONSTRAINT,
        NodeType.REQUIREMENT,
        NodeType.ACCEPTANCE_CRITERION,
    }
)
_INTENT_TYPES = frozenset({NodeType.PRODUCT_INTENT, NodeType.DESIRED_OUTCOME, NodeType.CONSTRAINT})
_IMPLEMENTATION_TYPES = frozenset(
    {
        NodeType.REPOSITORY,
        NodeType.MODULE,
        NodeType.FILE,
        NodeType.SYMBOL,
        NodeType.ENDPOINT,
        NodeType.SCHEMA,
        NodeType.BUILD_ARTIFACT,
        NodeType.DEPLOYMENT,
    }
)
_ACTIVE = "active"


def applicability(
    node: Node,
    graph: Graph,
) -> Mapping[AssessmentDimension, DimensionApplicability]:
    """Return the complete V1 applicability matrix for one stable node type.

    The graph parameter is deliberately part of the public signature: later versioned matrices may
    use stable topology, while V1 remains intentionally type-driven and therefore order independent.
    """
    del graph
    not_applicable = {dimension: DimensionApplicability.NOT_APPLICABLE for dimension in AssessmentDimension}
    if not isinstance(node.type, NodeType):
        return not_applicable
    if node.type in _INTENT_TYPES:
        return {
            AssessmentDimension.INTENT_CLARITY: DimensionApplicability.REQUIRED,
            AssessmentDimension.EVIDENCE_STRENGTH: DimensionApplicability.REQUIRED,
            AssessmentDimension.REQUIREMENT_COVERAGE: DimensionApplicability.REQUIRED,
            AssessmentDimension.IMPLEMENTATION_TRACEABILITY: DimensionApplicability.INHERITED,
            AssessmentDimension.TEST_VERIFICATION: DimensionApplicability.INHERITED,
            AssessmentDimension.CONSISTENCY: DimensionApplicability.REQUIRED,
            AssessmentDimension.FRESHNESS: DimensionApplicability.REQUIRED,
        }
    if node.type in {NodeType.REQUIREMENT, NodeType.ACCEPTANCE_CRITERION}:
        return {dimension: DimensionApplicability.REQUIRED for dimension in AssessmentDimension}
    if node.type in _IMPLEMENTATION_TYPES:
        return {
            AssessmentDimension.INTENT_CLARITY: DimensionApplicability.NOT_APPLICABLE,
            AssessmentDimension.EVIDENCE_STRENGTH: DimensionApplicability.REQUIRED,
            AssessmentDimension.REQUIREMENT_COVERAGE: DimensionApplicability.NOT_APPLICABLE,
            AssessmentDimension.IMPLEMENTATION_TRACEABILITY: DimensionApplicability.OPTIONAL,
            AssessmentDimension.TEST_VERIFICATION: DimensionApplicability.OPTIONAL,
            AssessmentDimension.CONSISTENCY: DimensionApplicability.REQUIRED,
            AssessmentDimension.FRESHNESS: DimensionApplicability.REQUIRED,
        }
    if node.type is NodeType.TEST:
        return {
            AssessmentDimension.INTENT_CLARITY: DimensionApplicability.NOT_APPLICABLE,
            AssessmentDimension.EVIDENCE_STRENGTH: DimensionApplicability.REQUIRED,
            AssessmentDimension.REQUIREMENT_COVERAGE: DimensionApplicability.NOT_APPLICABLE,
            AssessmentDimension.IMPLEMENTATION_TRACEABILITY: DimensionApplicability.NOT_APPLICABLE,
            AssessmentDimension.TEST_VERIFICATION: DimensionApplicability.OPTIONAL,
            AssessmentDimension.CONSISTENCY: DimensionApplicability.REQUIRED,
            AssessmentDimension.FRESHNESS: DimensionApplicability.REQUIRED,
        }
    return not_applicable


def assess_node(
    snapshot: AssessmentSnapshot,
    node_id: str,
    policy: AssessmentPolicy,
) -> NodeScorecard:
    """Evaluate exactly one visible node without reading stores or inferring unstated facts."""
    node = _node_index(snapshot.graph)[node_id]
    matrix = applicability(node, snapshot.graph)
    if not isinstance(node.type, NodeType):
        return _unassessed_scorecard(node, matrix)

    results = tuple(
        _dimension_result(snapshot, node, dimension, matrix[dimension], policy)
        for dimension in AssessmentDimension
    )
    contributors = tuple(
        result
        for result in results
        if result.applicability
        in {DimensionApplicability.REQUIRED, DimensionApplicability.INHERITED}
    )
    if not contributors:
        return _unassessed_scorecard(node, matrix)

    total_weight = sum(policy.dimension_weights[result.dimension] for result in contributors)
    robustness = sum(
        policy.dimension_weights[result.dimension] * _score(result) for result in contributors
    ) // total_weight
    confidence = sum(
        policy.dimension_weights[result.dimension] * _confidence(result) for result in contributors
    ) // total_weight
    blocking_refs = tuple(case.id for case in _blocking_cases(snapshot.cases, {node.id}))
    has_blocking_conflict = any(
        result.dimension is AssessmentDimension.CONSISTENCY and result.failed for result in contributors
    )
    health = _node_health(contributors, policy, has_blocking=has_blocking_conflict)
    if health is AssessmentHealth.RED and has_blocking_conflict:
        robustness = min(robustness, policy.red_below - 1)
    worst = min(contributors, key=lambda result: (_score(result), _confidence(result), result.dimension.value))
    return NodeScorecard(
        node_id=node.id,
        node_type=node.type,
        robustness=robustness,
        confidence=confidence,
        health=health,
        worst_dimension=worst.dimension,
        dimensions=results,
        blocking_case_refs=blocking_refs,
        recommended_next_action=_next_action(results),
    )


def score_dimension(rules: tuple[Rule, ...], context: RubricContext) -> DimensionResult:
    """Apply every distinct failed rule once and derive confidence from declared input slots."""
    failed_by_rule = {
        item.rule_id: item for item in (rule(context) for rule in rules) if item is not None
    }
    failed = tuple(sorted(failed_by_rule.values(), key=lambda item: item.rule_id))
    score = max(0, 100 - sum(item.points for item in failed))
    resolved = sum(item.resolved_inputs for item in context.input_checks)
    required = sum(item.required_inputs for item in context.input_checks)
    confidence = 0 if required == 0 else resolved * 100 // required
    health = _dimension_health(score, confidence, context.policy)
    return DimensionResult(
        dimension=context.dimension,
        applicability=context.applicability,
        score=score,
        health=health,
        confidence=confidence,
        passed=(),
        failed=failed,
        evidence_refs=context.evidence_refs,
        related_refs=tuple(sorted(set(context.related_edges) | {case.id for case in context.blocking_cases})),
        recommended_next_action=_next_action_for_failed(failed),
    )


def _dimension_result(
    snapshot: AssessmentSnapshot,
    node: Node,
    dimension: AssessmentDimension,
    dimension_applicability: DimensionApplicability,
    policy: AssessmentPolicy,
) -> DimensionResult:
    if dimension_applicability is DimensionApplicability.NOT_APPLICABLE:
        return DimensionResult(
            dimension=dimension,
            applicability=dimension_applicability,
            score=None,
            health=AssessmentHealth.UNASSESSED,
            confidence=None,
        )
    context = _context(snapshot, node, dimension, dimension_applicability, policy)
    return score_dimension(_rules_for(dimension), context)


def _context(
    snapshot: AssessmentSnapshot,
    node: Node,
    dimension: AssessmentDimension,
    dimension_applicability: DimensionApplicability,
    policy: AssessmentPolicy,
) -> RubricContext:
    nodes = _node_index(snapshot.graph)
    subject_ids = _subject_ids(snapshot.graph, node, dimension_applicability)
    related_edges = tuple(
        sorted(
            edge.id
            for edge in snapshot.graph.edges
            if edge.status == _ACTIVE and (edge.from_id in subject_ids or edge.to_id in subject_ids)
        )
    )
    related_nodes = tuple(sorted((nodes[item] for item in subject_ids), key=lambda item: item.id))
    refs = tuple(sorted({reference for item in related_nodes for reference in item.evidence_refs}))
    visible_evidence = _evidence_index(snapshot)
    evidence_resolved = bool(refs) and all(reference in visible_evidence for reference in refs)
    clarity_resolved = node.source_mode is not None and node.intent_fidelity_confidence is not None
    coverage_resolved = _has_intent_coverage(snapshot.graph, node)
    implementation_resolved = _has_implementation_link(snapshot.graph, subject_ids, nodes)
    test_resolved = _has_current_test_link(snapshot, subject_ids, nodes)
    freshness_resolved = evidence_resolved
    return RubricContext(
        dimension=dimension,
        applicability=dimension_applicability,
        policy=policy,
        node=node,
        snapshot=snapshot,
        related_nodes=related_nodes,
        related_edges=related_edges,
        evidence_refs=refs,
        blocking_cases=_blocking_cases(snapshot.cases, subject_ids),
        input_checks=_input_checks(
            dimension,
            clarity_resolved=clarity_resolved,
            evidence_resolved=evidence_resolved,
            coverage_resolved=coverage_resolved,
            implementation_resolved=implementation_resolved,
            test_resolved=test_resolved,
            freshness_resolved=freshness_resolved,
        ),
    )


def _input_checks(
    dimension: AssessmentDimension,
    *,
    clarity_resolved: bool,
    evidence_resolved: bool,
    coverage_resolved: bool,
    implementation_resolved: bool,
    test_resolved: bool,
    freshness_resolved: bool,
) -> tuple[InputCheck, ...]:
    resolved = {
        AssessmentDimension.INTENT_CLARITY: clarity_resolved,
        AssessmentDimension.EVIDENCE_STRENGTH: evidence_resolved,
        AssessmentDimension.REQUIREMENT_COVERAGE: coverage_resolved,
        AssessmentDimension.IMPLEMENTATION_TRACEABILITY: implementation_resolved,
        AssessmentDimension.TEST_VERIFICATION: test_resolved,
        AssessmentDimension.CONSISTENCY: True,
        AssessmentDimension.FRESHNESS: freshness_resolved,
    }[dimension]
    return (InputCheck(required_inputs=1, resolved_inputs=int(resolved)),)


def _rules_for(dimension: AssessmentDimension) -> tuple[Rule, ...]:
    return {
        AssessmentDimension.INTENT_CLARITY: (_no_explicit_typed_assertion,),
        AssessmentDimension.EVIDENCE_STRENGTH: (_no_visible_evidence_identity,),
        AssessmentDimension.REQUIREMENT_COVERAGE: (_no_intent_coverage,),
        AssessmentDimension.IMPLEMENTATION_TRACEABILITY: (_no_implementation_link,),
        AssessmentDimension.TEST_VERIFICATION: (_no_current_test_evidence,),
        AssessmentDimension.CONSISTENCY: (_blocking_conflict,),
        AssessmentDimension.FRESHNESS: (_stale_evidence,),
    }[dimension]


def _no_explicit_typed_assertion(context: RubricContext) -> RubricCheck | None:
    if context.node.source_mode is not None and context.node.intent_fidelity_confidence is not None:
        return None
    return _failed(context, "no_explicit_typed_assertion", 51, "Add explicit source mode and fidelity.")


def _no_visible_evidence_identity(context: RubricContext) -> RubricCheck | None:
    visible = _evidence_index(context.snapshot)
    if context.evidence_refs and all(reference in visible for reference in context.evidence_refs):
        return None
    return _failed(context, "no_visible_evidence_identity", 50, "Attach visible attributable evidence.")


def _no_intent_coverage(context: RubricContext) -> RubricCheck | None:
    if _has_intent_coverage(context.snapshot.graph, context.node):
        return None
    return _failed(context, "no_intent_coverage", 51, "Connect the assertion to an intent relation.")


def _no_implementation_link(context: RubricContext) -> RubricCheck | None:
    if _has_implementation_link(
        context.snapshot.graph,
        {item.id for item in context.related_nodes},
        _node_index(context.snapshot.graph),
    ):
        return None
    return _failed(context, "no_implementation_link", 26, "Connect required behavior to implementation.")


def _no_current_test_evidence(context: RubricContext) -> RubricCheck | None:
    if _has_current_test_link(
        context.snapshot,
        {item.id for item in context.related_nodes},
        _node_index(context.snapshot.graph),
    ):
        return None
    return _failed(context, "no_current_test_evidence", 60, "Connect required behavior to current test evidence.")


def _blocking_conflict(context: RubricContext) -> RubricCheck | None:
    contradictory = any(
        edge.status == _ACTIVE
        and edge.relation is RelationType.CONTRADICTS
        and (edge.from_id == context.node.id or edge.to_id == context.node.id)
        for edge in context.snapshot.graph.edges
    )
    if not context.blocking_cases and not contradictory:
        return None
    refs = tuple(sorted({case.id for case in context.blocking_cases} | set(context.related_edges)))
    return RubricCheck(
        rule_id="rubric:v1:consistency:blocking_conflict",
        points=100,
        severity=AssessmentHealth.RED,
        explanation="Resolve the visible blocking conflict.",
        references=refs,
    )


def _stale_evidence(context: RubricContext) -> RubricCheck | None:
    evidence = _evidence_index(context.snapshot)
    referenced = tuple(evidence[reference] for reference in context.evidence_refs if reference in evidence)
    if not referenced or not _has_newer_visible_revision(referenced, context.snapshot.ingestions):
        return None
    return _failed(context, "stale_evidence", 25, "Refresh evidence from the newest visible revision.")


def _failed(context: RubricContext, name: str, points: int, explanation: str) -> RubricCheck:
    return RubricCheck(
        rule_id=f"rubric:v1:{context.dimension.value}:{name}",
        points=points,
        severity=AssessmentHealth.RED,
        explanation=explanation,
        references=tuple(sorted(set(context.evidence_refs) | set(context.related_edges))),
    )


def _has_newer_visible_revision(
    referenced: tuple[EvidenceRecord, ...], ingestions: tuple[EvidenceIngestion, ...]
) -> bool:
    for record in referenced:
        associations = tuple(item for item in ingestions if item.evidence.id == record.id)
        for association in associations:
            for candidate in ingestions:
                if (
                    candidate.connector_id == association.connector_id
                    and candidate.evidence.connector_type == record.connector_type
                    and candidate.evidence.external_object_id == record.external_object_id
                    and candidate.sequence > association.sequence
                    and _revision_identity(candidate.evidence) != _revision_identity(record)
                ):
                    return True
    return False


def _revision_identity(record: EvidenceRecord) -> tuple[str, str, str | None]:
    """Return provider revision fields, excluding wall-clock recapture time."""
    return (record.external_version, record.content_hash, record.parent_ref)


def _node_index(graph: Graph) -> dict[str, Node]:
    return {node.id: node for node in graph.nodes}


def _evidence_index(snapshot: AssessmentSnapshot) -> dict[str, EvidenceRecord]:
    return {record.id: record for record in snapshot.evidence}


def _subject_ids(graph: Graph, node: Node, dimension_applicability: DimensionApplicability) -> set[str]:
    if dimension_applicability is not DimensionApplicability.INHERITED:
        return {node.id}
    descendants = {node.id}
    visible_node_ids = set(_node_index(graph))
    changed = True
    while changed:
        changed = False
        for edge in graph.edges:
            if (
                edge.status == _ACTIVE
                and edge.from_id in descendants
                and edge.relation
                in {
                    RelationType.REFINES,
                    RelationType.SPECIFIED_BY,
                    RelationType.HAS_ACCEPTANCE_CRITERION,
                }
                and edge.to_id in visible_node_ids
                and edge.to_id not in descendants
            ):
                descendants.add(edge.to_id)
                changed = True
    return descendants


def _has_intent_coverage(graph: Graph, node: Node) -> bool:
    nodes = _node_index(graph)
    if node.type in _INTENT_TYPES:
        return any(
            edge.status == _ACTIVE
            and edge.from_id == node.id
            and edge.relation in {RelationType.REFINES, RelationType.SPECIFIED_BY}
            and edge.to_id in nodes
            and nodes[edge.to_id].type in {NodeType.REQUIREMENT, NodeType.ACCEPTANCE_CRITERION}
            for edge in graph.edges
        )
    if node.type in {NodeType.REQUIREMENT, NodeType.ACCEPTANCE_CRITERION}:
        return any(
            edge.status == _ACTIVE
            and edge.to_id == node.id
            and edge.relation in {
                RelationType.REFINES,
                RelationType.SPECIFIED_BY,
                RelationType.HAS_ACCEPTANCE_CRITERION,
            }
            and edge.from_id in nodes
            and nodes[edge.from_id].type in _SEMANTIC_TYPES
            for edge in graph.edges
        )
    return True


def _has_implementation_link(graph: Graph, subject_ids: set[str], nodes: Mapping[str, Node]) -> bool:
    return any(
        (
            edge.status == _ACTIVE
            and edge.from_id in subject_ids
            and edge.relation in {RelationType.IMPLEMENTED_BY, RelationType.REALIZED_BY}
            and edge.to_id in nodes
            and nodes[edge.to_id].type in _IMPLEMENTATION_TYPES
        )
        or (
            edge.status == _ACTIVE
            and edge.to_id in subject_ids
            and edge.relation in {RelationType.IMPLEMENTED_BY, RelationType.REALIZED_BY}
            and edge.from_id in nodes
            and nodes[edge.from_id].type in {NodeType.REQUIREMENT, NodeType.ACCEPTANCE_CRITERION}
        )
        for edge in graph.edges
    )


def _has_current_test_link(
    snapshot: AssessmentSnapshot,
    subject_ids: set[str],
    nodes: Mapping[str, Node],
) -> bool:
    evidence = _evidence_index(snapshot)
    for edge in snapshot.graph.edges:
        if (
            edge.status == _ACTIVE
            and edge.from_id in subject_ids
            and edge.relation is RelationType.VERIFIED_BY
            and edge.to_id in nodes
            and nodes[edge.to_id].type is NodeType.TEST
        ):
            refs = nodes[edge.to_id].evidence_refs
            if refs and all(reference in evidence for reference in refs):
                test_evidence = tuple(evidence[reference] for reference in refs)
                if not _has_newer_visible_revision(test_evidence, snapshot.ingestions):
                    return True
    return False


def _blocking_cases(cases: tuple[ReconciliationCase, ...], subject_ids: set[str]) -> tuple[ReconciliationCase, ...]:
    return tuple(
        sorted(
            (
                case
                for case in cases
                if is_nonterminal_case_status(case.status)
                and (case.subject_ref in subject_ids or set(case.affected_refs) & subject_ids)
            ),
            key=lambda case: case.id,
        )
    )


def _dimension_health(score: int, confidence: int, policy: AssessmentPolicy) -> AssessmentHealth:
    if score < policy.red_below:
        return AssessmentHealth.RED
    if score < policy.green_at or confidence < policy.green_confidence_at:
        return AssessmentHealth.ORANGE
    return AssessmentHealth.GREEN


def _node_health(
    contributors: tuple[DimensionResult, ...], policy: AssessmentPolicy, *, has_blocking: bool
) -> AssessmentHealth:
    if has_blocking or any(_score(result) < policy.red_below for result in contributors):
        return AssessmentHealth.RED
    if all(
        _score(result) >= policy.green_at and _confidence(result) >= policy.green_confidence_at
        for result in contributors
    ):
        return AssessmentHealth.GREEN
    return AssessmentHealth.ORANGE


def _score(result: DimensionResult) -> int:
    assert result.score is not None
    return result.score


def _confidence(result: DimensionResult) -> int:
    assert result.confidence is not None
    return result.confidence


def _next_action(results: tuple[DimensionResult, ...]) -> str | None:
    failed = tuple(check for result in results for check in result.failed)
    return _next_action_for_failed(failed)


def _next_action_for_failed(failed: tuple[RubricCheck, ...]) -> str | None:
    if not failed:
        return None
    return min(failed, key=lambda check: (-check.points, check.rule_id)).explanation


def _unassessed_scorecard(
    node: Node,
    matrix: Mapping[AssessmentDimension, DimensionApplicability],
) -> NodeScorecard:
    return NodeScorecard(
        node_id=node.id,
        node_type=node.type,
        robustness=None,
        confidence=None,
        health=AssessmentHealth.UNASSESSED,
        worst_dimension=None,
        dimensions=tuple(
            DimensionResult(
                dimension=dimension,
                applicability=DimensionApplicability.NOT_APPLICABLE,
                score=None,
                health=AssessmentHealth.UNASSESSED,
                confidence=None,
            )
            for dimension in matrix
        ),
        blocking_case_refs=(),
        recommended_next_action=None,
    )
