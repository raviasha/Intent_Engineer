"""Integration contracts for descriptor-held control-plane assessment."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.assessment.service import GraphAssessmentService
from intent_engineering.cli.runtime import Runtime, load_runtime
from intent_engineering.control_plane import service as service_module
from intent_engineering.control_plane.http_models import AssessmentResponse
from intent_engineering.control_plane.service import ControlPlaneError, ControlPlaneService
from intent_engineering.core.models import EvidenceRecord, Graph
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import serialize_graph
from tests.helpers.cli import init_git_repo, run_intent

ORIGIN = "http://localhost:43127"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_FIXTURE = Path(__file__).parents[2] / "fixtures" / "assessment" / "rubric-v1.yaml"


def _durable_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


def _repository_traceback_locals(error: BaseException) -> str:
    frames: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            frames.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return "".join(frames)


def _leave_transaction_journal(project: Path) -> None:
    workspace = SecureDirectory.open(project / ".intent")
    paths = {
        "graph": "graph.yaml",
        "history": "history/changesets.jsonl",
        "cases": "reconciliation/cases.jsonl",
        "evidence": "evidence/evidence.jsonl",
        "receipts": "approvals/receipts.jsonl",
        "approvals": "approvals/approvals.jsonl",
        "intent_proposals": "history/intent-proposals.jsonl",
        "webauthn_credentials": "approvals/webauthn-credentials.jsonl",
        "webauthn_challenges": "approvals/webauthn-challenges.jsonl",
    }
    targets = {name: workspace.file(path) for name, path in paths.items()}

    def crash(stage: str) -> None:
        if stage == "target:graph":
            raise SystemExit

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        targets,
        fault_hook=crash,
    )
    try:
        with pytest.raises(SystemExit), coordinator.transaction() as transaction:
            transaction.write("graph", b"torn: [")
    finally:
        coordinator.close()
        for target in targets.values():
            target.close()
        workspace.close()


@pytest.fixture
def assessment_control_plane(tmp_path: Path) -> Iterator[tuple[Path, Runtime, ControlPlaneService]]:
    project = init_git_repo(tmp_path)
    assert run_intent(project, "init").returncode == 0
    config = yaml.safe_load((project / ".intent/config.yaml").read_text(encoding="utf-8"))
    actor = config["local_actor"]
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": [actor],
                "approvers": [actor],
                "executors": [actor],
                "identities": {actor: [actor]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    payload = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
    runtime = load_runtime(project)
    (project / ".intent/graph.yaml").write_bytes(
        serialize_graph(Graph.model_validate(payload["graph"]))
    )
    for item in payload["evidence"]:
        runtime.evidence_store.put(EvidenceRecord.model_validate(item))
    service = ControlPlaneService(runtime, origin=ORIGIN, clock=lambda: NOW)
    try:
        yield project, runtime, service
    finally:
        service.close()
        runtime.close()


def test_assessment_uses_descriptor_held_snapshot_without_writes(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
) -> None:
    project, _runtime, service = assessment_control_plane
    before = _durable_bytes(project)

    result = service.assessment()

    assert result["assessment"]["snapshot_digest"].startswith("sha256:")
    assert result["assessment"]["nodes"]
    assert _durable_bytes(project) == before


def test_assessment_builds_from_the_initial_authority_snapshot(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, runtime, service = assessment_control_plane

    real_snapshot = Runtime.assessment_snapshot

    def forbidden_second_snapshot(selected: Runtime, actor: str) -> object:
        if selected is runtime:
            raise AssertionError("assessment acquired a second runtime snapshot")
        return real_snapshot(selected, actor)

    monkeypatch.setattr(Runtime, "assessment_snapshot", forbidden_second_snapshot)
    before = _durable_bytes(project)

    result = service.assessment()

    assert result["assessment"]["snapshot_digest"].startswith("sha256:")
    assert _durable_bytes(project) == before


def test_assessment_rejects_graph_and_acl_aba_transitions(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _runtime, service = assessment_control_plane
    graph_path = project / ".intent/graph.yaml"
    policy_path = project / ".intent/approvals/policy.yaml"
    graph_preimage = graph_path.read_bytes()
    policy_preimage = policy_path.read_bytes()
    real_assess = service._assessment_service.assess

    def transition(snapshot: object) -> object:
        graph_path.write_bytes(graph_preimage.replace(b"graph:rubric", b"graph:changed"))
        graph_path.write_bytes(graph_preimage)
        policy_path.write_bytes(policy_preimage + b"\n")
        policy_path.write_bytes(policy_preimage)
        return real_assess(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(service._assessment_service, "assess", transition)

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        service.assessment()


def test_assessment_focus_and_node_lookup_share_the_visible_report(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
) -> None:
    _project, _runtime, service = assessment_control_plane

    focused = service.assessment("intent:export")
    node = service.assessment_node("intent:export")

    assert focused["focus"]["reference"] == "intent:export"
    assert focused["focus"]["node"]["node_id"] == "intent:export"
    assert node["node"]["node_id"] == "intent:export"
    assert node["snapshot_digest"] == focused["assessment"]["snapshot_digest"]


def test_assessment_pages_one_shared_bounded_node_projection(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
) -> None:
    project, runtime, service = assessment_control_plane
    graph = runtime.graph_store.load()
    template = graph.nodes[1]
    expanded = graph.model_copy(
        update={
            "nodes": (
                *graph.nodes,
                *(
                    template.model_copy(update={"id": f"req:page-{index:03d}"})
                    for index in range(98)
                ),
            )
        }
    )
    (project / ".intent/graph.yaml").write_bytes(serialize_graph(expanded))

    first = service.assessment()
    first_page = cast(dict[str, object], first["page"])
    cursor = cast(str, first_page["next_cursor"])
    second = service.assessment(page_cursor=cursor)
    second_page = cast(dict[str, object], second["page"])

    assert len(cast(list[object], first_page["rows"])) == 100
    assert len(cast(list[object], second_page["rows"])) == 1
    assert second_page["next_cursor"] is None
    assert (
        cast(dict[str, object], first["assessment"])["snapshot_digest"]
        == cast(dict[str, object], second["assessment"])["snapshot_digest"]
    )


def test_assessment_graph_projection_is_capped_by_nodes_and_http_bytes(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
) -> None:
    _project, runtime, service = assessment_control_plane
    snapshot = runtime.assessment_snapshot(runtime.config.local_actor)
    report = GraphAssessmentService(clock=lambda: NOW).assess(snapshot)
    template = report.nodes[0]
    oversized = report.model_copy(
        update={
            "nodes": (
                *report.nodes,
                *(
                    template.model_copy(update={"node_id": f"req:bounded-{index:04d}"})
                    for index in range(1_998)
                ),
            )
        }
    )

    projection = service._assessment_projection(oversized, focus=None, offset=0)
    validated = AssessmentResponse.model_validate(projection)
    encoded = json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    projected_nodes = cast(list[dict[str, object]], validated.assessment["nodes"])

    assert 100 <= len(projected_nodes) < 2_000
    assert len(encoded) <= 1024 * 1024
    assert all(
        node["dimensions"] == template.model_dump(mode="json")["dimensions"]
        for node in projected_nodes
        if cast(str, node["node_id"]).startswith("req:bounded-")
    )
    assert "response node projection truncated" in cast(list[str], validated.assessment["warnings"])


def test_forged_cursor_beyond_byte_projected_extent_fails_before_scoring(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, runtime, service = assessment_control_plane
    original_snapshot = runtime.assessment_snapshot(runtime.config.local_actor)
    base_report = GraphAssessmentService(clock=lambda: NOW).assess(original_snapshot)
    graph = runtime.graph_store.load()
    template_node = graph.nodes[1]
    expanded_graph = graph.model_copy(
        update={
            "nodes": (
                *graph.nodes,
                *(
                    template_node.model_copy(update={"id": f"req:cursor-{index:04d}"})
                    for index in range(1_997)
                ),
            )
        }
    )
    (project / ".intent/graph.yaml").write_bytes(serialize_graph(expanded_graph))
    template_scorecard = base_report.nodes[0]
    oversized_report = base_report.model_copy(
        update={
            "nodes": (
                *base_report.nodes,
                *(
                    template_scorecard.model_copy(update={"node_id": f"req:cursor-{index:04d}"})
                    for index in range(1_997)
                ),
            ),
        }
    )

    def oversized_assess(snapshot: object) -> object:
        assessment_snapshot = cast(Any, snapshot)
        return oversized_report.model_copy(
            update={
                "graph_digest": assessment_snapshot.graph_digest,
                "snapshot_digest": assessment_snapshot.aggregate_digest,
                "principal_projection_digest": assessment_snapshot.principal_projection_digest,
            }
        )

    monkeypatch.setattr(service._assessment_service, "assess", oversized_assess)
    first = service.assessment()
    cursor = cast(str, cast(dict[str, object], first["page"])["next_cursor"])
    projected_count = len(cast(dict[str, object], first["assessment"])["nodes"])
    scored = False

    def forbidden_assess(_snapshot: object) -> object:
        nonlocal scored
        scored = True
        raise AssertionError("forged cursor reached scoring")

    monkeypatch.setattr(service._assessment_service, "assess", forbidden_assess)
    forged = cursor.rsplit(":", 1)[0] + ":1900"

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        service.assessment(page_cursor=forged)

    assert 100 < projected_count < 1_900
    assert cursor.endswith(":100")
    assert not scored


def test_acl_hidden_and_unknown_node_lookup_fail_identically(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
) -> None:
    project, runtime, service = assessment_control_plane
    graph = runtime.graph_store.load()
    hidden_evidence = EvidenceRecord.model_validate(
        {
            "id": "evidence:hidden",
            "connector_type": "fixture",
            "external_object_id": "hidden",
            "external_version": "1",
            "author": "Other",
            "observed_at": "2026-09-01T00:00:00Z",
            "source_locator": "fixture://hidden",
            "content_hash": "sha256:hidden",
            "payload": {"kind": "requirement"},
            "acl": ["local:other"],
        }
    )
    runtime.evidence_store.put(hidden_evidence)
    hidden_node = graph.nodes[1].model_copy(
        update={"id": "req:hidden", "evidence_refs": (hidden_evidence.id,)}
    )
    (project / ".intent/graph.yaml").write_bytes(
        serialize_graph(graph.model_copy(update={"nodes": (*graph.nodes, hidden_node)}))
    )
    before = _durable_bytes(project)

    failures: list[tuple[type[BaseException], str]] = []
    for node_id in ("req:hidden", "req:unknown"):
        with pytest.raises(LookupError, match="^assessment node unavailable$") as caught:
            service.assessment_node(node_id)
        failures.append((type(caught.value), str(caught.value)))

    assert failures[0] == failures[1]
    assert _durable_bytes(project) == before


def test_missing_node_outcome_is_reauthenticated_before_lookup_failure(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project, _runtime, service = assessment_control_plane
    matched = 0

    def changed(_authority: object) -> bool:
        nonlocal matched
        matched += 1
        return False

    monkeypatch.setattr(service, "_read_only_authority_matches", changed)

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        service.assessment_node("req:unknown")

    assert matched == 1


def test_stale_page_cursor_fails_before_assessment_scoring(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _runtime, service = assessment_control_plane
    scored = False

    def forbidden_assess(*_args: object, **_kwargs: object) -> object:
        nonlocal scored
        scored = True
        raise AssertionError("stale cursor reached scoring")

    monkeypatch.setattr(GraphAssessmentService, "assess", forbidden_assess)
    before = _durable_bytes(project)

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        service.assessment(page_cursor="page:" + "0" * 64 + ":100")

    assert not scored
    assert _durable_bytes(project) == before


def test_assessment_never_recovers_an_interrupted_transaction(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
) -> None:
    project, _runtime, service = assessment_control_plane
    _leave_transaction_journal(project)
    before = _durable_bytes(project)

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        service.assessment()

    assert _durable_bytes(project) == before


def test_assessment_reauthenticates_held_authority_before_return(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _runtime, service = assessment_control_plane
    matched = 0

    def changed(_authority: object) -> bool:
        nonlocal matched
        matched += 1
        return False

    monkeypatch.setattr(service, "_read_only_authority_matches", changed)
    before = _durable_bytes(project)

    with pytest.raises(ControlPlaneError, match="^control plane unavailable$"):
        service.assessment()

    assert matched == 1
    assert _durable_bytes(project) == before


def test_assessment_cancellation_scrubs_snapshot_and_report_locals(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, runtime, service = assessment_control_plane
    secret = "PRIVATE-ASSESSMENT-GRAPH"
    graph = runtime.graph_store.load()
    (project / ".intent/graph.yaml").write_bytes(
        serialize_graph(graph.model_copy(update={"id": secret}))
    )
    signal = KeyboardInterrupt("cancelled")

    def interrupt(_authority: object) -> bool:
        raise signal

    monkeypatch.setattr(service, "_read_only_authority_matches", interrupt)
    before = _durable_bytes(project)

    with pytest.raises(KeyboardInterrupt) as caught:
        service.assessment()

    assert caught.value is signal
    assert secret not in _repository_traceback_locals(caught.value)
    assert _durable_bytes(project) == before


@pytest.mark.parametrize("failure", (RuntimeError, KeyboardInterrupt))
def test_assessment_close_failures_are_fixed_and_scrub_private_traceback_locals(
    assessment_control_plane: tuple[Path, Runtime, ControlPlaneService],
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    project, runtime, service = assessment_control_plane
    secret = "PRIVATE-CLOSE-ASSESSMENT-GRAPH"
    graph = runtime.graph_store.load()
    (project / ".intent/graph.yaml").write_bytes(
        serialize_graph(graph.model_copy(update={"id": secret}))
    )
    signal = failure("close failed")
    real_close = service_module._Authority.close

    def fail_close(authority: object) -> None:
        real_close(authority)  # type: ignore[arg-type]
        raise signal

    monkeypatch.setattr(service_module._Authority, "close", fail_close)

    expected = ControlPlaneError if failure is RuntimeError else KeyboardInterrupt
    with pytest.raises(expected) as caught:
        service.assessment()

    if failure is KeyboardInterrupt:
        assert caught.value is signal
    assert secret not in _repository_traceback_locals(caught.value)
