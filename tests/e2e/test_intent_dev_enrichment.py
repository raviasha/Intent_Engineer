"""Browser and HTTP journey contracts for progressive graph enrichment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]
from starlette.testclient import TestClient

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.control_plane import ControlPlaneService, build_control_plane_app
from intent_engineering.core.models import ProjectConfig
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.enrichment_models import EnrichmentSession
from intent_engineering.storage.yaml.graph_store import serialize_graph
from tests.e2e.test_intent_dev_web_runtime import _run
from tests.integration.intent_workflow.test_enrichment import (
    NOW,
    Clock,
    _clarification_proposal,
    _evidence,
    _evidence_bytes,
    _graph,
)

ORIGIN = "http://localhost:43127"
CSRF = "csrf-enrichment-43127"


def _enrichment_response(
    *,
    status: str = "open",
    current_gap_id: str | None = "gap:one",
    answered_gap_ids: list[str] | None = None,
    skipped_gap_ids: list[str] | None = None,
) -> dict[str, object]:
    answered = [] if answered_gap_ids is None else answered_gap_ids
    skipped = [] if skipped_gap_ids is None else skipped_gap_ids
    return {
        "schema_version": 1,
        "session": {
            "schema_version": 1,
            "id": "refine:session",
            "status": status,
            "focus_id": None,
            "budget_minutes": 5,
            "remaining_budget_seconds": 300,
            "snapshot_digest": "sha256:" + "a" * 64,
            "current_gap_id": current_gap_id,
            "answered_gap_ids": answered,
            "skipped_gap_ids": skipped,
            "answer_evidence_refs": ["evidence:answer"] * len(answered),
            "started_at": "2026-09-02T12:00:00Z",
            "updated_at": "2026-09-02T12:00:00Z",
        },
        "question": (
            {
                "gap_id": current_gap_id,
                "node_id": "requirement:owners",
                "dimension": "intent_clarity",
                "rule_id": "rubric:v1:intent_clarity:owner",
                "prompt": "Who owns this requirement?",
                "reason": "Ownership is missing.",
                "requested_fields": ["owner"],
                "evidence_scope": [],
            }
            if status == "open"
            else None
        ),
    }


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def enrichment_start(self, minutes: int | None, focus: str | None) -> dict[str, object]:
        self.calls.append(("start", minutes, focus))
        response = _enrichment_response()
        session = cast(dict[str, object], response["session"])
        session["focus_id"] = focus
        session["budget_minutes"] = minutes
        return response


class _LifecycleService(_Service):
    def enrichment_current(self, session_id: str) -> dict[str, object]:
        self.calls.append(("current", session_id))
        return _enrichment_response()

    def enrichment_answer(self, session_id: str, gap_id: str, answer: str) -> dict[str, object]:
        self.calls.append(("answer", session_id, gap_id, answer))
        return _enrichment_response(current_gap_id="gap:two", answered_gap_ids=["gap:one"])

    def enrichment_skip(self, session_id: str, gap_id: str) -> dict[str, object]:
        self.calls.append(("skip", session_id, gap_id))
        return _enrichment_response(current_gap_id="gap:two", skipped_gap_ids=["gap:one"])

    def enrichment_pause(self, session_id: str) -> dict[str, object]:
        self.calls.append(("pause", session_id))
        return _enrichment_response(status="paused", current_gap_id="gap:two")

    def enrichment_resume(self, session_id: str) -> dict[str, object]:
        self.calls.append(("resume", session_id))
        return _enrichment_response(current_gap_id="gap:two")


def _headers() -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Cookie": f"intent_csrf={CSRF}",
        "X-Intent-CSRF": CSRF,
        "Content-Type": "application/json",
    }


def test_enrichment_start_is_strict_csrf_protected_and_answer_never_uses_query() -> None:
    service = _Service()
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    rejected = client.post("/api/v1/enrichment/start", content=b'{"minutes":5,"focus":null}')
    started = client.post(
        "/api/v1/enrichment/start",
        content=b'{"minutes":5,"focus":null}',
        headers=_headers(),
    )
    duplicate = client.post(
        "/api/v1/enrichment/start",
        content=b'{"minutes":5,"minutes":15,"focus":null}',
        headers=_headers(),
    )

    assert rejected.status_code == 403
    assert started.status_code == 200
    assert started.json()["question"]["reason"] == "Ownership is missing."
    assert duplicate.status_code == 400
    assert service.calls == [("start", 5, None)]


def test_enrichment_http_routes_preserve_the_exact_session_lifecycle() -> None:
    service = _LifecycleService()
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    requests = (
        ("start", {"minutes": 5, "focus": None}),
        ("current", {"session_id": "refine:session"}),
        (
            "answer",
            {
                "session_id": "refine:session",
                "gap_id": "gap:one",
                "answer": "Workspace owners",
            },
        ),
        ("skip", {"session_id": "refine:session", "gap_id": "gap:one"}),
        ("pause", {"session_id": "refine:session"}),
        ("resume", {"session_id": "refine:session"}),
    )
    responses = [
        client.post(
            f"/api/v1/enrichment/{action}",
            content=json.dumps(body, separators=(",", ":")).encode(),
            headers=_headers(),
        )
        for action, body in requests
    ]

    assert [response.status_code for response in responses] == [200] * len(requests)
    assert responses[4].json()["session"]["status"] == "paused"
    assert responses[4].json()["session"]["current_gap_id"] == "gap:two"
    assert responses[4].json()["question"] is None
    assert responses[5].json()["question"]["gap_id"] == "gap:two"
    assert service.calls == [
        ("start", 5, None),
        ("current", "refine:session"),
        ("answer", "refine:session", "gap:one", "Workspace owners"),
        ("skip", "refine:session", "gap:one"),
        ("pause", "refine:session"),
        ("resume", "refine:session"),
    ]


def test_enrichment_http_cancellation_preserves_identity_and_scrubs_answer() -> None:
    class _Cancel(BaseException):
        pass

    signal = _Cancel("PRIVATE-HTTP-CANCEL")

    class _CancelledService(_LifecycleService):
        def enrichment_answer(self, session_id: str, gap_id: str, answer: str) -> dict[str, object]:
            retained_answer = answer
            if retained_answer:
                raise signal
            raise AssertionError("test requires an answer")

    client = TestClient(
        build_control_plane_app(cast(Any, _CancelledService()), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    with pytest.raises(_Cancel) as captured:
        client.post(
            "/api/v1/enrichment/answer",
            content=b'{"session_id":"refine:session","gap_id":"gap:one","answer":"PRIVATE-HTTP-ANSWER"}',
            headers=_headers(),
        )

    assert captured.value is signal
    assert signal.args == ()
    assert signal.__dict__ == {}
    assert signal.__cause__ is None
    assert signal.__context__ is None
    current = signal.__traceback__
    while current is not None:
        if "/src/intent_engineering/" in current.tb_frame.f_code.co_filename:
            assert "PRIVATE-HTTP-ANSWER" not in repr(current.tb_frame.f_locals)
            assert "PRIVATE-HTTP-CANCEL" not in repr(current.tb_frame.f_locals)
        current = current.tb_next


def test_enrichment_http_failure_is_fixed_and_does_not_echo_answer() -> None:
    class _FailedService(_LifecycleService):
        def enrichment_answer(self, session_id: str, gap_id: str, answer: str) -> dict[str, object]:
            raise RuntimeError(f"PRIVATE-HTTP-FAILURE:{answer}")

    client = TestClient(
        build_control_plane_app(cast(Any, _FailedService()), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    response = client.post(
        "/api/v1/enrichment/answer",
        content=b'{"session_id":"refine:session","gap_id":"gap:one","answer":"PRIVATE-HTTP-ANSWER"}',
        headers=_headers(),
    )

    assert response.status_code == 503
    assert response.json() == {
        "schema_version": 1,
        "status": "rejected",
        "reason": "request_unavailable",
    }
    assert b"PRIVATE-HTTP" not in response.content


def test_packaged_browser_exposes_separate_safe_enrichment_controls() -> None:
    assets = Path("src/intent_engineering/control_plane/assets")
    html = (assets / "index.html").read_text(encoding="utf-8")
    script = (assets / "app.js").read_text(encoding="utf-8")

    assert "Local review" in html
    assert '"enrichment"' in script
    assert "Improve graph" in script
    assert "Why this matters" in script
    assert "Score dimension" in script
    assert "Pause improvement session" in script
    assert "Resume improvement session" in script
    assert "Review proposed graph changes" in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "localStorage" not in script
    assert "console." not in script


def test_five_minute_answer_pause_restart_resume_keeps_only_evidence_reference(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialized = initialize_project(project)
    config = ProjectConfig(project_id="project:enrichment", local_actor="local:asha")
    initialized.config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    evidence = _evidence()
    initialized.graph_path.write_bytes(serialize_graph(_graph(evidence.id)))
    (initialized.workspace / "evidence/evidence.jsonl").write_bytes(_evidence_bytes(evidence))
    (initialized.workspace / "approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local:asha"],
                "approvers": ["local:asha"],
                "executors": ["local:asha"],
                "identities": {"local:asha": ["local:asha"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    graph_before = initialized.graph_path.read_bytes()
    answer = "Workspace owners"

    runtime = load_runtime(project)
    control: ControlPlaneService | None = None
    try:
        control = ControlPlaneService(runtime, origin=ORIGIN, clock=Clock())
        started = control.enrichment_start(5, None)
        session_id = cast(dict[str, object], started["session"])["id"]
        question = cast(dict[str, object], started["question"])
        answered = control.enrichment_answer(
            cast(str, session_id), cast(str, question["gap_id"]), answer
        )
        answered_session = cast(dict[str, object], answered["session"])
        evidence_ref = cast(list[str], answered_session["answer_evidence_refs"])[0]
        detached_session = EnrichmentSession.model_validate_json(
            json.dumps(answered_session, separators=(",", ":"), sort_keys=True), strict=True
        )
        _coordinator, submission = _clarification_proposal(runtime, detached_session)
        client = TestClient(
            build_control_plane_app(control, origin=ORIGIN, csrf_secret=CSRF),
            base_url=ORIGIN,
        )
        hidden = ConversationCapture(runtime.evidence_store).record_turn(
            conversation_ref="conversation:hidden-enrichment",
            role="human",
            author="local:other",
            content="Evidence Asha cannot read",
            captured_at=NOW,
            acl=("local:other",),
        )
        proposed_node = submission.changeset.nodes_added[0]
        hidden_node = proposed_node.model_copy(
            update={"evidence_refs": (*proposed_node.evidence_refs, hidden.id)}
        )
        hidden_changeset = submission.changeset.model_copy(
            update={
                "evidence_refs": (*submission.changeset.evidence_refs, hidden.id),
                "nodes_added": (hidden_node,),
            }
        )
        hidden_submission = submission.model_copy(
            update={
                "evidence_refs": (*submission.evidence_refs, hidden.id),
                "changeset": hidden_changeset,
            }
        )
        rejected = client.post(
            "/api/v1/enrichment/propose",
            content=json.dumps(
                {
                    "session_id": cast(str, session_id),
                    "submission": hidden_submission.model_dump(mode="json"),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
            headers=_headers(),
        )
        assert rejected.status_code == 503
        assert initialized.graph_path.read_bytes() == graph_before
        proposed = client.post(
            "/api/v1/enrichment/propose",
            content=json.dumps(
                {
                    "session_id": cast(str, session_id),
                    "submission": submission.model_dump(mode="json"),
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
            headers=_headers(),
        )
        assert proposed.status_code == 200
        assert proposed.json()["session_id"] == session_id
        assert proposed.json()["proposal"]["id"].startswith("proposal:")
        assert proposed.json()["proposal"]["schema_version"] == 2
        assert initialized.graph_path.read_bytes() == graph_before
        paused = control.enrichment_pause(cast(str, session_id))
        paused_session = cast(dict[str, object], paused["session"])
        assert paused_session["status"] == "paused"
        assert paused_session["current_gap_id"] is not None
        assert paused["question"] is None
    finally:
        if control is not None:
            control.close()
        runtime.close()

    restarted = load_runtime(project)
    restarted_control: ControlPlaneService | None = None
    try:
        restarted_control = ControlPlaneService(restarted, origin=ORIGIN, clock=Clock())
        resumed = restarted_control.enrichment_resume(cast(str, session_id))
        resumed_session = cast(dict[str, object], resumed["session"])
        assert evidence_ref in cast(list[str], resumed_session["answer_evidence_refs"])
    finally:
        if restarted_control is not None:
            restarted_control.close()
        restarted.close()

    assert initialized.graph_path.read_bytes() == graph_before
    assert answer.encode() in (initialized.workspace / "evidence/evidence.jsonl").read_bytes()
    assert (
        answer.encode()
        not in (initialized.workspace / "history/enrichment-sessions.jsonl").read_bytes()
    )


def test_browser_runs_one_question_flow_and_clears_raw_answer_from_dom_and_urls() -> None:
    result = _run(
        r"""
function enrichment(statusValue = "open", currentGap = "gap:one", answered = []) {
  return { schema_version: 1, session: { schema_version: 1, id: "refine:session",
    status: statusValue, focus_id: null, budget_minutes: 5, remaining_budget_seconds: 300,
    snapshot_digest: "sha256:" + "a".repeat(64), current_gap_id: currentGap,
    answered_gap_ids: answered, skipped_gap_ids: [],
    answer_evidence_refs: answered.length ? ["evidence:answer"] : [],
    started_at: "2026-09-02T12:00:00Z", updated_at: "2026-09-02T12:00:00Z" },
    question: statusValue === "open" ? { gap_id: currentGap,
      node_id: "requirement:owners", dimension: "intent_clarity",
      rule_id: "rubric:v1:intent_clarity:owner", prompt: "Who owns this requirement?",
      reason: "Ownership is missing.", requested_fields: ["owner"], evidence_scope: [] } : null };
}
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
respond(take("/api/v1/development/observation"), { schema_version: 1, command_ids: [] }); await settle();
button("Improve graph").click(); await settle();
button("Start improvement session").click(); await settle();
const startRequest = take("/api/v1/enrichment/start");
respond(startRequest, enrichment()); await settle();
const answer = walk(app).find((node) => node.id === "enrichment-answer");
answer.value = "PRIVATE-ANSWER-619";
button("Record answer as evidence").click(); await settle();
const answerRequest = take("/api/v1/enrichment/answer");
const answerBody = JSON.parse(answerRequest.init.body);
const clearedBeforeResponse = answer.value;
respond(answerRequest, enrichment("open", "gap:two", ["gap:one"])); await settle();
button("Pause improvement session").click(); await settle();
const pauseRequest = take("/api/v1/enrichment/pause");
respond(pauseRequest, enrichment("paused", "gap:two", ["gap:one"])); await settle();
process.stdout.write(JSON.stringify({ startBody: JSON.parse(startRequest.init.body), answerBody,
  clearedBeforeResponse, app: app.textContent, status: status.textContent,
  urls: calls.map((request) => request.path), hasResume: button("Resume improvement session").textContent,
  hasReview: button("Review proposed graph changes").textContent }));
"""
    )

    assert result["startBody"] == {"focus": None, "minutes": 5}
    assert result["answerBody"]["answer"] == "PRIVATE-ANSWER-619"
    assert result["clearedBeforeResponse"] == ""
    assert "PRIVATE-ANSWER-619" not in result["app"]
    assert all("PRIVATE-ANSWER-619" not in path for path in result["urls"])
    assert result["hasResume"] == "Resume improvement session"
    assert result["hasReview"] == "Review proposed graph changes"


def test_browser_submits_exact_enrichment_proposal_then_opens_its_governed_review() -> None:
    result = _run(
        r"""
function enrichment() {
  return { schema_version: 1, session: { schema_version: 1, id: "refine:session",
    status: "open", focus_id: null, budget_minutes: 5, remaining_budget_seconds: 300,
    snapshot_digest: "sha256:" + "a".repeat(64), current_gap_id: "gap:two",
    answered_gap_ids: ["gap:one"], skipped_gap_ids: [],
    answer_evidence_refs: ["evidence:answer"],
    started_at: "2026-09-02T12:00:00Z", updated_at: "2026-09-02T12:00:00Z" },
    question: { gap_id: "gap:two", node_id: "requirement:owners",
      dimension: "intent_clarity", rule_id: "rubric:v1:intent_clarity:owner",
      prompt: "Who owns this requirement?", reason: "Ownership is missing.",
      requested_fields: ["owner"], evidence_scope: [] } };
}
const proposalId = "proposal:sha256:" + "b".repeat(64);
const proposalRecord = {
  schema_version: 2, id: proposalId, kind: "requirement", proposed_by: "local:asha",
  proposed_at: "2026-09-02T12:00:05Z", baseline_graph_version: 3,
  evidence_refs: ["evidence:answer"], source_roles: [], changeset: {},
  core_node_ids: ["requirement:owners"], provisional_node_ids: [], assumptions: [],
  unanswered_questions: [], conflicting_authors: [], destructive: false,
  clarification_session_id: "clarification:one", task_id: "task:one"
};
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
respond(take("/api/v1/development/observation"), { schema_version: 1, command_ids: [] }); await settle();
button("Improve graph").click(); await settle();
button("Start improvement session").click(); await settle();
respond(take("/api/v1/enrichment/start"), enrichment()); await settle();
const submission = walk(app).find((node) => node.id === "enrichment-proposal-submission");
submission.value = JSON.stringify({ schema_version: 1, typed: "governed submission" });
button("Review proposed graph changes").click(); await settle();
const proposed = take("/api/v1/enrichment/propose");
respond(proposed, { schema_version: 1, session_id: "refine:session", proposal: proposalRecord });
await settle();
const preview = take(`/api/v1/proposals/${encodeURIComponent(proposalId)}`);
respond(preview, proposal(proposalId)); await settle();
process.stdout.write(JSON.stringify({
  body: JSON.parse(proposed.init.body), previewPath: preview.path,
  submissionAfter: submission.value, app: app.textContent,
}));
"""
    )

    assert result["body"] == {
        "session_id": "refine:session",
        "submission": {"schema_version": 1, "typed": "governed submission"},
    }
    assert result["previewPath"].endswith("proposal%3Asha256%3A" + "b" * 64)
    assert result["submissionAfter"] == ""
    assert "Proposal review" in result["app"]
    assert "Exact preview" in result["app"]


def test_browser_rejects_proposal_response_not_bound_to_current_enrichment_session() -> None:
    result = _run(
        r"""
function enrichment() {
  return { schema_version: 1, session: { schema_version: 1, id: "refine:session",
    status: "open", focus_id: null, budget_minutes: 5, remaining_budget_seconds: 300,
    snapshot_digest: "sha256:" + "a".repeat(64), current_gap_id: "gap:two",
    answered_gap_ids: ["gap:one"], skipped_gap_ids: [],
    answer_evidence_refs: ["evidence:answer"],
    started_at: "2026-09-02T12:00:00Z", updated_at: "2026-09-02T12:00:00Z" },
    question: { gap_id: "gap:two", node_id: "requirement:owners",
      dimension: "intent_clarity", rule_id: "rubric:v1:intent_clarity:owner",
      prompt: "Who owns this requirement?", reason: "Ownership is missing.",
      requested_fields: ["owner"], evidence_scope: [] } };
}
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
respond(take("/api/v1/development/observation"), { schema_version: 1, command_ids: [] }); await settle();
button("Improve graph").click(); await settle();
button("Start improvement session").click(); await settle();
respond(take("/api/v1/enrichment/start"), enrichment()); await settle();
const submission = walk(app).find((node) => node.id === "enrichment-proposal-submission");
submission.value = JSON.stringify({ schema_version: 1, typed: "governed submission" });
button("Review proposed graph changes").click(); await settle();
const proposed = take("/api/v1/enrichment/propose");
respond(proposed, { schema_version: 1, session_id: "refine:other", proposal: {} });
await settle();
process.stdout.write(JSON.stringify({
  paths: calls.map((request) => request.path), submissionAfter: submission.value,
  app: app.textContent, status: status.textContent,
}));
"""
    )

    assert not any(path.startswith("/api/v1/proposals/") for path in result["paths"])
    assert result["submissionAfter"] == ""
    assert "Who owns this requirement?" in result["app"]
    assert "Proposal review" in result["app"]
    assert "unavailable" in result["status"].lower()


def test_browser_invalid_latest_response_clears_stale_actions_and_question() -> None:
    result = _run(
        r"""
function enrichment() {
  return { schema_version: 1, session: { schema_version: 1, id: "refine:session",
    status: "open", focus_id: null, budget_minutes: 5, remaining_budget_seconds: 300,
    snapshot_digest: "sha256:" + "a".repeat(64), current_gap_id: "gap:one",
    answered_gap_ids: [], skipped_gap_ids: [], answer_evidence_refs: [],
    started_at: "2026-09-02T12:00:00Z", updated_at: "2026-09-02T12:00:00Z" },
    question: { gap_id: "gap:one", node_id: "requirement:owners",
      dimension: "intent_clarity", rule_id: "rubric:v1:intent_clarity:owner",
      prompt: "Who owns this requirement?", reason: "Ownership is missing.",
      requested_fields: ["owner"], evidence_scope: [] } };
}
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
respond(take("/api/v1/development/observation"), { schema_version: 1, command_ids: [] }); await settle();
button("Improve graph").click(); await settle();
button("Start improvement session").click(); await settle();
respond(take("/api/v1/enrichment/start"), enrichment()); await settle();
button("Pause improvement session").click(); await settle();
respond(take("/api/v1/enrichment/pause"), { schema_version: 1, session: null, question: null }); await settle();
const labels = walk(app).filter((node) => node.tagName === "button").map((node) => node.textContent);
process.stdout.write(JSON.stringify({ labels, app: app.textContent, status: status.textContent }));
"""
    )

    assert "Record answer as evidence" not in result["labels"]
    assert "Pause improvement session" not in result["labels"]
    assert "Resume improvement session" not in result["labels"]
    assert "Review proposed graph changes" not in result["labels"]
    assert "Who owns this requirement?" not in result["app"]
    assert "unavailable" in result["status"].lower()


def test_browser_restores_focus_after_question_pause_and_resume_rerenders() -> None:
    result = _run(
        r"""
function enrichment(statusValue, gap) {
  return { schema_version: 1, session: { schema_version: 1, id: "refine:session",
    status: statusValue, focus_id: null, budget_minutes: 5, remaining_budget_seconds: 300,
    snapshot_digest: "sha256:" + "a".repeat(64), current_gap_id: gap,
    answered_gap_ids: [], skipped_gap_ids: [], answer_evidence_refs: [],
    started_at: "2026-09-02T12:00:00Z", updated_at: "2026-09-02T12:00:00Z" },
    question: statusValue === "open" ? { gap_id: gap, node_id: "requirement:owners",
      dimension: "intent_clarity", rule_id: "rubric:v1:intent_clarity:owner",
      prompt: "Who owns this requirement?", reason: "Ownership is missing.",
      requested_fields: ["owner"], evidence_scope: [] } : null };
}
document.getElementById = (id) => id === "app" ? app : id === "status" ? status
  : walk(app).find((node) => node.id === id) || null;
await settle(); respond(take("/api/v1/status"), statusProjection); await settle();
respond(take("/api/v1/development/observation"), { schema_version: 1, command_ids: [] }); await settle();
button("Improve graph").click(); await settle();
button("Start improvement session").click(); await settle();
respond(take("/api/v1/enrichment/start"), enrichment("open", "gap:one")); await settle();
const afterStart = focused.id;
button("Skip current question").click(); await settle();
respond(take("/api/v1/enrichment/skip"), enrichment("open", "gap:two")); await settle();
const afterQuestion = focused.id;
button("Pause improvement session").click(); await settle();
respond(take("/api/v1/enrichment/pause"), enrichment("paused", "gap:two")); await settle();
const afterPause = focused.id;
button("Resume improvement session").click(); await settle();
respond(take("/api/v1/enrichment/resume"), enrichment("open", "gap:two")); await settle();
process.stdout.write(JSON.stringify({ afterStart, afterQuestion, afterPause, afterResume: focused.id }));
"""
    )

    assert result == {
        "afterStart": "enrichment-answer",
        "afterQuestion": "enrichment-answer",
        "afterPause": "enrichment-resume",
        "afterResume": "enrichment-answer",
    }
