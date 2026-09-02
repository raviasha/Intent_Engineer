# Grounded Requirement Alternatives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:writing-skills before changing the Intent Advisor skill. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users explicitly request simpler or materially different requirements, compare their trade-offs and projected graph health, and route a selected alternative through existing human-reviewed proposal governance.

**Architecture:** A new `RequirementAlternativeService` builds one bounded ACL-safe grounding packet, delegates optional generation through a provider-neutral port, validates every untrusted candidate locally, and computes hypothetical assessment deltas from a detached graph overlay. CLI, control-plane, MCP, and Intent Advisor adapters expose the same comparison contract; only authenticated non-MCP proposal submission can create a pending proposal, and existing confirmation remains the sole graph-mutation boundary.

**Tech Stack:** Python 3.12, Pydantic v2 strict models, Typer, Starlette, vanilla JavaScript/CSS, existing proposal and ChangeSet services, MCP v2, pytest, Ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-02-intent-graph-assessment-enrichment-design.md`

**Prerequisites:** Complete `docs/superpowers/plans/2026-09-02-intent-graph-assessment-foundation.md` and `docs/superpowers/plans/2026-09-02-intent-graph-visualization-enrichment.md`. Reuse their `AssessmentSnapshot`, overlay assessment, control-plane selection state, and proposal submission contracts.

## Global Constraints

- Generate alternatives only after an explicit user request or explicit opening of suggestions for one visible orange/red requirement.
- The provider is optional. Assessment, visualization, and refinement continue offline; alternative generation returns a fixed unavailable result.
- A reasoner receives bounded detached content, stable visible references, and no capability token, hidden count, repository path, credential, or mutation handle.
- Every candidate is untrusted until strict local validation and graph-overlay assessment succeed.
- Alternatives may replace automation with a manual workflow, narrow scope, remove features, or reuse an existing capability, provided they identify preserved and weakened intent/outcomes.
- A comparison always shows current/proposed text, preserved/weakened outcomes, constraint changes, complexity, risks, trade-offs, evidence, assumptions, projected dimensions, and exact graph effects.
- Projected scores are hypothetical and visually distinct. They never enter canonical graph, evidence, history, case, clarification, or assessment storage.
- MCP remains read-only. It may return grounding, validate a host-generated candidate, or generate through an injected reasoner, but cannot submit, approve, or apply.
- A selected alternative becomes an ordinary reviewed proposal. Existing independently authenticated confirmation and ChangeSet activation remain mandatory.
- Public failures are fixed and redacted. Cancellation keeps its signal identity and scrubs detached private payloads.

---

### Task 1: Define strict grounding, candidate, and comparison contracts

**Files:**
- Create: `src/intent_engineering/alternatives/__init__.py`
- Create: `src/intent_engineering/alternatives/models.py`
- Test: `tests/unit/alternatives/test_models.py`

**Interfaces:**
- Consumes: stable graph/evidence/case/assessment identifiers and `AssessmentDimension`.
- Produces: `AlternativeMode`, `AlternativeUnavailable`, `ComplexityEstimate`, `OutcomeEffect`, `ConstraintEffect`, `GraphEffect`, `AlternativeRequest`, `RequirementAlternativeCandidate`, `RequirementAlternative`, `AlternativeComparison`, and `AlternativeResult`.

- [ ] **Step 1: Write strict model REDs**

```python
def test_alternative_requires_grounding_tradeoffs_and_graph_effects() -> None:
    with pytest.raises(ValidationError):
        RequirementAlternativeCandidate.model_validate(
            {
                "schema_version": 1,
                "subject_ref": "req:export",
                "proposed_text": "Export manually.",
                "preserved_outcome_refs": (),
                "supporting_evidence_refs": (),
                "graph_effects": (),
            }
        )


def test_request_and_result_have_content_addressed_semantic_identities() -> None:
    request = alternative_request_fixture()
    result = alternative_result_fixture(request=request)
    assert request.id == f"alternative-request:{request.digest}"
    assert result.request_digest == request.digest
    assert RequirementAlternative.model_validate_json(
        result.alternatives[0].model_dump_json()
    ) == result.alternatives[0]
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/alternatives/test_models.py -q -W error`

Expected: collection fails because `intent_engineering.alternatives` does not exist.

- [ ] **Step 3: Implement minimal immutable contracts**

```python
class AlternativeMode(StrEnum):
    SIMPLIFY = "simplify"
    CHANGE_APPROACH = "change_approach"
    NARROW_SCOPE = "narrow_scope"
    REUSE_CAPABILITY = "reuse_capability"


class AlternativeRequest(StrictModel):
    schema_version: Literal[1] = 1
    id: str
    subject_ref: str
    subject_text: str
    requested_modes: tuple[AlternativeMode, ...]
    intent_refs: tuple[str, ...]
    desired_outcome_refs: tuple[str, ...]
    constraint_refs: tuple[str, ...]
    evidence_summaries: tuple[AlternativeEvidenceSummary, ...]
    current_scorecard: NodeScorecard
    snapshot_digest: str
    principal_projection_digest: str


class RequirementAlternativeCandidate(StrictModel):
    schema_version: Literal[1] = 1
    subject_ref: str
    proposed_text: str
    mode: AlternativeMode
    preserved_outcome_refs: tuple[str, ...]
    weakened_outcomes: tuple[OutcomeEffect, ...]
    constraint_effects: tuple[ConstraintEffect, ...]
    supporting_evidence_refs: tuple[str, ...]
    complexity: ComplexityEstimate
    complexity_rationale: str
    risks: tuple[str, ...]
    tradeoffs: tuple[str, ...]
    assumptions: tuple[str, ...]
    graph_effects: tuple[GraphEffect, ...]
```

Use exact scalar types, UTF-8 byte limits, canonical sorted unique reference tuples, fixed enums, maximum 8 candidates, 32 effects per candidate, and 16 KiB per candidate. Derive identities from canonical JSON rather than provider IDs.

- [ ] **Step 4: Run contract tests and static checks GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/alternatives/test_models.py -q -W error`

Run: `.venv/bin/ruff check src/intent_engineering/alternatives tests/unit/alternatives && .venv/bin/mypy src/intent_engineering/alternatives`

- [ ] **Step 5: Commit Task 1**

```bash
git add src/intent_engineering/alternatives tests/unit/alternatives/test_models.py
git commit -m "feat: define grounded requirement alternative contracts"
```

---

### Task 2: Build bounded ACL-safe requests and strict local validation

**Files:**
- Create: `src/intent_engineering/alternatives/grounding.py`
- Create: `src/intent_engineering/alternatives/validation.py`
- Test: `tests/unit/alternatives/test_grounding.py`
- Test: `tests/unit/alternatives/test_validation.py`
- Test: `tests/integration/alternatives/test_acl_safety.py`

**Interfaces:**
- Consumes: one descriptor-held `AssessmentSnapshot`, matching `AssessmentReport`, visible subject reference, and requested modes.
- Produces: `AlternativeRequestBuilder.build(...) -> AlternativeRequest` and `AlternativeValidator.validate(request, candidate) -> RequirementAlternative`.

- [ ] **Step 1: Write grounding and secrecy REDs**

```python
def test_request_contains_only_visible_bounded_grounding(alternative_fixture) -> None:
    request = alternative_fixture.build_request("req:export")
    encoded = request.model_dump_json()
    assert "evidence:visible" in encoded
    assert alternative_fixture.hidden_marker not in encoded
    assert alternative_fixture.workspace_path not in encoded
    assert len(encoded.encode("utf-8")) <= 64 * 1024


def test_hidden_and_unknown_subjects_are_indistinguishable(alternative_fixture) -> None:
    hidden = alternative_fixture.try_build("req:hidden")
    unknown = alternative_fixture.try_build("req:unknown")
    assert hidden == unknown == AlternativeUnavailable.SUBJECT_UNAVAILABLE
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/alternatives/test_grounding.py tests/unit/alternatives/test_validation.py tests/integration/alternatives/test_acl_safety.py -q -W error`

Expected: request builder and validator are absent.

- [ ] **Step 3: Implement the request builder**

Require the report and snapshot graph/evidence/case/policy/principal identities to match. Select the subject, its nearest visible intent/outcome/constraint context, current visible evidence summaries, open visible cases, failed rubric checks, and declared implementation/test links. Use fixed traversal depth and item caps, deterministic stable-ID ordering, and summaries that never infer missing-versus-hidden data.

- [ ] **Step 4: Implement strict candidate validation**

Reject candidates that invent references, omit a current visible subject, claim to preserve an outcome without naming it, remove an undeclared constraint, cite invisible evidence, use duplicate effects, exceed bounds, contain unknown effect kinds, or declare a score. Require every changed graph reference to be in the request scope or to be a content-addressed proposed node ID derived locally.

```python
def validate(
    self,
    request: AlternativeRequest,
    candidate: RequirementAlternativeCandidate,
) -> RequirementAlternative:
    self._require_matching_subject(request, candidate)
    self._require_grounded_refs(request, candidate)
    self._require_explicit_outcome_and_constraint_accounting(request, candidate)
    changeset = self._build_detached_changeset(request, candidate)
    return RequirementAlternative.from_validated(candidate, changeset=changeset)
```

- [ ] **Step 5: Test malformed, injected, oversized, stale, and cancellation paths GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/alternatives tests/integration/alternatives/test_acl_safety.py -q -W error`

Assert provider exception text, hidden markers, authorization material, and candidate plaintext never enter fixed error strings, logs, cache keys, or traceback locals retained across cancellation.

- [ ] **Step 6: Commit Task 2**

```bash
git add src/intent_engineering/alternatives/grounding.py src/intent_engineering/alternatives/validation.py tests/unit/alternatives tests/integration/alternatives/test_acl_safety.py
git commit -m "feat: validate ACL-safe alternative grounding"
```

---

### Task 3: Add provider-neutral generation and projected assessment

**Files:**
- Create: `src/intent_engineering/alternatives/service.py`
- Create: `src/intent_engineering/alternatives/overlay.py`
- Test: `tests/unit/alternatives/test_overlay.py`
- Test: `tests/integration/alternatives/test_service.py`

**Interfaces:**
- Consumes: `AlternativeRequestBuilder`, optional `AlternativeReasoner`, `AlternativeValidator`, `GraphAssessmentService`, and exact current snapshot loader.
- Produces: `AlternativeReasoner.suggest(request)`, `RequirementAlternativeService.request(...)`, `.validate_candidate(...)`, and `.compare(...)`.

- [ ] **Step 1: Write service/overlay REDs**

```python
class RecordingReasoner:
    def suggest(
        self, request: AlternativeRequest
    ) -> tuple[RequirementAlternativeCandidate, ...]:
        self.request = request
        return (manual_export_candidate(request),)


def test_manual_alternative_preserves_outcome_and_gets_hypothetical_scores(service) -> None:
    result = service.request(
        subject_ref="req:export", modes=(AlternativeMode.CHANGE_APPROACH,)
    )
    comparison = result.alternatives[0].comparison
    assert comparison.proposed_text == "An owner exports the report on request."
    assert comparison.preserved_outcome_refs == ("outcome:portable-report",)
    assert comparison.projected_assessment.state == "projected"
    assert service.graph_bytes() == service.before_graph


def test_absent_provider_returns_fixed_unavailable_without_affecting_assessment(service) -> None:
    result = service.with_reasoner(None).request(subject_ref="req:export")
    assert result.status == "provider_unavailable"
    assert service.current_assessment() == service.before_assessment
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/alternatives/test_overlay.py tests/integration/alternatives/test_service.py -q -W error`

Expected: service, reasoner protocol, and overlay evaluator are absent.

- [ ] **Step 3: Implement the provider-neutral port and safe call boundary**

```python
class AlternativeReasoner(Protocol):
    def suggest(
        self, request: AlternativeRequest
    ) -> tuple[RequirementAlternativeCandidate, ...]: ...


class RequirementAlternativeService:
    def request(
        self,
        *,
        subject_ref: str,
        modes: tuple[AlternativeMode, ...] = (AlternativeMode.SIMPLIFY,),
    ) -> AlternativeResult: ...

    def validate_candidate(
        self,
        *,
        request_id: str,
        candidate: RequirementAlternativeCandidate,
    ) -> AlternativeComparison: ...
```

Call the reasoner once with only the detached request. Catch provider/parser failures as `provider_unavailable` or `invalid_provider_output`, preserve cancellation identity, validate candidates independently, discard invalid candidates, deduplicate by semantic digest, and return stable ordering. Never retry generation implicitly.

- [ ] **Step 4: Compute projections from an exact detached graph overlay**

Apply each validated candidate's `ChangeSet` to an in-memory clone only, build a new assessment snapshot from that overlay plus the same evidence/case/clarification/history projection, and invoke `GraphAssessmentService`. Label all returned scorecards `projected` and bind them to the request, proposal-effect, baseline snapshot, and policy digests. Refuse stale request IDs before applying the overlay.

- [ ] **Step 5: Run deterministic, failure, concurrency, and offline tests GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/alternatives tests/integration/alternatives -q -W error`

Assert canonical bytes remain exact, repeated candidate validation is semantically byte-identical, concurrent identical requests may share only a fully keyed detached cache entry, and principal/snapshot changes never share results.

- [ ] **Step 6: Commit Task 3**

```bash
git add src/intent_engineering/alternatives/service.py src/intent_engineering/alternatives/overlay.py tests/unit/alternatives/test_overlay.py tests/integration/alternatives/test_service.py
git commit -m "feat: generate and assess requirement alternatives"
```

---

### Task 4: Route selected alternatives through proposal governance

**Files:**
- Create: `src/intent_engineering/alternatives/proposals.py`
- Modify: `src/intent_engineering/intent_workflow/clarification.py`
- Test: `tests/integration/alternatives/test_proposal_submission.py`
- Test: `tests/integration/intent_workflow/test_proposal_governance.py`

**Interfaces:**
- Consumes: validated `AlternativeComparison`, current authenticated actor/aliases, exact current snapshot, and existing clarification/proposal service.
- Produces: `AlternativeProposalService.submit(alternative_id, expected_request_digest, expected_effect_digest) -> IntentProposal`.

- [ ] **Step 1: Write governance REDs**

```python
def test_submit_creates_pending_proposal_without_mutating_graph(alternatives) -> None:
    comparison = alternatives.validated_manual_export()
    before = alternatives.graph_bytes()
    proposal = alternatives.submit(comparison)
    assert proposal.changeset.digest == comparison.graph_effect_digest
    assert proposal.evidence_refs == comparison.supporting_evidence_refs
    assert alternatives.graph_bytes() == before
    assert alternatives.proposal_store.get(proposal.id) == proposal


def test_stale_or_self_approved_alternative_cannot_activate(alternatives) -> None:
    proposal = alternatives.submit(alternatives.validated_manual_export())
    alternatives.advance_graph()
    with pytest.raises(IntentProposalConflictError):
        alternatives.confirm(proposal, actor=proposal.proposed_by)
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/alternatives/test_proposal_submission.py tests/integration/intent_workflow/test_proposal_governance.py -q -W error`

Expected: no alternative proposal service exists.

- [ ] **Step 3: Implement exact proposal conversion**

Reauthenticate actor and aliases, rebuild the current ACL-safe snapshot, require request/snapshot/policy/effect digests to match, and convert the already validated overlay `ChangeSet` into the existing requirement `IntentProposal` type. Carry supporting evidence and assumptions; mark weakened outcomes and constraint removals as explicit review items. Do not persist the model's prose outside the reviewed proposal payload.

- [ ] **Step 4: Preserve existing confirmation and conflict rules**

Do not add an alternative-specific approval path. Exercise stale baseline, changed evidence, changed ACL, changed policy, proposer/approver identity collision, conflicting authors, partial selection, rejection, replay, and crash-recovery behavior through existing services.

- [ ] **Step 5: Run proposal and transaction compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/alternatives/test_proposal_submission.py tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py tests/integration/test_startup_transaction_recovery.py -q -W error`

- [ ] **Step 6: Commit Task 4**

```bash
git add src/intent_engineering/alternatives/proposals.py src/intent_engineering/intent_workflow/clarification.py tests/integration/alternatives/test_proposal_submission.py tests/integration/intent_workflow/test_proposal_governance.py
git commit -m "feat: govern selected requirement alternatives"
```

---

### Task 5: Expose safe CLI and read-only MCP alternative workflows

**Files:**
- Create: `src/intent_engineering/cli/alternatives.py`
- Modify: `src/intent_engineering/cli/app.py`
- Modify: `src/intent_engineering/cli/runtime.py`
- Modify: `src/intent_engineering/integrations/mcp_server/tools.py`
- Modify: `src/intent_engineering/integrations/mcp_server/server.py`
- Test: `tests/e2e/test_cli_alternatives.py`
- Test: `tests/contract/mcp/test_alternative_tools.py`

**Interfaces:**
- Consumes: Task 3 service and Task 4 authenticated proposal submission.
- Produces: `intent alternatives SUBJECT [--mode ...] [--format text|json]`, optional interactive selection/proposal submission on a TTY, and read-only MCP tools `intent_alternative_context`, `intent_alternative_validate`, and `intent_alternatives`.

- [ ] **Step 1: Write CLI/MCP REDs**

```python
def test_cli_compares_current_and_manual_alternative(cli_alternatives) -> None:
    result = cli_alternatives.run("req:export", mode="change_approach")
    assert result.exit_code == 0
    assert "Current requirement" in result.stdout
    assert "Proposed requirement" in result.stdout
    assert "Projected — not approved" in result.stdout
    assert "manual" in result.stdout.lower()


async def test_mcp_alternative_tools_are_read_only_and_token_free(alternative_mcp) -> None:
    tools = {item.name: item for item in await alternative_mcp.list_tools()}
    assert tools["intent_alternative_validate"].annotations.read_only_hint is True
    assert "intent_alternative_submit" not in tools
    response = await alternative_mcp.validate_host_candidate()
    assert "capability_token" not in response.model_dump_json()
    assert alternative_mcp.durable_bytes() == alternative_mcp.before_bytes
```

- [ ] **Step 2: Run adapter tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_alternatives.py tests/contract/mcp/test_alternative_tools.py -q -W error`

Expected: command and tools are absent.

- [ ] **Step 3: Implement CLI comparison and explicit selection**

Print a stable side-by-side comparison with approved/projected labels, preserved/weakened outcomes, constraint effects, complexity, risks, assumptions, graph effects, and dimension deltas. Without a configured reasoner, return a successful fixed `provider unavailable; assessment remains available` result. Proposal submission requires an interactive authenticated local-human confirmation of the exact alternative and effect digests; JSON/non-TTY mode never submits.

- [ ] **Step 4: Implement read-only MCP grounding, validation, and optional generation**

`intent_alternative_context` returns the bounded request for host reasoning. `intent_alternative_validate` accepts one strict candidate and returns a detached comparison. `intent_alternatives` invokes the optional injected server reasoner. All three acquire one descriptor-held authorized snapshot, reauthenticate before returning, enforce raw byte and collection bounds, and produce identical errors for hidden and unknown subjects.

- [ ] **Step 5: Run CLI/MCP security and compatibility GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_alternatives.py tests/contract/mcp/test_alternative_tools.py tests/contract/mcp/test_intent_server_reads.py tests/e2e/test_mcp_server.py -q -W error`

- [ ] **Step 6: Commit Task 5**

```bash
git add src/intent_engineering/cli/alternatives.py src/intent_engineering/cli/app.py src/intent_engineering/cli/runtime.py src/intent_engineering/integrations/mcp_server/tools.py src/intent_engineering/integrations/mcp_server/server.py tests/e2e/test_cli_alternatives.py tests/contract/mcp/test_alternative_tools.py
git commit -m "feat: expose grounded requirement alternatives"
```

---

### Task 6: Add comparison UI and Intent Advisor guidance

**Files:**
- Modify: `src/intent_engineering/control_plane/http_models.py`
- Modify: `src/intent_engineering/control_plane/service.py`
- Modify: `src/intent_engineering/control_plane/web.py`
- Modify: `src/intent_engineering/control_plane/assets/index.html`
- Modify: `src/intent_engineering/control_plane/assets/app.js`
- Modify: `src/intent_engineering/control_plane/assets/styles.css`
- Modify: `plugins/intent-advisor/skills/intent-advisor/SKILL.md`
- Test: `tests/e2e/test_intent_dev_alternatives.py`
- Test: `tests/contract/agent_host/test_advisory_plugin.py`
- Test: `tests/contract/agent_host/test_advisory_prompt.py`

**Interfaces:**
- Consumes: alternative request/result/comparison and proposal-submission public methods.
- Produces: Suggest alternatives action for visible orange/red requirements, approved-versus-projected comparison UI, authenticated proposal submission route, and an explicit Intent Advisor alternative route.

- [ ] **Step 1: Write UI and plugin REDs**

```javascript
const screen = await loadAlternativeFixture("manual-export.json");
screen.selectNode("req:export");
screen.openSuggestions();
assert.equal(screen.currentRequirement(), "Automate scheduled CSV export.");
assert.equal(screen.proposedRequirement(), "An owner exports the report on request.");
assert.equal(screen.projectionLabel(), "Projected — not approved");
assert.deepEqual(screen.scoreDelta(), { implementation_traceability: 18, freshness: 0 });
```

```python
def test_advisor_generates_only_after_explicit_request_and_cannot_submit(plugin_contract) -> None:
    guidance = plugin_contract.route("show simpler requirements for req:export")
    assert "intent_alternative_context" in guidance
    assert "intent_alternative_validate" in guidance
    assert "intent_alternative_submit" not in guidance
```

- [ ] **Step 2: Run UI/plugin tests and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_intent_dev_alternatives.py tests/contract/agent_host/test_advisory_plugin.py tests/contract/agent_host/test_advisory_prompt.py -q -W error`

Expected: comparison UI and advisor route are absent.

- [ ] **Step 3: Add strict control-plane endpoints and comparison view**

Add bounded GET generation/status and POST candidate-validation/proposal-submission endpoints. Existing Origin, CSRF, WebAuthn/local-human authority, stale preimage, and fixed-error rules apply. UI selection stays synchronized with the assessment graph/table, renders text with safe DOM APIs, makes weakened outcomes and removed constraints prominent, and never computes scores in JavaScript.

- [ ] **Step 4: Update Intent Advisor using the skill-authoring workflow**

When the user explicitly requests alternatives, the advisor obtains the bounded context, drafts up to the allowed maximum, sends each untrusted draft to the read-only validation tool, and presents only validated comparisons. It must state that scores are projected and that selecting an alternative still requires authenticated local proposal review. It never treats ordinary feature prompts as requests for alternatives and never submits or approves over MCP.

- [ ] **Step 5: Run actual packaged UI and plugin contract GREEN**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_intent_dev_alternatives.py tests/e2e/test_intent_dev_web_runtime.py tests/contract/control_plane tests/contract/agent_host -q -W error`

Build the wheel offline and verify the comparison UI assets and updated Intent Advisor skill are packaged. Test keyboard navigation, non-color projected labels, injection payloads, provider-offline state, hidden subjects, stale proposals, and cancellation.

- [ ] **Step 6: Commit Task 6**

```bash
git add src/intent_engineering/control_plane plugins/intent-advisor/skills/intent-advisor/SKILL.md tests/e2e/test_intent_dev_alternatives.py tests/contract/agent_host/test_advisory_plugin.py tests/contract/agent_host/test_advisory_prompt.py
git commit -m "feat: compare alternatives in intent dev and advisor"
```

---

### Task 7: Document and prove the complete alternative journey

**Files:**
- Modify: `README.md`
- Modify: `docs/assessment.md`
- Modify: `docs/intent-aware-agent.md`
- Modify: `docs/mcp.md`
- Create: `tests/e2e/test_intent_requirement_alternative_journey.py`
- Modify: `tests/e2e/test_public_alpha.py`

**Interfaces:**
- Consumes: all Increment 3 public surfaces.
- Produces: documented explicit-request, host-reasoned, CLI/UI comparison, proposal review, approval, reassessment, and offline journeys.

- [ ] **Step 1: Write complete-journey REDs**

```python
def test_manual_alternative_is_only_canonical_after_reviewed_activation(journey) -> None:
    approved_before = journey.assess("req:export")
    comparison = journey.request_manual_alternative("req:export")
    assert journey.assess("req:export") == approved_before
    proposal = journey.submit_selected(comparison)
    assert journey.assess("req:export") == approved_before
    journey.confirm_with_independent_human(proposal)
    approved_after = journey.assess("req:export")
    assert approved_after.snapshot_digest != approved_before.snapshot_digest
    assert approved_after.state == "approved"
```

- [ ] **Step 2: Run release journey and confirm RED**

Run: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_intent_requirement_alternative_journey.py tests/e2e/test_public_alpha.py -q -W error`

Expected: the public alternative journey is absent.

- [ ] **Step 3: Document user behavior and trust boundaries**

Document explicit invocation, AI-provider optionality, host-generated candidate validation, manual/narrowed/reuse examples, approved/projected score labels, preserved versus weakened outcomes, proposal selection, independent confirmation, reassessment after activation, MCP read-only behavior, and offline fallback. State that alternatives are suggestions rather than requirements or authority.

- [ ] **Step 4: Run focused, affected, full, static, packaging, and help gates**

Run all alternative/assessment/enrichment/control-plane/CLI/MCP/plugin tests, then existing onboarding, preflight, clarification, proposal, assurance, rendering, and GitHub compatibility tests. Run the exact full offline suite, Ruff check/format, mypy, wheel asset verification, CLI help snapshots, and `git diff --check`.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/alternatives tests/integration/alternatives tests/e2e/test_cli_alternatives.py tests/e2e/test_intent_dev_alternatives.py tests/contract/mcp/test_alternative_tools.py tests/contract/agent_host -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
uv build --offline
.venv/bin/intent alternatives --help
git diff --check
git status --short
```

The pre-existing Codex host-version assertion and multiprocessing hang recorded in the design spec must be fixed or explicitly triaged outside this feature. Do not weaken, skip, or relabel them to claim this increment passes.

- [ ] **Step 5: Commit Task 7**

```bash
git add README.md docs/assessment.md docs/intent-aware-agent.md docs/mcp.md tests/e2e/test_intent_requirement_alternative_journey.py tests/e2e/test_public_alpha.py
git commit -m "docs: release grounded requirement alternatives"
```

- [ ] **Step 6: Request independent code review**

Review the complete three-increment implementation against the approved design. Treat hidden-state inference, ungrounded references, reasoner-set scores, approved/projected ambiguity, implicit generation, MCP mutation authority, proposal bypass, stale overlay acceptance, or provider data retention as merge blockers.
