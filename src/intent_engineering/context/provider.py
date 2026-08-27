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
_REF_LIMITS = {
    "relevant_intent": 10,
    "relevant_requirements": 10,
    "decisions": 10,
    "constraints": 10,
    "acceptance_criteria": 10,
    "code_refs": 20,
    "test_refs": 20,
    "open_reconciliation_cases": 10,
    "evidence_refs": 20,
}
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
        latest_cases = {case.id: case for case in cases}
        self._cases = tuple(latest_cases[case_id] for case_id in sorted(latest_cases))
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

    def for_refs(
        self,
        node_ids: Sequence[str],
        *,
        actor: str | Collection[str],
    ) -> ContextPack:
        """Return bounded two-hop context for exact active, ACL-visible node IDs."""
        requested: tuple[str, ...] = ()
        principals: str | frozenset[str] = ""
        pack: ContextPack | None = None
        returned_ids: set[str] = set()
        failed = False
        try:
            if (
                isinstance(node_ids, str)
                or not node_ids
                or len(node_ids) > 256
                or any(type(node_id) is not str or not node_id for node_id in node_ids)
                or len(node_ids) != len(set(node_ids))
            ):
                raise ValueError("invalid exact context references")
            requested = tuple(sorted(node_ids))
            if type(actor) is str:
                principals = actor
            else:
                principals = frozenset(actor)
                if not principals or any(type(item) is not str or not item for item in principals):
                    raise ValueError("invalid context principal")
            pack = self._build(
                query="",
                repository_scope=None,
                actor=principals,
                exact_seed_ids=frozenset(requested),
                fixed_caps=True,
            )
            returned_ids = {
                item.id
                for category in (
                    pack.relevant_intent,
                    pack.relevant_requirements,
                    pack.decisions,
                    pack.constraints,
                    pack.acceptance_criteria,
                    pack.code_refs,
                    pack.test_refs,
                )
                for item in category
            }
            if not set(requested).issubset(returned_ids):
                raise ValueError("unavailable exact context references")
            return pack
        except Exception:  # noqa: BLE001 - hide absent versus unauthorized references
            failed = True
        finally:
            node_ids = ()
            actor = ""
            requested = ()
            principals = ""
            pack = None
            returned_ids.clear()
        if failed:
            raise ValueError("intent context unavailable") from None
        raise ValueError("intent context unavailable") from None

    def _build(
        self,
        *,
        query: str,
        repository_scope: str | None,
        actor: str | Collection[str] | None,
        exact_seed_id: str | None = None,
        exact_seed_ids: frozenset[str] | None = None,
        fixed_caps: bool = False,
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
        seed_ids = set(exact_seed_ids or ())
        if exact_seed_ids is None:
            seed_ids = (
                {exact_seed_id}
                if exact_seed_id is not None and exact_seed_id in active_nodes
                else set()
            )
        if exact_seed_id is None and exact_seed_ids is None:
            seed_ids = {node_id for node_id, score in scores.items() if score > 0}
        if exact_seed_ids is not None:
            if not seed_ids.issubset(active_nodes):
                raise ValueError("unavailable exact context references")
            scores.update({node_id: 1 for node_id in seed_ids})
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
        capped_nodes = {
            name: tuple(
                self._cap(
                    name,
                    nodes,
                    hard_limit=_REF_LIMITS[name] if fixed_caps else None,
                )
            )
            for name, nodes in grouped.items()
        }
        node_items = {
            name: tuple(self._item_for_node(node) for node in nodes)
            for name, nodes in capped_nodes.items()
        }
        ordered_cases = tuple(
            sorted(selected_cases, key=lambda case: (-self._case_score(case, scores), case.id))
        )
        capped_cases = tuple(
            self._cap(
                "open_reconciliation_cases",
                ordered_cases,
                hard_limit=(
                    _REF_LIMITS["open_reconciliation_cases"] if fixed_caps else None
                ),
            )
        )
        case_items = tuple(self._item_for_case(case) for case in capped_cases)
        context_nodes = tuple(node for nodes in capped_nodes.values() for node in nodes)
        evidence_refs = self._evidence_refs(
            context_nodes,
            capped_cases,
            scores,
            hard_limit=_REF_LIMITS["evidence_refs"] if fixed_caps else None,
        )

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

    def _cap(
        self,
        category: str,
        values: Sequence[_Item],
        *,
        hard_limit: int | None = None,
    ) -> tuple[_Item, ...]:
        limit = self._config.context_limits.get(category, len(values))
        if hard_limit is not None:
            limit = min(limit, hard_limit)
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
        self,
        nodes: Sequence[Node],
        cases: Sequence[ReconciliationCase],
        scores: dict[str, int],
        *,
        hard_limit: int | None = None,
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
        return tuple(
            str(value)
            for value in self._cap("evidence_refs", ordered, hard_limit=hard_limit)
        )

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
