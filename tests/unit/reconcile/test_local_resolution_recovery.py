"""Crash-only recovery tests for the local resolution journal."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import intent_engineering.storage.yaml.graph_store as yaml_graph_store
from intent_engineering.core.models import ReconciliationStatus, ResolutionAction
from intent_engineering.reconcile import LocalResolutionService
from intent_engineering.reconcile.service import transition_case
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from tests.contract.storage.test_evidence_store_contract import evidence_record
from tests.contract.storage.test_graph_store_contract import graph
from tests.unit.reconcile.test_case_lifecycle import reconciliation_case

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _service(tmp_path: Path, *, needs_human: bool = True) -> LocalResolutionService:
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
    if needs_human:
        case_store.put(transition_case(opened, ReconciliationStatus.PROPOSED, "tester", NOW))
        case_store.put(
            transition_case(
                case_store.get(opened.id), ReconciliationStatus.NEEDS_HUMAN, "tester", NOW
            )
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
        original_write = yaml_graph_store.atomic_write_bytes

        def interrupt_after_graph_write(path: Path, content: bytes) -> None:
            original_write(path, content)
            raise SystemExit()

        monkeypatch.setattr(yaml_graph_store, "atomic_write_bytes", interrupt_after_graph_write)
    elif stage == "history":
        original_append = service._graph_store._history_store.append

        def interrupt_after_history_write(changeset: object) -> object:
            original_append(changeset)  # type: ignore[arg-type]
            raise SystemExit()

        monkeypatch.setattr(
            service._graph_store._history_store,
            "append",
            interrupt_after_history_write,
        )
    else:
        original_put = service._case_store.put

        def interrupt_after_case_write(updated: object) -> bool:
            original_put(updated)  # type: ignore[arg-type]
            raise SystemExit()

        monkeypatch.setattr(service._case_store, "put", interrupt_after_case_write)
    with pytest.raises(SystemExit):
        service.resolve("case-1", ResolutionAction.UPDATE_IMPLEMENTATION, approve=approval)
    assert service._journal_path().exists()
    target = {
        "graph": service._graph_store.path,
        "history": service._graph_store._history_store.path,
        "case": service._case_store.path,
    }[stage]
    assert target.exists()
    assert target.read_bytes() != before[target]
    fresh = LocalResolutionService(
        YamlGraphStore(tmp_path / "graph.yaml", history_path=tmp_path / "history.jsonl"),
        JsonlEvidenceStore(tmp_path / "evidence.jsonl"),
        JsonlCaseStore(tmp_path / "cases.jsonl"),
        "tester",
    )
    fresh.recover()
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not fresh._journal_path().exists()
    fresh.recover()
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before


@pytest.mark.parametrize("append_number", (1, 2))
def test_system_exit_during_preview_case_append_recovers_exact_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, append_number: int
) -> None:
    service = _service(tmp_path, needs_human=False)
    paths = service._paths()
    before = {path: path.read_bytes() if path.exists() else None for path in paths}
    original_put = service._case_store.put
    calls = 0

    def interrupt(case: object) -> bool:
        nonlocal calls
        calls += 1
        result = original_put(case)  # type: ignore[arg-type]
        if calls == append_number:
            raise SystemExit()
        return result

    monkeypatch.setattr(service._case_store, "put", interrupt)
    with pytest.raises(SystemExit):
        service.resolve("case-1", ResolutionAction.UPDATE_IMPLEMENTATION)
    assert service._journal_path().exists()
    durable_preview = JsonlCaseStore(service._case_store.path).get("case-1")
    expected_status = (
        ReconciliationStatus.PROPOSED if append_number == 1 else ReconciliationStatus.NEEDS_HUMAN
    )
    assert durable_preview.status is expected_status
    fresh = LocalResolutionService(
        YamlGraphStore(tmp_path / "graph.yaml", history_path=tmp_path / "history.jsonl"),
        JsonlEvidenceStore(tmp_path / "evidence.jsonl"),
        JsonlCaseStore(tmp_path / "cases.jsonl"),
        "tester",
    )
    fresh.recover()
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not fresh._journal_path().exists()
    fresh.recover()
    assert {path: path.read_bytes() if path.exists() else None for path in paths} == before
    assert not fresh._journal_path().exists()
