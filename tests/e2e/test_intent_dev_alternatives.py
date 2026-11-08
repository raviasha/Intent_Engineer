"""Browser and HTTP contracts for explicit requirement alternatives."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from starlette.testclient import TestClient

from intent_engineering.control_plane import build_control_plane_app
from tests.e2e.test_intent_dev_web_runtime import _run
from tests.unit.alternatives.fixtures import available_alternative_result

ORIGIN = "http://localhost:43127"
CSRF = "csrf-alternatives-43127"


def _headers() -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Cookie": f"intent_csrf={CSRF}",
        "X-Intent-CSRF": CSRF,
        "Content-Type": "application/json",
    }


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.result = available_alternative_result()
        self.comparison = self.result.alternatives[0]

    def alternatives(self, subject_ref: str, modes: tuple[str, ...]) -> dict[str, object]:
        self.calls.append(("alternatives", subject_ref, modes))
        return {
            "schema_version": 1,
            "request_digest": self.result.request_digest,
            "status": self.result.status.value,
            "alternatives": [
                {
                    "comparison_id": item.id,
                    "comparison": item.model_dump(mode="json"),
                }
                for item in self.result.alternatives
            ],
        }

    def alternative_propose(
        self,
        alternative_id: str,
        expected_request_digest: str,
        expected_effect_digest: str,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "propose",
                alternative_id,
                expected_request_digest,
                expected_effect_digest,
            )
        )
        return {
            "schema_version": 1,
            "proposal_id": "proposal:sha256:" + "a" * 64,
            "status": "pending_review",
        }

    def alternative_validate(
        self,
        subject_ref: str,
        modes: tuple[str, ...],
        candidate: object,
    ) -> dict[str, object]:
        self.calls.append(("validate", subject_ref, modes, candidate))
        return {
            "schema_version": 1,
            "request_digest": self.result.request_digest,
            "status": self.result.status.value,
            "alternatives": [
                {
                    "comparison_id": item.id,
                    "comparison": item.model_dump(mode="json"),
                }
                for item in self.result.alternatives
            ],
        }


def test_alternative_routes_are_explicit_strict_and_csrf_protected() -> None:
    service = _Service()
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    rejected = client.post(
        "/api/v1/alternatives",
        content=b'{"subject_ref":"requirement:one","modes":["simplify"]}',
    )
    generated = client.post(
        "/api/v1/alternatives",
        content=b'{"subject_ref":"requirement:one","modes":["simplify"]}',
        headers=_headers(),
    )
    duplicate = client.post(
        "/api/v1/alternatives",
        content=(
            b'{"subject_ref":"requirement:one","subject_ref":"requirement:two",'
            b'"modes":["simplify"]}'
        ),
        headers=_headers(),
    )

    assert rejected.status_code == 403
    assert generated.status_code == 200
    assert generated.json()["status"] == "available"
    assert duplicate.status_code == 400
    assert service.calls == [("alternatives", "requirement:one", ("simplify",))]


def test_selecting_an_alternative_creates_only_a_pending_review_proposal() -> None:
    service = _Service()
    item = service.comparison
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    response = client.post(
        "/api/v1/alternatives/propose",
        json={
            "alternative_id": item.id,
            "expected_request_digest": item.request_digest,
            "expected_effect_digest": item.alternative.graph_effect_digest,
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": 1,
        "proposal_id": "proposal:sha256:" + "a" * 64,
        "status": "pending_review",
    }
    assert service.calls == [
        (
            "propose",
            item.id,
            item.request_digest,
            item.alternative.graph_effect_digest,
        )
    ]


def test_host_candidate_is_revalidated_by_the_local_control_plane() -> None:
    service = _Service()
    candidate = service.comparison.alternative.candidate.model_dump(mode="json")
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    response = client.post(
        "/api/v1/alternatives/validate",
        json={
            "subject_ref": "req:export",
            "modes": ["simplify"],
            "candidate": candidate,
        },
        headers=_headers(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "available"
    assert len(service.calls) == 1
    operation, subject_ref, modes, submitted = service.calls[0]
    assert (operation, subject_ref, modes) == ("validate", "req:export", ("simplify",))
    assert submitted.model_dump(mode="json") == candidate


def test_packaged_browser_labels_alternatives_as_projected_not_approved() -> None:
    assets = Path("src/intent_engineering/control_plane/assets")
    script = (assets / "app.js").read_text(encoding="utf-8")

    assert "Suggest alternatives" in script
    assert "Current approved requirement" in script
    assert "Projected alternative — not approved" in script
    assert "Create review proposal" in script
    assert "Validate host candidate locally" in script
    assert 'actionButton("Validate host candidate"' in script
    assert "Weakened outcome" in script
    assert "Removed constraint" in script
    assert "Preserved intent" in script
    assert "Exact graph effect" in script
    assert "localStorage" not in script
    assert "innerHTML" not in script


def test_browser_requests_alternatives_only_after_explicit_click_and_stages_review() -> None:
    result = _run(
        r"""
function dim(score) { return { dimension: "requirement_coverage", applicability: "required",
  score, health: "orange", confidence: 80, passed: [], failed: [], evidence_refs: [],
  related_refs: [], recommended_next_action: "Clarify the requirement" }; }
function card(score) { return { node_id: "req:export", node_type: "requirement",
  robustness: score, confidence: 80, health: "orange", worst_dimension: "requirement_coverage",
  dimensions: [dim(score)], blocking_case_refs: [], recommended_next_action: "Clarify the requirement",
  projected_robustness: null, projected_confidence: null }; }
const current = card(55); const projected = card(78);
const assessment = { schema_version: 1, assessment: { schema_version: 1,
  project_id: "project", graph_id: "graph", graph_version: 1,
  graph_digest: "sha256:" + "1".repeat(64), evidence_digest: "sha256:" + "2".repeat(64),
  ingestion_digest: "sha256:" + "3".repeat(64), case_digest: "sha256:" + "4".repeat(64),
  clarification_digest: "sha256:" + "5".repeat(64), history_digest: "sha256:" + "6".repeat(64),
  policy_digest: "sha256:" + "7".repeat(64), snapshot_digest: "sha256:" + "8".repeat(64),
  principal_projection_digest: "sha256:" + "9".repeat(64), generated_at: "2026-09-02T00:00:00Z",
  project: { project_id: "project", robustness: 55, confidence: 80, health: "orange",
    branch_ids: [], contributing_node_ids: ["req:export"], contribution_weights: {} },
  branches: [], nodes: [current], gaps: [], warnings: [], assessment_complete: true },
  focus: null, page: { rows: [current], next_cursor: null } };
const digest = "sha256:" + "a".repeat(64);
const effectDigest = "sha256:" + "b".repeat(64);
const comparisonId = "alternative-comparison:sha256:" + "c".repeat(64);
const alternatives = { schema_version: 1, request_digest: digest, status: "available",
  alternatives: [{ comparison_id: comparisonId, comparison: { schema_version: 1,
    current_state: "approved", projected_state: "projected", request_digest: digest,
    snapshot_digest: "sha256:" + "8".repeat(64), policy_digest: "sha256:" + "7".repeat(64),
    principal_projection_digest: "sha256:" + "9".repeat(64), alternative: { schema_version: 1,
      request_digest: digest, graph_effect_digest: effectDigest, candidate: { schema_version: 1,
        subject_ref: "req:export", proposed_text: "Provide CSV on demand.", mode: "simplify",
        preserved_outcome_refs: ["outcome:portable"], weakened_outcomes: [], constraint_effects: [],
        supporting_evidence_refs: ["evidence:visible"], complexity: { schema_version: 1,
          implementation: "low", operations: "low" }, complexity_rationale: "No scheduler.",
        risks: ["User initiates download."], tradeoffs: ["Less automation."],
        assumptions: ["On-demand is sufficient."], graph_effects: [] } },
    current_scorecard: current, projected_scorecard: projected, dimension_deltas: [] } }] };

respond(take("/api/v1/status"), statusProjection); await settle();
respond(take("/api/v1/development/observation"), { schema_version: 1, command_ids: [] }); await settle();
button("Open graph assessment").click(); await settle();
respond(take("/api/v1/assessment"), assessment); await settle();
const callsBeforeClick = calls.filter((call) => call.path === "/api/v1/alternatives").length;
button("Suggest alternatives").click(); await settle();
const request = take("/api/v1/alternatives");
const requestBody = JSON.parse(request.init.body);
respond(request, alternatives); await settle();
const comparisonText = app.textContent;
button("Create review proposal").click(); await settle();
const proposalRequest = take("/api/v1/alternatives/propose");
const proposalBody = JSON.parse(proposalRequest.init.body);
respond(proposalRequest, { schema_version: 1, proposal_id: "proposal:sha256:" + "d".repeat(64),
  status: "pending_review" }); await settle();
const preview = take("/api/v1/proposals/proposal%3Asha256%3A" + "d".repeat(64));
process.stdout.write(JSON.stringify({ callsBeforeClick, requestBody, proposalBody, comparisonText,
  previewPath: preview.path }));
"""
    )

    assert result["callsBeforeClick"] == 0
    assert result["requestBody"] == {
        "subject_ref": "req:export",
        "modes": ["simplify", "change_approach"],
    }
    assert "Current approved requirement" in result["comparisonText"]
    assert "Projected alternative — not approved" in result["comparisonText"]
    assert result["proposalBody"] == {
        "alternative_id": "alternative-comparison:sha256:" + "c" * 64,
        "expected_request_digest": "sha256:" + "a" * 64,
        "expected_effect_digest": "sha256:" + "b" * 64,
    }
    assert result["previewPath"].startswith("/api/v1/proposals/")
