"""Resumable host-driven reconciliation; only canonical graph/evidence/cases are written.

Semantic judgments come from the host, not a filename heuristic or an implicit LLM.
Every completion is scoped to explicit file snapshots and captured source manifests.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from hashlib import sha256
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    EvidenceSide,
    JsonValue,
    Node,
    ReconciliationCase,
    ReconciliationCaseType,
    RelationType,
    SourceMode,
)
from intent_engineering.core.models.changeset import NodeUpdate
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.intent_workflow.background import BackgroundIntentService, _digest
from intent_engineering.intent_workflow.conversation import (
    ConversationCapture,
    validate_conversation_record,
)
from intent_engineering.intent_workflow.initial_reconciliation_models import (
    AssessmentFile,
    Comparison,
    InitialReconciliationRequest,
)
from intent_engineering.intent_workflow.source_refresh import SourceRefreshService
from intent_engineering.storage.executor import LocalChangeSetExecutor

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime

CONNECTOR = "conversation:initial-reconciliation"
ACTOR = "agent:initial-reconciliation"
INTENT_TYPES = {"NEED", "PRODUCT_INTENT", "DESIRED_OUTCOME", "CONSTRAINT", "PRINCIPLE"}
REQUIREMENT_TYPES = {"REQUIREMENT", "CAPABILITY", "ACCEPTANCE_CRITERION"}


class InitialReconciliationService:
    def __init__(self, runtime: Runtime, *, clock: Callable[[], datetime] | None = None) -> None:
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(UTC))
        self.refresh = SourceRefreshService(runtime, clock=self.clock)

    def _events(self, actor: str) -> list[dict[str, Any]]:
        result = []
        for entry in self.runtime.evidence_store.ledger(CONNECTOR):
            record = entry.evidence
            if record.acl != (actor,):
                continue
            validate_conversation_record(
                record,
                conversation_ref=record.external_object_id,
                role="agent",
                author=ACTOR,
                acl=(actor,),
            )
            result.append(BackgroundIntentService._content(record))
        return result

    def _record(
        self, state: dict[str, Any], draft: InitialReconciliationRequest, actor: str, now: datetime
    ) -> None:
        sequence = len(self.runtime.evidence_store.ledger(CONNECTOR)) + 1
        ConversationCapture(self.runtime.evidence_store, connector_id=CONNECTOR).record_turn(
            conversation_ref="initial-reconciliation:" + state["run_id"],
            role="agent",
            author=ACTOR,
            captured_at=now,
            acl=(actor,),
            content=cast(
                JsonValue,
                {
                    "state": state,
                    "sequence": sequence,
                    "request_key": _digest([draft.session_id, draft.turn_id, draft.action]),
                    "request_digest": _digest(draft.model_dump(mode="json")),
                    "attribution": "host_reported",
                    "human_approval": False,
                },
            ),
        )

    def _sources(self, actor: str) -> list[dict[str, Any]]:
        return [
            s.model_dump(mode="json")
            for _, s in sorted(self.refresh._sources(self.refresh._events(actor)).items())
            if s.selection.enabled
        ]

    def _file(self, item: AssessmentFile) -> bytes:
        path = PurePosixPath(item.path)
        if (
            path.is_absolute()
            or str(path) != item.path
            or any(p in {"..", ".intent", ".git"} for p in path.parts)
        ):
            raise ValueError("unsafe assessment path")
        if any(
            path.match(p) or fnmatchcase(item.path, p)
            for p in self.runtime.config.source_exclusions
        ):
            raise ValueError("excluded assessment path")
        return self.runtime.project_directory.read_relative(
            item.path, nonblocking=True, max_bytes=1048576
        ).content

    def _fresh(self, state: dict[str, Any], actor: str, principals: frozenset[str]) -> bool:
        if self.runtime.graph_store.load().version != state["graph_version"]:
            return False
        if self._sources(actor) != state["sources"]:
            return False
        if not refs_allowed(state["evidence_refs"], self.runtime.evidence(), principals):
            return False
        try:
            return all(
                sha256(self._file(AssessmentFile(path=f["path"], role=f["role"]))).hexdigest()
                == f["digest"]
                for f in state["files"]
            )
        except (OSError, ValueError):
            return False

    def _start(
        self,
        draft: InitialReconciliationRequest,
        actor: str,
        principals: frozenset[str],
        now: datetime,
    ) -> dict[str, Any]:
        graph = self.runtime.graph_store.load()
        evidence = self.runtime.evidence()
        nodes = [
            n
            for n in graph.nodes
            if n.status == "active"
            and n.type in INTENT_TYPES | REQUIREMENT_TYPES
            and refs_allowed(n.evidence_refs, evidence, principals)
        ]
        if not nodes:
            raise ValueError("capture source-backed intent and requirements before assessment")
        sources = self._sources(actor)
        refs = list(dict.fromkeys(r for n in nodes for r in n.evidence_refs))
        source_refs = [r for s in sources for r in s["evidence_refs"]]
        if not refs_allowed(source_refs, evidence, principals):
            raise ValueError("unavailable selected source evidence")
        refs.extend(source_refs)
        files = []
        for item in draft.files:
            raw = self._file(item)
            digest = sha256(raw).hexdigest()
            content = raw.decode("utf-8")
            if "\0" in content:
                raise ValueError("binary assessment artifact")
            record = normalize_raw_source(
                RawSourceObject(
                    connector_type="initial:local",
                    external_object_id=item.path,
                    external_version="sha256:" + digest,
                    source_locator=item.path,
                    author=None,
                    observed_at=now,
                    content_hash="sha256:" + digest,
                    payload={"content": content, "capture_method": "local_verified_read"},
                    acl=(actor,),
                )
            )
            previous = next((e for e in evidence if e.id == record.id), None)
            if previous is not None:
                record = previous
            self.runtime.evidence_store.associate("initial:local", record)
            files.append(
                {
                    **item.model_dump(),
                    "digest": digest,
                    "evidence_ref": record.id,
                    "node_id": "code:file:" + _digest(item.path) if item.role == "code" else None,
                }
            )
            refs.append(record.id)
        expected: list[dict[str, str]] = []
        for node in nodes:
            axes = (
                ("intent_requirements", "intent_code")
                if node.type in INTENT_TYPES
                else ("requirements_intent", "requirements_code")
            )
            expected.extend({"axis": axis, "subject_ref": node.id} for axis in axes)
        expected.extend(
            {"axis": "code_requirements", "subject_ref": f["node_id"]}
            for f in files
            if f["role"] == "code"
        )
        state: dict[str, Any] = {
            "scope": draft.scope,
            "files": files,
            "sources": sources,
            "graph_version": graph.version,
            "nodes": [n.model_dump(mode="json") for n in nodes],
            "evidence_refs": list(dict.fromkeys(refs)),
            "expected": expected,
            "comparisons": {},
            "status": "running",
            "case_ids": [],
            "limitations": (
                []
                if sources
                else [
                    "No selected external intent sources; use captured intent and label inference."
                ]
            )
            + (
                []
                if any(f["role"] == "requirements" for f in files)
                else ["No PRD file in the declared scope."]
            ),
        }
        state["run_id"] = "initial:" + _digest(state)
        self._record(state, draft, actor, now)
        return state

    @staticmethod
    def _key(axis: str, subject: str) -> str:
        return _digest([axis, subject])

    def _validate_comparison(
        self, c: Comparison, state: dict[str, Any], principals: frozenset[str]
    ) -> None:
        intents = {n["id"] for n in state["nodes"] if n["type"] in INTENT_TYPES}
        requirements = {n["id"] for n in state["nodes"] if n["type"] in REQUIREMENT_TYPES}
        codes = {f["node_id"] for f in state["files"] if f["role"] == "code"}
        allowed = {
            "intent_requirements": (intents, requirements),
            "requirements_intent": (requirements, intents),
            "requirements_code": (requirements, codes),
            "code_requirements": (codes, requirements),
            "intent_code": (intents, codes),
            "source_conflict": (intents | requirements, intents | requirements),
        }
        left, right = allowed[c.axis]
        if c.subject_ref not in left or not set(c.related_refs) <= right:
            raise ValueError("invalid comparison endpoints")
        if not set(c.evidence_refs) <= set(state["evidence_refs"]) or not refs_allowed(
            c.evidence_refs, self.runtime.evidence(), principals
        ):
            raise ValueError("comparison needs accessible snapshot evidence")
        references = {n["id"]: set(n["evidence_refs"]) for n in state["nodes"]}
        references.update(
            {f["node_id"]: {f["evidence_ref"]} for f in state["files"] if f["role"] == "code"}
        )
        if any(
            not references[r].intersection(c.evidence_refs)
            for r in (c.subject_ref, *c.related_refs)
        ):
            raise ValueError("cite evidence for each compared endpoint")
        authoritative = {
            r
            for s in state["sources"]
            if s["selection"]["authoritative"]
            for r in s["evidence_refs"]
        }
        if authoritative and not authoritative.intersection(c.evidence_refs):
            raise ValueError("comparison must consider the designated source intent")
        if c.axis == "source_conflict" and len(authoritative.intersection(c.evidence_refs)) < 2:
            raise ValueError("source conflict requires both source positions")

    def _result(self, state: dict[str, Any], *, status: str | None = None) -> dict[str, Any]:
        rows = list(state["comparisons"].values())
        missing = [
            s
            for s in state["expected"]
            if self._key(s["axis"], s["subject_ref"]) not in state["comparisons"]
        ]
        actual = status or state["status"]
        ready = actual in {"ready", "ready_with_findings"}
        return {
            "run_id": state["run_id"],
            "status": actual,
            "scope": state["scope"],
            "graph_version": state["graph_version"],
            "notify_user": ready,
            "completed_comparisons": len(rows),
            "missing_comparisons": missing,
            "comparisons": rows,
            "evidence_refs": state["evidence_refs"],
            "code_refs": [f for f in state["files"] if f["role"] == "code"],
            "sources": state["sources"],
            "limitations": state["limitations"],
            "findings": [r for r in rows if r["status"] != "aligned"] if ready else [],
            "case_ids": state["case_ids"] if ready else [],
            "test_code_traceability": "deferred",
            "source_priority": "designated_intent",
            "semantic_assurance": "host_reported_with_validated_evidence",
            "human_approval": False,
        }

    def run(self, draft: InitialReconciliationRequest) -> dict[str, Any]:
        draft = InitialReconciliationRequest.model_validate_json(draft.model_dump_json())
        with self.refresh._transaction() as (actor, principals, now):
            events = self._events(actor)
            states = {e["state"]["run_id"]: e["state"] for e in events}
            key = _digest([draft.session_id, draft.turn_id, draft.action])
            prior = next((e for e in events if e["request_key"] == key), None)
            if prior is not None and prior["request_digest"] != _digest(
                draft.model_dump(mode="json")
            ):
                raise ValueError("assessment turn changed; use a new turn for new input")
            state = (
                states.get(draft.run_id) if draft.run_id else next(reversed(states.values()), None)
            )
            if prior is not None:
                state = states[prior["state"]["run_id"]]
                return self._result(
                    state, status=None if self._fresh(state, actor, principals) else "stale"
                )
            if draft.action == "start":
                if (
                    state
                    and state["scope"] == draft.scope
                    and [{"path": f["path"], "role": f["role"]} for f in state["files"]]
                    == [f.model_dump() for f in draft.files]
                    and self._fresh(state, actor, principals)
                ):
                    return self._result(state)
                return self._result(self._start(draft, actor, principals, now))
            if state is None:
                if draft.action == "status" and draft.run_id is None:
                    return {"status": "not_started", "notify_user": False, "findings": []}
                raise ValueError("unknown assessment")
            if not self._fresh(state, actor, principals):
                return self._result(state, status="stale")
            if draft.action == "status" or state["status"] in {"ready", "ready_with_findings"}:
                return self._result(state)
            if draft.action == "checkpoint":
                for comparison in draft.comparisons:
                    self._validate_comparison(comparison, state, principals)
                    state["comparisons"][self._key(comparison.axis, comparison.subject_ref)] = (
                        comparison.model_dump(mode="json")
                    )
                self._record(state, draft, actor, now)
            elif draft.action == "finish":
                if (
                    self._result(state)["missing_comparisons"]
                    or any(c["status"] == "unknown" for c in state["comparisons"].values())
                    or any(
                        s["dirty"] or not s["initialized"] or not s["reconciled"]
                        for s in state["sources"]
                    )
                ):
                    return self._result(state, status="incomplete")
                self._publish(state, actor, now)
                if not self._fresh(state, actor, principals):
                    raise ValueError("assessment inputs changed during publication")
                self._record(state, draft, actor, now)
            return self._result(state)

    def _publish(self, state: dict[str, Any], actor: str, now: datetime) -> None:
        graph = self.runtime.graph_store.load()
        inverse = {
            "intent_requirements": "requirements_intent",
            "requirements_intent": "intent_requirements",
            "requirements_code": "code_requirements",
            "code_requirements": "requirements_code",
        }
        for review in state["comparisons"].values():
            if review["axis"] not in inverse or review["status"] not in {"aligned", "partial"}:
                continue
            for target in review["related_refs"]:
                reverse = state["comparisons"].get(self._key(inverse[review["axis"]], target))
                if reverse and reverse["status"] == "missing":
                    raise ValueError("contradictory coverage in reverse comparison")
        stamp: dict[str, Any] = {
            "status": "active",
            "created_by": ACTOR,
            "created_at": now,
            "last_modified_by": ACTOR,
            "last_modified_at": now,
        }
        receipt = ConversationCapture(
            self.runtime.evidence_store, connector_id=CONNECTOR
        ).record_turn(
            conversation_ref="initial-review:" + state["run_id"],
            role="agent",
            author=ACTOR,
            captured_at=now,
            acl=(actor,),
            content=cast(
                JsonValue,
                {
                    "state": state,
                    "attribution": "host_reported",
                    "human_approval": False,
                    "request_key": "review:" + state["run_id"],
                    "request_digest": _digest(state),
                },
            ),
        )
        existing = {n.id: n for n in graph.nodes}
        added = []
        updated = []
        for f in state["files"]:
            if f["role"] != "code":
                continue
            prior = existing.get(f["node_id"])
            if prior and (prior.type != "FILE" or prior.created_by != ACTOR):
                raise ValueError("preserve existing code node ownership")
            node = Node(
                id=f["node_id"],
                type="FILE",
                label=f["path"],
                **stamp,
                source_mode=SourceMode.DERIVED,
                evidence_refs=(f["evidence_ref"], receipt.id),
            )
            if prior:
                node = node.model_copy(update={"created_at": prior.created_at})
                updated.append(NodeUpdate(node_id=node.id, replacement=node))
            else:
                added.append(node)
        edges = []
        pairs = {
            (e.from_id, e.relation.value, e.to_id) for e in graph.edges if e.status == "active"
        }
        for c in state["comparisons"].values():
            if c["status"] not in {"aligned", "partial"}:
                continue
            if c["axis"] not in {"intent_requirements", "requirements_code"}:
                continue
            relation = "SPECIFIED_BY" if c["axis"] == "intent_requirements" else "IMPLEMENTED_BY"
            for target in c["related_refs"]:
                pair = (c["subject_ref"], relation, target)
                if pair not in pairs:
                    edges.append(
                        Edge.model_validate(
                            {
                                "id": "trace:" + _digest(pair),
                                "from": pair[0],
                                "relation": RelationType(relation),
                                "to": target,
                                **stamp,
                            }
                        )
                    )
                    pairs.add(pair)
        cases = []
        evidence = {e.id: e for e in self.runtime.evidence()}
        for c in state["comparisons"].values():
            if c["status"] in {"aligned", "deferred"}:
                continue
            case_type = {
                "requirements_code": "CODE_LAG",
                "code_requirements": "UNDOCUMENTED_CODE",
                "source_conflict": "CONFLICTING_SOURCES",
            }.get(c["axis"], "AMBIGUOUS_DIVERGENCE")
            fingerprint = _digest([state["run_id"], c])
            cases.append(
                ReconciliationCase(
                    id="case:initial:" + fingerprint,
                    subject_ref=c["subject_ref"],
                    case_type=ReconciliationCaseType(case_type),
                    affected_refs=tuple(c["related_refs"]),
                    detector_id="initial_reconciliation." + c["axis"],
                    fingerprint=fingerprint,
                    created_at=now,
                    created_by=ACTOR,
                    impact=c["explanation"] + " Recommended change: " + c["recommendation"],
                    evidence_sides=(
                        EvidenceSide(
                            label=c["status"],
                            claim=c["explanation"] + " " + c["authority_basis"],
                            evidence_refs=tuple(dict.fromkeys([*c["evidence_refs"], receipt.id])),
                            observed_at=max(
                                evidence[r].observed_at for r in (*c["evidence_refs"], receipt.id)
                            ),
                            authors=tuple(
                                sorted(
                                    {
                                        author
                                        for r in (*c["evidence_refs"], receipt.id)
                                        if (author := evidence[r].author) is not None
                                    }
                                )
                            ),
                            confidence=c["confidence"],
                            source_mode=SourceMode.INFERRED,
                        ),
                    ),
                )
            )
        change = ChangeSet(
            id="changeset:initial:" + state["run_id"],
            actor=ACTOR,
            timestamp=now,
            baseline_graph_version=graph.version,
            evidence_refs=(*state["evidence_refs"], receipt.id),
            nodes_added=tuple(added),
            nodes_updated=tuple(updated),
            nodes_superseded=(),
            edges_added=tuple(edges),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=tuple(c.id for c in cases),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )
        result = LocalChangeSetExecutor(
            self.runtime.graph_store, self.runtime.case_store, self.runtime.transactions
        ).apply(change, created_cases=cases)
        state["graph_version"] = result.version
        state["status"] = (
            "ready_with_findings"
            if any(c["status"] != "aligned" for c in state["comparisons"].values())
            else "ready"
        )
        state["case_ids"] = [c.id for c in cases]
