"""Pure critical-branch and project rollups over detached node scorecards."""

from __future__ import annotations

from collections.abc import Mapping

from intent_engineering.assessment.models import (
    AssessmentHealth,
    AssessmentSnapshot,
    BranchScorecard,
    NodeScorecard,
    ProjectScorecard,
)
from intent_engineering.assessment.policy import AssessmentPolicy
from intent_engineering.core.models import Graph, NodeType

_ACTIVE = "active"
_BRANCH_ROOT_TYPES = frozenset({NodeType.PRODUCT_INTENT, NodeType.DESIRED_OUTCOME})


def _health(scorecards: tuple[NodeScorecard | BranchScorecard, ...]) -> AssessmentHealth:
    if any(scorecard.health is AssessmentHealth.RED for scorecard in scorecards):
        return AssessmentHealth.RED
    assessed = tuple(
        scorecard for scorecard in scorecards if scorecard.health is not AssessmentHealth.UNASSESSED
    )
    if not assessed:
        return AssessmentHealth.UNASSESSED
    if all(scorecard.health is AssessmentHealth.GREEN for scorecard in assessed):
        return AssessmentHealth.GREEN
    return AssessmentHealth.ORANGE


def _robustness(scorecard: NodeScorecard | BranchScorecard) -> int:
    assert scorecard.robustness is not None
    return scorecard.robustness


def _confidence(scorecard: NodeScorecard | BranchScorecard) -> int:
    assert scorecard.confidence is not None
    return scorecard.confidence


def _branch_members(graph: Graph, root_id: str, policy: AssessmentPolicy) -> tuple[str, ...]:
    nodes = {node.id: node for node in graph.nodes}
    adjacency: dict[str, list[str]] = {}
    for edge in graph.edges:
        target = nodes.get(edge.to_id)
        if (
            edge.status != _ACTIVE
            or edge.relation not in policy.critical_relations
            or target is None
            or target.status != _ACTIVE
            or target.type not in policy.critical_node_types
        ):
            continue
        adjacency.setdefault(edge.from_id, []).append(target.id)

    members: set[str] = set()
    pending = [root_id]
    while pending:
        node_id = pending.pop()
        if node_id in members:
            continue
        members.add(node_id)
        pending.extend(sorted(adjacency.get(node_id, ()), reverse=True))
    return tuple(sorted(members))


def _branch_scorecard(
    snapshot: AssessmentSnapshot,
    root_id: str,
    scorecards: Mapping[str, NodeScorecard],
    policy: AssessmentPolicy,
) -> BranchScorecard:
    path_ids = tuple(
        node_id
        for node_id in _branch_members(snapshot.graph, root_id, policy)
        if node_id in scorecards
    )
    path_scorecards = tuple(scorecards[node_id] for node_id in path_ids)
    assessed = tuple(
        scorecard
        for scorecard in path_scorecards
        if scorecard.robustness is not None and scorecard.confidence is not None
    )
    node_ids = tuple(scorecard.node_id for scorecard in assessed)
    if not assessed:
        return BranchScorecard(
            branch_id=root_id,
            root_node_id=root_id,
            node_ids=node_ids,
            robustness=None,
            confidence=None,
            health=AssessmentHealth.UNASSESSED,
            contribution_weights={node_id: 1 for node_id in node_ids},
        )

    robustness = sum(_robustness(scorecard) for scorecard in assessed) // len(assessed)
    confidence = sum(_confidence(scorecard) for scorecard in assessed) // len(assessed)
    health = _health(path_scorecards)
    if health is AssessmentHealth.RED:
        robustness = min(robustness, 49)
    return BranchScorecard(
        branch_id=root_id,
        root_node_id=root_id,
        node_ids=node_ids,
        robustness=robustness,
        confidence=confidence,
        health=health,
        contribution_weights={node_id: 1 for node_id in node_ids},
    )


def _project_scorecard(
    snapshot: AssessmentSnapshot,
    branches: tuple[BranchScorecard, ...],
    policy: AssessmentPolicy,
) -> ProjectScorecard:
    branch_ids = tuple(branch.branch_id for branch in branches)
    contributing_node_ids = tuple(
        sorted({node_id for branch in branches for node_id in branch.node_ids})
    )
    weights = {
        branch_id: policy.branch_weights.get(branch_id, policy.default_branch_weight)
        for branch_id in branch_ids
    }
    assessed = tuple(
        branch
        for branch in branches
        if branch.robustness is not None and branch.confidence is not None
    )
    if not assessed:
        return ProjectScorecard(
            project_id=snapshot.project_id,
            robustness=None,
            confidence=None,
            health=AssessmentHealth.UNASSESSED,
            branch_ids=branch_ids,
            contributing_node_ids=contributing_node_ids,
            contribution_weights=weights,
        )

    total_weight = sum(weights[branch.branch_id] for branch in assessed)
    robustness = (
        sum(weights[branch.branch_id] * _robustness(branch) for branch in assessed) // total_weight
    )
    confidence = (
        sum(weights[branch.branch_id] * _confidence(branch) for branch in assessed) // total_weight
    )
    health = _health(branches)
    if health is AssessmentHealth.RED:
        robustness = min(robustness, 49)
    return ProjectScorecard(
        project_id=snapshot.project_id,
        robustness=robustness,
        confidence=confidence,
        health=health,
        branch_ids=branch_ids,
        contributing_node_ids=contributing_node_ids,
        contribution_weights=weights,
    )


def roll_up(
    snapshot: AssessmentSnapshot,
    nodes: tuple[NodeScorecard, ...],
    policy: AssessmentPolicy,
) -> tuple[ProjectScorecard, tuple[BranchScorecard, ...]]:
    """Build canonical branch and project scorecards from already assessed visible nodes."""
    scorecards = {scorecard.node_id: scorecard for scorecard in nodes}
    root_ids = tuple(
        sorted(
            node.id
            for node in snapshot.graph.nodes
            if node.status == _ACTIVE and node.type in _BRANCH_ROOT_TYPES
        )
    )
    branches = tuple(
        _branch_scorecard(snapshot, root_id, scorecards, policy) for root_id in root_ids
    )
    return _project_scorecard(snapshot, branches, policy), branches


__all__ = ["roll_up"]
