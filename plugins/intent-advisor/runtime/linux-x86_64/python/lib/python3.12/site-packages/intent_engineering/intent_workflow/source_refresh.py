"""Task-aligned, host-reported source refresh with bounded shared read receipts.

State snapshots and read receipts live in the immutable evidence ledger, under the
existing transaction lock. No remote credentials, polling service or webhook listener
is created here. A host must actually read providers before submitting observations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from intent_engineering.core.models import EvidenceRecord, JsonValue, is_nonterminal_case_status
from intent_engineering.core.models.reconciliation import is_intent_requirement_case
from intent_engineering.core.policy.access import evidence_allowed, refs_allowed
from intent_engineering.intent_workflow.background import BackgroundIntentService, _digest
from intent_engineering.intent_workflow.background_models import BackgroundModel, Identity, Text
from intent_engineering.intent_workflow.conversation import (
    ConversationCapture,
    validate_conversation_record,
)

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime

CONNECTOR = "conversation:source-refresh"
ACTOR = "agent:source-refresh"
Provider = Literal["notion", "confluence", "slack", "google-drive", "github", "local"]
Refs = Annotated[tuple[Identity, ...], Field(max_length=512)]
MAX_CHECK_AGE = timedelta(hours=1)


class SourceSelection(BackgroundModel):
    """Exact bounded scope selected by the user, with reported selection provenance."""

    source_id: Identity
    provider: Provider
    scope: Text
    locator: Text
    user_statement: Annotated[str, Field(min_length=1, max_length=16384)]
    authoritative: bool
    collection: bool = False
    enabled: bool = True


class SourceSignal(BackgroundModel):
    source_id: Identity
    event_id: Identity
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("source time requires a timezone")
        return value.astimezone(UTC)


class SourceObservation(BackgroundModel):
    source_id: Identity
    generation: Annotated[int, Field(ge=1)]
    kind: Literal["metadata", "content", "unavailable"]
    checked_at: datetime
    complete: bool
    validator: Text | None = None
    evidence_refs: Refs = ()
    reason: Text | None = None

    @field_validator("checked_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return SourceSignal.aware_time(value)

    @model_validator(mode="after")
    def valid_shape(self) -> SourceObservation:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("duplicate source evidence")
        if self.kind != "content" and self.evidence_refs:
            raise ValueError("only content observations carry evidence")
        if self.kind == "unavailable" and self.complete:
            raise ValueError("unavailable source is not complete")
        return self


class RefreshRequest(BackgroundModel):
    task: Annotated[str, Field(min_length=1, max_length=16384)]
    session_id: Identity
    turn_id: Identity
    observations: Annotated[tuple[SourceObservation, ...], Field(max_length=32)] = ()
    reconciliation_ref: Identity | None = None
    force_refresh: bool = False

    @model_validator(mode="after")
    def unique_sources(self) -> RefreshRequest:
        ids = [item.source_id for item in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate source observations")
        return self


class _Check(BackgroundModel):
    observation: SourceObservation
    generation: int
    receipt_ref: str
    success: bool
    action: str
    selection: SourceSelection | None = None
    evidence_refs: tuple[str, ...] = ()
    cached: bool = False


class _Source(BackgroundModel):
    selection: SourceSelection
    generation: int = 1
    dirty: bool = True
    initialized: bool = False
    validator: str | None = None
    evidence_refs: tuple[str, ...] = ()
    previous_refs: tuple[str, ...] = ()
    reconciled: bool = False
    reconciled_graph_version: int | None = None
    latest_check: _Check | None = None


class _Task(BackgroundModel):
    task: str
    started_at: datetime
    graph_version: int
    checks: dict[str, _Check] = Field(default_factory=dict)
    reconciliation_ref: str | None = None
    require_fresh: bool = False


class SourceRefreshService:
    def __init__(self, runtime: Runtime, *, clock: Callable[[], datetime] | None = None) -> None:
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def _transaction(self) -> Iterator[tuple[str, frozenset[str], datetime]]:
        from intent_engineering.cli.intent_workflow import _principals, _snapshot_config

        config_file = self.runtime.workspace_directory.file("config.yaml")
        checkpoints = self.runtime.workspace_directory.file("cache/checkpoints.yaml")
        try:
            with self.runtime.transactions.transaction(
                rollback_base_exceptions=True,
                extras={"background_config": config_file, "checkpoints": checkpoints},
            ) as transaction:
                BackgroundIntentService._validate_state(transaction)
                config, _ = _snapshot_config(self.runtime)
                now = SourceSignal.aware_time(self.clock())
                yield config.local_actor, _principals(self.runtime, config), now
                BackgroundIntentService._validate_state(transaction)
        finally:
            config_file.close()
            checkpoints.close()

    def _events(self, actor: str) -> list[dict[str, Any]]:
        events = []
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
            events.append({**BackgroundIntentService._content(record), "ref": record.id})
        return events

    def _record(self, actor: str, now: datetime, kind: str, key: str, value: Any) -> str:
        # Distinct transitions may share a timestamp and return to identical values.
        # Give each append a new identity; callers handle exact retries before writing.
        sequence = len(self.runtime.evidence_store.ledger(CONNECTOR)) + 1
        return (
            ConversationCapture(self.runtime.evidence_store, connector_id=CONNECTOR)
            .record_turn(
                conversation_ref="source-refresh:" + _digest([actor, kind, key]),
                role="agent",
                author=ACTOR,
                captured_at=now,
                acl=(actor,),
                content=cast(
                    JsonValue,
                    {
                        "kind": kind,
                        "key": key,
                        "value": value,
                        "sequence": sequence,
                        "attribution": "host_reported",
                        "human_approval": False,
                    },
                ),
            )
            .id
        )

    def _sources(self, events: list[dict[str, Any]]) -> dict[str, _Source]:
        return {
            e["key"]: _Source.model_validate(e["value"]) for e in events if e["kind"] == "source"
        }

    def sources(self, selection: SourceSelection | None = None) -> dict[str, Any]:
        with self._transaction() as (actor, _, now):
            sources = self._sources(self._events(actor))
            if selection is not None:
                selection = SourceSelection.model_validate_json(selection.model_dump_json())
                prior = sources.get(selection.source_id)
                if prior is None and len(sources) >= 32:
                    raise ValueError("source selection limit reached")
                if prior is None or prior.selection != selection:
                    state = (
                        _Source(selection=selection)
                        if prior is None
                        else prior.model_copy(
                            update={
                                "selection": selection,
                                "generation": prior.generation + 1,
                                "dirty": True,
                                "reconciled": False,
                                "validator": None,
                                "latest_check": None,
                            }
                        )
                    )
                    self._record(
                        actor, now, "source", selection.source_id, state.model_dump(mode="json")
                    )
                    sources[selection.source_id] = state
            return {
                "sources": [s.model_dump(mode="json") for _, s in sorted(sources.items())],
                "subscriptions_configured": False,
                "capture_method": "host_reported",
            }

    def signal(self, signal: SourceSignal) -> dict[str, Any]:
        signal = SourceSignal.model_validate_json(signal.model_dump_json())
        with self._transaction() as (actor, _, now):
            if signal.observed_at > now + timedelta(seconds=60):
                raise ValueError("invalid signal time")
            events = self._events(actor)
            sources = self._sources(events)
            if signal.source_id not in sources:
                raise ValueError("unknown source")
            source = sources[signal.source_id]
            key = _digest([signal.source_id, signal.event_id])
            prior = next((e for e in events if e["kind"] == "signal" and e["key"] == key), None)
            material = signal.model_dump(mode="json")
            if prior is not None:
                if prior["value"] != material:
                    raise ValueError("source signal changed")
            else:
                source = source.model_copy(
                    update={
                        "generation": source.generation + 1,
                        "dirty": True,
                        "latest_check": None,
                    }
                )
                self._record(actor, now, "signal", key, material)
                self._record(actor, now, "source", signal.source_id, source.model_dump(mode="json"))
            return {
                "status": "invalidated",
                "source_id": signal.source_id,
                "generation": source.generation,
                "subscriptions_configured": False,
            }

    def _check(
        self,
        actor: str,
        principals: frozenset[str],
        now: datetime,
        task_key: str,
        task: _Task,
        source: _Source,
        observation: SourceObservation,
    ) -> tuple[_Source, _Check]:
        if not task.started_at <= observation.checked_at <= now + timedelta(seconds=60):
            raise ValueError("invalid source check time")
        valid = observation.complete and observation.kind != "unavailable"
        action = "retry" if observation.kind == "unavailable" else "fetch"
        evidence = self.runtime.evidence()
        if valid and observation.kind == "metadata":
            valid = (
                source.initialized
                and not source.dirty
                and observation.validator is not None
                and observation.validator == source.validator
            )
            if valid:
                action = "reuse"
        elif valid:
            records = {r.id: r for r in evidence}
            if not refs_allowed(observation.evidence_refs, evidence, principals):
                raise ValueError("unavailable source evidence")
            selected = [records[ref] for ref in observation.evidence_refs]
            if any(r.connector_type != f"background:{source.selection.provider}" for r in selected):
                raise ValueError("mismatched source evidence")
            if not source.selection.collection and (
                len(selected) > 1
                or any(r.source_locator != source.selection.locator for r in selected)
            ):
                raise ValueError("mismatched source evidence scope")
            if len({r.external_object_id for r in selected}) != len(selected):
                raise ValueError("duplicate object evidence in manifest")
            same = source.initialized and set(source.evidence_refs) == set(
                observation.evidence_refs
            )
            previous = (
                source.previous_refs
                if same
                else tuple(
                    sorted(
                        {
                            *source.previous_refs,
                            *source.evidence_refs,
                        }
                    )
                )
            )
            source = source.model_copy(
                update={
                    "generation": source.generation + int(not same),
                    "initialized": True,
                    "dirty": False,
                    "validator": observation.validator,
                    "evidence_refs": tuple(sorted(observation.evidence_refs)),
                    "previous_refs": previous,
                    "reconciled": source.reconciled and same,
                }
            )
            action = "reuse" if same else "captured"
        if not valid:
            # A failed or changed metadata check also invalidates concurrent task receipts.
            source = source.model_copy(
                update={"dirty": True, "generation": source.generation + int(not source.dirty)}
            )
        receipt = self._record(
            actor,
            now,
            "check",
            task_key + ":" + observation.source_id,
            {
                "task_key": task_key,
                "observation": observation.model_dump(mode="json"),
                "selection": source.selection.model_dump(mode="json"),
                "evidence_refs": list(source.evidence_refs),
                "previous_refs": list(source.previous_refs),
                "success": valid,
            },
        )
        check = _Check(
            observation=observation,
            generation=source.generation,
            receipt_ref=receipt,
            success=valid,
            action=action,
            selection=source.selection,
            evidence_refs=source.evidence_refs,
        )
        return source.model_copy(update={"latest_check": check if valid else None}), check

    @staticmethod
    def _valid_check(
        check: _Check | None,
        source: _Source,
        now: datetime,
        evidence: tuple[EvidenceRecord, ...],
        principals: frozenset[str],
        receipts: dict[str, dict[str, Any]],
    ) -> bool:
        if check is None:
            return False
        observation = check.observation
        receipt = receipts.get(check.receipt_ref)
        return (
            check.success
            and observation.complete
            and observation.kind != "unavailable"
            and observation.checked_at <= now + timedelta(seconds=60)
            and now < observation.checked_at + MAX_CHECK_AGE
            and source.initialized
            and not source.dirty
            and check.generation == source.generation
            and check.selection == source.selection
            and set(check.evidence_refs) == set(source.evidence_refs)
            and (observation.kind == "content" or observation.validator is not None)
            and observation.validator == source.validator
            and (
                observation.kind != "content"
                or set(observation.evidence_refs) == set(source.evidence_refs)
            )
            and refs_allowed(
                (*source.evidence_refs, *source.previous_refs, check.receipt_ref),
                evidence,
                principals,
            )
            and receipt is not None
            and receipt["success"]
            and receipt["observation"] == observation.model_dump(mode="json")
            and receipt["selection"] == source.selection.model_dump(mode="json")
            and set(receipt["evidence_refs"]) == set(source.evidence_refs)
        )

    def refresh(self, request: RefreshRequest) -> dict[str, Any]:
        request = RefreshRequest.model_validate_json(request.model_dump_json())
        with self._transaction() as (actor, principals, now):
            events = self._events(actor)
            sources = self._sources(events)
            active = {key: s for key, s in sources.items() if s.selection.enabled}
            key = _digest([request.session_id, request.turn_id])
            stored = next(
                (e["value"] for e in reversed(events) if e["kind"] == "task" and e["key"] == key),
                None,
            )
            graph = self.runtime.graph_store.load()
            task = (
                _Task.model_validate(stored)
                if stored is not None
                else _Task(
                    task=request.task,
                    started_at=now,
                    graph_version=graph.version,
                )
            )
            if task.task != request.task:
                raise ValueError("refresh task changed")
            force_refresh = request.force_refresh or task.require_fresh
            checks = dict(task.checks)
            for observation in request.observations:
                source = active.get(observation.source_id)
                if source is None:
                    raise ValueError("unknown or disabled source")
                if observation.generation != source.generation:
                    continue  # Return the current generation; never erase a racing signal.
                prior = checks.get(observation.source_id)
                if prior is not None and not prior.cached and prior.observation == observation:
                    continue
                changed, observed_check = self._check(
                    actor, principals, now, key, task, source, observation
                )
                checks[observation.source_id] = observed_check
                active[observation.source_id] = changed
            task = task.model_copy(update={"checks": checks})
            evidence = self.runtime.evidence()
            # Newly submitted checks were appended after the initial event snapshot.
            check_receipts = {
                e["ref"]: e["value"] for e in self._events(actor) if e["kind"] == "check"
            }
            work = []
            receipts: list[str] = []
            read_refs: set[str] = set()
            complete = bool(active)
            for source_id, source in sorted(active.items()):
                if not refs_allowed(
                    (*source.evidence_refs, *source.previous_refs), evidence, principals
                ):
                    # Losing permission is an invalidation, even if an alias is later restored.
                    source = source.model_copy(
                        update={
                            "dirty": True,
                            "generation": source.generation + int(not source.dirty),
                            "latest_check": None,
                        }
                    )
                    active[source_id] = source
                check = checks.get(source_id)
                success = self._valid_check(
                    check, source, now, evidence, principals, check_receipts
                )
                if force_refresh and check is not None and check.cached:
                    success = False
                if (
                    not success
                    and not force_refresh
                    and self._valid_check(
                        source.latest_check, source, now, evidence, principals, check_receipts
                    )
                ):
                    assert source.latest_check is not None
                    check = source.latest_check.model_copy(update={"cached": True})
                    checks[source_id] = check
                    success = True
                complete = complete and success
                action = (
                    ("reuse" if check.cached else check.action)
                    if success and check is not None
                    else "retry"
                    if check is not None and check.observation.kind == "unavailable"
                    else "fetch"
                    if source.dirty or not source.initialized
                    else "check_revision"
                )
                if success and check is not None:
                    receipts.append(check.receipt_ref)
                    read_refs.update((*source.evidence_refs, *source.previous_refs))
                work.append(
                    {
                        "source_id": source_id,
                        **source.selection.model_dump(mode="json"),
                        "generation": source.generation,
                        "action": action,
                        "validator": source.validator,
                        "evidence_refs": list(source.evidence_refs),
                        "previous_refs": list(source.previous_refs),
                        "cached": bool(success and check is not None and check.cached),
                        "checked_at": (
                            check.observation.checked_at.isoformat() if success and check else None
                        ),
                        "expires_at": (
                            (check.observation.checked_at + MAX_CHECK_AGE).isoformat()
                            if success and check
                            else None
                        ),
                    }
                )
            task = task.model_copy(
                update={"checks": checks, "require_fresh": force_refresh and not complete}
            )
            needs_review = (
                any(
                    not s.reconciled or s.reconciled_graph_version != graph.version
                    for s in active.values()
                )
                or task.graph_version != graph.version
            )
            context = BackgroundIntentService(self.runtime)._context(
                task=request.task,
                session_id=request.session_id,
            )
            # All visible intent cases are conservative blockers: a source-level change
            # may affect a requirement that task keyword matching would fail to retrieve.
            cases = [
                c.id
                for c in self.runtime.cases()
                if is_nonterminal_case_status(c.status)
                and is_intent_requirement_case(c.case_type)
                and refs_allowed(c.all_evidence_refs, evidence, principals)
            ]
            blocked = bool(cases or context["questions"] or context["disagreements"])
            if request.reconciliation_ref is not None:
                if not complete:
                    raise ValueError("interpretation requires complete source checks")
                review = next(
                    (
                        e
                        for e in BackgroundIntentService(self.runtime)._events(principals)
                        if e.id == request.reconciliation_ref
                    ),
                    None,
                )
                if review is None or not evidence_allowed(review, principals):
                    raise ValueError("unavailable interpretation")
                content = BackgroundIntentService._content(review)
                cited = {
                    *content["evidence_refs"],
                    *(r for n in content["nodes"] for r in n["evidence_refs"]),
                }
                if (
                    content["session_id"] != request.session_id
                    or content["classification"] not in {"clear", "aligned"}
                    or content["result_graph_version"] != graph.version
                    or review.observed_at < task.started_at
                    or not set(receipts) <= cited
                ):
                    raise ValueError("interpretation must cite current task receipts and graph")
                if not blocked:
                    needs_review = False
                    task = task.model_copy(
                        update={"graph_version": graph.version, "reconciliation_ref": review.id}
                    )
                    active = {
                        k: s.model_copy(
                            update={
                                "reconciled": True,
                                "previous_refs": (),
                                "reconciled_graph_version": graph.version,
                            }
                        )
                        for k, s in active.items()
                    }
            for source_id, source in active.items():
                if source != sources[source_id]:
                    self._record(actor, now, "source", source_id, source.model_dump(mode="json"))
            if active and task.model_dump(mode="json") != stored:
                self._record(actor, now, "task", key, task.model_dump(mode="json"))
            status = (
                "not_configured"
                if not active
                else "refresh_required"
                if not complete
                else "blocked"
                if blocked
                else "needs_interpretation"
                if needs_review
                else "ready"
            )
            return {
                "status": status,
                "task_key": key,
                "started_at": task.started_at.isoformat(),
                "graph_version": graph.version,
                "sources": work,
                "required_evidence_refs": receipts,
                "review_evidence_refs": sorted(read_refs),
                "blocking_case_ids": cases,
                "questions": context["questions"],
                "disagreements": context["disagreements"],
                "capture_method": "host_reported",
                "subscriptions_configured": False,
            }
