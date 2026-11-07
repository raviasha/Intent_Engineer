"""Runtime coverage for the packaged synchronized assessment UI."""

from __future__ import annotations

import pytest

from tests.e2e.test_intent_dev_web_runtime import _run

_FIXTURE = r"""
function dimension(name, score, confidence, health, action = null) {
  return { dimension: name, applicability: "required", score, health, confidence,
    passed: [], failed: health === "red" ? [{ rule_id: `rubric:v1:${name}:gap`, points: 12,
      severity: "red", explanation: "Close the visible gap", references: [] }] : [],
    evidence_refs: [], related_refs: [], recommended_next_action: action };
}
function scorecard(id, type, robustness, confidence, health, worst, projected, action = null) {
  return { node_id: id, node_type: type, robustness, confidence, health, worst_dimension: worst,
    dimensions: [dimension(worst, health === "red" ? 40 : robustness, confidence, health, action)],
    blocking_case_refs: [], recommended_next_action: action,
    projected_robustness: projected, projected_confidence: projected === null ? null : confidence };
}
const red = scorecard("req:csv", "REQUIREMENT", 88, 82, "red", "test_verification", 93,
  "Add current negative-path tests");
const green = scorecard("intent:export", "PRODUCT_INTENT", 91, 90, "green", "intent_clarity", 95);
const hostile = scorecard("req:</script><img src=x>", "TEST", 65, 60, "orange", "freshness", null,
  "Review </script><img src=x onerror=alert(1)>");
function assessmentResponse(rows, nextCursor = null) {
  return { schema_version: 1, assessment: { schema_version: 1, project_id: "project",
    graph_id: "graph:project", graph_version: 7, snapshot_digest: "sha256:snapshot",
    project: { project_id: "project", robustness: 49, confidence: 82, health: "red" },
    branches: [{ branch_id: "intent:export", root_node_id: "intent:export",
      node_ids: ["intent:export", "req:csv"], robustness: 49, confidence: 82, health: "red" }],
    nodes: [red, green, hostile], gaps: [], warnings: [], assessment_complete: true },
    focus: null, page: { rows, next_cursor: nextCursor } };
}
function focusedAssessmentResponse(node, rows = [node]) {
  const response = assessmentResponse(rows);
  response.focus = { reference: node.node_id, node, branch: null };
  return response;
}
async function openAssessment() {
  respond(take("/api/v1/status"), statusProjection); await settle();
  respond(take("/api/v1/development/observation"), { schema_version: 1, command_ids: [] }); await settle();
  button("Open graph assessment").click(); await settle();
}
function byId(id) { return walk(app).find((node) => node.id === id); }
function assessmentItems(role) {
  return walk(app).filter((node) => node.attributes?.get("data-assessment-role") === role);
}
function assessmentItem(role, id) {
  return assessmentItems(role).find((node) => node.dataset.nodeId === id);
}
function key(node, value) {
  return node.handlers.get("keydown")?.({ key: value, preventDefault() {} });
}
const assessmentScrolls = [];
function installAssessmentFocusProbe() {
  document.getElementById = (id) => id === "app" ? app : id === "status" ? status
    : walk(app).find((node) => node.id === id) || null;
  Element.prototype.scrollIntoView = function(options) {
    this.scrollOptions = options;
    assessmentScrolls.push({ role: this.attributes.get("data-assessment-role"),
      nodeId: this.dataset.nodeId, options });
  };
}
"""


def test_graph_table_and_detail_share_selection_and_server_scorecard_objects() -> None:
    """Catches divergent score copies, one-way selection, or unsafe assessment rendering."""
    result = _run(
        _FIXTURE
        + r"""
const parsedAssessments = [];
const parse = JSON.parse.bind(JSON);
JSON.parse = (value) => { const parsed = parse(value); if (parsed?.assessment) parsedAssessments.push(parsed); return parsed; };
installAssessmentFocusProbe();
await openAssessment();
respond(take("/api/v1/assessment"), assessmentResponse([red, green, hostile])); await settle();

assessmentItem("graph", "req:csv").click(); await settle();
const graphSelectedRow = assessmentItem("table", "req:csv").attributes.get("aria-selected");
const graphClickFocus = { role: focused.attributes.get("data-assessment-role"), nodeId: focused.dataset.nodeId };
const firstHealth = byId("assessment-detail-health").textContent;
const firstWorst = byId("assessment-detail-worst").textContent;
const firstScore = byId("assessment-detail-score").textContent;
const worstOnGraph = assessmentItem("graph", "req:csv").textContent;

key(assessmentItem("table", "intent:export"), "Enter"); await settle();
const tableSelectedGraph = assessmentItem("graph", "intent:export").attributes.get("aria-pressed");
const tableKeyFocus = { role: focused.attributes.get("data-assessment-role"), nodeId: focused.dataset.nodeId };
const keyboardGraph = assessmentItem("graph", "req:csv");
key(keyboardGraph, " "); keyboardGraph.click(); await settle();
const keyboardSelectedRow = assessmentItem("table", "req:csv").attributes.get("aria-selected");
const graphKeyFocus = { role: focused.attributes.get("data-assessment-role"), nodeId: focused.dataset.nodeId };

const shared = parsedAssessments[0].assessment.nodes.find((node) => node.node_id === "req:csv");
shared.robustness = 77;
button("Approved scores").click(); await settle();
const sharedGraph = assessmentItem("graph", "req:csv").textContent;
const sharedRow = assessmentItem("table", "req:csv").textContent;
const sharedDetail = byId("assessment-detail-score").textContent;

button("Projected scores").click(); await settle();
const projectedGraph = assessmentItem("graph", "req:csv").textContent;
const projectedDetail = byId("assessment-detail-score").textContent;
const projectedDetailHealth = byId("assessment-detail-health").textContent;
const projectedLabel = byId("assessment-overlay-label").textContent;
const projectedTable = assessmentItem("table", "req:csv").textContent;
const projectedHead = walk(app).find((node) => node.tagName === "thead").textContent;
const projectedUnavailable = assessmentItem("graph", "req:</script><img src=x>").textContent;
const projectedProject = walk(app).find((node) => node.className.includes("assessment-project-summary")).textContent;
button("Approved scores").click(); await settle();
const approvedLabel = byId("assessment-overlay-label").textContent;
const approvedProject = walk(app).find((node) => node.className.includes("assessment-project-summary")).textContent;
const tags = walk(app).map((node) => node.tagName);

process.stdout.write(JSON.stringify({ graphSelectedRow, firstHealth, firstWorst, firstScore,
  graphClickFocus, tableSelectedGraph, tableKeyFocus, keyboardSelectedRow, graphKeyFocus,
  worstOnGraph, sharedGraph, sharedRow, sharedDetail, projectedGraph, projectedDetail,
  projectedDetailHealth, projectedLabel, projectedTable, projectedHead, projectedUnavailable,
  projectedProject, approvedProject, scrolls: assessmentScrolls, approvedLabel, tags, app: app.textContent }));
"""
    )

    assert result["graphSelectedRow"] == "true"
    assert result["firstHealth"] == "Red"
    assert result["firstWorst"] == "test_verification"
    assert result["firstScore"] == "88"
    assert result["graphClickFocus"] == {"role": "table", "nodeId": "req:csv"}
    assert result["tableSelectedGraph"] == "true"
    assert result["tableKeyFocus"] == {"role": "graph", "nodeId": "intent:export"}
    assert result["keyboardSelectedRow"] == "true"
    assert result["graphKeyFocus"] == {"role": "table", "nodeId": "req:csv"}
    assert "Worst dimension" in result["worstOnGraph"]
    assert "test_verification" in result["worstOnGraph"]
    assert "77" in result["sharedGraph"]
    assert "77" in result["sharedRow"]
    assert result["sharedDetail"] == "77"
    assert "Projected score 93" in result["projectedGraph"]
    assert result["projectedDetail"] == "93"
    assert result["projectedDetailHealth"] == "Projected health unavailable"
    assert result["projectedLabel"] == "Projected scores"
    assert "Projected score" in result["projectedHead"]
    assert "Projected confidence" in result["projectedHead"]
    assert "Projected health" in result["projectedHead"]
    assert "93" in result["projectedTable"]
    assert "Projected health unavailable" in result["projectedTable"]
    assert "Projected score N/A — unavailable" in result["projectedUnavailable"]
    assert "Projected project robustness N/A — unavailable" in result["projectedProject"]
    assert "Projected project health unavailable" in result["projectedProject"]
    assert "Approved project robustness 49" in result["approvedProject"]
    assert "Approved project health Red" in result["approvedProject"]
    assert result["scrolls"] == [
        {"role": "table", "nodeId": "req:csv", "options": {"block": "nearest"}},
        {"role": "graph", "nodeId": "intent:export", "options": {"block": "nearest"}},
        {"role": "table", "nodeId": "req:csv", "options": {"block": "nearest"}},
    ]
    assert result["approvedLabel"] == "Approved scores"
    assert "script" not in result["tags"]
    assert "img" not in result["tags"]
    assert "Review </script><img src=x onerror=alert(1)>" in result["app"]


def test_filters_keyboard_and_pagination_render_only_server_bounded_pages() -> None:
    """Catches client expansion, unbounded table rendering, or local pagination."""
    result = _run(
        _FIXTURE
        + r"""
await openAssessment();
respond(take("/api/v1/assessment"), assessmentResponse([red, green, hostile], "page:" + "a".repeat(64) + ":100")); await settle();

const health = byId("assessment-filter-health"); health.value = "red";
health.handlers.get("change")({ target: health }); await settle();
const redGraphIds = assessmentItems("graph").map((node) => node.dataset.nodeId);
const redTableIds = assessmentItems("table").map((node) => node.dataset.nodeId);

health.value = ""; health.handlers.get("change")({ target: health }); await settle();
const type = byId("assessment-filter-type"); type.value = "TEST";
type.handlers.get("change")({ target: type }); await settle();
const testGraphIds = assessmentItems("graph").map((node) => node.dataset.nodeId);

type.value = ""; type.handlers.get("change")({ target: type }); await settle();
button("Next scorecard page").click(); await settle();
const nextPath = "/api/v1/assessment?cursor=page%3A" + "a".repeat(64) + "%3A100";
respond(take(nextPath), assessmentResponse([green])); await settle();
const nextTableIds = assessmentItems("table").map((node) => node.dataset.nodeId);
const nextGraphIds = assessmentItems("graph").map((node) => node.dataset.nodeId);
const nextDisabled = button("Next scorecard page").disabled;

process.stdout.write(JSON.stringify({ redGraphIds, redTableIds, testGraphIds, nextTableIds,
  nextGraphIds, nextDisabled, calls: calls.filter((request) => request.path.startsWith("/api/v1/assessment")).map((request) => request.path) }));
"""
    )

    assert result["redGraphIds"] == ["req:csv"]
    assert result["redTableIds"] == ["req:csv"]
    assert result["testGraphIds"] == ["req:</script><img src=x>"]
    assert result["nextTableIds"] == ["intent:export"]
    assert result["nextGraphIds"] == [
        "req:csv",
        "intent:export",
        "req:</script><img src=x>",
    ]
    assert result["nextDisabled"] is True
    assert result["calls"] == [
        "/api/v1/assessment",
        "/api/v1/assessment?cursor=page%3A" + "a" * 64 + "%3A100",
    ]


def test_off_page_graph_selection_uses_server_focus_page_then_focuses_row() -> None:
    """Catches graph selection that leaves its reciprocal table row off page."""
    result = _run(
        _FIXTURE
        + r"""
installAssessmentFocusProbe();
await openAssessment();
respond(take("/api/v1/assessment"), assessmentResponse([green])); await settle();

assessmentItem("graph", "req:csv").click(); await settle();
const focusPath = "/api/v1/assessment?focus=req%3Acsv";
respond(take(focusPath), focusedAssessmentResponse(red)); await settle();
const selectedRow = assessmentItem("table", "req:csv");
const afterGraph = { selected: selectedRow.attributes.get("aria-selected"),
  focusedRole: focused.attributes.get("data-assessment-role"), focusedNode: focused.dataset.nodeId };
key(selectedRow, "Enter"); await settle();
const afterTable = { focusedRole: focused.attributes.get("data-assessment-role"),
  focusedNode: focused.dataset.nodeId };
process.stdout.write(JSON.stringify({ afterGraph, afterTable, scrolls: assessmentScrolls,
  calls: calls.filter((request) => request.path.startsWith("/api/v1/assessment")).map((request) => request.path) }));
"""
    )

    assert result["afterGraph"] == {
        "selected": "true",
        "focusedRole": "table",
        "focusedNode": "req:csv",
    }
    assert result["afterTable"] == {"focusedRole": "graph", "focusedNode": "req:csv"}
    assert result["scrolls"] == [
        {"role": "table", "nodeId": "req:csv", "options": {"block": "nearest"}},
        {"role": "graph", "nodeId": "req:csv", "options": {"block": "nearest"}},
    ]
    assert result["calls"] == ["/api/v1/assessment", "/api/v1/assessment?focus=req%3Acsv"]


def test_native_graph_keyboard_activation_emits_one_focus_request() -> None:
    """Catches custom button key handling duplicating the browser's native click."""
    result = _run(
        _FIXTURE
        + r"""
installAssessmentFocusProbe();
await openAssessment();
respond(take("/api/v1/assessment"), assessmentResponse([green])); await settle();
const graph = assessmentItem("graph", "req:csv");
const customKeydown = graph.handlers.get("keydown");
if (customKeydown) customKeydown({ key: "Enter", preventDefault() {} });
graph.click(); await settle();
const focusPath = "/api/v1/assessment?focus=req%3Acsv";
const focusCalls = calls.filter((request) => request.path === focusPath).length;
while (pending.some((request) => request.path === focusPath)) {
  respond(take(focusPath), focusedAssessmentResponse(red));
}
await settle();
process.stdout.write(JSON.stringify({ hasCustomKeydown: Boolean(customKeydown),
  focusCalls,
  selected: assessmentItem("table", "req:csv").attributes.get("aria-selected") }));
"""
    )

    assert result == {"hasCustomKeydown": False, "focusCalls": 1, "selected": "true"}


def test_newer_page_response_wins_without_committing_pending_focus_selection() -> None:
    """Catches a pending off-page selection leaking across a newer page response."""
    result = _run(
        _FIXTURE
        + r"""
installAssessmentFocusProbe();
await openAssessment();
const cursor = "page:" + "b".repeat(64) + ":100";
respond(take("/api/v1/assessment"), assessmentResponse([green], cursor)); await settle();

assessmentItem("graph", "req:csv").click(); await settle();
const focusPath = "/api/v1/assessment?focus=req%3Acsv";
const pendingFocus = take(focusPath);
button("Next scorecard page").click(); await settle();
const nextPath = "/api/v1/assessment?cursor=page%3A" + "b".repeat(64) + "%3A100";
respond(take(nextPath), assessmentResponse([hostile])); await settle();
const afterNext = {
  graphSelected: assessmentItems("graph").filter((node) => node.attributes.get("aria-pressed") === "true").map((node) => node.dataset.nodeId),
  tableSelected: assessmentItems("table").filter((node) => node.attributes.get("aria-selected") === "true").map((node) => node.dataset.nodeId),
  detail: byId("assessment-detail").textContent,
};

respond(pendingFocus, focusedAssessmentResponse(red)); await settle();
const afterStaleFocus = {
  graphSelected: assessmentItems("graph").filter((node) => node.attributes.get("aria-pressed") === "true").map((node) => node.dataset.nodeId),
  tableIds: assessmentItems("table").map((node) => node.dataset.nodeId),
  tableSelected: assessmentItems("table").filter((node) => node.attributes.get("aria-selected") === "true").map((node) => node.dataset.nodeId),
  detail: byId("assessment-detail").textContent,
  scrolls: assessmentScrolls,
};
process.stdout.write(JSON.stringify({ afterNext, afterStaleFocus,
  calls: calls.filter((request) => request.path.startsWith("/api/v1/assessment")).map((request) => request.path) }));
"""
    )

    assert result["afterNext"]["graphSelected"] == []
    assert result["afterNext"]["tableSelected"] == []
    assert "Select a graph node or table row" in result["afterNext"]["detail"]
    assert result["afterStaleFocus"]["graphSelected"] == []
    assert result["afterStaleFocus"]["tableIds"] == ["req:</script><img src=x>"]
    assert result["afterStaleFocus"]["tableSelected"] == []
    assert "Select a graph node or table row" in result["afterStaleFocus"]["detail"]
    assert result["afterStaleFocus"]["scrolls"] == []
    assert result["calls"] == [
        "/api/v1/assessment",
        "/api/v1/assessment?focus=req%3Acsv",
        "/api/v1/assessment?cursor=page%3A" + "b" * 64 + "%3A100",
    ]


@pytest.mark.parametrize(
    "hostile_mutation",
    (
        "focused.focus = null;",
        'focused.focus.reference = "req:other";',
        "focused.focus.node = green;",
        "focused.page.rows = [red, red];",
    ),
)
def test_hostile_focus_response_fails_closed_before_shared_state_mutation(
    hostile_mutation: str,
) -> None:
    """Catches unauthenticated focus payloads leaving a half-selected UI."""
    result = _run(
        _FIXTURE
        + rf"""
await openAssessment();
respond(take("/api/v1/assessment"), assessmentResponse([green])); await settle();
assessmentItem("graph", "req:csv").click(); await settle();
const focused = focusedAssessmentResponse(red);
{hostile_mutation}
respond(take("/api/v1/assessment?focus=req%3Acsv"), focused); await settle();
process.stdout.write(JSON.stringify({{ graphCount: assessmentItems("graph").length,
  tableCount: assessmentItems("table").length, app: app.textContent, status: status.textContent }}));
"""
    )

    assert result["graphCount"] == 0
    assert result["tableCount"] == 0
    assert "Assessment unavailable" in result["app"]
    assert "Assessment is unavailable" in result["status"]
    assert "Graph assessment updated" not in result["status"]


def test_duplicate_assessment_page_rows_are_rejected_before_rendering() -> None:
    """Catches duplicate table identities bypassing the canonical scorecard map."""
    result = _run(
        _FIXTURE
        + r"""
await openAssessment();
respond(take("/api/v1/assessment"), assessmentResponse([red, red])); await settle();
process.stdout.write(JSON.stringify({ graphCount: assessmentItems("graph").length,
  tableCount: assessmentItems("table").length, app: app.textContent, status: status.textContent }));
"""
    )

    assert result["graphCount"] == 0
    assert result["tableCount"] == 0
    assert "Assessment unavailable" in result["app"]
    assert "Assessment is unavailable" in result["status"]


def test_oversized_assessment_is_rejected_before_dom_rendering() -> None:
    """Catches a hostile server response driving more than the public DOM bounds."""
    result = _run(
        _FIXTURE
        + r"""
await openAssessment();
const oversized = assessmentResponse([]);
oversized.assessment.nodes = Array.from({ length: 2001 }, (_, index) =>
  scorecard(`req:${index}`, "REQUIREMENT", 80, 80, "green", "intent_clarity", null));
respond(take("/api/v1/assessment"), oversized); await settle();
process.stdout.write(JSON.stringify({ graphCount: assessmentItems("graph").length,
  tableCount: assessmentItems("table").length, app: app.textContent, status: status.textContent }));
"""
    )

    assert result["graphCount"] == 0
    assert result["tableCount"] == 0
    assert "Assessment unavailable" in result["app"]
    assert "Assessment is unavailable" in result["status"]


def test_combined_assessment_checks_are_bounded_before_rendering() -> None:
    """Catches passed and failed checks evading the shared per-dimension bound."""
    result = _run(
        _FIXTURE
        + r"""
await openAssessment();
const excessiveChecks = assessmentResponse([red]);
const target = excessiveChecks.assessment.nodes[0].dimensions[0];
target.passed = Array.from({ length: 64 }, (_, index) => ({ rule_id: `pass:${index}`,
  points: 0, severity: "green", explanation: "Passed", references: [] }));
respond(take("/api/v1/assessment"), excessiveChecks); await settle();
process.stdout.write(JSON.stringify({ graphCount: assessmentItems("graph").length,
  tableCount: assessmentItems("table").length, app: app.textContent, status: status.textContent }));
"""
    )

    assert result["graphCount"] == 0
    assert result["tableCount"] == 0
    assert "Assessment unavailable" in result["app"]
    assert "Assessment is unavailable" in result["status"]
