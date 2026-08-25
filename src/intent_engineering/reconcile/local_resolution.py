"""Atomic local application service for reconciliation resolution."""

from __future__ import annotations

import base64
import json
from contextlib import ExitStack
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from intent_engineering.core.models import (
    ChangeSet,
    ReconciliationCase,
    ReconciliationStatus,
    ResolutionAction,
)
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage._atomic import atomic_write_bytes, same_path_lock
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore


class ResolutionUnavailable(ValueError):
    """A deliberately non-enumerating resolution failure."""


class LocalResolutionService:
    """Resolve one human-reviewed case over a locked graph/history/case snapshot."""

    def __init__(
        self,
        graph_store: YamlGraphStore,
        evidence_store: JsonlEvidenceStore,
        case_store: JsonlCaseStore,
        actor: str,
    ) -> None:
        self._graph_store = graph_store
        self._evidence_store = evidence_store
        self._case_store = case_store
        self._actor = actor

    def resolve(
        self,
        case_id: str,
        action: ResolutionAction,
        *,
        approve: str | None = None,
        at: datetime | None = None,
    ) -> tuple[ReconciliationCase, ChangeSet | None, str | None]:
        """Prevalidate all state, then commit graph/history/case or restore exact bytes."""
        paths = self._paths()
        with ExitStack() as locks:
            for path in sorted(paths, key=str):
                locks.enter_context(same_path_lock(path))
            self._recover()
            snapshots = {path: path.read_bytes() if path.exists() else None for path in paths}
            try:
                case = self._case_store.get(case_id)
                graph = self._graph_store.load()
                records = tuple(
                    self._evidence_store.get(reference) for reference in case.all_evidence_refs
                )
                if not refs_allowed(case.all_evidence_refs, records, self._actor):
                    raise ResolutionUnavailable("resolution unavailable")
                timestamp = at or datetime.now(UTC)
                if case.status is ReconciliationStatus.OPEN and action in {
                    ResolutionAction.DEFER,
                    ResolutionAction.MARK_FALSE_POSITIVE,
                }:
                    target = (
                        ReconciliationStatus.DEFERRED
                        if action is ResolutionAction.DEFER
                        else ReconciliationStatus.FALSE_POSITIVE
                    )
                    updated = transition_case(case, target, self._actor, timestamp)
                    self._case_store.put(updated)
                    return updated, None, None
                if case.status is ReconciliationStatus.OPEN:
                    proposed = transition_case(
                        case, ReconciliationStatus.PROPOSED, self._actor, timestamp
                    )
                    reviewed = transition_case(
                        proposed, ReconciliationStatus.NEEDS_HUMAN, self._actor, timestamp
                    )
                    self._case_store.put(proposed)
                    self._case_store.put(reviewed)
                    changeset = self._changeset(
                        reviewed, graph.version, action, reviewed.created_at
                    )
                    return (
                        reviewed,
                        changeset,
                        self._approval_hash(reviewed, graph.version, action, changeset),
                    )
                if case.status is not ReconciliationStatus.NEEDS_HUMAN or approve is None:
                    raise ResolutionUnavailable("resolution unavailable")
                changeset = self._changeset(case, graph.version, action, timestamp)
                expected = self._approval_hash(
                    case,
                    graph.version,
                    action,
                    self._changeset(case, graph.version, action, case.created_at),
                )
                if approve != expected:
                    raise ResolutionUnavailable("resolution unavailable")
                resolved = transition_case(
                    case,
                    ReconciliationStatus.RESOLVED,
                    self._actor,
                    timestamp,
                    action,
                    changeset.id,
                )
                self._write_journal(snapshots)
                self._graph_store.apply(changeset)
                self._case_store.put(resolved)
                self._journal_path().unlink(missing_ok=True)
                return resolved, changeset, None
            except Exception as error:
                self._restore(snapshots)
                self._journal_path().unlink(missing_ok=True)
                if isinstance(error, ResolutionUnavailable):
                    raise
                raise ResolutionUnavailable("resolution unavailable") from error

    @staticmethod
    def _approval_hash(
        case: ReconciliationCase, graph_version: int, action: ResolutionAction, changeset: ChangeSet
    ) -> str:
        payload = {
            "action": action.value,
            "case": case.model_dump(mode="json"),
            "changeset": changeset.model_dump(mode="json"),
            "graph_version": graph_version,
        }
        return sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _paths(self) -> tuple[Path, ...]:
        return (
            self._graph_store.path,
            self._graph_store._history_store.path,
            self._case_store.path,
            self._evidence_store.path,
        )

    def _journal_path(self) -> Path:
        return self._graph_store._history_store.path.with_name(".resolution-journal.json")

    def _write_journal(self, snapshots: dict[Path, bytes | None]) -> None:
        payload = {
            "version": 1,
            "preimages": {
                str(path): None if content is None else base64.b64encode(content).decode("ascii")
                for path, content in snapshots.items()
            },
        }
        atomic_write_bytes(
            self._journal_path(),
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        )

    def _recover(self) -> None:
        journal = self._journal_path()
        if not journal.exists():
            return
        try:
            loaded = json.loads(journal.read_text(encoding="utf-8"))
            encoded = loaded["preimages"]
            snapshots = {
                path: None if encoded[str(path)] is None else base64.b64decode(encoded[str(path)])
                for path in self._paths()
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ResolutionUnavailable("resolution unavailable") from error
        self._restore(snapshots)
        journal.unlink()

    @staticmethod
    def _restore(snapshots: dict[Path, bytes | None]) -> None:
        for path, content in snapshots.items():
            if content is None:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                atomic_write_bytes(path, content)

    def _changeset(
        self, case: ReconciliationCase, version: int, action: ResolutionAction, timestamp: datetime
    ) -> ChangeSet:
        material = f"{case.id}\x00{version}\x00{action.value}\x00" + "\x00".join(
            case.all_evidence_refs
        )
        return ChangeSet(
            id=f"changeset:resolve:{sha256(material.encode('utf-8')).hexdigest()}",
            actor=self._actor,
            timestamp=timestamp,
            baseline_graph_version=version,
            evidence_refs=case.all_evidence_refs,
            nodes_added=(),
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(case.id,),
            validation_status="validated",
        )
