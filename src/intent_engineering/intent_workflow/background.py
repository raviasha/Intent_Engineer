"""Conversational maintenance using canonical evidence, transactions and ChangeSets.

Reported user words are evidence, not authenticated approvals. Legacy proposals and
approval ledgers are never activated or rewritten by this service.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from intent_engineering.context.provider import ContextProvider
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    EvidenceRecord,
    EvidenceSide,
    Graph,
    JsonValue,
    Node,
    NodeType,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    ResolutionAction,
    SourceMode,
    is_nonterminal_case_status,
)
from intent_engineering.core.models.changeset import NodeUpdate
from intent_engineering.core.models.reconciliation import (
    ClassificationEvent,
    is_intent_requirement_case,
)
from intent_engineering.core.policy.access import evidence_allowed, refs_allowed
from intent_engineering.intent_workflow.background_models import (
    BackgroundAssertion,
    BackgroundSubmission,
)
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.transaction import LocalTransaction
from intent_engineering.validation import validate_canonical_snapshot

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime

ACTOR = "agent:intent-background"
CONNECTOR = "conversation:intent-background"
SEMANTIC_TYPES = frozenset(
    {
        NodeType.CONTEXT,
        NodeType.NEED,
        NodeType.PRODUCT_INTENT,
        NodeType.DESIRED_OUTCOME,
        NodeType.ASSUMPTION,
        NodeType.PRINCIPLE,
        NodeType.CONSTRAINT,
        NodeType.REQUIREMENT,
        NodeType.CAPABILITY,
        NodeType.ACCEPTANCE_CRITERION,
        NodeType.DECISION,
        NodeType.ARCHITECTURE,
        NodeType.INTERFACE,
        NodeType.DATA_CONTRACT,
        NodeType.POLICY,
    }
)
SEMANTIC_RELATIONS = frozenset(
    {
        "CREATES_NEED",
        "MOTIVATES",
        "SEEKS_OUTCOME",
        "CONSTRAINS",
        "ASSUMES",
        "JUSTIFIES",
        "REFINES",
        "SUPERSEDES",
        "CONTRADICTS",
        "REALIZED_BY",
        "SPECIFIED_BY",
        "HAS_ACCEPTANCE_CRITERION",
        "DECIDED_BY",
        "DEPENDS_ON",
        "IMPLEMENTS_CAPABILITY",
    }
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class BackgroundIntentService:
    def __init__(self, runtime: Runtime, *, clock: Callable[[], datetime] | None = None) -> None:
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(UTC))

    def _events(self, principals: frozenset[str]) -> tuple[EvidenceRecord, ...]:
        return tuple(
            item.evidence
            for item in self.runtime.evidence_store.ledger(CONNECTOR)
            if evidence_allowed(item.evidence, principals)
        )

    @staticmethod
    def _content(event: EvidenceRecord) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(event.model_dump_json())["payload"]["content"])

    def _conversation(self, session_id: str, principals: frozenset[str]) -> list[dict[str, Any]]:
        # Source questions govern the shared project, not just the chat that found
        # them. Only questions bound to successful refresh receipts cross sessions;
        # ordinary conversational questions retain their existing session boundary.
        receipts = set()
        for entry in self.runtime.evidence_store.ledger("conversation:source-refresh"):
            record = entry.evidence
            if record.author != "agent:source-refresh" or not evidence_allowed(record, principals):
                continue
            content = self._content(record)
            if content.get("kind") == "check" and content["value"].get("success") is True:
                receipts.add(record.id)
        events = [{**self._content(e), "evidence_ref": e.id} for e in self._events(principals)]
        shared_questions = {
            e["evidence_ref"]
            for e in events
            if e["question"] and receipts.intersection(e["evidence_refs"])
        }
        result = []
        for content in events:
            if content["session_id"] == session_id:
                result.append(content)
                continue
            answers = [a for a in content["answers"] if a["question_ref"] in shared_questions]
            if content["evidence_ref"] in shared_questions:
                result.append({**content, "answers": answers})
            elif answers:
                result.append({**content, "answers": answers, "question": None, "conflict": None})
        return result

    def context(self, *, task: str, session_id: str) -> dict[str, Any]:
        """Read relevant intent and prior chat answers without starting a service UI."""
        config_file = self.runtime.workspace_directory.file("config.yaml")
        checkpoints = self.runtime.workspace_directory.file("cache/checkpoints.yaml")
        try:
            with self.runtime.transactions.read_transaction(
                {"background_config": config_file, "checkpoints": checkpoints}
            ) as transaction:
                self._validate_state(transaction)
                return self._context(task=task, session_id=session_id)
        finally:
            config_file.close()
            checkpoints.close()

    @staticmethod
    def _validate_state(transaction: LocalTransaction) -> None:
        canonical = {
            name: transaction.read_optional(name)
            for name in (
                "graph",
                "history",
                "cases",
                "evidence",
                "receipts",
                "checkpoints",
            )
        }
        canonical["config"] = transaction.read("background_config")
        if not validate_canonical_snapshot(canonical).valid:
            raise ValueError("canonical intent state is invalid")

    def _context(self, *, task: str, session_id: str) -> dict[str, Any]:
        from intent_engineering.cli.intent_workflow import _principals, _snapshot_config

        if not task or len(task) > 16384 or not session_id or len(session_id) > 512:
            raise ValueError("invalid background context")
        config, _ = _snapshot_config(self.runtime)
        principals = _principals(self.runtime, config)
        graph = self.runtime.graph_store.load()
        semantic_ids = {n.id for n in graph.nodes if n.type in SEMANTIC_TYPES}
        graph = Graph.model_validate(
            {
                **graph.model_dump(),
                "nodes": tuple(n for n in graph.nodes if n.id in semantic_ids),
                "edges": tuple(
                    e
                    for e in graph.edges
                    if e.from_id in semantic_ids
                    and e.to_id in semantic_ids
                    and e.relation.value in SEMANTIC_RELATIONS
                ),
            }
        )
        evidence = self.runtime.evidence()
        cases = tuple(c for c in self.runtime.cases() if is_intent_requirement_case(c.case_type))
        pack = ContextProvider(graph, cases, config, evidence).for_task(task, actor=principals)
        conversation = self._conversation(session_id, principals)
        answers = [dict(a) for event in conversation for a in event["answers"]]
        answered = {a["question_ref"] for a in answers}
        resolved_questions = {
            a["question_ref"]
            for a in answers
            if a.get("resolution") in {"retain_existing", "revise_intent"}
        }
        disagreements = [
            {
                "question_ref": e["evidence_ref"],
                "question": e["question"],
                "conflict": e["conflict"],
                "relevant_node_ids": e["relevant_node_ids"],
            }
            for e in conversation
            if e["conflict"] and e["evidence_ref"] not in resolved_questions
        ]
        questions = [
            {
                "question_ref": e["evidence_ref"],
                "question": e["question"],
                "understanding": e["understanding"],
                "conflict": e["conflict"],
                "relevant_node_ids": e["relevant_node_ids"],
            }
            for e in conversation
            if e["question"] and e["evidence_ref"] not in answered
        ]
        return {
            "graph_version": graph.version,
            "context": pack.model_dump(mode="json"),
            "questions": questions[-16:],
            "answers": answers[-64:],
            "disagreements": disagreements[-16:],
            "pending_proposal_ids": [
                p.id
                for p in self.runtime.intent_proposals.list()
                if self.runtime.intent_proposals.decision_for(p.id) is None
                and refs_allowed(p.evidence_refs, evidence, principals)
            ][:64],
            "scope": "intent_requirements",
            "human_approval": False,
        }

    def submit(self, submission: BackgroundSubmission) -> dict[str, Any]:
        """Apply one clear interpretation or persist one consequential chat question."""
        from intent_engineering.cli.intent_workflow import _principals, _snapshot_config

        submission = BackgroundSubmission.model_validate_json(submission.model_dump_json())
        config_file = self.runtime.workspace_directory.file("config.yaml")
        checkpoints = self.runtime.workspace_directory.file("cache/checkpoints.yaml")
        try:
            with self.runtime.transactions.transaction(
                rollback_base_exceptions=True,
                extras={"background_config": config_file, "checkpoints": checkpoints},
            ) as transaction:
                config, _ = _snapshot_config(self.runtime)
                principals = _principals(self.runtime, config)
                self._validate_state(transaction)
                return self._submit(submission, principals, config.local_actor)
        finally:
            config_file.close()
            checkpoints.close()

    def _submit(
        self, draft: BackgroundSubmission, principals: frozenset[str], local_actor: str
    ) -> dict[str, Any]:
        locator = "background:" + _digest([draft.session_id, draft.turn_id])
        material = draft.model_dump(mode="json")
        for event in self._events(principals):
            if event.external_object_id == locator:
                if self._content(event)["submission"] != material:
                    raise ValueError("background turn changed")
                return self._result(event)
        graph = self.runtime.graph_store.load()
        if draft.graph_version != graph.version:
            raise ValueError("stale background graph version")
        evidence = self.runtime.evidence()
        nodes = {n.id: n for n in graph.nodes}
        for ref in draft.relevant_node_ids:
            if (
                ref not in nodes
                or nodes[ref].type not in SEMANTIC_TYPES
                or not refs_allowed(nodes[ref].evidence_refs, evidence, principals)
            ):
                raise ValueError("unavailable relevant intent")
        cited = tuple(
            dict.fromkeys(
                (*draft.evidence_refs, *(r for n in draft.nodes for r in n.evidence_refs))
            )
        )
        if not refs_allowed(cited, evidence, principals):
            raise ValueError("unavailable background evidence")
        conversation = self._conversation(draft.session_id, principals)
        questions = {e["evidence_ref"]: e for e in conversation if e["question"]}
        answered = {
            a["question_ref"]
            for e in conversation
            for a in e["answers"]
            if not questions[a["question_ref"]]["conflict"]
            or a.get("resolution") in {"retain_existing", "revise_intent"}
        }
        for answer in draft.answers:
            if (
                answer.question_ref not in questions
                or answer.question_ref in answered
                or answer.quote not in draft.user_statement
            ):
                raise ValueError("answer must cite an unanswered question and current user words")
        if draft.question and any(
            e["question"] == draft.question for e in conversation if e["evidence_ref"] in answered
        ):
            raise ValueError("reuse the recorded answer instead of repeating its question")
        existing = [nodes[ref].model_dump(mode="json") for ref in draft.relevant_node_ids]
        blocking = tuple(
            c
            for c in self.runtime.cases()
            if is_nonterminal_case_status(c.status) and is_intent_requirement_case(c.case_type)
        )
        if draft.classification == "aligned" and any(
            set(c.affected_refs) & set(draft.relevant_node_ids)
            or c.subject_ref in draft.relevant_node_ids
            for c in blocking
        ):
            raise ValueError("relevant unresolved disagreement is not aligned")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("background clock must be timezone-aware")
        now = now.astimezone(UTC)
        changeset_id = "changeset:background:" + _digest(material)
        resolving = []
        for answer in draft.answers:
            if answer.resolution not in {"retain_existing", "revise_intent"}:
                continue
            if draft.classification != "clear" or not questions[answer.question_ref]["conflict"]:
                raise ValueError("resolution requires a clear answer to a conflict question")
            matches = [
                c
                for c in blocking
                if c.detector_id == "intent_workflow.background.v1"
                and answer.question_ref in c.all_evidence_refs
            ]
            if len(matches) != 1:
                raise ValueError("resolution must reference this conversation's open conflict")
            case = matches[0]
            changed = {n.id for n in draft.nodes} & set(case.affected_refs)
            if (answer.resolution == "retain_existing" and changed) or (
                answer.resolution == "revise_intent" and not changed
            ):
                raise ValueError("conflict resolution must match its graph effect")
            # Do not let one chat answer erase another contributor's existing position.
            if any(nodes[r].created_by != ACTOR for r in case.affected_refs if r in nodes):
                raise ValueError("preserve external positions as an unresolved disagreement")
            resolving.append((case, answer.resolution))
        resolving_ids = {c.id for c, _ in resolving}
        semantic = bool(
            draft.nodes or draft.edges or draft.classification == "conflicting" or resolving
        )
        content = {
            **material,
            "submission": material,
            "attribution": "agent_reported_user_statement",
            "human_approval": False,
            "existing_intent": existing,
            "result_graph_version": graph.version + int(semantic),
        }
        event = ConversationCapture(
            self.runtime.evidence_store, connector_id=CONNECTOR
        ).record_turn(
            conversation_ref=locator,
            role="agent",
            author=ACTOR,
            content=cast(JsonValue, content),
            captured_at=now,
            acl=(local_actor,),
        )
        added: list[Node] = []
        updated: list[NodeUpdate] = []
        for assertion in draft.nodes:
            if assertion.type not in SEMANTIC_TYPES:
                raise ValueError("background maintenance covers intent and requirements only")
            supporting = "\n".join(
                str(record.payload.get("content", ""))
                for record in evidence
                if record.id in assertion.evidence_refs
                and record.author is not None
                and not record.author.startswith("agent:")
                and isinstance(record.payload.get("content"), str)
            )
            if assertion.source_mode is SourceMode.EXPLICIT and (
                assertion.label not in draft.user_statement and assertion.label not in supporting
            ):
                raise ValueError("explicit assertion must quote its source")
            if assertion.source_mode is SourceMode.INFERRED and assertion.confidence > 0.85:
                raise ValueError("inferred intent must retain uncertainty")
            prior = nodes.get(assertion.id)
            if prior:
                if prior.created_by != ACTOR:
                    raise ValueError("preserve other authors' positions; add a separate assertion")
                if not refs_allowed(prior.evidence_refs, evidence, principals):
                    raise ValueError("unavailable prior intent")
                if prior.type != assertion.type or prior.status != "active":
                    raise ValueError("stable identity cannot change type or reactivate")
                if any(
                    (prior.id in c.affected_refs or prior.id == c.subject_ref)
                    and c.id not in resolving_ids
                    for c in blocking
                ):
                    raise ValueError("preserve disputed positions; add a separate cited position")
                if (
                    not assertion.revision_quote
                    or (
                        assertion.revision_quote not in draft.user_statement
                        and not self._same_source_revision(prior, assertion, evidence)
                    )
                    or (
                        prior.source_mode is SourceMode.EXPLICIT
                        and assertion.source_mode is not SourceMode.EXPLICIT
                    )
                ):
                    raise ValueError(
                        "existing meaning requires an explicit conversational or same-source revision"
                    )
            refs = tuple(
                dict.fromkeys(
                    (*(prior.evidence_refs if prior else ()), *assertion.evidence_refs, event.id)
                )
            )
            node = Node(
                id=assertion.id,
                type=assertion.type,
                label=assertion.label,
                status="active",
                created_by=prior.created_by if prior else ACTOR,
                created_at=prior.created_at if prior else now,
                last_modified_by=ACTOR,
                last_modified_at=now,
                source_mode=assertion.source_mode,
                intent_fidelity_confidence=assertion.confidence,
                confidence_basis=assertion.basis,
                last_reassessed_at=now,
                evidence_refs=refs,
                implementation_status=prior.implementation_status if prior else None,
            )
            if prior:
                updated.append(NodeUpdate(node_id=node.id, replacement=node))
            else:
                added.append(node)
        all_nodes = {**nodes, **{n.id: n for n in added}}
        edges = []
        for link in draft.edges:
            if link.relation.value not in SEMANTIC_RELATIONS:
                raise ValueError("background relationship outside intent scope")
            for ref in (link.from_id, link.to_id):
                if ref not in all_nodes or all_nodes[ref].type not in SEMANTIC_TYPES:
                    raise ValueError("invalid background relationship endpoint")
                if ref in nodes and not refs_allowed(
                    nodes[ref].evidence_refs, evidence, principals
                ):
                    raise ValueError("unavailable relationship endpoint")
            edges.append(
                Edge(
                    **link.model_dump(),
                    status="active",
                    created_by=ACTOR,
                    created_at=now,
                    last_modified_by=ACTOR,
                    last_modified_at=now,
                )
            )
        cases = (
            self._conflict(draft, graph, event, now, evidence)
            if draft.classification == "conflicting"
            else ()
        )
        resolved = []
        for case, resolution in resolving:
            history = list(case.history)
            current = case.status
            stages = (
                ReconciliationStatus.OPEN,
                ReconciliationStatus.PROPOSED,
                ReconciliationStatus.NEEDS_HUMAN,
                ReconciliationStatus.RESOLVED,
            )
            for next_status in stages[stages.index(current) + 1 :]:
                history.append(
                    ClassificationEvent(actor=ACTOR, at=now, prior=current, new=next_status)
                )
                current = next_status
                if current is not ReconciliationStatus.RESOLVED:
                    self.runtime.case_store.put(
                        ReconciliationCase.model_validate(
                            {
                                **case.model_dump(),
                                "status": current,
                                "history": tuple(history),
                            }
                        )
                    )
            # The legacy lifecycle names remain for compatibility; every event is agent-attributed,
            # and the resolution cites a reported chat decision, never a human approval record.
            resolved.append(
                ReconciliationCase.model_validate(
                    {
                        **case.model_dump(),
                        "status": current,
                        "history": tuple(history),
                        "resolution": ResolutionAction.UPDATE_INTENT
                        if resolution == "revise_intent"
                        else ResolutionAction.MERGE_INTERPRETATIONS,
                        "resolved_by_changeset": changeset_id,
                    }
                )
            )
        if semantic:
            changeset = ChangeSet(
                id=changeset_id,
                actor=ACTOR,
                timestamp=now,
                baseline_graph_version=graph.version,
                evidence_refs=tuple(
                    dict.fromkeys(
                        (
                            event.id,
                            *cited,
                            *(
                                r
                                for n in (*added, *(u.replacement for u in updated))
                                for r in n.evidence_refs
                            ),
                            *(r for c in (*cases, *resolved) for r in c.all_evidence_refs),
                        )
                    )
                ),
                nodes_added=tuple(added),
                nodes_updated=tuple(updated),
                nodes_superseded=(),
                edges_added=tuple(edges),
                edges_updated=(),
                edges_superseded=(),
                confidence_changes=(),
                implementation_status_changes=(),
                reconciliation_cases_created=tuple(c.id for c in cases),
                reconciliation_cases_resolved=tuple(c.id for c in resolved),
                validation_status="validated",
            )
            LocalChangeSetExecutor(
                self.runtime.graph_store, self.runtime.case_store, self.runtime.transactions
            ).apply(
                changeset,
                created_cases=cases,
                resolved_cases=resolved,
            )
        return self._result(event)

    def _same_source_revision(
        self, prior: Node, assertion: BackgroundAssertion, evidence: tuple[EvidenceRecord, ...]
    ) -> bool:
        """Allow a quoted refresh only along one unambiguous source and author lineage."""
        original = [
            r
            for r in evidence
            if r.id in prior.evidence_refs and r.connector_type != "conversation"
        ]
        identities = {(r.connector_type, r.external_object_id, r.author) for r in original}
        if len(identities) != 1 or not assertion.revision_quote:
            return False
        connector, object_id, author = next(iter(identities))
        if not connector.startswith("background:") or not author or author.startswith("agent:"):
            return False
        versions = [
            item.evidence
            for item in self.runtime.evidence_store.ledger(connector)
            if item.evidence.external_object_id == object_id
        ]
        if not versions:
            return False
        latest = versions[-1]
        content = latest.payload.get("content")
        # Preserve prior chat overrides, multi-source positions and independently authored edits.
        return (
            latest.id not in prior.evidence_refs
            and latest.id in assertion.evidence_refs
            and latest.author == author
            and latest.observed_at >= max(r.observed_at for r in original)
            and isinstance(content, str)
            and assertion.revision_quote in content
            and assertion.label in content
            and any(
                isinstance(r.payload.get("content"), str)
                and prior.label in str(r.payload["content"])
                for r in original
            )
        )

    @staticmethod
    def _conflict(
        draft: BackgroundSubmission,
        graph: Graph,
        event: EvidenceRecord,
        now: datetime,
        evidence: tuple[EvidenceRecord, ...],
    ) -> tuple[ReconciliationCase, ...]:
        selected = [n for n in graph.nodes if n.id in draft.relevant_node_ids]
        refs = tuple(dict.fromkeys(r for n in selected for r in n.evidence_refs))
        if not refs:
            raise ValueError("conflict must cite existing evidence")
        indexed = {record.id: record for record in evidence}
        fingerprint = _digest([event.id, draft.relevant_node_ids, draft.conflict])
        return (
            ReconciliationCase(
                id="case:background:" + fingerprint,
                fingerprint=fingerprint,
                subject_ref=draft.relevant_node_ids[0],
                affected_refs=draft.relevant_node_ids,
                case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
                detector_id="intent_workflow.background.v1",
                created_at=now,
                created_by=ACTOR,
                evidence_sides=(
                    EvidenceSide(
                        label="existing_intent",
                        claim="; ".join(n.label for n in selected),
                        evidence_refs=refs,
                        observed_at=max(indexed[ref].observed_at for ref in refs),
                        authors=tuple(
                            sorted(
                                {
                                    author
                                    for ref in refs
                                    if (author := indexed[ref].author) is not None
                                }
                            )
                        ),
                        confidence=0.8,
                    ),
                    EvidenceSide(
                        label="chat_request",
                        claim=draft.conflict or "",
                        evidence_refs=(event.id,),
                        observed_at=now,
                        authors=(ACTOR,),
                        confidence=0.8,
                        source_mode=SourceMode.INFERRED,
                    ),
                ),
            ),
        )

    def _result(self, event: EvidenceRecord) -> dict[str, Any]:
        content = self._content(event)
        return {
            "status": "question"
            if content["question"]
            else "maintained"
            if content["nodes"] or content["edges"]
            else "continue",
            "graph_version": content["result_graph_version"],
            "evidence_ref": event.id,
            "question": content["question"],
            "understanding": content["understanding"],
            "existing_intent": content["existing_intent"],
            "conflict": content["conflict"],
            "human_approval": False,
        }
