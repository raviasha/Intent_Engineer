"""Deterministically select concise context from immutable local state."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Collection, Iterable, Sequence
from typing import TypeVar

from intent_engineering.core.models import (
    ContextItem,
    ContextPack,
    EvidenceRecord,
    Graph,
    Node,
    NodeType,
    ProjectConfig,
    ReconciliationCase,
    is_nonterminal_case_status,
)

_WORD = re.compile(r"\w+", re.UNICODE)
_INTENT_TYPES = frozenset(
    {
        NodeType.CONTEXT,
        NodeType.NEED,
        NodeType.PRODUCT_INTENT,
        NodeType.DESIRED_OUTCOME,
        NodeType.ASSUMPTION,
        NodeType.PRINCIPLE,
    }
)
_REQUIREMENT_TYPES = frozenset({NodeType.REQUIREMENT, NodeType.CAPABILITY})
_DECISION_TYPES = frozenset(
    {NodeType.DECISION, NodeType.ARCHITECTURE, NodeType.INTERFACE, NodeType.DATA_CONTRACT}
)
_CONSTRAINT_TYPES = frozenset({NodeType.CONSTRAINT, NodeType.POLICY})
_CODE_TYPES = frozenset(
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
_Item = TypeVar("_Item")


def _tokens(value: str) -> frozenset[str]:
    """Extract lower-cased Unicode word tokens from a query or label."""
    return frozenset(_WORD.findall(value.lower()))


def _node_type(node: Node) -> str:
    return node.type.value if isinstance(node.type, NodeType) else node.type


class ContextProvider:
    """Build bounded, access-filtered context from graph, cases, and evidence."""

    def __init__(
        self,
        graph: Graph,
        cases: Sequence[ReconciliationCase],
        config: ProjectConfig,
        evidence: Sequence[EvidenceRecord] = (),
    ) -> None:
        self._graph = graph
        self._cases = tuple(cases)
        self._config = config
        self._evidence = {record.id: record for record in evidence}

    def for_task(
        self,
        task: str,
        repository_scope: str | None = None,
        actor: str | Collection[str] | None = None,
    ) -> ContextPack:
        """Return bounded context for a natural-language task."""
        return self._build(query=task, repository_scope=repository_scope, actor=actor)

    def for_symbol(
        self,
        symbol_ref: str,
        actor: str | Collection[str] | None = None,
    ) -> ContextPack:
        """Return bounded context for a symbol identifier or label."""
        return self._build(
            query=symbol_ref,
            repository_scope=None,
            actor=actor,
            exact_seed_id=symbol_ref or None,
        )

    def _build(
        self,
        *,
        query: str,
        repository_scope: str | None,
        actor: str | Collection[str] | None,
        exact_seed_id: str | None = None,
    ) -> ContextPack:
        query_tokens = _tokens(query)
        active_nodes = {
            node.id: node
            for node in self._graph.nodes
            if node.status == "active" and self._node_allowed(node, repository_scope, actor)
        }
        scores = {
            node_id: len(query_tokens & _tokens(node.label))
            for node_id, node in active_nodes.items()
        }
        seed_ids = (
            {exact_seed_id}
            if exact_seed_id is not None and exact_seed_id in active_nodes
            else set()
        )
        if exact_seed_id is None:
            seed_ids = {node_id for node_id, score in scores.items() if score > 0}
        selected_ids = self._expand(seed_ids, active_nodes)
        selected_nodes = tuple(active_nodes[node_id] for node_id in selected_ids)
        selected_cases = self._selected_cases(selected_ids, repository_scope, actor)

        def ordered(nodes: Iterable[Node]) -> tuple[Node, ...]:
            return tuple(sorted(nodes, key=lambda node: (-scores[node.id], node.id)))

        grouped = {
            "relevant_intent": ordered(
                node for node in selected_nodes if node.type in _INTENT_TYPES
            ),
            "relevant_requirements": ordered(
                node for node in selected_nodes if node.type in _REQUIREMENT_TYPES
            ),
            "decisions": ordered(node for node in selected_nodes if node.type in _DECISION_TYPES),
            "constraints": ordered(
                node for node in selected_nodes if node.type in _CONSTRAINT_TYPES
            ),
            "acceptance_criteria": ordered(
                node for node in selected_nodes if node.type is NodeType.ACCEPTANCE_CRITERION
            ),
            "code_refs": ordered(node for node in selected_nodes if node.type in _CODE_TYPES),
            "test_refs": ordered(node for node in selected_nodes if node.type is NodeType.TEST),
        }
        capped_nodes = {name: tuple(self._cap(name, nodes)) for name, nodes in grouped.items()}
        node_items = {
            name: tuple(self._item_for_node(node) for node in nodes)
            for name, nodes in capped_nodes.items()
        }
        ordered_cases = tuple(
            sorted(selected_cases, key=lambda case: (-self._case_score(case, scores), case.id))
        )
        capped_cases = tuple(self._cap("open_reconciliation_cases", ordered_cases))
        case_items = tuple(self._item_for_case(case) for case in capped_cases)
        context_nodes = tuple(node for nodes in capped_nodes.values() for node in nodes)
        evidence_refs = self._evidence_refs(context_nodes, capped_cases, scores)

        return ContextPack(
            task=query,
            relevant_intent=node_items["relevant_intent"],
            relevant_requirements=node_items["relevant_requirements"],
            decisions=node_items["decisions"],
            constraints=node_items["constraints"],
            acceptance_criteria=node_items["acceptance_criteria"],
            code_refs=node_items["code_refs"],
            test_refs=node_items["test_refs"],
            open_reconciliation_cases=case_items,
            evidence_refs=evidence_refs,
            warnings=self._warnings(context_nodes, scores),
        )

    def _expand(self, seeds: set[str], active_nodes: dict[str, Node]) -> set[str]:
        """Follow only active graph edges, in either direction, for two hops."""
        neighbours: dict[str, set[str]] = {node_id: set() for node_id in active_nodes}
        for edge in self._graph.edges:
            if (
                edge.status == "active"
                and edge.from_id in active_nodes
                and edge.to_id in active_nodes
            ):
                neighbours[edge.from_id].add(edge.to_id)
                neighbours[edge.to_id].add(edge.from_id)
        selected = set(seeds)
        frontier = deque((node_id, 0) for node_id in sorted(seeds))
        while frontier:
            node_id, hops = frontier.popleft()
            if hops == 2:
                continue
            for neighbour in sorted(neighbours[node_id]):
                if neighbour not in selected:
                    selected.add(neighbour)
                    frontier.append((neighbour, hops + 1))
        return selected

    def _selected_cases(
        self,
        selected_ids: set[str],
        repository_scope: str | None,
        actor: str | Collection[str] | None,
    ) -> tuple[ReconciliationCase, ...]:
        return tuple(
            case
            for case in self._cases
            if is_nonterminal_case_status(case.status)
            and (case.subject_ref in selected_ids or bool(set(case.affected_refs) & selected_ids))
            and self._refs_allowed(case.all_evidence_refs, repository_scope, actor)
        )

    def _node_allowed(
        self,
        node: Node,
        repository_scope: str | None,
        actor: str | Collection[str] | None,
    ) -> bool:
        return self._refs_allowed(node.evidence_refs, repository_scope, actor)

    def _refs_allowed(
        self,
        evidence_refs: Sequence[str],
        repository_scope: str | None,
        actor: str | Collection[str] | None,
    ) -> bool:
        records = tuple(self._evidence.get(reference) for reference in evidence_refs)
        return all(record is not None for record in records) and all(
            self._evidence_allowed(record, repository_scope, actor)
            for record in records
            if record is not None
        )

    @staticmethod
    def _scope(record: EvidenceRecord) -> str | None:
        scope = record.payload.get("repository_scope")
        return scope if isinstance(scope, str) else None

    def _evidence_allowed(
        self,
        record: EvidenceRecord,
        repository_scope: str | None,
        actor: str | Collection[str] | None,
    ) -> bool:
        if record.acl and (
            actor is None
            or (type(actor) is str and actor not in record.acl)
            or (type(actor) is not str and frozenset(record.acl).isdisjoint(actor))
        ):
            return False
        scope = self._scope(record)
        return repository_scope is None or scope == repository_scope

    def _cap(self, category: str, values: Sequence[_Item]) -> tuple[_Item, ...]:
        limit = self._config.context_limits.get(category, len(values))
        return tuple(values[: max(0, limit)])

    @staticmethod
    def _item_for_node(node: Node) -> ContextItem:
        return ContextItem(
            id=node.id,
            type=_node_type(node),
            label=node.label,
            confidence=node.intent_fidelity_confidence,
            evidence_refs=node.evidence_refs,
        )

    @staticmethod
    def _item_for_case(case: ReconciliationCase) -> ContextItem:
        return ContextItem(
            id=case.id,
            type=NodeType.RECONCILIATION_CASE.value,
            label=f"{case.case_type.value}: {case.subject_ref}",
            confidence=None,
            evidence_refs=case.all_evidence_refs,
        )

    @staticmethod
    def _case_score(case: ReconciliationCase, scores: dict[str, int]) -> int:
        return max(
            (scores.get(reference, 0) for reference in (case.subject_ref, *case.affected_refs)),
            default=0,
        )

    def _evidence_refs(
        self, nodes: Sequence[Node], cases: Sequence[ReconciliationCase], scores: dict[str, int]
    ) -> tuple[str, ...]:
        evidence_scores: dict[str, int] = {}
        for node in nodes:
            for reference in node.evidence_refs:
                evidence_scores[reference] = max(evidence_scores.get(reference, 0), scores[node.id])
        for case in cases:
            score = self._case_score(case, scores)
            for reference in case.all_evidence_refs:
                evidence_scores[reference] = max(evidence_scores.get(reference, 0), score)
        ordered = tuple(
            sorted(evidence_scores, key=lambda reference: (-evidence_scores[reference], reference))
        )
        return tuple(str(value) for value in self._cap("evidence_refs", ordered))

    @staticmethod
    def _warnings(nodes: Sequence[Node], scores: dict[str, int]) -> tuple[str, ...]:
        warnings: list[tuple[int, str, str]] = []
        for node in nodes:
            if (
                node.intent_fidelity_confidence is not None
                and node.intent_fidelity_confidence < 0.65
            ):
                warnings.append((-scores[node.id], node.id, f"low confidence: {node.id}"))
            if not node.evidence_refs:
                warnings.append((-scores[node.id], node.id, f"missing evidence: {node.id}"))
        return tuple(message for _, _, message in sorted(warnings))
