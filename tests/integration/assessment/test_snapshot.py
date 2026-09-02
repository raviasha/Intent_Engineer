"""Integration tests for ACL-safe assessment snapshot construction."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import traceback
from datetime import UTC, datetime

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.assessment.snapshot import (
    AssessmentUnavailable,
    build_assessment_snapshot,
)
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceIngestion,
    EvidenceSide,
    ReconciliationCase,
    ReconciliationCaseType,
    SourceMode,
)
from intent_engineering.intent_workflow import (
    ClarificationEvent,
    ClarificationQuestion,
    ClarificationSession,
)
from intent_engineering.intent_workflow.proposal_store import (
    IntentLedgerRecord,
    serialize_intent_ledger_record,
)
from intent_engineering.storage.jsonl.case_store import serialize_case
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.jsonl.history_store import serialize_changeset
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.yaml.graph_store import parse_graph, serialize_graph


def test_snapshot_excludes_hidden_topology_without_count_leak(assessment_runtime) -> None:
    """Catches hidden evidence, nodes, and adjacent edges surviving the actor projection."""
    snapshot = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert tuple(node.id for node in snapshot.graph.nodes) == (
        "intent:public",
        "req:public",
    )
    assert tuple(edge.id for edge in snapshot.graph.edges) == ("edge:public",)
    assert snapshot.omitted_count is None
    assert "PRIVATE-HIDDEN" not in snapshot.model_dump_json()


@pytest.mark.parametrize(
    "target", ("graph", "evidence", "cases", "history", "intent_proposals", "config")
)
def test_same_version_replacement_during_snapshot_fails_fixed(
    assessment_runtime, target: str
) -> None:
    """Catches acceptance of a replaced canonical name after descriptor-held acquisition."""
    assessment_runtime.replace_after_read(target)

    with pytest.raises(AssessmentUnavailable, match="^assessment unavailable$"):
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")


def test_runtime_exposes_the_same_snapshot_boundary(assessment_runtime) -> None:
    """Catches runtime callers bypassing the reviewed assessment acquisition function."""
    direct = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert assessment_runtime.runtime.assessment_snapshot("local:asha") == direct


def test_assessment_package_exports_snapshot_boundary() -> None:
    """Catches callers having to import private module structure for the public interface."""
    from intent_engineering import assessment

    assert assessment.AssessmentUnavailable is AssessmentUnavailable
    assert assessment.build_assessment_snapshot is build_assessment_snapshot


def test_snapshot_resolves_exact_live_actor_aliases(assessment_runtime) -> None:
    """Catches direct-actor-only ACL checks that discard an authenticated provider alias."""
    path = assessment_runtime.paths["evidence"]
    records, ingestions, _legacy = parse_evidence_lines(path.read_bytes())
    replaced = {
        record.id: record.model_copy(update={"acl": ("github:asha",)})
        if record.id == "evidence:public-requirement"
        else record
        for record in records
    }
    path.write_bytes(
        b"".join(
            _canonical_json(
                ingestion.model_copy(
                    update={"evidence": replaced[ingestion.evidence.id]}
                ).model_dump(mode="json")
            )
            + b"\n"
            for ingestion in ingestions
        )
    )

    snapshot = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert tuple(node.id for node in snapshot.graph.nodes) == (
        "intent:public",
        "req:public",
    )


def test_alias_policy_replacement_after_read_fails_fixed(assessment_runtime) -> None:
    """Catches an ACL decision escaping after its exact alias registry is replaced."""
    assessment_runtime.replace_after_read("acl_policy")

    with pytest.raises(AssessmentUnavailable, match="^assessment unavailable$"):
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")


def test_hidden_state_is_indistinguishable_from_absent_state(assessment_runtime) -> None:
    """Catches hidden counts or identities influencing any serialized snapshot field or digest."""
    hidden = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")
    graph_path = assessment_runtime.paths["graph"]
    graph = parse_graph(graph_path.read_bytes())
    graph_path.write_bytes(
        serialize_graph(
            graph.model_copy(
                update={
                    "nodes": tuple(node for node in graph.nodes if node.id != "req:hidden"),
                    "edges": tuple(
                        edge for edge in graph.edges if edge.id != "edge:hidden-adjacent"
                    ),
                }
            )
        )
    )
    evidence_path = assessment_runtime.paths["evidence"]
    records, _ingestions, _legacy = parse_evidence_lines(evidence_path.read_bytes())
    visible_records = tuple(record for record in records if record.id != "evidence:hidden")
    evidence_path.write_bytes(
        b"".join(
            _canonical_json(
                EvidenceIngestion(
                    connector_id="markdown",
                    sequence=sequence,
                    predecessor_id=None,
                    evidence=record,
                ).model_dump(mode="json")
            )
            + b"\n"
            for sequence, record in enumerate(visible_records, start=1)
        )
    )

    absent = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert hidden == absent
    assert tuple(item.sequence for item in hidden.ingestions) == (1, 2)


def test_snapshot_identity_is_stable_across_cross_connector_frame_order(
    assessment_runtime,
) -> None:
    """Catches component digests being computed before canonical ingestion ordering."""
    evidence_path = assessment_runtime.paths["evidence"]
    records, _ingestions, _legacy = parse_evidence_lines(evidence_path.read_bytes())
    by_id = {record.id: record for record in records}
    alpha_public = EvidenceIngestion(
        connector_id="alpha",
        sequence=1,
        predecessor_id=None,
        evidence=by_id["evidence:public-intent"],
    )
    alpha_hidden = EvidenceIngestion(
        connector_id="alpha",
        sequence=2,
        predecessor_id=None,
        evidence=by_id["evidence:hidden"],
    )
    beta_public = EvidenceIngestion(
        connector_id="beta",
        sequence=1,
        predecessor_id=None,
        evidence=by_id["evidence:public-requirement"],
    )

    def write_frames(frames: tuple[EvidenceIngestion, ...]) -> None:
        evidence_path.write_bytes(
            b"".join(_canonical_json(item.model_dump(mode="json")) + b"\n" for item in frames)
        )

    write_frames((alpha_public, beta_public, alpha_hidden))
    first = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")
    write_frames((beta_public, alpha_public, alpha_hidden))
    reordered = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert reordered == first


def test_snapshot_does_not_read_unrelated_transaction_payloads(
    assessment_runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches assessment retaining approval or credential stores outside its declared inputs."""
    original = SecureFile.read_optional_nonblocking
    unrelated = {
        "approvals.jsonl",
        "receipts.jsonl",
        "webauthn-challenges.jsonl",
        "webauthn-credentials.jsonl",
    }

    def reject_unrelated(self: SecureFile, *, max_bytes: int | None = None):
        if self.name in unrelated:
            raise AssertionError("assessment read unrelated canonical payload")
        return original(self, max_bytes=max_bytes)

    monkeypatch.setattr(SecureFile, "read_optional_nonblocking", reject_unrelated)

    snapshot = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert snapshot.project_id == "project:assessment"


def _canonical_json(material: object) -> bytes:
    return json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(material: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(material)).hexdigest()


def test_clarification_with_hidden_question_evidence_is_not_visible(
    assessment_runtime,
) -> None:
    """Catches question evidence being omitted from clarification reference closure."""
    question = ClarificationQuestion(
        id="question:private",
        prompt_digest="sha256:" + "1" * 64,
        evidence_ref="evidence:hidden",
        author="agent:codex",
        asked_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        predecessor_evidence_ref="evidence:public-requirement",
        required=True,
    )
    opened_at = question.asked_at
    session_material = {
        "schema_version": 1,
        "task_id": "task:private-question",
        "conversation_ref": "conversation:assessment",
        "request_evidence_ref": "evidence:public-intent",
        "classification_evidence_ref": "evidence:public-requirement",
        "opened_by": "agent:codex",
        "opened_at": opened_at.isoformat().replace("+00:00", "Z"),
        "baseline_graph_version": 7,
        "questions": [question.model_dump(mode="json")],
    }
    bare_session = ClarificationSession(
        id="clarification:" + _digest(session_material),
        task_id="task:private-question",
        conversation_ref="conversation:assessment",
        request_evidence_ref="evidence:public-intent",
        classification_evidence_ref="evidence:public-requirement",
        opened_by="agent:codex",
        opened_at=opened_at,
        baseline_graph_version=7,
        questions=(question,),
    )
    event_material = {
        "schema_version": 1,
        "event_type": "opened",
        "session": bare_session.model_dump(mode="json", exclude={"latest_event_id"}),
        "actor": "agent:codex",
        "at": opened_at.isoformat().replace("+00:00", "Z"),
        "predecessor_event_id": None,
        "proposal_id": None,
        "decision_id": None,
        "activation_changeset_id": None,
    }
    event_id = "clarification-event:" + _digest(event_material)
    event = ClarificationEvent(
        id=event_id,
        event_type="opened",
        session=bare_session.model_copy(update={"latest_event_id": event_id}),
        actor="agent:codex",
        at=opened_at,
    )
    assessment_runtime.paths["intent_proposals"].write_bytes(
        serialize_intent_ledger_record(IntentLedgerRecord(sequence=0, clarification=event))
    )

    snapshot = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert snapshot.clarifications == ()
    assert "question:private" not in snapshot.model_dump_json()


def _case(case_id: str, subject: str, evidence_ref: str, marker: str) -> ReconciliationCase:
    return ReconciliationCase(
        id=case_id,
        subject_ref=subject,
        case_type=ReconciliationCaseType.AMBIGUOUS_DIVERGENCE,
        affected_refs=(subject,),
        evidence_sides=(
            EvidenceSide(
                label=marker,
                claim=marker,
                evidence_refs=(evidence_ref,),
                observed_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
                authors=("product:priya",),
                confidence=0.8,
                source_mode=SourceMode.EXPLICIT,
            ),
        ),
        detector_id="assessment-fixture",
        fingerprint=("1" if "public" in case_id else "2") * 64,
        created_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        created_by="detector:assessment-fixture",
        impact=marker,
    )


def _changeset(changeset_id: str, subject: str, evidence_ref: str) -> ChangeSet:
    return ChangeSet(
        id=changeset_id,
        actor="local:asha",
        timestamp=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        baseline_graph_version=6,
        evidence_refs=(evidence_ref,),
        nodes_added=(),
        nodes_updated=(),
        nodes_superseded=(subject,),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(),
        validation_status="valid",
    )


def test_cases_and_history_require_closed_visible_references(assessment_runtime) -> None:
    """Catches downstream case or audit records retaining hidden graph/evidence identities."""
    public_case = _case("case:public", "req:public", "evidence:public-requirement", "PUBLIC-CASE")
    hidden_case = _case("case:hidden", "req:hidden", "evidence:hidden", "PRIVATE-HIDDEN-CASE")
    assessment_runtime.paths["cases"].write_bytes(
        serialize_case(public_case) + serialize_case(hidden_case)
    )
    public_history = _changeset("changeset:public", "req:public", "evidence:public-requirement")
    hidden_history = _changeset("changeset:hidden", "req:hidden", "evidence:hidden")
    assessment_runtime.paths["history"].write_bytes(
        serialize_changeset(public_history) + serialize_changeset(hidden_history)
    )

    snapshot = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert tuple(case.id for case in snapshot.cases) == ("case:public",)
    assert tuple(changeset.id for changeset in snapshot.history) == ("changeset:public",)
    assert "PRIVATE-HIDDEN-CASE" not in snapshot.model_dump_json()


@pytest.mark.parametrize("target", ("evidence", "cases", "history", "intent_proposals"))
def test_corrupt_frames_fail_fixed_without_parser_details(assessment_runtime, target: str) -> None:
    """Catches raw canonical-parser errors escaping through the assessment boundary."""
    assessment_runtime.paths[target].write_bytes(b'{"PRIVATE-CORRUPT":\n')

    with pytest.raises(AssessmentUnavailable, match="^assessment unavailable$") as caught:
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_duplicate_graph_identity_fails_fixed(assessment_runtime) -> None:
    """Catches duplicate stable IDs being normalized or filtered before rejection."""
    path = assessment_runtime.paths["graph"]
    graph = parse_graph(path.read_bytes())
    payload = graph.model_dump(mode="json", by_alias=True)
    payload["nodes"].append(payload["nodes"][0])
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    with pytest.raises(AssessmentUnavailable, match="^assessment unavailable$"):
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")


@pytest.mark.parametrize("kind", ("symlink", "hardlink"))
def test_unsafe_graph_substitution_fails_without_blocking(assessment_runtime, kind: str) -> None:
    """Catches canonical reads that follow aliases, accept extra links, or block on a FIFO."""
    graph = assessment_runtime.paths["graph"]
    outside = graph.parent / "outside-graph.yaml"
    outside.write_bytes(graph.read_bytes())
    graph.unlink()
    if kind == "symlink":
        graph.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, graph)
    with pytest.raises(AssessmentUnavailable, match="^assessment unavailable$"):
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")


def test_fifo_graph_substitution_fails_without_blocking(assessment_runtime) -> None:
    """Catches opening a replaced canonical FIFO before authenticating its descriptor kind."""
    graph = assessment_runtime.paths["graph"]
    graph.unlink()
    os.mkfifo(graph)
    child = os.fork()
    if child == 0:  # pragma: no cover - assertion is the child's exit status
        signal.alarm(3)
        try:
            build_assessment_snapshot(assessment_runtime.runtime, "local:asha")
        except AssessmentUnavailable:
            os._exit(0)
        except (OSError, RuntimeError, ValueError):
            os._exit(2)
        os._exit(3)

    _pid, status = os.waitpid(child, 0)

    assert os.WIFEXITED(status)
    assert os.WEXITSTATUS(status) == 0


def test_oversize_config_fails_fixed(assessment_runtime) -> None:
    """Catches unbounded authority-file retention during assessment acquisition."""
    assessment_runtime.paths["config"].write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(AssessmentUnavailable, match="^assessment unavailable$"):
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")


class CancellationSignal(BaseException):
    """Test-only control-flow signal whose exact identity must survive acquisition."""


def test_cancellation_preserves_exact_signal_identity(assessment_runtime) -> None:
    """Catches cancellation being wrapped in the fixed public data-failure exception."""
    secret = "PRIVATE-CANCELLATION-CONTEXT"
    signal = CancellationSignal("cancelled")

    def cancel(*_args: object, **_kwargs: object):
        try:
            raise ValueError(secret)
        except ValueError:
            raise signal

    assessment_runtime.runtime.transactions.snapshot = cancel  # type: ignore[method-assign]

    with pytest.raises(CancellationSignal) as caught:
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert caught.value is signal
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)


def _repository_traceback_locals(error: BaseException) -> str:
    values: list[str] = []
    for frame, _line in traceback.walk_tb(error.__traceback__):
        if "/src/intent_engineering/" in frame.f_code.co_filename:
            values.append(repr(frame.f_locals))
    return "\n".join(values)


@pytest.mark.parametrize("target", ("evidence", "config"))
def test_fixed_failure_scrubs_raw_record_and_config_from_traceback_locals(
    assessment_runtime, target: str
) -> None:
    """Catches hostile canonical bytes surviving in retained repository traceback frames."""
    secret = "PRIVATE-TRACEBACK-RECORD-8197"
    content = (
        '{"payload":"' + secret + '"}\n'
        if target == "evidence"
        else "unknown_private_config: " + secret + "\n"
    )
    assessment_runtime.paths[target].write_text(content, encoding="utf-8")

    with pytest.raises(AssessmentUnavailable) as caught:
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)
