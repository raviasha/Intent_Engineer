# Intent Graph Visualization and Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add synchronized graph/table health visualization and resumable voluntary graph-improvement sessions over the deterministic assessment foundation.

**Architecture:** `ControlPlaneService` exposes one snapshot-bound assessment projection to the shipped UI, while non-canonical renderers consume the same scorecards. A dedicated append-only enrichment session store persists only progress and stable references; answers flow through existing conversation evidence, and proposed interpretations flow through existing clarification/proposal governance.

**Tech Stack:** Python 3.12, Pydantic v2, Starlette, vanilla JavaScript/CSS, Typer, JSONL stores, existing transaction coordinator, Mermaid, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-02-intent-graph-assessment-enrichment-design.md`

**Prerequisite:** Complete `docs/superpowers/plans/2026-09-02-intent-graph-assessment-foundation.md` and consume its public assessment contracts without duplicating rubric logic.

## Global Constraints

- Graph and table must display identical values from one `AssessmentReport`.
- Color is always paired with a label and icon; the worst significant gap determines color.
- Approved and projected scores must never share an unlabeled visual state.
- A user can start a voluntary improvement session without first submitting a new requirement.
- Large-graph responses and DOM rendering are bounded and paginated.
- Improvement sessions accept a 5, 15, or 30 minute budget, one visible bounded focus reference, or both; at least one is required.
- Questions are deterministic, one at a time, and ordered by red critical path, confidence impact, robustness impact, dependency reach, staleness, then stable ID.
- Human answers are stored immediately as attributed evidence; session state stores references, not duplicate plaintext.
- Skipping never fabricates an answer; pausing never mutates the graph.
- Only existing reviewed proposals and ChangeSets may change canonical graph state.
- Plugin behavior remains advisory: it may navigate, but cannot answer or approve.

---

### Task 1: Add control-plane assessment HTTP contracts

**Files:**
- Modify: `src/intent_engineering/control_plane/http_models.py`
- Modify: `src/intent_engineering/control_plane/service.py`
- Modify: `src/intent_engineering/control_plane/web.py`
- Test: `tests/contract/control_plane/test_assessment_http.py`
- Test: `tests/integration/control_plane/test_assessment_service.py`

**Interfaces:**
- Consumes: `Runtime.assessment_snapshot`, `GraphAssessmentService.assess`, and existing control-plane authority snapshots.
- Produces: `ControlPlaneService.assessment(focus: str | None = None) -> dict[str, object]`, `GET /api/v1/assessment`, and `GET /api/v1/assessment/nodes/{node_id}`.

- [ ] **Step 1: Write strict HTTP/service REDs**

```python
def test_assessment_get_uses_one_authority_snapshot_without_writes(client, service) -> None:
    before = service.durable_bytes()
    response = client.get("/api/v1/assessment", headers={"Host": service.host})
    assert response.status_code == 200
    assert response.json()["assessment"]["snapshot_digest"].startswith("sha256:")
    assert service.durable_bytes() == before


def test_hidden_node_lookup_is_indistinguishable_from_unknown(client) -> None:
    hidden = client.get("/api/v1/assessment/nodes/req:hidden")
    unknown = client.get("/api/v1/assessment/nodes/req:unknown")
    assert (hidden.status_code, hidden.content) == (unknown.status_code, unknown.content)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/control_plane/test_assessment_http.py tests/integration/control_plane/test_assessment_service.py -q -W error`

Expected: routes and service methods are absent.

- [ ] **Step 3: Implement bounded response models and public service methods**

Add strict response models mirroring assessment contracts with maximum 2,000 nodes per response, 100 rows per page, 64 rubric checks per dimension, and identifiers capped at existing graph-ID limits. Acquire one authority snapshot, build the assessment from descriptor-held inputs, reauthenticate authority before returning, and scrub report/snapshot locals on every failure and cancellation path.

- [ ] **Step 4: Register canonical read-only routes**

Allow absent Origin only under the existing exact-loopback GET rule. Reject query aliases, duplicate query keys, percent-encoding variants, scalar subclasses, oversized raw targets, and stale page cursors before service behavior.

- [ ] **Step 5: Run control-plane compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/control_plane tests/integration/control_plane -q -W error`

Expected: all pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/intent_engineering/control_plane/http_models.py src/intent_engineering/control_plane/service.py src/intent_engineering/control_plane/web.py tests/contract/control_plane/test_assessment_http.py tests/integration/control_plane/test_assessment_service.py
git commit -m "feat: expose graph assessment to the local control plane"
```

---

### Task 2: Build the synchronized graph and score table

**Files:**
- Modify: `src/intent_engineering/control_plane/assets/index.html`
- Modify: `src/intent_engineering/control_plane/assets/app.js`
- Modify: `src/intent_engineering/control_plane/assets/styles.css`
- Test: `tests/contract/control_plane/test_assessment_assets.py`
- Test: `tests/e2e/test_intent_dev_assessment_ui.py`

**Interfaces:**
- Consumes: Task 1 assessment JSON only.
- Produces: an Assessment page with shared `selectedNodeId`, filters, accessible graph nodes, paginated table rows, detail panel, and approved/projected overlay switch.

- [ ] **Step 1: Write shipped-asset runtime REDs**

```javascript
const screen = await loadAssessmentFixture("red-critical-path.json");
screen.clickGraphNode("req:csv");
assert.equal(screen.selectedTableRow(), "req:csv");
assert.equal(screen.detail().healthLabel, "Red");
assert.equal(screen.detail().worstDimension, "test_verification");
assert.equal(screen.detail().overallScore, "88");
```

Drive the actual packaged JavaScript in the repository's existing DOM harness. Add reciprocal table-to-graph selection, keyboard selection, accessible labels, filters, pagination, approved/projected labeling, and HTML/script-injection fixtures.

- [ ] **Step 2: Run asset tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/control_plane/test_assessment_assets.py tests/e2e/test_intent_dev_assessment_ui.py -q -W error`

Expected: the Assessment view and selectors are absent.

- [ ] **Step 3: Implement one shared client state and safe DOM rendering**

```javascript
const assessmentState = Object.seal({
  report: null,
  selectedNodeId: null,
  filters: Object.seal({ health: null, dimension: null, type: null }),
  pageCursor: null,
  overlay: "approved",
});
```

Use `textContent`, explicit DOM constructors, bounded arrays, and deterministic stable IDs. Do not use `innerHTML`, local storage, console logging, authorization tokens, or client-side score calculation. The graph and table read the same scorecard objects.

- [ ] **Step 4: Add accessible styling and large-graph bounds**

Define green/orange/red/unassessed CSS variables with sufficient contrast, visible icons/text, focus rings, and non-color differentiation. Render only the server-provided bounded graph subset and current table page.

- [ ] **Step 5: Run shipped UI and packaging GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/control_plane/test_assessment_assets.py tests/contract/control_plane/test_web_assets.py tests/e2e/test_intent_dev_assessment_ui.py tests/e2e/test_intent_dev_web_runtime.py -q -W error`

Build the wheel offline and assert `index.html`, `app.js`, and `styles.css` contain the assessment experience.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/intent_engineering/control_plane/assets/index.html src/intent_engineering/control_plane/assets/app.js src/intent_engineering/control_plane/assets/styles.css tests/contract/control_plane/test_assessment_assets.py tests/e2e/test_intent_dev_assessment_ui.py
git commit -m "feat: visualize graph robustness in intent dev"
```

---

### Task 3: Add deterministic health overlays to generated renders

**Files:**
- Modify: `src/intent_engineering/render/mermaid.py`
- Modify: `src/intent_engineering/render/markdown.py`
- Modify: `src/intent_engineering/render/renderer.py`
- Modify: `src/intent_engineering/cli/app.py`
- Test: `tests/unit/render/test_assessment_renderers.py`
- Test: `tests/e2e/test_cli_assessment.py`

**Interfaces:**
- Consumes: `AssessmentReport` and the same visible `Graph` it assesses.
- Produces: `render_mermaid(graph, assessment=None)`, `render_markdown(graph, cases, assessment=None)`, `GraphRenderer(..., assessment=None)`, and `intent render --assessment`.

- [ ] **Step 1: Write stable render REDs**

```python
def test_mermaid_health_styles_are_stable_and_noncanonical(render_fixture) -> None:
    before = render_fixture.graph_path.read_bytes()
    output = render_mermaid(render_fixture.graph, render_fixture.assessment)
    assert 'classDef health_red' in output
    assert 'req_csv:::health_red' in output
    assert 'Test verification: 40' in output
    assert render_fixture.graph_path.read_bytes() == before
```

- [ ] **Step 2: Run renderer tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/render/test_assessment_renderers.py -q -W error`

Expected: render functions reject the assessment argument.

- [ ] **Step 3: Implement escaped health projections**

Require report graph ID/version/snapshot digest to match the rendered graph projection. Emit deterministic class definitions and textual labels. Markdown adds a score table and evidence-linked deductions. Never copy hidden scorecards into a visible graph.

- [ ] **Step 4: Add CLI flag and run rendering compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/render tests/e2e/test_cli_assessment.py tests/e2e/test_cli_local.py -q -W error`

Expected: legacy render output is byte-identical without `--assessment`; assessed output is stable and non-canonical.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/intent_engineering/render src/intent_engineering/cli/app.py tests/unit/render/test_assessment_renderers.py tests/e2e/test_cli_assessment.py
git commit -m "feat: render intent health overlays"
```

---

### Task 4: Persist bounded enrichment session progress

**Files:**
- Create: `src/intent_engineering/intent_workflow/enrichment_models.py`
- Create: `src/intent_engineering/intent_workflow/enrichment_store.py`
- Modify: `src/intent_engineering/cli/runtime.py`
- Modify: `src/intent_engineering/storage/transaction.py`
- Test: `tests/unit/intent_workflow/test_enrichment_store.py`
- Test: `tests/integration/intent_workflow/test_enrichment_recovery.py`

**Interfaces:**
- Consumes: existing secure JSONL framing and `LocalTransactionCoordinator`.
- Produces: `EnrichmentSession`, `EnrichmentEvent`, `EnrichmentSessionStore.append(event)`, `.latest(session_id)`, `.events(session_id=None)`, and a new `enrichment_sessions` transaction target.

- [ ] **Step 1: Write strict store/recovery REDs**

```python
def test_session_state_contains_references_not_answer_plaintext(store) -> None:
    store.append(answered_event(answer_evidence_ref="evidence:answer:1"))
    assert b"PRIVATE ANSWER" not in store.bytes()
    assert store.latest("refine:1").answer_evidence_refs == ("evidence:answer:1",)


def test_torn_enrichment_and_evidence_append_rolls_back(runtime) -> None:
    runtime.fail_after("enrichment_sessions")
    with pytest.raises(InjectedCrash):
        runtime.answer_enrichment("refine:1", "Workspace owners")
    assert runtime.evidence_bytes() == runtime.before_evidence
    assert runtime.enrichment_bytes() == runtime.before_enrichment
```

- [ ] **Step 2: Run store tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_enrichment_store.py tests/integration/intent_workflow/test_enrichment_recovery.py -q -W error`

Expected: modules and transaction target are absent.

- [ ] **Step 3: Implement append-only lifecycle records**

Use exact lifecycle transitions `open -> paused|complete|cancelled`, contiguous sequence, exact predecessor event digest, UTC timestamps, optional bounded focus ID, optional budget enum `{5,15,30}`, remaining active-budget seconds, stable answered/skipped gap IDs, and evidence references only. Require focus or budget, exclude paused time from the budget, and reject duplicate/conflicting frames and noncanonical schema-1 JSON.

- [ ] **Step 4: Wire secure store and recovery domain**

Add `.intent/history/enrichment-sessions.jsonl` to `Runtime`, close it in `Runtime.close`, and add it to current transaction target sets without changing legacy recovery decoding.

- [ ] **Step 5: Run store/storage/recovery GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_enrichment_store.py tests/integration/intent_workflow/test_enrichment_recovery.py tests/unit/storage tests/integration/test_startup_transaction_recovery.py -q -W error`

- [ ] **Step 6: Commit Task 4**

```bash
git add src/intent_engineering/intent_workflow/enrichment_models.py src/intent_engineering/intent_workflow/enrichment_store.py src/intent_engineering/cli/runtime.py src/intent_engineering/storage/transaction.py tests/unit/intent_workflow/test_enrichment_store.py tests/integration/intent_workflow/test_enrichment_recovery.py
git commit -m "feat: persist resumable graph enrichment sessions"
```

---

### Task 5: Implement question prioritization and evidence-backed enrichment

**Files:**
- Create: `src/intent_engineering/intent_workflow/enrichment.py`
- Modify: `src/intent_engineering/intent_workflow/conversation.py`
- Test: `tests/integration/intent_workflow/test_enrichment.py`

**Interfaces:**
- Consumes: `GraphAssessmentService`, `AssessmentReport`, `EnrichmentSessionStore`, existing conversation evidence capture, proposal service, and one authenticated Runtime transaction.
- Produces: `GraphEnrichmentService.start(minutes: Literal[5, 15, 30] | None = None, focus: str | None = None)`, `.current(session_id)`, `.answer(session_id, gap_id, answer)`, `.skip(session_id, gap_id)`, `.pause(session_id)`, `.resume(session_id)`, and `.propose(session_id, submission)`.

- [ ] **Step 1: Write priority/lifecycle REDs**

```python
def test_next_question_uses_approved_stable_priority(enrichment) -> None:
    session = enrichment.start(minutes=5)
    assert session.current_gap_id == "gap:red-critical"
    answered = enrichment.answer(session.id, session.current_gap_id, "Workspace owners")
    assert answered.answer_evidence_refs == ("evidence:conversation:answer-1",)
    assert enrichment.graph_bytes() == enrichment.before_graph


def test_pause_restart_resume_preserves_evidence_and_reassesses(enrichment) -> None:
    paused = enrichment.pause(enrichment.start(minutes=15).id)
    enrichment.restart()
    resumed = enrichment.resume(paused.id)
    assert resumed.snapshot_digest == enrichment.current_assessment().snapshot_digest


def test_active_budget_exhaustion_completes_without_fabricating_data(enrichment) -> None:
    session = enrichment.start(minutes=5)
    enrichment.clock.advance_active(minutes=5)
    completed = enrichment.current(session.id)
    assert completed.status == "complete"
    assert completed.answer_evidence_refs == ()
    assert enrichment.graph_bytes() == enrichment.before_graph
```

- [ ] **Step 2: Run enrichment tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_enrichment.py -q -W error`

Expected: `GraphEnrichmentService` is absent.

- [ ] **Step 3: Implement deterministic gap selection**

Sort eligible gaps by `(health severity, critical path, confidence impact, robustness impact, negative dependency reach, staleness severity, gap_id)`. Convert known rubric rules to fixed question templates; an optional wording port may rephrase only after returning the same gap ID, requested fields, and evidence scope.

- [ ] **Step 4: Implement atomic answer capture and stale handling**

Under one transaction, authenticate actor/config/ACL/policy/snapshot/session preimages, capture a human conversation evidence record, and append the session event. If state changes, preserve already durable answers, invalidate stale candidate interpretations, reassess, and select the next eligible gap. Exact replay returns the same detached result; divergence fails fixed.

Use an injected monotonic/fixed clock to debit only active interaction time. Budget exhaustion completes the session without fabricating an answer or proposal; a focus-only session completes when no eligible visible gaps remain.

- [ ] **Step 5: Delegate interpretation to existing proposal governance**

`propose()` accepts typed graph effects and evidence refs, requires all referenced session answers to be current and visible, then calls the existing clarification/proposal service. It never applies the proposal.

- [ ] **Step 6: Run enrichment/clarification/proposal GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_enrichment.py tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py -q -W error`

- [ ] **Step 7: Commit Task 5**

```bash
git add src/intent_engineering/intent_workflow/enrichment.py src/intent_engineering/intent_workflow/conversation.py tests/integration/intent_workflow/test_enrichment.py
git commit -m "feat: guide progressive intent graph enrichment"
```

---

### Task 6: Expose `intent refine` and browser enrichment controls

**Files:**
- Create: `src/intent_engineering/cli/refine.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/control_plane/http_models.py`
- Modify: `src/intent_engineering/control_plane/service.py`
- Modify: `src/intent_engineering/control_plane/web.py`
- Modify: `src/intent_engineering/control_plane/assets/app.js`
- Modify: `src/intent_engineering/control_plane/assets/index.html`
- Test: `tests/e2e/test_cli_refine.py`
- Test: `tests/e2e/test_intent_dev_enrichment.py`

**Interfaces:**
- Consumes: Task 5 `GraphEnrichmentService` only through public methods.
- Produces: `intent refine [--minutes 5|15|30] [--focus ID]` with at least one option, HTTP start/current/answer/skip/pause/resume/propose routes, and corresponding `intent dev` controls.

- [ ] **Step 1: Write CLI/browser journey REDs**

```python
def test_five_minute_refine_session_can_pause_and_resume(refine_harness) -> None:
    started = refine_harness.start(minutes=5)
    answered = refine_harness.answer(started.question_id, "Workspace owners")
    paused = refine_harness.pause()
    resumed = refine_harness.restart_and_resume(paused.session_id)
    assert answered.evidence_ref in resumed.answer_evidence_refs
    assert refine_harness.graph_bytes() == refine_harness.before_graph
```

- [ ] **Step 2: Run journey tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_refine.py tests/e2e/test_intent_dev_enrichment.py -q -W error`

Expected: CLI command, routes, and UI controls are absent.

- [ ] **Step 3: Implement strict adapters**

CLI prompts only on a TTY and supports structured output for start/current/pause status. HTTP POSTs require existing exact Origin+CSRF protections and strict JSON. The UI shows time choices, focus, one question, why it matters, score dimension, answer/skip/pause, and a clearly separate proposal review action.

- [ ] **Step 4: Add cancellation, restart, stale, and secrecy journeys**

Assert no raw answer in session rows, logs, fixed errors, URL/query state, client persistence, or repository traceback locals; answer text appears only in its authorized evidence record and transient form control.

- [ ] **Step 5: Run CLI/control-plane/plugin compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_refine.py tests/e2e/test_intent_dev_enrichment.py tests/contract/control_plane tests/integration/control_plane tests/contract/agent_host -q -W error`

- [ ] **Step 6: Commit Task 6**

```bash
git add src/intent_engineering/cli/refine.py src/intent_engineering/cli/app.py src/intent_engineering/control_plane tests/e2e/test_cli_refine.py tests/e2e/test_intent_dev_enrichment.py
git commit -m "feat: add on-demand graph improvement sessions"
```

---

### Task 7: Finish enrichment MCP reads, documentation, and release proof

**Files:**
- Modify: `src/intent_engineering/integrations/mcp_server/tools.py`
- Modify: `src/intent_engineering/integrations/mcp_server/server.py`
- Test: `tests/contract/mcp/test_enrichment_tools.py`
- Modify: `README.md`
- Modify: `docs/assessment.md`
- Modify: `docs/intent-aware-agent.md`
- Create: `tests/e2e/test_intent_graph_enrichment_journey.py`

**Interfaces:**
- Consumes: assessment and enrichment public read methods.
- Produces: read-only `intent_enrichment_status(session_id)` and `intent_enrichment_next_question(session_id)` tools plus the documented voluntary improvement journey.

- [ ] **Step 1: Write MCP/release REDs**

```python
async def test_mcp_can_read_but_not_answer_enrichment_question(enrichment_mcp) -> None:
    tools = {item.name for item in await enrichment_mcp.list_tools()}
    assert "intent_enrichment_next_question" in tools
    assert "intent_enrichment_answer" not in tools
    before = enrichment_mcp.durable_bytes()
    await enrichment_mcp.call("intent_enrichment_next_question", {"session_id": "refine:1"})
    assert enrichment_mcp.durable_bytes() == before
```

- [ ] **Step 2: Run MCP/journey tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/mcp/test_enrichment_tools.py tests/e2e/test_intent_graph_enrichment_journey.py -q -W error`

Expected: read tools and documented journey are absent.

- [ ] **Step 3: Register bounded read-only tools and document behavior**

Use the existing strict raw boundary. Document Improve graph, `intent refine`, pause/resume, immediate evidence capture, no pre-approval graph mutation, score projections, offline behavior, and plugin advisory limits.

- [ ] **Step 4: Run focused, affected, full, static, package, and help gates**

Run all assessment/enrichment/control-plane/render/MCP tests, then existing onboarding/clarification/proposal/assurance compatibility, then the exact full offline suite. Run Ruff, format, mypy, wheel asset verification, CLI helps, and `git diff --check`.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment tests/integration/assessment tests/integration/intent_workflow/test_enrichment.py tests/contract/control_plane tests/integration/control_plane tests/unit/render tests/contract/mcp/test_enrichment_tools.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
uv build --offline
.venv/bin/intent refine --help
.venv/bin/intent render --help
git diff --check
git status --short
```

- [ ] **Step 5: Commit Task 7**

```bash
git add src/intent_engineering/integrations/mcp_server/tools.py src/intent_engineering/integrations/mcp_server/server.py tests/contract/mcp/test_enrichment_tools.py README.md docs/assessment.md docs/intent-aware-agent.md tests/e2e/test_intent_graph_enrichment_journey.py
git commit -m "docs: release visual intent graph enrichment"
```

- [ ] **Step 6: Request independent code review**

Review for graph/table disagreement, hidden-evidence inference, stale projected scores, plaintext duplication, bypass of proposal governance, unbounded rendering, or plugin authority expansion.
