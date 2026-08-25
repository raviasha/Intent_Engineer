"""Crash-only recovery tests for the local resolution journal."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.core.models import ReconciliationStatus, ResolutionAction
from intent_engineering.reconcile import LocalResolutionService, ResolutionUnavailable
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from tests.contract.storage.test_evidence_store_contract import evidence_record
from tests.contract.storage.test_graph_store_contract import graph
from tests.unit.reconcile.test_case_lifecycle import reconciliation_case

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _service(tmp_path: Path) -> LocalResolutionService:
    graph_store = YamlGraphStore(tmp_path / "graph.yaml", history_path=tmp_path / "history.jsonl")
    graph_store.initialize(graph())
    evidence_store = JsonlEvidenceStore(tmp_path / "evidence.jsonl")
    evidence = evidence_record(id="ev-1")
    evidence_store.put(evidence)
    case_store = JsonlCaseStore(tmp_path / "cases.jsonl")
    opened = reconciliation_case(
        evidence_sides=(
            reconciliation_case()
            .evidence_sides[0]
            .model_copy(update={"evidence_refs": (evidence.id,)}),
        )
    )
    case_store.put(opened)
    case_store.put(transition_case(opened, ReconciliationStatus.PROPOSED, "tester", NOW))
    case_store.put(
        transition_case(case_store.get(opened.id), ReconciliationStatus.NEEDS_HUMAN, "tester", NOW)
    )
    return LocalResolutionService(graph_store, evidence_store, case_store, "tester")


@pytest.mark.parametrize("stage", ("graph", "history", "case"))
def test_system_exit_after_each_durable_resolution_stage_recovers_exact_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    service = _service(tmp_path)
    paths = service._paths()
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    case = service._case_store.get("case-1")
    graph_version = service._graph_store.load().version
    canonical = service._canonical_changeset(
        case, graph_version, ResolutionAction.UPDATE_IMPLEMENTATION
    )
    approval = service._approval_hash(
        case, graph_version, ResolutionAction.UPDATE_IMPLEMENTATION, canonical
    )
    if stage == "graph":
        monkeypatch.setattr(
            service._graph_store, "apply", lambda _: (_ for _ in ()).throw(SystemExit())
        )
    elif stage == "history":
        monkeypatch.setattr(
            service._graph_store._history_store,
            "append",
            lambda _: (_ for _ in ()).throw(SystemExit()),
        )
    else:
        monkeypatch.setattr(
            service._case_store, "put", lambda _: (_ for _ in ()).throw(SystemExit())
        )
    with pytest.raises(SystemExit):
        service.resolve("case-1", ResolutionAction.UPDATE_IMPLEMENTATION, approve=approval)
    assert service._journal_path().exists()
    fresh = LocalResolutionService(
        YamlGraphStore(tmp_path / "graph.yaml", history_path=tmp_path / "history.jsonl"),
        JsonlEvidenceStore(tmp_path / "evidence.jsonl"),
        JsonlCaseStore(tmp_path / "cases.jsonl"),
        "tester",
    )
    with pytest.raises(ResolutionUnavailable):
        fresh.resolve("case-1", ResolutionAction.UPDATE_IMPLEMENTATION)
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not fresh._journal_path().exists()
    with pytest.raises(ResolutionUnavailable):
        fresh.resolve("case-1", ResolutionAction.UPDATE_IMPLEMENTATION)
