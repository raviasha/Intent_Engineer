# Intent Graph Assessment Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, explainable, ACL-safe robustness scorecards for graph nodes, intent branches, and projects through shared application, CLI, MCP, and CI boundaries.

**Architecture:** A new `intent_engineering.assessment` package owns immutable contracts, versioned rubric policy, snapshot construction, and pure scoring. Adapters load one descriptor-held snapshot, call `GraphAssessmentService`, and render its detached report without writing canonical graph state or invoking a model provider.

**Tech Stack:** Python 3.12, Pydantic v2 strict models, Typer, existing YAML/JSONL stores and transaction coordinator, MCP v2, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-02-intent-graph-assessment-enrichment-design.md`

## Global Constraints

- Assessment is derived output and must never write graph, evidence, case, clarification, proposal, approval, receipt, or history state.
- Scores are deterministic integers from 0–100; model output never contributes directly.
- Health uses the worst significant required gap: green `>=75`, orange `50–74`, red `<50`; green also requires confidence `>=75` and no blocking conflict.
- Non-applicable dimensions are `N/A` and excluded from rollups.
- Default dimension and branch weights are equal; custom integer weights are published and included in the policy digest.
- Critical red paths cap branch and project robustness at 49.
- All reports bind exact graph, evidence, ingestion, case, clarification, history, policy, and principal-projection identities.
- ACL-hidden state must not be inferable through values, counts, topology, errors, or timing-dependent branches.
- Repeated assessment of an identical snapshot produces byte-identical semantic JSON.
- CI compares repository states directly and never requires a second intent-branch checkout or the prompt plugin.
- Before Task 1, run the full offline suite. Resolve or explicitly triage the existing Codex-version assertion and multiprocessing hang separately; do not weaken those tests as part of assessment work.

---

### Task 1: Define strict assessment contracts and versioned policy

**Files:**
- Create: `src/intent_engineering/assessment/__init__.py`
- Create: `src/intent_engineering/assessment/models.py`
- Create: `src/intent_engineering/assessment/policy.py`
- Test: `tests/unit/assessment/test_models.py`
- Test: `tests/unit/assessment/test_policy.py`

**Interfaces:**
- Consumes: `StrictModel`, `Graph`, `EvidenceRecord`, `EvidenceIngestion`, `ReconciliationCase`, `ClarificationEvent`, and `ChangeSet`.
- Produces: `AssessmentDimension`, `AssessmentHealth`, `DimensionApplicability`, `RubricCheck`, `DimensionResult`, `NodeScorecard`, `BranchScorecard`, `ProjectScorecard`, `AssessmentReport`, `AssessmentSnapshot`, and `AssessmentPolicy.v1()`.

- [ ] **Step 1: Write contract REDs**

```python
def test_report_is_strict_frozen_and_canonically_ordered() -> None:
    report = report_fixture(node_ids=("req:z", "req:a"))
    assert tuple(item.node_id for item in report.nodes) == ("req:a", "req:z")
    assert AssessmentReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        AssessmentReport.model_validate({**report.model_dump(), "unknown": True})


def test_v1_policy_has_explicit_thresholds_equal_default_weights_and_digest() -> None:
    policy = AssessmentPolicy.v1()
    assert (policy.red_below, policy.green_at, policy.green_confidence_at) == (50, 75, 75)
    assert set(policy.dimension_weights.values()) == {1}
    assert policy.digest == "sha256:" + hashlib.sha256(policy.canonical_bytes()).hexdigest()
```

- [ ] **Step 2: Run the contract tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment/test_models.py tests/unit/assessment/test_policy.py -q -W error`

Expected: collection fails because `intent_engineering.assessment` does not exist.

- [ ] **Step 3: Implement the minimal strict contracts**

```python
class AssessmentDimension(StrEnum):
    INTENT_CLARITY = "intent_clarity"
    EVIDENCE_STRENGTH = "evidence_strength"
    REQUIREMENT_COVERAGE = "requirement_coverage"
    IMPLEMENTATION_TRACEABILITY = "implementation_traceability"
    TEST_VERIFICATION = "test_verification"
    CONSISTENCY = "consistency"
    FRESHNESS = "freshness"


class AssessmentHealth(StrEnum):
    GREEN = "green"
    ORANGE = "orange"
    RED = "red"
    UNASSESSED = "unassessed"


class AssessmentPolicy(StrictModel):
    schema_version: Literal[1] = 1
    rubric_version: Literal["rubric:v1"] = "rubric:v1"
    red_below: int = 50
    green_at: int = 75
    green_confidence_at: int = 75
    dimension_weights: Mapping[AssessmentDimension, int]
    critical_node_types: tuple[NodeType, ...]
    critical_relations: tuple[RelationType, ...]
```

Validate exact built-in types before coercion, reject booleans as integers, sort/deduplicate stable IDs, expose immutable tuples/mappings, and compute digests from canonical JSON with `sort_keys=True`, compact separators, and UTF-8.

- [ ] **Step 4: Run model/policy tests GREEN and static checks**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment -q -W error`

Run: `.venv/bin/ruff check src/intent_engineering/assessment tests/unit/assessment && .venv/bin/mypy src/intent_engineering/assessment`

Expected: all pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/intent_engineering/assessment tests/unit/assessment
git commit -m "feat: define explainable graph assessment contracts"
```

---

### Task 2: Implement the deterministic rubric and applicability matrix

**Files:**
- Create: `src/intent_engineering/assessment/rubric.py`
- Test: `tests/unit/assessment/test_rubric.py`
- Test: `tests/fixtures/assessment/rubric-v1.yaml`

**Interfaces:**
- Consumes: `AssessmentPolicy`, one visible `AssessmentSnapshot`, and stable graph node/edge relationships.
- Produces: `assess_node(snapshot: AssessmentSnapshot, node_id: str, policy: AssessmentPolicy) -> NodeScorecard` and `applicability(node: Node, graph: Graph) -> Mapping[AssessmentDimension, DimensionApplicability]`.

- [ ] **Step 1: Write score/applicability REDs with hand-derived literals**

```python
@pytest.mark.parametrize(
    ("node_id", "dimension", "want"),
    [
        ("intent:export", AssessmentDimension.TEST_VERIFICATION, "inherited"),
        ("req:csv", AssessmentDimension.TEST_VERIFICATION, "required"),
        ("file:export", AssessmentDimension.INTENT_CLARITY, "not_applicable"),
    ],
)
def test_v1_applicability_is_explicit(rubric_snapshot, node_id, dimension, want) -> None:
    assert applicability(rubric_snapshot.node(node_id), rubric_snapshot.graph)[dimension] == want


def test_failed_rules_deduct_once_and_explain_the_score(rubric_snapshot) -> None:
    scorecard = assess_node(rubric_snapshot.snapshot, "req:csv", AssessmentPolicy.v1())
    verification = scorecard.dimension(AssessmentDimension.TEST_VERIFICATION)
    assert verification.score == 40
    assert [item.rule_id for item in verification.failed] == [
        "rubric:v1:test_verification:no_current_test_evidence"
    ]
    assert scorecard.health is AssessmentHealth.RED
```

- [ ] **Step 2: Run rubric tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment/test_rubric.py -q -W error`

Expected: import fails for `assessment.rubric`.

- [ ] **Step 3: Implement pure rule evaluation**

```python
Rule = Callable[[RubricContext], RubricCheck | None]


def score_dimension(rules: tuple[Rule, ...], context: RubricContext) -> DimensionResult:
    failed = tuple(sorted(filter(None, (rule(context) for rule in rules)), key=lambda x: x.rule_id))
    score = max(0, 100 - sum(item.points for item in failed))
    resolved = sum(item.resolved_inputs for item in context.input_checks)
    required = sum(item.required_inputs for item in context.input_checks)
    confidence = 0 if required == 0 else resolved * 100 // required
    return DimensionResult(score=score, confidence=confidence, failed=failed, ...)
```

Implement named V1 checks for clarity, evidence identity/currentness, requirement-to-intent coverage, implementation links, test links, blocking cases/contradictions, and evidence freshness. Never inspect labels to infer truth beyond explicit typed assertions and relations.

- [ ] **Step 4: Add boundary matrices**

Cover scores 0 and 100, exact 49/50/74/75 boundaries, inherited and optional dimensions, duplicate evidence references, missing visible inputs, blocking cases overriding averages, unknown node types, and permutation stability.

- [ ] **Step 5: Run rubric and existing assurance tests GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment tests/integration/intent_workflow/test_scheduled_assurance.py -q -W error`

Expected: all pass with warnings as errors.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/intent_engineering/assessment/rubric.py tests/unit/assessment/test_rubric.py tests/fixtures/assessment/rubric-v1.yaml
git commit -m "feat: score intent graphs with a deterministic rubric"
```

---

### Task 3: Build one ACL-safe descriptor-held assessment snapshot

**Files:**
- Create: `src/intent_engineering/assessment/snapshot.py`
- Modify: `src/intent_engineering/cli/runtime.py`
- Test: `tests/integration/assessment/test_snapshot.py`
- Test: `tests/integration/assessment/conftest.py`

**Interfaces:**
- Consumes: `Runtime`, authenticated actor, exact alias resolver, `LocalTransactionCoordinator.snapshot()`, and canonical parsers for graph, evidence, cases, history, and intent ledger.
- Produces: `build_assessment_snapshot(runtime: Runtime, actor: str) -> AssessmentSnapshot` and `Runtime.assessment_snapshot(actor: str) -> AssessmentSnapshot`.

- [ ] **Step 1: Write ACL, preimage, and replacement REDs**

```python
def test_snapshot_excludes_hidden_topology_without_count_leak(assessment_runtime) -> None:
    snapshot = build_assessment_snapshot(assessment_runtime.runtime, "local:asha")
    assert tuple(node.id for node in snapshot.graph.nodes) == ("intent:public", "req:public")
    assert snapshot.omitted_count is None
    assert "PRIVATE-HIDDEN" not in snapshot.model_dump_json()


@pytest.mark.parametrize("target", ("graph", "evidence", "cases", "history", "intent_proposals", "config"))
def test_same_version_replacement_during_snapshot_fails_fixed(assessment_runtime, target) -> None:
    assessment_runtime.replace_after_read(target)
    with pytest.raises(AssessmentUnavailable, match="assessment unavailable"):
        build_assessment_snapshot(assessment_runtime.runtime, "local:asha")
```

- [ ] **Step 2: Run snapshot tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/assessment/test_snapshot.py -q -W error`

Expected: collection fails for missing `assessment.snapshot`.

- [ ] **Step 3: Implement descriptor-held parsing and filtering**

Use one transaction snapshot over `graph`, `evidence`, `cases`, `history`, `intent_proposals`, and a bounded read-only `config` extra. Parse only bytes in that snapshot. Resolve exact live aliases once, filter evidence first, retain only nodes with complete visible provenance, retain only edges whose endpoints are retained, then filter cases/clarifications/history to visible references. Compute each component digest and an aggregate digest from canonical bytes.

```python
def build_assessment_snapshot(runtime: Runtime, actor: str) -> AssessmentSnapshot:
    held = runtime.transactions.snapshot(_ASSESSMENT_TARGETS, extras={"config": ...})
    parsed = _parse_held(held)
    visible = _filter_for_actor(parsed, actor, runtime.config)
    _assert_preimages_unchanged(runtime, held)
    return AssessmentSnapshot.from_visible(visible, preimages=held.content)
```

- [ ] **Step 4: Add hostile/cancellation tests**

Cover corrupt frames, duplicate IDs, symlink/FIFO/hardlink substitution, config/ACL drift, hidden-adjacent edges, cancellation identity, cause/context scrubbing, and absence of raw record/config content in repository traceback locals.

- [ ] **Step 5: Run snapshot and storage/recovery compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/assessment tests/unit/storage tests/integration/test_startup_transaction_recovery.py -q -W error`

Expected: all pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/intent_engineering/assessment/snapshot.py src/intent_engineering/cli/runtime.py tests/integration/assessment
git commit -m "feat: snapshot visible graph assessment inputs"
```

---

### Task 4: Produce node, branch, and project assessment reports

**Files:**
- Create: `src/intent_engineering/assessment/service.py`
- Create: `src/intent_engineering/assessment/rollup.py`
- Test: `tests/unit/assessment/test_rollup.py`
- Test: `tests/integration/assessment/test_service.py`

**Interfaces:**
- Consumes: `AssessmentSnapshot`, `AssessmentPolicy`, and Task 2's `assess_node`.
- Produces: `GraphAssessmentService.assess(snapshot, policy=None) -> AssessmentReport`, `GraphAssessmentService.projected(current, proposed_graph, policy=None) -> AssessmentComparison`, and a bounded in-memory cache keyed by aggregate snapshot, principal projection, and policy digests.

- [ ] **Step 1: Write rollup/report REDs**

```python
def test_red_critical_path_caps_branch_and_project_at_49(service, critical_snapshot) -> None:
    report = service.assess(critical_snapshot)
    assert report.branch("intent:export").robustness == 49
    assert report.project.robustness == 49
    assert report.project.health is AssessmentHealth.RED


def test_identical_snapshot_is_semantically_byte_identical(service, complete_snapshot) -> None:
    first = service.assess(complete_snapshot)
    second = service.assess(complete_snapshot)
    assert first.semantic_bytes() == second.semantic_bytes()
```

- [ ] **Step 2: Run service tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment/test_rollup.py tests/integration/assessment/test_service.py -q -W error`

Expected: imports fail for `assessment.service` and `assessment.rollup`.

- [ ] **Step 3: Implement critical paths and rollups**

Traverse only active typed relations declared in policy. Build branch membership from each visible `PRODUCT_INTENT`/`DESIRED_OUTCOME`; reject cycles from path membership rather than recursing forever. Compute equal-weight means, apply the red critical-path cap, choose worst dimension by health/severity/stable ID, and publish every contributing node ID and weight.

- [ ] **Step 4: Implement report cache and projected comparison**

Use a maximum 32-entry LRU of immutable detached reports. Cache keys include snapshot aggregate, principal projection, and policy digest. `projected()` validates a proposed graph overlay against the current graph identity and returns current/projected values without writing either graph.

- [ ] **Step 5: Add cache/ACL/staleness/concurrency tests and run GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment tests/integration/assessment -q -W error`

Expected: identical concurrent assessments converge; actor/policy/snapshot changes never share a cache entry; stale overlays fail fixed.

- [ ] **Step 6: Commit Task 4**

```bash
git add src/intent_engineering/assessment/service.py src/intent_engineering/assessment/rollup.py tests/unit/assessment tests/integration/assessment
git commit -m "feat: assess graph robustness and critical paths"
```

---

### Task 5: Add `intent assess` and CI policy evaluation

**Files:**
- Create: `src/intent_engineering/cli/assessment.py`
- Modify: `src/intent_engineering/cli/app.py`
- Create: `src/intent_engineering/assessment/gate.py`
- Test: `tests/e2e/test_cli_assessment.py`
- Test: `tests/unit/assessment/test_gate.py`
- Modify: `.github/workflows/intent-sync.yml`
- Test: `tests/e2e/test_github_action_workflow.py`

**Interfaces:**
- Consumes: `Runtime.assessment_snapshot`, `GraphAssessmentService`, and versioned CLI output helpers.
- Produces: `intent assess --project . [--focus ID] [--format text|json|markdown]`, `AssessmentGate.evaluate(base, head, policy) -> GateResult`, and assessment artifact generation in CI.

- [ ] **Step 1: Write CLI/gate REDs**

```python
def test_assess_json_is_read_only_versioned_and_explainable(initialized_project) -> None:
    before = durable_bytes(initialized_project)
    result = run_intent(initialized_project, "assess", "--format", "json")
    assert result.returncode == 0
    assert json.loads(result.stdout)["assessment"]["schema_version"] == 1
    assert json.loads(result.stdout)["assessment"]["nodes"][0]["dimensions"]
    assert durable_bytes(initialized_project) == before


def test_gate_fails_only_for_new_red_critical_gap() -> None:
    assert AssessmentGate().evaluate(base=green_report(), head=red_report()).exit_code == 5
    assert AssessmentGate().evaluate(base=red_report(), head=red_report()).exit_code == 0
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_assessment.py tests/unit/assessment/test_gate.py -q -W error`

Expected: CLI reports unknown command and gate import fails.

- [ ] **Step 3: Implement CLI and gate**

Keep the command read-only, close `Runtime` in `finally`, validate `--focus` against visible node/branch IDs, and emit fixed errors. Gate rules support `new_red`, `minimum_confidence`, `orange_warning`, and `robustness_regression`; default CI behavior emits an artifact and fails only on newly introduced red critical gaps.

- [ ] **Step 4: Update clean-checkout workflow and structural test**

Add install/init/restore steps already required by the workflow, run `intent assess --format json`, compare with the merge-base artifact when present, and upload the versioned report. Do not install or depend on the prompt plugin.

- [ ] **Step 5: Run CLI/workflow/compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_assessment.py tests/unit/assessment/test_gate.py tests/e2e/test_github_action_workflow.py tests/e2e/test_cli_local.py -q -W error`

Expected: all pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add src/intent_engineering/cli/assessment.py src/intent_engineering/cli/app.py src/intent_engineering/assessment/gate.py tests/e2e/test_cli_assessment.py tests/unit/assessment/test_gate.py .github/workflows/intent-sync.yml tests/e2e/test_github_action_workflow.py
git commit -m "feat: expose graph assessment to developers and CI"
```

---

### Task 6: Expose read-only assessment through MCP

**Files:**
- Modify: `src/intent_engineering/integrations/mcp_server/tools.py`
- Modify: `src/intent_engineering/integrations/mcp_server/server.py`
- Test: `tests/contract/mcp/test_assessment_tools.py`
- Test: `tests/e2e/test_mcp_server.py`

**Interfaces:**
- Consumes: `McpReadServices.runtime`, `GraphAssessmentService`, and `Runtime.assessment_snapshot`.
- Produces: `intent_assessment_summary()`, `intent_assessment_scorecard(reference)`, and `intent_assessment_gaps(limit=20, health=None)` as read-only structured-output tools.

- [ ] **Step 1: Write raw-boundary and production REDs**

```python
async def test_production_assessment_tools_are_read_only_and_token_free(mcp_project) -> None:
    before = durable_bytes(mcp_project)
    result = await call_tool("intent_assessment_summary", {})
    assert result["assessment"]["project"]["health"] in {"green", "orange", "red", "unassessed"}
    assert "token" not in json.dumps(result).casefold()
    assert durable_bytes(mcp_project) == before
```

Add exact-container, scalar-subclass, cycle/alias, UTF-8-size, bounded `limit`, invalid health, unknown/hidden reference, cancellation, and traceback-secrecy cases before handlers run.

- [ ] **Step 2: Run MCP tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/mcp/test_assessment_tools.py tests/e2e/test_mcp_server.py -q -W error`

Expected: the three tools are absent.

- [ ] **Step 3: Implement public read methods and registration**

Add public `McpReadServices.assessment_summary`, `.assessment_scorecard`, and `.assessment_gaps` methods. Reuse the existing strict raw JSON walker before lookup. Mark every tool `readOnlyHint=True`, `destructiveHint=False`, and do not register an assessment mutation tool.

- [ ] **Step 4: Run MCP/read compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/mcp tests/e2e/test_mcp_server.py tests/unit/assessment tests/integration/assessment -q -W error`

Expected: all pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add src/intent_engineering/integrations/mcp_server/tools.py src/intent_engineering/integrations/mcp_server/server.py tests/contract/mcp/test_assessment_tools.py tests/e2e/test_mcp_server.py
git commit -m "feat: expose read-only intent assessment tools"
```

---

### Task 7: Complete foundation documentation and release verification

**Files:**
- Modify: `README.md`
- Modify: `docs/intent-aware-agent.md`
- Modify: `docs/mcp.md`
- Create: `docs/assessment.md`
- Create: `tests/e2e/test_intent_assessment_foundation.py`
- Modify: `tests/e2e/test_public_alpha.py`

**Interfaces:**
- Consumes: all Task 1–6 public APIs.
- Produces: one documented and executable offline journey from an approved graph to CLI, MCP, and CI-identical assessment output.

- [ ] **Step 1: Write the release-journey RED**

```python
def test_one_snapshot_has_identical_cli_mcp_and_ci_assessment(assessment_harness) -> None:
    cli = assessment_harness.cli_assess()
    mcp = assessment_harness.mcp_assess()
    gate = assessment_harness.ci_assess()
    assert cli.semantic_digest == mcp.semantic_digest == gate.semantic_digest
    assert cli.graph_version == assessment_harness.graph().version
    assert assessment_harness.canonical_bytes() == assessment_harness.before_bytes
```

- [ ] **Step 2: Run the journey and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_intent_assessment_foundation.py tests/e2e/test_public_alpha.py -q -W error`

Expected: documentation/journey assertions fail before docs and harness are complete.

- [ ] **Step 3: Document rubric, commands, colors, confidence, and governance**

Document that scores are derived, explainable, ACL-filtered, non-canonical, and model-independent. Show `intent assess`, MCP reads, CI defaults, exact exit codes, `N/A`, `unassessed`, and the red critical-path cap. Do not claim visualization, enrichment, or alternatives from later plans are already available.

- [ ] **Step 4: Run focused, affected, and full gates**

Run focused assessment tests, then assurance/render/CLI/MCP/validation/control-plane compatibility, then the exact full offline warnings-as-errors suite. Run Ruff on all changed Python, Ruff format check, `mypy src`, wheel packaging, CLI help, `git diff --check`, and inspect the exact staged path list.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/assessment tests/integration/assessment tests/e2e/test_cli_assessment.py tests/contract/mcp/test_assessment_tools.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
uv build --offline
.venv/bin/intent assess --help
git diff --check
git status --short
```

- [ ] **Step 5: Commit Task 7**

```bash
git add README.md docs/intent-aware-agent.md docs/mcp.md docs/assessment.md tests/e2e/test_intent_assessment_foundation.py tests/e2e/test_public_alpha.py
git commit -m "docs: release explainable intent assessment"
```

- [ ] **Step 6: Request independent code review**

Review the complete foundation diff against this plan and the design spec. Treat ACL leaks, nondeterminism, stale-snapshot acceptance, canonical writes, and score/model coupling as merge blockers.
