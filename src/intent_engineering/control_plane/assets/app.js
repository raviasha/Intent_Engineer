(() => {
  "use strict";

  const api = Object.freeze({
    status: "/api/v1/status",
    inbox: "/api/v1/inbox",
    proposal: "/api/v1/proposals/",
    clarificationAnswerPreview: "/api/v1/clarifications/answers/preview",
    clarificationAnswerDiscard: "/api/v1/clarifications/answers/discard",
    registrationOptions: "/api/v1/webauthn/register/options",
    registrationVerify: "/api/v1/webauthn/register/verify",
    teamEnrollment: "/api/v1/team/enrollment",
    membership: "/api/v1/team/membership",
    teamEnrollmentOptions: "/api/v1/team/enrollment/options",
    teamEnrollmentVerify: "/api/v1/team/enrollment/verify",
    teamEnrollmentCancel: "/api/v1/team/enrollment/cancel",
    teamPublicationPreview: "/api/v1/team/publication/preview",
    teamSetup: "/api/v1/team/setup",
    teamSetupInspect: "/api/v1/team/setup/inspect",
    teamSetupEnroll: "/api/v1/team/setup/enroll",
    teamSetupProtectionPreview: "/api/v1/team/setup/protection-preview",
    teamSetupPublicationPreview: "/api/v1/team/setup/publication-preview",
    teamSetupOptions: "/api/v1/team/setup/options",
    teamSetupVerify: "/api/v1/team/setup/verify",
    teamSetupCancel: "/api/v1/team/setup/cancel",
    decisionOptions: "/api/v1/decisions/options",
    decisionVerify: "/api/v1/decisions/verify",
    developmentObservation: "/api/v1/development/observation",
    reviewedTests: "/api/v1/development/tests/run",
    assessment: "/api/v1/assessment",
    enrichmentStart: "/api/v1/enrichment/start",
    enrichmentCurrent: "/api/v1/enrichment/current",
    enrichmentAnswer: "/api/v1/enrichment/answer",
    enrichmentSkip: "/api/v1/enrichment/skip",
    enrichmentPause: "/api/v1/enrichment/pause",
    enrichmentResume: "/api/v1/enrichment/resume",
    enrichmentPropose: "/api/v1/enrichment/propose",
    browserBootstrap: "/_intent/browser/bootstrap",
  });
  const viewNames = new Set(["home", "onboarding", "inbox", "proposal", "enrichment", "team_state", "assessment"]);
  const MAX_ASSESSMENT_NODES = 2000;
  const MAX_ASSESSMENT_ROWS = 100;
  const MAX_ASSESSMENT_DIMENSIONS = 7;
  const MAX_ASSESSMENT_CHECKS = 64;
  const assessmentHealth = Object.freeze({
    green: Object.freeze({ label: "Green", icon: "✓" }),
    orange: Object.freeze({ label: "Orange", icon: "!" }),
    red: Object.freeze({ label: "Red", icon: "×" }),
    unassessed: Object.freeze({ label: "Unassessed", icon: "?" }),
  });
  const app = document.getElementById("app");
  const statusRegion = document.getElementById("status");
  const navigation = Array.from(document.querySelectorAll("[data-view]"));
  const state = {
    view: "home",
    status: null,
    inbox: null,
    proposalId: null,
    preview: null,
    selectedNodeIds: [],
    proposalGeneration: 0,
    developmentObservation: null,
    teamEnrollment: null,
    membership: null,
    teamPublication: null,
    teamSetup: null,
    teamSetupPreview: null,
    teamSetupResult: null,
  };
  const assessmentState = Object.seal({
    report: null,
    selectedNodeId: null,
    filters: Object.seal({ health: "", dimension: "", type: "" }),
    pageCursor: null,
    pageFocus: null,
    nextCursor: null,
    pageRows: [],
    overlay: "approved",
    unavailable: false,
    generation: 0,
  });
  const enrichmentState = Object.seal({
    session: null,
    question: null,
    unavailable: false,
    generation: 0,
  });
  let csrfToken = readCsrfBootstrap();

  function readCsrfBootstrap() {
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const candidate = fragment.get("csrf");
    history.replaceState(null, "", window.location.pathname);
    return typeof candidate === "string" && /^[A-Za-z0-9_-]{1,512}$/.test(candidate)
      ? candidate
      : "";
  }

  function announce(message) {
    statusRegion.textContent = message;
  }

  async function exchangeCsrfBootstrap() {
    if (!csrfToken) {
      return;
    }
    const response = await fetch(api.browserBootstrap, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token: csrfToken }),
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    if (!response.ok) {
      csrfToken = "";
      throw new Error("Local review bootstrap is unavailable. Restart intent dev.");
    }
  }

  function isWrite(method) {
    return method !== "GET";
  }

  async function fetchJson(path, options = {}) {
    const method = options.method || "GET";
    const headers = new Headers(options.headers || {});
    headers.set("accept", "application/json");
    if (isWrite(method)) {
      headers.set("content-type", "application/json");
      if (!csrfToken) {
        throw new Error("Local review bootstrap is unavailable. Restart intent dev.");
      }
      headers.set("x-intent-csrf", csrfToken);
    }
    const response = await fetch(path, {
      method,
      headers,
      body: options.body,
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    let body = "";
    try {
      body = await response.text();
      if (!response.ok) {
        throw new Error(response.status === 503 ? "challenge_unavailable" : "request_rejected");
      }
      return JSON.parse(body);
    } finally {
      body = "";
    }
  }

  function addText(parent, tag, value) {
    const element = document.createElement(tag);
    element.textContent = String(value);
    parent.append(element);
    return element;
  }

  function addValue(parent, label, value) {
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    if (Array.isArray(value)) {
      const list = document.createElement("ul");
      for (const item of value) {
        const entry = document.createElement("li");
        if (item !== null && typeof item === "object") {
          const nested = document.createElement("dl");
          for (const [key, nestedValue] of Object.entries(item)) {
            addValue(nested, key, nestedValue);
          }
          entry.append(nested);
        } else {
          entry.textContent = String(item);
        }
        list.append(entry);
      }
      description.append(list);
    } else if (value !== null && typeof value === "object") {
      const nested = document.createElement("dl");
      for (const [key, nestedValue] of Object.entries(value)) {
        addValue(nested, key, nestedValue);
      }
      description.append(nested);
    } else {
      description.textContent = String(value);
    }
    parent.append(term, description);
  }

  function addProjection(panel, projection) {
    const details = document.createElement("dl");
    for (const [key, value] of Object.entries(projection || {})) {
      addValue(details, key, value);
    }
    panel.append(details);
  }

  function actionButton(label, handler, className = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (className) {
      button.className = className;
    }
    button.addEventListener("click", handler);
    return button;
  }

  function panel(title) {
    const section = document.createElement("section");
    section.className = "panel";
    section.setAttribute("aria-labelledby", "view-title");
    addText(section, "h2", title).id = "view-title";
    return section;
  }

  function boundedAssessmentArray(value, maximum, label) {
    if (!Array.isArray(value) || value.length > maximum) {
      throw new Error(`Invalid bounded assessment ${label}.`);
    }
    return value;
  }

  function assessmentScore(value) {
    return Number.isInteger(value) && value >= 0 && value <= 100 ? value : null;
  }

  function validateAssessmentNode(node) {
    if (
      !node ||
      typeof node !== "object" ||
      typeof node.node_id !== "string" ||
      node.node_id.length === 0 ||
      node.node_id.length > 512 ||
      typeof node.node_type !== "string" ||
      node.node_type.length > 128 ||
      !assessmentHealth[node.health]
    ) {
      throw new Error("Invalid assessment scorecard.");
    }
    if (node.robustness !== null && assessmentScore(node.robustness) === null) {
      throw new Error("Invalid assessment robustness.");
    }
    if (node.confidence !== null && assessmentScore(node.confidence) === null) {
      throw new Error("Invalid assessment confidence.");
    }
    if (
      node.projected_robustness !== null &&
      assessmentScore(node.projected_robustness) === null
    ) {
      throw new Error("Invalid projected robustness.");
    }
    if (
      node.projected_confidence !== null &&
      assessmentScore(node.projected_confidence) === null
    ) {
      throw new Error("Invalid projected confidence.");
    }
    const dimensions = boundedAssessmentArray(
      node.dimensions,
      MAX_ASSESSMENT_DIMENSIONS,
      "dimensions"
    );
    for (const dimension of dimensions) {
      if (!dimension || typeof dimension !== "object" || typeof dimension.dimension !== "string") {
        throw new Error("Invalid assessment dimension.");
      }
      boundedAssessmentArray(dimension.passed, MAX_ASSESSMENT_CHECKS, "passed checks");
      boundedAssessmentArray(dimension.failed, MAX_ASSESSMENT_CHECKS, "failed checks");
      if (dimension.passed.length + dimension.failed.length > MAX_ASSESSMENT_CHECKS) {
        throw new Error("Too many assessment checks.");
      }
      if (dimension.score !== null && assessmentScore(dimension.score) === null) {
        throw new Error("Invalid assessment dimension score.");
      }
      if (dimension.confidence !== null && assessmentScore(dimension.confidence) === null) {
        throw new Error("Invalid assessment dimension confidence.");
      }
    }
    boundedAssessmentArray(node.blocking_case_refs, MAX_ASSESSMENT_CHECKS, "blocking cases");
    return node;
  }

  function exactAssessmentObject(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function acceptAssessmentResponse(payload, expectedFocus = null) {
    if (
      !payload ||
      typeof payload !== "object" ||
      !payload.assessment ||
      typeof payload.assessment !== "object" ||
      !payload.page ||
      typeof payload.page !== "object"
    ) {
      throw new Error("Invalid assessment response.");
    }
    const nodes = boundedAssessmentArray(
      payload.assessment.nodes,
      MAX_ASSESSMENT_NODES,
      "nodes"
    );
    const rows = boundedAssessmentArray(payload.page.rows, MAX_ASSESSMENT_ROWS, "rows");
    boundedAssessmentArray(
      payload.assessment.branches,
      MAX_ASSESSMENT_NODES,
      "branches"
    );
    boundedAssessmentArray(payload.assessment.gaps, MAX_ASSESSMENT_NODES, "gaps");
    boundedAssessmentArray(payload.assessment.warnings, MAX_ASSESSMENT_NODES, "warnings");
    const nodesById = new Map();
    for (const node of nodes) {
      validateAssessmentNode(node);
      if (nodesById.has(node.node_id)) {
        throw new Error("Duplicate assessment scorecard.");
      }
      nodesById.set(node.node_id, node);
    }
    const rowIds = new Set();
    const canonicalRows = rows.map((row) => {
      if (!row || typeof row !== "object" || !nodesById.has(row.node_id)) {
        throw new Error("Assessment page does not reference a scorecard.");
      }
      if (rowIds.has(row.node_id)) {
        throw new Error("Duplicate assessment page row.");
      }
      rowIds.add(row.node_id);
      const canonicalNode = nodesById.get(row.node_id);
      if (!exactAssessmentObject(row, canonicalNode)) {
        throw new Error("Assessment page row does not match its scorecard.");
      }
      return canonicalNode;
    });
    if (expectedFocus === null) {
      if (payload.focus !== null) {
        throw new Error("Unexpected assessment focus.");
      }
    } else {
      if (
        !payload.focus ||
        typeof payload.focus !== "object" ||
        payload.focus.reference !== expectedFocus ||
        !payload.focus.node ||
        typeof payload.focus.node !== "object" ||
        payload.focus.node.node_id !== expectedFocus
      ) {
        throw new Error("Assessment focus does not match the request.");
      }
      const canonicalFocus = nodesById.get(expectedFocus);
      if (
        !canonicalFocus ||
        !exactAssessmentObject(payload.focus.node, canonicalFocus) ||
        canonicalRows.filter((node) => node === canonicalFocus).length !== 1
      ) {
        throw new Error("Assessment focus is not one canonical page row.");
      }
      payload.focus.node = canonicalFocus;
    }
    const nextCursor = payload.page.next_cursor;
    if (nextCursor !== null && (typeof nextCursor !== "string" || nextCursor.length > 512)) {
      throw new Error("Invalid assessment cursor.");
    }
    return { report: payload.assessment, rows: canonicalRows, nextCursor };
  }

  function assessmentNode(nodeId) {
    if (!assessmentState.report || typeof nodeId !== "string") {
      return null;
    }
    return assessmentState.report.nodes.find((node) => node.node_id === nodeId) || null;
  }

  function displayedAssessmentScore(node) {
    return assessmentState.overlay === "projected"
      ? node.projected_robustness
      : node.robustness;
  }

  function displayedAssessmentConfidence(node) {
    return assessmentState.overlay === "projected"
      ? node.projected_confidence
      : node.confidence;
  }

  function scoreText(value) {
    return assessmentScore(value) === null ? "N/A" : String(value);
  }

  function assessmentOverlayName() {
    return assessmentState.overlay === "projected" ? "Projected" : "Approved";
  }

  function assessmentValueText(label, value) {
    if (assessmentScore(value) !== null) {
      return `${assessmentOverlayName()} ${label} ${value}`;
    }
    return assessmentState.overlay === "projected"
      ? `Projected ${label} N/A — unavailable`
      : `Approved ${label} N/A — unassessed`;
  }

  function displayedAssessmentHealth(node) {
    return assessmentState.overlay === "projected" ? "unassessed" : node.health;
  }

  function assessmentHealthText(node, subject = "") {
    const prefix = subject ? `${subject} ` : "";
    return assessmentState.overlay === "projected"
      ? `Projected ${prefix}health unavailable`
      : `Approved ${prefix}health ${assessmentHealth[node.health].label}`;
  }

  function focusAssessmentRepresentation(role, nodeId) {
    const nodes = role === "table"
      ? assessmentState.pageRows
      : assessmentState.report.nodes;
    const index = nodes.findIndex((node) => node.node_id === nodeId);
    if (index < 0) {
      return;
    }
    const candidate = document.getElementById(
      role === "table" ? `assessment-row-${index}` : `assessment-node-${index}`
    );
    if (
      !candidate ||
      candidate.dataset.nodeId !== nodeId ||
      candidate.dataset.assessmentRole !== role
    ) {
      return;
    }
    candidate.focus();
    if (typeof candidate.scrollIntoView === "function") {
      candidate.scrollIntoView({ block: "nearest" });
    }
  }

  async function selectAssessmentNode(nodeId, source) {
    if (!assessmentNode(nodeId)) {
      return;
    }
    if (
      source === "graph" &&
      !assessmentState.pageRows.some((node) => node.node_id === nodeId)
    ) {
      await loadAssessment(null, nodeId, "table", nodeId);
      return;
    }
    assessmentState.selectedNodeId = nodeId;
    render();
    focusAssessmentRepresentation(source === "table" ? "graph" : "table", nodeId);
  }

  function assessmentSelectionHandler(node, source) {
    return (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        void selectAssessmentNode(node.node_id, source);
      }
    };
  }

  function assessmentMatchesFilters(node) {
    const filters = assessmentState.filters;
    return (
      (!filters.health || node.health === filters.health) &&
      (!filters.type || node.node_type === filters.type) &&
      (!filters.dimension ||
        node.worst_dimension === filters.dimension ||
        node.dimensions.some((dimension) => dimension.dimension === filters.dimension))
    );
  }

  function addOption(select, value, label) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.append(option);
  }

  function assessmentFilter(id, label, values, selected, key) {
    const wrapper = document.createElement("label");
    wrapper.textContent = label;
    const select = document.createElement("select");
    select.id = id;
    addOption(select, "", `All ${label.toLowerCase()}`);
    for (const value of values) {
      addOption(select, value, value);
    }
    select.value = selected;
    select.addEventListener("change", (event) => {
      assessmentState.filters[key] = event.target.value;
      render();
    });
    wrapper.append(select);
    return wrapper;
  }

  function addAssessmentHealth(parent, health, label = null) {
    const display = assessmentHealth[health] || assessmentHealth.unassessed;
    const icon = addText(parent, "span", display.icon);
    icon.className = "assessment-health-icon";
    icon.setAttribute("aria-hidden", "true");
    const text = addText(parent, "span", label === null ? display.label : label);
    text.className = "assessment-health-label";
  }

  function assessmentClass(health, base) {
    return `${base} assessment-health health-${health}`;
  }

  function renderAssessmentGraph(section) {
    const graph = document.createElement("section");
    graph.className = "assessment-graph";
    graph.setAttribute("aria-labelledby", "assessment-graph-title");
    addText(graph, "h3", "Graph scorecards").id = "assessment-graph-title";
    addText(
      graph,
      "p",
      "This view renders the server-bounded canonical graph subset. Select a node to synchronize every view."
    );
    const items = document.createElement("div");
    items.className = "assessment-graph-items";
    assessmentState.report.nodes.forEach((node, index) => {
      if (!assessmentMatchesFilters(node)) {
        return;
      }
      const item = document.createElement("button");
      item.type = "button";
      item.id = `assessment-node-${index}`;
      item.dataset.nodeId = node.node_id;
      item.dataset.assessmentRole = "graph";
      item.setAttribute("data-assessment-role", "graph");
      item.setAttribute(
        "aria-pressed",
        String(assessmentState.selectedNodeId === node.node_id)
      );
      item.className = assessmentClass(
        displayedAssessmentHealth(node),
        "assessment-graph-node"
      );
      addAssessmentHealth(
        item,
        displayedAssessmentHealth(node),
        assessmentHealthText(node)
      );
      addText(item, "strong", node.node_id);
      addText(
        item,
        "span",
        assessmentValueText("score", displayedAssessmentScore(node))
      );
      addText(
        item,
        "span",
        assessmentValueText("confidence", displayedAssessmentConfidence(node))
      );
      const worst = document.createElement("span");
      const worstIcon = addText(worst, "span", "◆");
      worstIcon.className = "assessment-dimension-icon";
      worstIcon.setAttribute("aria-hidden", "true");
      addText(worst, "span", `Worst dimension (approved): ${node.worst_dimension || "N/A"}`);
      item.append(worst);
      item.addEventListener("click", () => void selectAssessmentNode(node.node_id, "graph"));
      items.append(item);
    });
    graph.append(items);
    section.append(graph);
  }

  function renderAssessmentTable(section) {
    const region = document.createElement("section");
    region.className = "assessment-table-region";
    region.setAttribute("aria-labelledby", "assessment-table-title");
    addText(region, "h3", "Scorecard table").id = "assessment-table-title";
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const heading = document.createElement("tr");
    for (const value of [
      "Node",
      "Type",
      `${assessmentOverlayName()} health`,
      `${assessmentOverlayName()} score`,
      `${assessmentOverlayName()} confidence`,
      "Worst dimension (approved)",
      "Next action",
    ]) {
      addText(heading, "th", value).setAttribute("scope", "col");
    }
    head.append(heading);
    const body = document.createElement("tbody");
    assessmentState.pageRows.forEach((node, index) => {
      if (!assessmentMatchesFilters(node)) {
        return;
      }
      const row = document.createElement("tr");
      row.id = `assessment-row-${index}`;
      row.dataset.nodeId = node.node_id;
      row.dataset.assessmentRole = "table";
      row.setAttribute("data-assessment-role", "table");
      row.setAttribute("tabindex", "0");
      row.setAttribute(
        "aria-selected",
        String(assessmentState.selectedNodeId === node.node_id)
      );
      row.className = assessmentClass(
        displayedAssessmentHealth(node),
        "assessment-table-row"
      );
      addText(row, "th", node.node_id).setAttribute("scope", "row");
      addText(row, "td", node.node_type);
      const health = document.createElement("td");
      addAssessmentHealth(
        health,
        displayedAssessmentHealth(node),
        assessmentHealthText(node)
      );
      row.append(health);
      addText(
        row,
        "td",
        assessmentValueText("score", displayedAssessmentScore(node))
      );
      addText(
        row,
        "td",
        assessmentValueText("confidence", displayedAssessmentConfidence(node))
      );
      addText(row, "td", node.worst_dimension || "N/A");
      addText(row, "td", node.recommended_next_action || "No action suggested");
      row.addEventListener("click", () => void selectAssessmentNode(node.node_id, "table"));
      row.addEventListener("keydown", assessmentSelectionHandler(node, "table"));
      body.append(row);
    });
    table.append(head, body);
    region.append(table);
    const pagination = document.createElement("div");
    pagination.className = "assessment-pagination";
    const first = actionButton("First scorecard page", () =>
      loadAssessment(null, assessmentState.pageFocus)
    );
    first.disabled = assessmentState.pageCursor === null;
    const next = actionButton("Next scorecard page", () =>
      loadAssessment(assessmentState.nextCursor, assessmentState.pageFocus)
    );
    next.disabled = assessmentState.nextCursor === null;
    pagination.append(first, next);
    region.append(pagination);
    section.append(region);
  }

  function renderAssessmentDetail(section) {
    const node = assessmentNode(assessmentState.selectedNodeId);
    const detail = document.createElement("aside");
    detail.id = "assessment-detail";
    detail.className = "assessment-detail";
    detail.setAttribute("aria-labelledby", "assessment-detail-title");
    addText(detail, "h3", "Selected scorecard detail").id = "assessment-detail-title";
    if (!node) {
      addText(detail, "p", "Select a graph node or table row to inspect its server explanation.");
      section.append(detail);
      return;
    }
    addText(detail, "p", node.node_id);
    const healthLine = document.createElement("p");
    addAssessmentHealth(healthLine, displayedAssessmentHealth(node), "");
    if (assessmentState.overlay === "projected") {
      addText(healthLine, "span", "Projected health unavailable").id =
        "assessment-detail-health";
    } else {
      addText(healthLine, "span", "Approved health: ");
      addText(healthLine, "span", assessmentHealth[node.health].label).id =
        "assessment-detail-health";
    }
    detail.append(healthLine);
    const worstLine = document.createElement("p");
    addText(worstLine, "span", "Approved worst dimension: ");
    addText(worstLine, "span", node.worst_dimension || "N/A").id =
      "assessment-detail-worst";
    detail.append(worstLine);
    const scoreLine = document.createElement("p");
    addText(scoreLine, "span", `${assessmentOverlayName()} score: `);
    addText(scoreLine, "span", scoreText(displayedAssessmentScore(node))).id =
      "assessment-detail-score";
    if (displayedAssessmentScore(node) === null) {
      addText(
        scoreLine,
        "span",
        assessmentState.overlay === "projected" ? " — unavailable" : " — unassessed"
      );
    }
    detail.append(scoreLine);
    addText(
      detail,
      "p",
      assessmentValueText("confidence", displayedAssessmentConfidence(node))
    );
    if (node.recommended_next_action) {
      addText(detail, "p", `Recommended next action: ${node.recommended_next_action}`);
    }
    for (const dimension of node.dimensions) {
      const card = document.createElement("article");
      card.className = assessmentClass(
        assessmentState.overlay === "projected" ? "unassessed" : dimension.health,
        "assessment-dimension"
      );
      addText(card, "h4", dimension.dimension);
      addText(
        card,
        "p",
        assessmentState.overlay === "projected"
          ? "Projected dimension score N/A — unavailable; projected confidence N/A — unavailable."
          : dimension.applicability === "not_applicable"
          ? "Approved dimension: N/A — this dimension does not apply."
          : `Approved dimension score ${scoreText(dimension.score)}; approved confidence ${scoreText(dimension.confidence)}`
      );
      if (assessmentState.overlay === "projected") {
        addText(card, "p", "The checks below explain the approved score, not a projected score.");
      }
      for (const check of dimension.failed) {
        addText(card, "p", `${check.severity || "gap"}: ${check.explanation || check.rule_id}`);
      }
      if (dimension.recommended_next_action) {
        addText(card, "p", `Recommended next action: ${dimension.recommended_next_action}`);
      }
      detail.append(card);
    }
    section.append(detail);
  }

  function renderAssessment() {
    const section = panel("Graph assessment");
    if (assessmentState.unavailable) {
      addText(section, "p", "Assessment unavailable. No canonical state was changed.");
      section.append(actionButton("Retry graph assessment", () => loadAssessment(null)));
      return section;
    }
    if (!assessmentState.report) {
      addText(section, "p", "Loading the server-derived graph assessment.");
      return section;
    }
    addText(
      section,
      "p",
      "Scores are derived, explainable, ACL-filtered, noncanonical, and model-independent."
    );
    const project = assessmentState.report.project;
    if (project && typeof project === "object") {
      const summary = document.createElement("p");
      const projectHealth = assessmentState.overlay === "projected"
        ? "unassessed"
        : project.health;
      summary.className = assessmentClass(projectHealth, "assessment-project-summary");
      if (assessmentState.overlay === "projected") {
        addAssessmentHealth(summary, "unassessed", "Projected project health unavailable");
        addText(summary, "span", "Projected project robustness N/A — unavailable");
        addText(summary, "span", "Projected project confidence N/A — unavailable");
      } else {
        addAssessmentHealth(
          summary,
          project.health,
          `Approved project health ${assessmentHealth[project.health].label}`
        );
        addText(
          summary,
          "span",
          `Approved project robustness ${scoreText(project.robustness)}`
        );
        addText(
          summary,
          "span",
          `Approved project confidence ${scoreText(project.confidence)}`
        );
      }
      section.append(summary);
    }
    const controls = document.createElement("div");
    controls.className = "assessment-controls";
    const approved = actionButton("Approved scores", () => {
      assessmentState.overlay = "approved";
      render();
    });
    approved.setAttribute("aria-pressed", String(assessmentState.overlay === "approved"));
    const projected = actionButton("Projected scores", () => {
      assessmentState.overlay = "projected";
      render();
    });
    projected.setAttribute("aria-pressed", String(assessmentState.overlay === "projected"));
    const overlayLabel = addText(
      controls,
      "strong",
      `${assessmentOverlayName()} scores`
    );
    overlayLabel.id = "assessment-overlay-label";
    controls.append(approved, projected);
    const types = Array.from(
      new Set(assessmentState.report.nodes.map((node) => node.node_type))
    ).sort();
    controls.append(
      assessmentFilter(
        "assessment-filter-health",
        "Health",
        ["green", "orange", "red", "unassessed"],
        assessmentState.filters.health,
        "health"
      ),
      assessmentFilter(
        "assessment-filter-dimension",
        "Dimension",
        [
          "intent_clarity",
          "evidence_strength",
          "requirement_coverage",
          "implementation_traceability",
          "test_verification",
          "consistency",
          "freshness",
        ],
        assessmentState.filters.dimension,
        "dimension"
      ),
      assessmentFilter(
        "assessment-filter-type",
        "Type",
        types,
        assessmentState.filters.type,
        "type"
      )
    );
    section.append(controls);
    renderAssessmentGraph(section);
    renderAssessmentTable(section);
    renderAssessmentDetail(section);
    return section;
  }

  function exactObjectKeys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return false;
    }
    const actual = Object.keys(value).sort();
    const wanted = Array.from(expected).sort();
    return actual.length === wanted.length && actual.every((key, index) => key === wanted[index]);
  }

  function acceptEnrichmentResponse(payload) {
    const sessionKeys = [
      "schema_version", "id", "status", "focus_id", "budget_minutes",
      "remaining_budget_seconds", "snapshot_digest", "current_gap_id",
      "answered_gap_ids", "skipped_gap_ids", "answer_evidence_refs", "started_at", "updated_at",
    ];
    const questionKeys = [
      "gap_id", "node_id", "dimension", "rule_id", "prompt", "reason",
      "requested_fields", "evidence_scope",
    ];
    if (
      !exactObjectKeys(payload, ["schema_version", "session", "question"]) ||
      payload.schema_version !== 1 ||
      !exactObjectKeys(payload.session, sessionKeys)
    ) {
      throw new Error("Invalid enrichment response.");
    }
    const session = payload.session;
    if (
      session.schema_version !== 1 ||
      typeof session.id !== "string" ||
      !/^refine:[^\s]{1,249}$/.test(session.id) ||
      !["open", "paused", "complete", "cancelled"].includes(session.status) ||
      ![null, 5, 15, 30].includes(session.budget_minutes) ||
      !Number.isInteger(session.remaining_budget_seconds) ||
      session.remaining_budget_seconds < 0 ||
      session.remaining_budget_seconds > 1800 ||
      typeof session.snapshot_digest !== "string" ||
      !/^sha256:[0-9a-f]{64}$/.test(session.snapshot_digest)
    ) {
      throw new Error("Invalid enrichment session.");
    }
    for (const key of ["answered_gap_ids", "skipped_gap_ids", "answer_evidence_refs"]) {
      const values = session[key];
      if (!Array.isArray(values) || values.length > 10000 || values.some((item) => typeof item !== "string" || item.length > 256)) {
        throw new Error("Invalid enrichment progress.");
      }
    }
    const question = payload.question;
    if (session.status === "open") {
      if (
        !exactObjectKeys(question, questionKeys) ||
        typeof question.gap_id !== "string" ||
        question.gap_id !== session.current_gap_id ||
        typeof question.node_id !== "string" ||
        typeof question.dimension !== "string" ||
        typeof question.rule_id !== "string" ||
        typeof question.prompt !== "string" ||
        !question.prompt ||
        question.prompt.length > 2048 ||
        typeof question.reason !== "string" ||
        !question.reason ||
        question.reason.length > 2048 ||
        !Array.isArray(question.requested_fields) ||
        question.requested_fields.length > 16 ||
        !Array.isArray(question.evidence_scope) ||
        question.evidence_scope.length > 256
      ) {
        throw new Error("Invalid enrichment question.");
      }
    } else if (question !== null) {
      throw new Error("Invalid inactive enrichment session.");
    } else if (
      session.status === "paused" &&
      (typeof session.current_gap_id !== "string" || !session.current_gap_id)
    ) {
      throw new Error("Invalid paused enrichment session.");
    } else if (
      (session.status === "complete" || session.status === "cancelled") &&
      session.current_gap_id !== null
    ) {
      throw new Error("Invalid terminal enrichment session.");
    }
    return { session, question };
  }

  function acceptEnrichmentProposalResponse(payload, expectedSession) {
    const proposalKeys = [
      "schema_version", "id", "kind", "proposed_by", "proposed_at",
      "baseline_graph_version", "evidence_refs", "source_roles", "changeset",
      "core_node_ids", "provisional_node_ids", "assumptions", "unanswered_questions",
      "conflicting_authors", "destructive", "clarification_session_id", "task_id",
    ];
    if (
      !expectedSession ||
      !exactObjectKeys(payload, ["schema_version", "session_id", "proposal"]) ||
      payload.schema_version !== 1 ||
      payload.session_id !== expectedSession.id ||
      !exactObjectKeys(payload.proposal, proposalKeys)
    ) {
      throw new Error("Invalid enrichment proposal response.");
    }
    const proposal = payload.proposal;
    if (
      proposal.schema_version !== 2 ||
      typeof proposal.id !== "string" ||
      !/^proposal:sha256:[0-9a-f]{64}$/.test(proposal.id) ||
      !["bootstrap", "requirement"].includes(proposal.kind) ||
      typeof proposal.proposed_by !== "string" ||
      !proposal.proposed_by ||
      !Number.isInteger(proposal.baseline_graph_version) ||
      proposal.baseline_graph_version < 0 ||
      typeof proposal.changeset !== "object" ||
      proposal.changeset === null ||
      Array.isArray(proposal.changeset) ||
      typeof proposal.clarification_session_id !== "string" ||
      !proposal.clarification_session_id ||
      typeof proposal.task_id !== "string" ||
      !proposal.task_id ||
      typeof proposal.destructive !== "boolean"
    ) {
      throw new Error("Invalid enrichment proposal.");
    }
    for (const key of [
      "evidence_refs", "source_roles", "core_node_ids", "provisional_node_ids",
      "assumptions", "unanswered_questions", "conflicting_authors",
    ]) {
      if (!Array.isArray(proposal[key]) || proposal[key].length > 10000) {
        throw new Error("Invalid enrichment proposal.");
      }
    }
    if (
      !Array.isArray(expectedSession.answer_evidence_refs) ||
      expectedSession.answer_evidence_refs.length === 0 ||
      expectedSession.answer_evidence_refs.some(
        (reference) => !proposal.evidence_refs.includes(reference)
      )
    ) {
      throw new Error("Enrichment proposal is not bound to this session.");
    }
    return proposal;
  }

  function focusEnrichmentAfterRender(session) {
    const targetId = session && session.status === "open"
      ? "enrichment-answer"
      : session && session.status === "paused"
      ? "enrichment-resume"
      : "enrichment-start";
    const target = document.getElementById(targetId);
    if (target && target.id === targetId && typeof target.focus === "function") {
      target.focus();
    }
  }

  async function updateEnrichment(path, body) {
    const generation = enrichmentState.generation + 1;
    enrichmentState.generation = generation;
    try {
      const accepted = acceptEnrichmentResponse(await fetchJson(path, {
        method: "POST",
        body,
      }));
      if (generation !== enrichmentState.generation) {
        return;
      }
      enrichmentState.session = accepted.session;
      enrichmentState.question = accepted.question;
      enrichmentState.unavailable = false;
      if (state.view === "enrichment") {
        render();
        focusEnrichmentAfterRender(accepted.session);
      }
      announce("Graph improvement session updated. Approved graph state is unchanged.");
    } catch (_error) {
      if (generation !== enrichmentState.generation) {
        return;
      }
      enrichmentState.session = null;
      enrichmentState.question = null;
      enrichmentState.unavailable = true;
      if (state.view === "enrichment") {
        render();
        focusEnrichmentAfterRender(null);
      }
      announce("Graph improvement session is unavailable. Approved graph state is unchanged.");
    }
  }

  function startEnrichment(minutesField, focusField) {
    const selected = minutesField.value;
    const minutes = selected === "" ? null : Number(selected);
    const focus = typeof focusField.value === "string" && focusField.value ? focusField.value : null;
    focusField.value = "";
    if (![null, 5, 15, 30].includes(minutes) || (minutes === null && focus === null)) {
      announce("Choose 5, 15, or 30 minutes, or provide a focus.");
      return;
    }
    void updateEnrichment(api.enrichmentStart, JSON.stringify({ minutes, focus }));
  }

  function answerEnrichment(answerField) {
    const session = enrichmentState.session;
    const question = enrichmentState.question;
    let answer = typeof answerField.value === "string" ? answerField.value : "";
    let body = "";
    answerField.value = "";
    if (!answer || !session || !question) {
      answer = "";
      announce("Enter an answer to the current question.");
      return;
    }
    body = JSON.stringify({ session_id: session.id, gap_id: question.gap_id, answer });
    answer = "";
    void updateEnrichment(api.enrichmentAnswer, body).finally(() => {
      body = "";
    });
  }

  function enrichmentSessionAction(path, includeGap = false) {
    const session = enrichmentState.session;
    const question = enrichmentState.question;
    if (!session || (includeGap && !question)) {
      return;
    }
    const body = includeGap
      ? { session_id: session.id, gap_id: question.gap_id }
      : { session_id: session.id };
    void updateEnrichment(path, JSON.stringify(body));
  }

  async function proposeEnrichment(submissionField) {
    const session = enrichmentState.session;
    const generation = enrichmentState.generation;
    let rawSubmission = typeof submissionField.value === "string" ? submissionField.value : "";
    let submission = null;
    let body = "";
    submissionField.value = "";
    try {
      if (!session || session.answer_evidence_refs.length === 0 || !rawSubmission) {
        throw new Error("Enrichment proposal submission is unavailable.");
      }
      submission = JSON.parse(rawSubmission);
      rawSubmission = "";
      if (!submission || typeof submission !== "object" || Array.isArray(submission)) {
        throw new Error("Invalid enrichment proposal submission.");
      }
      body = JSON.stringify({ session_id: session.id, submission });
      submission = null;
      const proposal = acceptEnrichmentProposalResponse(
        await fetchJson(api.enrichmentPropose, { method: "POST", body }),
        session
      );
      body = "";
      if (
        enrichmentState.session !== session ||
        enrichmentState.generation !== generation ||
        state.view !== "enrichment"
      ) {
        throw new Error("Enrichment session changed before proposal review.");
      }
      selectProposal(proposal.id);
      await showView("proposal");
    } catch (_error) {
      if (
        enrichmentState.session === session &&
        enrichmentState.generation === generation &&
        state.view === "enrichment"
      ) {
        announce("Graph proposal is unavailable. No approved graph state changed.");
      }
    } finally {
      submissionField.value = "";
      rawSubmission = "";
      submission = null;
      body = "";
    }
  }

  function renderEnrichment() {
    const section = panel("Improve graph");
    addText(
      section,
      "p",
      "Answer one evidence-grounded question at a time. Answers are evidence; no graph change is applied without proposal review."
    );
    const start = document.createElement("fieldset");
    addText(start, "legend", "Start an improvement session");
    const minutesLabel = document.createElement("label");
    minutesLabel.textContent = "Time budget";
    const minutes = document.createElement("select");
    minutes.id = "enrichment-minutes";
    addOption(minutes, "", "Focus only");
    addOption(minutes, "5", "5 minutes");
    addOption(minutes, "15", "15 minutes");
    addOption(minutes, "30", "30 minutes");
    minutes.value = "5";
    minutesLabel.append(minutes);
    const focusLabel = document.createElement("label");
    focusLabel.textContent = "Optional node or branch focus";
    const focus = document.createElement("input");
    focus.id = "enrichment-focus";
    focus.type = "text";
    focus.maxLength = 256;
    focus.autocomplete = "off";
    focusLabel.append(focus);
    const startButton = actionButton(
      "Start improvement session",
      () => startEnrichment(minutes, focus)
    );
    startButton.id = "enrichment-start";
    start.append(
      minutesLabel,
      focusLabel,
      startButton
    );
    section.append(start);

    if (enrichmentState.unavailable) {
      addText(section, "p", "Improvement session unavailable. No approved graph state changed.");
    }
    const session = enrichmentState.session;
    const question = enrichmentState.question;
    if (session) {
      addText(section, "p", `Session status: ${session.status}`);
      if (question) {
        const current = document.createElement("article");
        current.className = "enrichment-question";
        addText(current, "h3", question.prompt);
        addText(current, "p", `Why this matters: ${question.reason}`);
        addText(current, "p", `Score dimension: ${question.dimension}`);
        const answerLabel = document.createElement("label");
        answerLabel.textContent = "Your answer";
        const answer = document.createElement("textarea");
        answer.id = "enrichment-answer";
        answer.maxLength = 16384;
        answer.autocomplete = "off";
        answerLabel.append(answer);
        current.append(
          answerLabel,
          actionButton("Record answer as evidence", () => answerEnrichment(answer)),
          actionButton("Skip current question", () => enrichmentSessionAction(api.enrichmentSkip, true)),
          actionButton("Pause improvement session", () => enrichmentSessionAction(api.enrichmentPause))
        );
        section.append(current);
      } else if (session.status === "paused") {
        const resume = actionButton(
          "Resume improvement session",
          () => enrichmentSessionAction(api.enrichmentResume)
        );
        resume.id = "enrichment-resume";
        section.append(resume);
      }
      const proposal = document.createElement("aside");
      proposal.className = "proposal-review-action";
      addText(proposal, "h3", "Proposal review");
      addText(proposal, "p", "Graph changes remain separate and require the existing governed review flow.");
      const submissionLabel = document.createElement("label");
      submissionLabel.textContent = "Governed proposal submission";
      const submission = document.createElement("textarea");
      submission.id = "enrichment-proposal-submission";
      submission.maxLength = 240000;
      submission.autocomplete = "off";
      submissionLabel.append(submission);
      proposal.append(
        submissionLabel,
        actionButton("Review proposed graph changes", () => proposeEnrichment(submission))
      );
      section.append(proposal);
    }
    return section;
  }

  function updateNavigation() {
    for (const button of navigation) {
      const selected = button.dataset.view === state.view;
      if (selected) {
        button.setAttribute("aria-current", "page");
      } else {
        button.removeAttribute("aria-current");
      }
    }
  }

  function renderHome() {
    const section = panel("Home");
    if (state.status) {
      addProjection(section, state.status);
    } else {
      addText(section, "p", "Loading local review status.");
    }
    section.append(actionButton("Refresh readiness", refreshStatus));
    if (state.developmentObservation) {
      addText(section, "h3", "Development evidence");
      addProjection(section, state.developmentObservation);
      for (const commandId of state.developmentObservation.command_ids || []) {
        section.append(
          actionButton("Run reviewed tests", () => runReviewedTests(commandId))
        );
      }
    }
    section.append(
      actionButton("Refresh development evidence", () => refreshDevelopmentObservation(false))
    );
    section.append(actionButton("Open graph assessment", () => showView("assessment")));
    section.append(actionButton("Improve graph", () => showView("enrichment")));
    return section;
  }

  function renderOnboarding() {
    const section = panel("Onboarding");
    addText(
      section,
      "p",
      "Review submitted onboarding proposals in Inbox, then enroll this device before signing a decision."
    );
    section.append(actionButton("Register this device with WebAuthn", registerDevice));
    return section;
  }

  function renderInbox() {
    const section = panel("Inbox");
    const inbox = state.inbox;
    if (!inbox) {
      addText(section, "p", "Loading review items.");
      return section;
    }
    const items = [
      ...(inbox.pending_proposal_ids || []),
      ...(inbox.open_case_ids || []),
    ];
    const clarificationSessions = Array.isArray(inbox.clarification_sessions)
      ? inbox.clarification_sessions
      : [];
    if (items.length > 0) {
      const list = document.createElement("ul");
      for (const identifier of items) {
        const entry = document.createElement("li");
        entry.append(
          actionButton(`Review ${identifier}`, () => {
            selectProposal(identifier);
            showView("proposal");
          })
        );
        list.append(entry);
      }
      section.append(list);
    }
    for (const session of clarificationSessions) {
      if (!session || typeof session !== "object" || !Array.isArray(session.questions)) {
        continue;
      }
      const clarification = document.createElement("article");
      addText(clarification, "h3", `Clarification ${session.id}`);
      addText(clarification, "p", `Task: ${session.task_id}`);
      for (const question of session.questions) {
        if (!question || typeof question !== "object") {
          continue;
        }
        const fieldset = document.createElement("fieldset");
        addText(fieldset, "legend", question.prompt);
        const label = document.createElement("label");
        label.textContent = `Answer ${question.id}${question.required ? " (required)" : ""}`;
        const answer = document.createElement("textarea");
        answer.maxLength = 16384;
        answer.required = Boolean(question.required);
        label.append(answer);
        fieldset.append(
          label,
          actionButton(`Preview answer for ${question.id}`, () =>
            previewClarificationAnswer(session.id, question.id, answer)
          )
        );
        clarification.append(fieldset);
      }
      section.append(clarification);
    }
    if (items.length === 0 && clarificationSessions.length === 0) {
      addText(section, "p", "No visible proposals, cases, or clarification questions need review.");
    }
    section.append(actionButton("Refresh inbox", loadInbox));
    return section;
  }

  function selectionRequired(payload) {
    return Boolean(
      payload &&
      (payload.action === "confirm_baseline" || payload.action === "confirm_proposal")
    );
  }

  function selectionSatisfied(payload) {
    return !selectionRequired(payload) || state.selectedNodeIds.length > 0;
  }

  function renderSelectedNodes(payload) {
    const fieldset = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = "Selected nodes bound to this decision";
    fieldset.append(legend);
    if (state.selectedNodeIds.length === 0) {
      addText(
        fieldset,
        "p",
        selectionRequired(payload)
          ? "This decision requires at least one server-bound selected node."
          : "This action is explicitly bound to an empty node selection."
      );
      return fieldset;
    }
    for (const nodeId of state.selectedNodeIds) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.disabled = true;
      label.append(checkbox, document.createTextNode(` Selected node: ${nodeId}`));
      fieldset.append(label);
    }
    addText(fieldset, "p", "The server-generated decision payload fixes this selection.");
    return fieldset;
  }

  function renderDecisionSummary(section, payload) {
    const summary = document.createElement("dl");
    summary.className = "decision-summary";
    const subject = payload && payload.subject;
    addValue(summary, "Action", payload && payload.action);
    addValue(
      summary,
      "Subject",
      subject && typeof subject === "object" ? `${subject.kind}: ${subject.id}` : "unavailable"
    );
    addValue(summary, "Result digest", payload && payload.result_digest);
    section.append(summary);
  }

  function renderProposal() {
    const section = panel("Proposal review");
    if (!state.preview) {
      if (state.proposalId) {
        addText(section, "p", "Loading the complete proposal and evidence preview.");
      } else {
        addText(section, "p", "Choose a proposal or case in Inbox to review its complete preview.");
        section.append(actionButton("Open inbox", () => showView("inbox")));
      }
      return section;
    }
    addText(section, "h3", "Exact preview");
    addProjection(section, state.preview.preview);
    addText(section, "h3", "Decision binding");
    addProjection(section, state.preview.payload);
    const payload = state.preview.payload;
    renderDecisionSummary(section, payload);
    section.append(renderSelectedNodes(payload));
    const actions = document.createElement("div");
    actions.className = "actions";
    const authorize = actionButton(
      `Authorize ${decisionLabel(payload)} with WebAuthn`,
      authorizeDecision,
      "danger"
    );
    authorize.disabled = !selectionSatisfied(payload);
    actions.append(authorize, actionButton("Cancel review without applying a decision", cancelReview));
    section.append(actions);
    return section;
  }

  async function membershipAction(action, response) {
    const body = { session_id: state.membership.session_id };
    if (response) body.response = response;
    return fetchJson(`${api.membership}/${action}`, { method: "POST", body: JSON.stringify(body) });
  }

  async function reviewMembership(action = "preview") {
    try {
      state.membership = await membershipAction(action);
      render();
      announce("Enrollment review updated.");
    } catch (_error) {
      announce("Team enrollment unavailable.");
    }
  }

  async function enrollMembership() {
    let options = null;
    let credential = null;
    let response = null;
    try {
      options = creationOptions(await membershipAction("register-options"));
      credential = await navigator.credentials.create(options);
      response = serializeCredential(credential);
      await membershipAction("register-verify", response);
      await reviewMembership();
    } catch (_error) {
      announce("Team enrollment unavailable.");
    } finally {
      clearOptionBuffers(options);
      options = credential = response = null;
    }
  }

  async function authorizeMembership() {
    let options = null;
    let credential = null;
    let response = null;
    try {
      options = requestOptions(await membershipAction("options"));
      credential = await navigator.credentials.get(options);
      response = serializeCredential(credential);
      state.membership = await membershipAction("verify", response);
      render();
      announce("Enrollment progress updated.");
    } catch (_error) {
      // Refresh durable progress, including an attempted write whose response was lost.
      try { state.membership = await fetchJson(api.membership); render(); } catch (_refreshError) {}
      announce("Team enrollment unavailable. Check enrollment progress.");
    } finally {
      clearOptionBuffers(options);
      options = credential = response = null;
    }
  }

  async function authorizeMemberPublication() {
    let options = null;
    let credential = null;
    let response = null;
    try {
      options = requestOptions(await membershipAction("publish-options"));
      credential = await navigator.credentials.get(options);
      response = serializeCredential(credential);
      const result = await membershipAction("publish-verify", response);
      state.membership = { ...state.membership, ...result };
      render();
      announce("Team-state publication progress updated.");
    } catch (_error) {
      try { state.membership = await fetchJson(api.membership); render(); } catch (_refreshError) {}
      announce("Team-state publication is unavailable. Check publication progress.");
    } finally {
      clearOptionBuffers(options);
      options = credential = response = null;
    }
  }

  function renderMembership(section) {
    const membership = state.membership;
    addText(section, "h3", "Second developer enrollment");
    addProjection(section, membership);
    if (membership.action === "invite" && membership.state === "review_required") {
      section.append(actionButton("Create public invitation", () => reviewMembership("create-invite")));
    }
    if (membership.action === "join" && membership.state === "review_required") {
      section.append(actionButton("Enroll this device with WebAuthn", enrollMembership));
      section.append(actionButton("Review join", () => reviewMembership()));
    }
    if (membership.action === "approve-join" && membership.state === "review_required") {
      section.append(actionButton("Review exact membership change", () => reviewMembership()));
    }
    if (membership.state === "preview_ready") {
      section.append(actionButton(membership.action === "join" ? "Authorize join with WebAuthn" : "Approve membership with WebAuthn", authorizeMembership, "danger"));
    }
    if (membership.state === "member-active") {
      section.append(actionButton("Review team-state publication", () => reviewMembership("publish-preview")));
    }
    if (membership.state === "publication_preview" || membership.state === "publication_draft") {
      section.append(actionButton("Authorize team-state publication with WebAuthn", authorizeMemberPublication, "danger"));
    }
    if (["approved", "publication-pending", "publication_pending", "pr-pending", "publication_recovery_required"].includes(membership.state)) {
      section.append(actionButton("Refresh enrollment progress", () => reviewMembership("reconcile")));
    }
    if (membership.state === "closed") {
      section.append(actionButton("Discard closed enrollment and start fresh", () => reviewMembership("restart")));
    }
    if (membership.state === "response-ready") {
      addText(section, "p", "Your public response is ready. Share it with your sponsor. After approval merges, continue your normal development workflow to restore shared state.");
    }
    if (membership.can_cancel === true) {
      section.append(actionButton("Cancel enrollment", () => reviewMembership("cancel")));
    }
    return section;
  }

  function renderTeamState() {
    const section = panel("Team state");
    addText(
      section,
      "p",
      "This local projection shows the available shared-state readiness only; it does not disclose credentials."
    );
    if (state.status) {
      addProjection(section, state.status);
    }
    if (state.membership && state.membership.state !== "unconfigured") {
      return renderMembership(section);
    }
    if (state.teamSetup) {
      addText(section, "h3", "GitHub team setup");
      addProjection(section, state.teamSetup);
      if (state.teamSetupPreview) {
        addText(section, "h3", "Exact setup preview");
        addProjection(section, state.teamSetupPreview.preview);
        addText(section, "h3", "Decision binding");
        addProjection(section, state.teamSetupPreview.payload);
      }
      if (state.teamSetupResult && state.teamSetupResult.pull_request_url) {
        addText(section, "p", state.teamSetupResult.pull_request_url);
      }
      const actions = document.createElement("div");
      actions.className = "actions";
      if (state.teamSetup.state === "publication_pending") {
        addText(section, "p", "Merge the reviewed pull request in GitHub, then refresh to verify and finish local setup.");
        actions.append(actionButton("Refresh publication merge status", inspectTeamSetup));
      }
      if (state.teamSetup.state === "publication_recovery_required") {
        addText(section, "p", "A GitHub publication write may have occurred. Preview and authorize an exact recovery retry; setup cannot be cancelled until provider state is reconciled.");
      }
      if (state.teamSetup.state === "publication_restart_required") {
        addText(section, "p", "The exact publication pull request was closed without merging. Preview and authorize a fresh exact pull request; setup cannot be cancelled until provider state is reconciled.");
      }
      if (state.teamSetup.state === "setup_required") {
        actions.append(actionButton("Inspect GitHub identity and repository", inspectTeamSetup));
      }
      if (state.teamSetup.state === "identity_verified") {
        actions.append(actionButton("Enroll this device with WebAuthn", enrollProductionTeam));
      }
      if (["enrolled", "default_branch_prerequisite", "code_changes_staged", "protection_configured"].includes(state.teamSetup.state)) {
        actions.append(actionButton("Preview branch protection changes", previewTeamProtection));
      }
      if (["protection_configured", "publication_draft", "publication_recovery_required", "publication_restart_required"].includes(state.teamSetup.state)) {
        actions.append(
          actionButton("Preview encrypted team-state publication", previewTeamPublication)
        );
      }
      if (state.teamSetupPreview && state.teamSetupPreview.payload) {
        const action = state.teamSetupPreview.payload.action;
        if (action === "approve_external_write") {
          const phase = state.teamSetupPreview.preview && state.teamSetupPreview.preview.phase;
          actions.append(
            actionButton(
              phase === "code_changes"
                ? "Authorize code suggestion staging with WebAuthn"
                : "Authorize branch protection with WebAuthn",
              authorizeTeamSetup,
              "danger"
            )
          );
        } else if (action === "publish_state") {
          actions.append(
            actionButton("Authorize publication with WebAuthn", authorizeTeamSetup, "danger")
          );
        }
      }
      if (![
        "cancelled",
        "unconfigured",
        "published",
        "publication_pending",
        "publication_recovery_required",
        "publication_restart_required",
      ].includes(state.teamSetup.state)) {
        actions.append(actionButton("Cancel team setup", cancelTeamSetup));
      }
      section.append(actions);
      return section;
    }
    if (state.teamEnrollment) {
      addText(section, "h3", "Recipient enrollment");
      addProjection(section, state.teamEnrollment);
    }
    if (state.teamPublication) {
      addText(section, "h3", "Pending publication preview");
      addProjection(section, state.teamPublication.preview);
    }
    const proofLabel = document.createElement("label");
    proofLabel.textContent = "GitHub device authorization proof";
    const proof = document.createElement("input");
    proof.type = "password";
    proof.autocomplete = "off";
    proof.maxLength = 16384;
    proofLabel.append(proof);
    section.append(
      proofLabel,
      actionButton("Verify GitHub identity and enroll this device", () => enrollTeamRecipient(proof)),
      actionButton("Preview reviewed team-state publication", loadTeamPublicationPreview),
      actionButton("Refresh team-state readiness", refreshTeamEnrollment)
    );
    return section;
  }

  function render() {
    updateNavigation();
    let next = null;
    if (state.view === "onboarding") {
      next = renderOnboarding();
    } else if (state.view === "inbox") {
      next = renderInbox();
    } else if (state.view === "proposal") {
      next = renderProposal();
    } else if (state.view === "team_state") {
      next = renderTeamState();
    } else if (state.view === "assessment") {
      next = renderAssessment();
    } else if (state.view === "enrichment") {
      next = renderEnrichment();
    } else {
      next = renderHome();
    }
    app.replaceChildren(next);
  }

  function clearPreview() {
    state.preview = null;
    state.selectedNodeIds = [];
    state.proposalId = null;
  }

  function discardProposalReview() {
    state.proposalGeneration += 1;
    clearPreview();
  }

  function selectProposal(proposalId) {
    discardProposalReview();
    state.proposalId = proposalId;
  }

  function payloadMatchesProposal(payload, proposalId) {
    return (
      payload &&
      typeof payload === "object" &&
      payload.subject &&
      typeof payload.subject === "object" &&
      payload.subject.id === proposalId
    );
  }

  function selectionMatchesPayload(payload, proposalId) {
    return (
      payloadMatchesProposal(payload, proposalId) &&
      Array.isArray(payload.selected_node_ids) &&
      payload.selected_node_ids.length === state.selectedNodeIds.length &&
      payload.selected_node_ids.every(
        (nodeId, index) => nodeId === state.selectedNodeIds[index]
      )
    );
  }

  function currentReviewMatches(proposalId, generation, preview) {
    return (
      state.proposalId === proposalId &&
      state.proposalGeneration === generation &&
      state.preview === preview &&
      selectionMatchesPayload(preview && preview.payload, proposalId)
    );
  }

  function decisionLabel(payload) {
    const subject = payload && payload.subject;
    const subjectId = subject && typeof subject.id === "string" ? subject.id : "unavailable subject";
    const actions = {
      confirm_baseline: "confirm baseline",
      answer_clarification: "answer clarification",
      confirm_proposal: "confirm proposal",
      resolve_conflict: "resolve conflict",
      publish_state: "publish state",
      approve_external_write: "approve external write",
    };
    const action = payload && typeof payload.action === "string" ? payload.action : "authorize";
    return `${actions[action] || action} ${subjectId}`;
  }

  function pendingAnswerId(payload) {
    const subject = payload && payload.subject;
    return payload &&
      payload.action === "answer_clarification" &&
      subject &&
      subject.kind === "answer" &&
      typeof subject.id === "string" &&
      /^answer:[0-9a-f]{64}$/.test(subject.id)
      ? subject.id
      : null;
  }

  async function discardPendingAnswer(payload) {
    const answerId = pendingAnswerId(payload);
    if (!answerId) {
      return;
    }
    await fetchJson(api.clarificationAnswerDiscard, {
      method: "POST",
      body: JSON.stringify({ answer_id: answerId }),
    });
  }

  async function cancelReview() {
    const payload = state.preview && state.preview.payload;
    discardProposalReview();
    state.view = "inbox";
    render();
    announce("Review cancelled. No decision was sent.");
    try {
      await discardPendingAnswer(payload);
    } catch (_error) {
      announce("Review cancelled. The private answer will expire without authority.");
    }
  }

  async function previewClarificationAnswer(sessionId, questionId, field) {
    let answer = typeof field.value === "string" ? field.value : "";
    const inbox = state.inbox;
    if (!answer) {
      announce("Enter an answer before requesting an authoritative preview.");
      return;
    }
    try {
      const preview = await fetchJson(api.clarificationAnswerPreview, {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, question_id: questionId, answer }),
      });
      const payload = preview && preview.payload;
      if (
        state.view !== "inbox" ||
        state.inbox !== inbox ||
        !payload ||
        payload.action !== "answer_clarification" ||
        !Array.isArray(payload.selected_node_ids) ||
        payload.selected_node_ids.length !== 0 ||
        !pendingAnswerId(payload)
      ) {
        throw new Error("Clarification answer preview binding changed.");
      }
      discardProposalReview();
      state.proposalId = payload.subject.id;
      state.preview = preview;
      state.selectedNodeIds = [];
      state.view = "proposal";
      render();
      announce("Answer preview loaded. Confirm its digest with WebAuthn to submit it.");
    } catch (_error) {
      announce("Answer preview is unavailable. No answer was submitted.");
    } finally {
      field.value = "";
      answer = "";
    }
  }

  async function refreshStatus() {
    try {
      state.status = await fetchJson(api.status);
      if (state.view === "home" || state.view === "team_state") {
        render();
      }
      announce("Local review status updated.");
    } catch (_error) {
      announce("Local review status is unavailable. No decision was applied.");
    }
  }

  async function refreshTeamEnrollment(quiet = false) {
    void fetchJson(api.membership).then((membership) => {
      state.membership = membership;
      if (state.view === "team_state") render();
    }).catch(() => {});
    void fetchJson(api.teamSetup)
      .then(async (setup) => {
        state.teamSetup = setup;
        if (state.view === "team_state") {
          render();
        }
        if (setup.state === "publication_pending") {
          await inspectTeamSetup();
        }
      })
      .catch(() => {
        // Older injected enrollment services intentionally use the manual compatibility UI.
      });
    try {
      state.teamEnrollment = await fetchJson(api.teamEnrollment);
      if (state.view === "team_state") {
        render();
      }
      if (!quiet) {
        announce("Team recipient status updated.");
      }
    } catch (_error) {
      announce("Team recipient status is unavailable.");
    }
  }

  async function loadTeamPublicationPreview() {
    try {
      state.teamPublication = await fetchJson(api.teamPublicationPreview);
      if (state.view === "team_state") {
        render();
      }
      announce("Exact team-state publication preview loaded for review.");
    } catch (_error) {
      state.teamPublication = null;
      announce("Team-state publication is unavailable. Nothing was published.");
    }
  }

  async function inspectTeamSetup() {
    try {
      state.teamSetup = await fetchJson(api.teamSetupInspect, { method: "POST", body: "{}" });
      state.teamSetupPreview = null;
      render();
      announce("GitHub identity and repository inspected for team setup.");
    } catch (_error) {
      announce("GitHub identity inspection failed. Team setup was not changed.");
    }
  }

  async function enrollProductionTeam() {
    let options = null;
    let credential = null;
    let response = null;
    try {
      options = await fetchJson(api.teamSetupEnroll, { method: "POST", body: "{}" });
      credential = await navigator.credentials.create({ publicKey: creationOptions(options).publicKey });
      response = serializeCredential(credential);
      await fetchJson(api.teamEnrollmentVerify, {
        method: "POST",
        body: JSON.stringify({ response }),
      });
      state.teamSetup = { ...state.teamSetup, state: "enrolled", enrollment: "enrolled" };
      render();
      announce("Team recipient enrolled with the inspected GitHub identity.");
    } catch (error) {
      announce(
        cancellation(error)
          ? "Team enrollment cancelled. No recipient key was created."
          : "Team enrollment could not complete. The project remains local-only."
      );
    } finally {
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      response = null;
      credential = null;
      options = null;
    }
  }

  async function loadTeamSetupPreview(path, successMessage) {
    try {
      const result = await fetchJson(path, { method: "POST", body: "{}" });
      if (result.state && !result.payload) {
        state.teamSetup = { ...state.teamSetup, ...result };
        state.teamSetupPreview = null;
      } else {
        state.teamSetupPreview = result;
      }
      render();
      announce(result.guidance || successMessage);
    } catch (_error) {
      state.teamSetupPreview = null;
      render();
      announce("Team setup preview is unavailable. Nothing was changed.");
    }
  }

  function previewTeamProtection() {
    return loadTeamSetupPreview(
      api.teamSetupProtectionPreview,
      "Exact branch protection preview loaded for review."
    );
  }

  function previewTeamPublication() {
    return loadTeamSetupPreview(
      api.teamSetupPublicationPreview,
      "Exact encrypted publication preview loaded for review."
    );
  }

  async function authorizeTeamSetup() {
    const preview = state.teamSetupPreview;
    if (!preview || !preview.payload) {
      announce("Review the exact setup preview before authorizing it.");
      return;
    }
    let payload = preview.payload;
    let options = null;
    let credential = null;
    let response = null;
    try {
      options = await fetchJson(api.teamSetupOptions, {
        method: "POST",
        body: JSON.stringify({ payload }),
      });
      credential = await navigator.credentials.get({ publicKey: requestOptions(options).publicKey });
      response = serializeCredential(credential);
      const result = await fetchJson(api.teamSetupVerify, {
        method: "POST",
        body: JSON.stringify({ payload, response }),
      });
      state.teamSetupResult = result;
      state.teamSetup = { ...state.teamSetup, ...result };
      state.teamSetupPreview = null;
      render();
      announce(
        result.state === "published"
          ? "Encrypted team state published through the reviewed pull request."
          : result.state === "publication_pending"
          ? "Publication pull request opened. Merge it in GitHub, then refresh publication status."
          : result.state === "code_changes_staged"
          ? "Code suggestions staged. Commit and merge the exact files to the protected default branch, then preview protection again."
          : "Branch protection configured after WebAuthn authorization."
      );
    } catch (error) {
      announce(
        cancellation(error)
          ? "Team setup authorization cancelled. Nothing was changed."
          : "Team setup authorization failed. Review the exact preview again."
      );
    } finally {
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      payload = null;
      response = null;
      credential = null;
      options = null;
    }
  }

  async function cancelTeamSetup() {
    try {
      state.teamSetup = await fetchJson(api.teamSetupCancel, { method: "POST", body: "{}" });
      state.teamSetupPreview = null;
      state.teamSetupResult = null;
      render();
      announce("Team setup cancelled. No pending authority remains.");
    } catch (_error) {
      announce("Team setup cancellation was refused. Recovery state was retained; inspect or retry the reviewed operation before restarting setup.");
    }
  }

  async function loadInbox() {
    try {
      state.inbox = await fetchJson(api.inbox);
      if (state.view === "inbox") {
        render();
      }
      announce("Inbox updated.");
    } catch (_error) {
      announce("Inbox is unavailable. No decision was applied.");
    }
  }

  async function refreshDevelopmentObservation(quiet = false) {
    try {
      state.developmentObservation = await fetchJson(api.developmentObservation);
      if (state.view === "home") {
        render();
      }
      if (!quiet) {
        announce("Development evidence updated.");
      }
    } catch (_error) {
      if (!quiet) {
        announce("Development evidence is temporarily unavailable.");
      }
    }
  }

  async function runReviewedTests(commandId) {
    if (typeof commandId !== "string" || !/^test:sha256:[0-9a-f]{64}$/.test(commandId)) {
      announce("Reviewed test selection is unavailable.");
      return;
    }
    try {
      const result = await fetchJson(api.reviewedTests, {
        method: "POST",
        body: JSON.stringify({ command_id: commandId }),
      });
      announce(`Reviewed tests finished with status ${result.status}.`);
      await refreshDevelopmentObservation(true);
    } catch (_error) {
      announce("Reviewed tests could not run. No completion was asserted.");
    }
  }

  async function loadPreview() {
    if (!state.proposalId) {
      return;
    }
    const proposalId = state.proposalId;
    const generation = state.proposalGeneration;
    try {
      const preview = await fetchJson(`${api.proposal}${encodeURIComponent(proposalId)}`);
      if (state.proposalId !== proposalId || state.proposalGeneration !== generation) {
        return;
      }
      const payload = preview && preview.payload;
      const selectedNodeIds = Array.isArray(payload && payload.selected_node_ids)
        ? payload.selected_node_ids.filter((value) => typeof value === "string")
        : [];
      if (!payloadMatchesProposal(payload, proposalId)) {
        throw new Error("Preview binding does not match the selected review item.");
      }
      state.preview = preview;
      state.selectedNodeIds = selectedNodeIds;
      if (state.view === "proposal") {
        render();
      }
      announce("Complete proposal preview loaded. Review the evidence and selected nodes.");
    } catch (_error) {
      if (state.proposalId === proposalId && state.proposalGeneration === generation) {
        discardProposalReview();
        render();
        announce("Proposal preview is unavailable. No decision was applied.");
      }
    }
  }

  async function loadAssessment(
    cursor = null,
    focus = null,
    focusRole = null,
    requestedSelection = null
  ) {
    if (
      (cursor !== null && typeof cursor !== "string") ||
      (focus !== null && typeof focus !== "string") ||
      (requestedSelection !== null && typeof requestedSelection !== "string")
    ) {
      return;
    }
    const previousSelection = assessmentState.selectedNodeId;
    const initialAssessment = assessmentState.report === null;
    const generation = assessmentState.generation + 1;
    assessmentState.generation = generation;
    assessmentState.unavailable = false;
    if (!assessmentState.report && state.view === "assessment") {
      render();
    }
    let path = focus === null
      ? api.assessment
      : `${api.assessment}?focus=${encodeURIComponent(focus)}`;
    if (cursor !== null) {
      path += `${focus === null ? "?" : "&"}cursor=${encodeURIComponent(cursor)}`;
    }
    try {
      const accepted = acceptAssessmentResponse(await fetchJson(path), focus);
      if (assessmentState.generation !== generation) {
        return;
      }
      let nextSelection = null;
      if (requestedSelection !== null) {
        if (
          focus !== requestedSelection ||
          !accepted.rows.some((node) => node.node_id === requestedSelection)
        ) {
          throw new Error("Requested assessment selection was not authenticated.");
        }
        nextSelection = requestedSelection;
      } else if (
        previousSelection !== null &&
        accepted.rows.some((node) => node.node_id === previousSelection)
      ) {
        nextSelection = previousSelection;
      } else if (initialAssessment && accepted.rows.length > 0) {
        nextSelection = accepted.rows[0].node_id;
      }
      assessmentState.report = accepted.report;
      assessmentState.pageRows = accepted.rows;
      assessmentState.pageCursor = cursor;
      assessmentState.pageFocus = focus;
      assessmentState.nextCursor = accepted.nextCursor;
      assessmentState.selectedNodeId = nextSelection;
      assessmentState.unavailable = false;
      if (state.view === "assessment") {
        render();
        if (focusRole !== null && assessmentState.selectedNodeId !== null) {
          focusAssessmentRepresentation(focusRole, assessmentState.selectedNodeId);
        }
      }
      announce("Graph assessment updated from the canonical server snapshot.");
    } catch (_error) {
      if (assessmentState.generation !== generation) {
        return;
      }
      assessmentState.report = null;
      assessmentState.pageRows = [];
      assessmentState.pageCursor = null;
      assessmentState.pageFocus = null;
      assessmentState.nextCursor = null;
      assessmentState.selectedNodeId = null;
      assessmentState.unavailable = true;
      if (state.view === "assessment") {
        render();
      }
      announce("Assessment is unavailable. No canonical state was changed.");
    }
  }

  async function showView(view) {
    if (!viewNames.has(view)) {
      return;
    }
    if (view !== "proposal" && state.view === "proposal") {
      discardProposalReview();
    }
    state.view = view;
    render();
    app.focus();
    if (view === "inbox") {
      await loadInbox();
    } else if (view === "proposal") {
      await loadPreview();
    } else if (view === "team_state") {
      await refreshTeamEnrollment();
    } else if (view === "assessment") {
      await loadAssessment(null);
    } else if (view === "enrichment" && enrichmentState.session) {
      await updateEnrichment(
        api.enrichmentCurrent,
        JSON.stringify({ session_id: enrichmentState.session.id })
      );
    }
  }

  function base64urlToBuffer(value) {
    if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
      throw new Error("Invalid WebAuthn base64url value.");
    }
    const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
    const binary = atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes.buffer;
  }

  function bufferToBase64url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    for (const byte of bytes) {
      binary += String.fromCharCode(byte);
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function clearBuffer(value) {
    if (value instanceof ArrayBuffer) {
      new Uint8Array(value).fill(0);
    }
  }

  function clearOptionBuffers(options) {
    const publicKey = options && options.publicKey;
    if (!publicKey || typeof publicKey !== "object") {
      return;
    }
    clearBuffer(publicKey.challenge);
    if (publicKey.user) {
      clearBuffer(publicKey.user.id);
    }
    for (const credential of publicKey.excludeCredentials || []) {
      clearBuffer(credential.id);
    }
    for (const credential of publicKey.allowCredentials || []) {
      clearBuffer(credential.id);
    }
  }

  function convertCredentialDescriptors(descriptors) {
    if (!Array.isArray(descriptors)) {
      return;
    }
    for (const descriptor of descriptors) {
      if (!descriptor || typeof descriptor !== "object") {
        throw new Error("Invalid WebAuthn credential descriptor.");
      }
      descriptor.id = base64urlToBuffer(descriptor.id);
    }
  }

  function creationOptions(options) {
    const publicKey = options && options.publicKey;
    if (!publicKey || typeof publicKey !== "object" || !publicKey.user || typeof publicKey.user !== "object") {
      throw new Error("Invalid registration options.");
    }
    publicKey.challenge = base64urlToBuffer(publicKey.challenge);
    publicKey.user.id = base64urlToBuffer(publicKey.user.id);
    convertCredentialDescriptors(publicKey.excludeCredentials);
    publicKey.authenticatorSelection = publicKey.authenticatorSelection || {};
    publicKey.authenticatorSelection.userVerification = "required";
    return options;
  }

  function requestOptions(options) {
    const publicKey = options && options.publicKey;
    if (!publicKey || typeof publicKey !== "object") {
      throw new Error("Invalid authentication options.");
    }
    publicKey.challenge = base64urlToBuffer(publicKey.challenge);
    convertCredentialDescriptors(publicKey.allowCredentials);
    publicKey.userVerification = "required";
    return options;
  }

  function serializeCredential(credential) {
    if (!credential || credential.type !== "public-key" || !credential.response) {
      throw new Error("Invalid WebAuthn credential.");
    }
    const response = credential.response;
    const result = {
      id: credential.id,
      rawId: bufferToBase64url(credential.rawId),
      type: credential.type,
      response: {
        clientDataJSON: bufferToBase64url(response.clientDataJSON),
      },
    };
    if (response.attestationObject instanceof ArrayBuffer) {
      result.response.attestationObject = bufferToBase64url(response.attestationObject);
    } else {
      result.response.authenticatorData = bufferToBase64url(response.authenticatorData);
      result.response.signature = bufferToBase64url(response.signature);
      result.response.userHandle = response.userHandle ? bufferToBase64url(response.userHandle) : null;
    }
    return result;
  }

  function clearSerializedCredential(response) {
    if (!response || typeof response !== "object") {
      return;
    }
    response.id = "";
    response.rawId = "";
    if (response.response) {
      for (const key of Object.keys(response.response)) {
        response.response[key] = null;
      }
    }
  }

  function cancellation(error) {
    return error && (error.name === "AbortError" || error.name === "NotAllowedError");
  }

  async function registerDevice() {
    let options = null;
    let credential = null;
    let response = null;
    let result = null;
    try {
      options = await fetchJson(api.registrationOptions, { method: "POST", body: "{}" });
      credential = await navigator.credentials.create({ publicKey: creationOptions(options).publicKey });
      response = serializeCredential(credential);
      result = await fetchJson(api.registrationVerify, {
        method: "POST",
        body: JSON.stringify({ response }),
      });
      announce("Device registration completed. Credential details are not shown or retained.");
    } catch (error) {
      announce(
        cancellation(error)
          ? "Device registration cancelled. No credential response was sent."
          : "Device registration could not complete. No decision was applied."
      );
    } finally {
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      result = null;
      response = null;
      credential = null;
      options = null;
    }
  }

  async function enrollTeamRecipient(field) {
    let identityProof = typeof field.value === "string" ? field.value : "";
    let options = null;
    let credential = null;
    let response = null;
    let result = null;
    let completed = false;
    if (!identityProof) {
      announce("Complete GitHub device authorization before team enrollment.");
      return;
    }
    try {
      options = await fetchJson(api.teamEnrollmentOptions, {
        method: "POST",
        body: JSON.stringify({ identity_proof: identityProof }),
      });
      field.value = "";
      identityProof = "";
      credential = await navigator.credentials.create({ publicKey: creationOptions(options).publicKey });
      response = serializeCredential(credential);
      result = await fetchJson(api.teamEnrollmentVerify, {
        method: "POST",
        body: JSON.stringify({ response }),
      });
      completed = true;
      announce("Team recipient enrolled after GitHub identity and WebAuthn verification.");
      await refreshTeamEnrollment(true);
    } catch (error) {
      if (!completed) {
        try {
          await fetchJson(api.teamEnrollmentCancel, { method: "POST", body: "{}" });
        } catch (_cancelError) {
          // The server also expires abandoned enrollment authority.
        }
      }
      announce(
        cancellation(error)
          ? "Team enrollment cancelled. No recipient key was created."
          : "Team enrollment could not complete. The project remains local-only."
      );
    } finally {
      field.value = "";
      identityProof = "";
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      result = null;
      response = null;
      credential = null;
      options = null;
    }
  }

  async function authorizeDecision() {
    const proposalId = state.proposalId;
    const generation = state.proposalGeneration;
    const preview = state.preview;
    if (
      !proposalId ||
      !preview ||
      !selectionSatisfied(preview.payload) ||
      !currentReviewMatches(proposalId, generation, preview)
    ) {
      announce("Review the exact server-bound decision before authorizing it.");
      return;
    }
    let payload = preview.payload;
    let options = null;
    let credential = null;
    let response = null;
    let result = null;
    let applied = false;
    try {
      options = await fetchJson(api.decisionOptions, {
        method: "POST",
        body: JSON.stringify({ payload }),
      });
      credential = await navigator.credentials.get({ publicKey: requestOptions(options).publicKey });
      if (!currentReviewMatches(proposalId, generation, preview)) {
        throw new Error("Review selection changed before verification.");
      }
      response = serializeCredential(credential);
      result = await fetchJson(api.decisionVerify, {
        method: "POST",
        body: JSON.stringify({ response, payload }),
      });
      applied = true;
      announce("Decision applied after user-verifying WebAuthn confirmation.");
    } catch (error) {
      announce(
        cancellation(error)
          ? "WebAuthn review cancelled. No decision was sent."
          : "The challenge may have expired or changed. Review the proposal again; no decision was applied."
      );
    } finally {
      if (!applied) {
        try {
          await discardPendingAnswer(payload);
        } catch (_error) {
          // The server also expires abandoned private previews at the authority deadline.
        }
      }
      clearOptionBuffers(options);
      clearSerializedCredential(response);
      result = null;
      response = null;
      credential = null;
      options = null;
      payload = null;
      if (currentReviewMatches(proposalId, generation, preview)) {
        discardProposalReview();
        if (state.view === "proposal") {
          state.view = "inbox";
        }
        render();
      }
    }
    if (applied) {
      void refreshStatus();
    }
  }

  for (const button of navigation) {
    button.addEventListener("click", () => showView(button.dataset.view));
  }

  async function start() {
    try {
      await exchangeCsrfBootstrap();
      render();
      await refreshStatus();
      await refreshDevelopmentObservation(true);
    } catch (_error) {
      render();
      announce("Local review bootstrap is unavailable. Restart intent dev.");
    }
  }

  void start();
})();
