# Intent Graph Assessment and Progressive Enrichment Design

Date: 2026-09-02

Status: Approved

Scope: Explainable graph robustness, gap visualization, voluntary enrichment, and simpler requirement alternatives

## 1. Summary

Intent Engineering already captures attributed evidence, asks progressive clarification questions,
applies graph changes through reviewed ChangeSets, runs scheduled assurance, and renders non-canonical
Markdown and Mermaid projections. This design adds a derived assessment layer that helps people
understand and improve the quality of their intent graph even when they are not introducing a new
requirement.

The release will provide:

1. deterministic, explainable robustness scorecards for nodes, intent branches, and the project;
2. synchronized graph and table views with accessible green, orange, and red health states;
3. resumable, time-boxed graph-improvement sessions that ask one high-impact question at a time;
4. optional AI suggestions for simpler or materially different requirements that preserve the
   underlying intent; and
5. CLI, UI, MCP, and CI projections over one shared assessment service.

Assessment is not canonical truth. Scores, colors, questions, and AI suggestions are derived from a
version-bound, ACL-filtered snapshot. Human answers become immutable attributed evidence, while any
interpretation or graph change still requires the existing proposal and approval workflow.

## 2. Goals

- Let a developer assess and improve the graph without first submitting a new requirement.
- Preserve the current prompt-time progressive clarification flow for active feature work.
- Make graph quality visible at node, intent-branch, and project levels.
- Explain every score and deduction in terms of fixed rubric checks and evidence.
- Prevent a healthy average from hiding one blocking gap.
- Let users pause and resume improvement sessions without losing their original statements.
- Suggest simpler or alternate requirements without treating a model as an authority.
- Keep deterministic assessment, visualization, and question selection available offline.
- Preserve local-first operation, evidence provenance, ACLs, stable identities, and reviewed
  ChangeSets.

## 3. Non-goals

- An opaque model-generated truth, quality, or confidence score.
- Automatically accepting AI interpretations or modifying the canonical graph.
- Automatically resolving contradictory human evidence.
- Requiring a model provider to render or assess the graph.
- Persisting derived scores or colors as fields on canonical graph nodes.
- Replacing existing clarification, reconciliation, proposal, or ChangeSet governance.
- Treating missing or inaccessible evidence as evidence that a claim is false.
- Requiring a separate intent-branch checkout for feature work or CI assessment.

This design narrowly supersedes the public-alpha non-goal that excluded all production semantic
scoring. Transparent, deterministic rubric-based assessment is now in scope. Opaque truth scoring,
embeddings-based authority, and provider-dependent scoring remain out of scope.

## 4. Existing foundations

The implementation must reuse rather than duplicate these existing boundaries:

- prompt preflight and task classification for aligned, new/ambiguous, conflicting, and
  no-semantic-impact work;
- clarification sessions with immutable question and answer evidence;
- proposal confirmation and reviewed ChangeSet activation;
- scheduled assurance and reconciliation cases;
- graph, evidence, history, and case stores;
- ACL-filtered context and MCP projections;
- `intent dev` for the local control-plane UI;
- `intent render` for non-canonical Markdown and Mermaid output; and
- `intent check` or the consolidated validation/assurance path for local and CI use.

The assessment layer may consume these services' detached outputs. It may not bypass their mutation
or authorization boundaries.

## 5. Considered approaches

### 5.1 Derived assessment layer — selected

A new application service evaluates one immutable snapshot and produces a detached assessment
report. Scores and colors are recomputed from graph, evidence, reconciliation, clarification, and
history state. Optional model reasoning consumes the same grounded projection and returns proposals.

Benefits:

- deterministic and explainable;
- no score churn in canonical graph history;
- compatible with offline use and current rendering boundaries;
- easy to invalidate by graph/evidence digest; and
- clean separation between assessment and authority.

### 5.2 Scores stored in canonical graph nodes — rejected

Persisted scores simplify querying but become stale whenever evidence changes, create noisy graph
mutations, and blur the distinction between source-backed assertions and derived analysis.

### 5.3 AI-first graph reviewer — rejected as the foundation

An AI-first evaluator is flexible but non-reproducible and provider-dependent. It may be used only
for optional question wording and requirement alternatives after deterministic grounding.

## 6. Architecture

### 6.1 GraphAssessmentService

`GraphAssessmentService` is the sole scoring authority. It receives an already authenticated actor
and one descriptor-held, deeply detached snapshot containing:

- canonical graph and graph version;
- ACL-visible evidence records and ingestion lineage;
- visible reconciliation cases;
- visible clarification state;
- relevant semantic ChangeSet history;
- project scoring policy; and
- an exact snapshot identity covering every input preimage.

It returns an immutable `AssessmentReport`. It performs no canonical write, connector call, or model
call.

### 6.2 GraphEnrichmentService

`GraphEnrichmentService` manages resumable voluntary improvement sessions. It:

- accepts a time budget of 5, 15, or 30 minutes, or a bounded focus area;
- selects the highest-impact eligible gap;
- emits one question at a time;
- records each human answer immediately through the existing conversation evidence boundary;
- records skips without fabricating answers;
- recomputes the derived assessment after new evidence;
- accumulates proposed interpretations separately from evidence; and
- delegates final graph changes to existing proposal and ChangeSet services.

The service owns session progress, only. It does not own evidence, graph, or proposal persistence.

### 6.3 RequirementAlternativeService

`RequirementAlternativeService` is an optional provider-neutral reasoning port plus a strict local
validator. It consumes only a bounded ACL-visible `AlternativeRequest` derived from an assessment.
It returns zero or more detached `RequirementAlternative` values.

The validator rejects any suggestion that lacks:

- a selected subject requirement or intent;
- preserved intent and desired-outcome references;
- supporting evidence references;
- explicit changed or removed constraints;
- complexity reduction rationale;
- risks and trade-offs;
- expected score effects by dimension; or
- assumptions requiring human confirmation.

Submitting an alternative creates a normal proposal. It never changes the graph directly.

### 6.4 Presentation adapters

All presentation surfaces consume the same `AssessmentReport` contract:

- `intent dev` exposes synchronized graph and table views plus improvement sessions;
- `intent assess` emits human-readable or versioned JSON assessment output;
- `intent refine` runs the same guided session through the CLI;
- MCP exposes read-only assessment, gap, and alternative-suggestion tools; and
- `intent render` can add deterministic health styling to non-canonical Markdown and Mermaid.

CI calls the same assessment service through the consolidated check path. No adapter recomputes or
reinterprets scores.

## 7. Assessment contracts

### 7.1 AssessmentReport

The versioned report contains:

- `schema_version`;
- project identity;
- graph ID and version;
- graph, evidence, case, clarification, history, policy, and aggregate snapshot digests;
- actor/principal projection identity without exposing hidden principals;
- generated-at timestamp supplied by the caller's fixed clock;
- project scorecard;
- intent-branch scorecards;
- per-node scorecards;
- ordered high-impact gaps;
- assessment warnings; and
- an `assessment_complete` flag.

Report ordering is canonical and independent of store or traversal order. Repeated assessment of an
identical snapshot yields byte-identical semantic content; presentation timestamps are excluded from
the semantic report digest.

### 7.2 NodeScorecard

Each visible graph node receives:

- stable node ID and type;
- overall robustness score;
- assessment confidence;
- health state: green, orange, red, or unassessed;
- worst applicable dimension;
- one result per dimension;
- blocking case references visible to the actor;
- the highest-impact next action; and
- current and projected values when evaluating a proposal preview.

### 7.3 DimensionResult

Each dimension result contains:

- dimension ID;
- applicability: required, inherited, optional, or not applicable;
- score when applicable;
- health state;
- confidence contribution;
- passed rubric checks;
- failed rubric checks and exact deductions;
- evidence references;
- related node, edge, case, or clarification references; and
- recommended next action.

Unknown node types or unsupported rubric versions produce `unassessed`, never an implicit pass.

## 8. Deterministic scoring model

### 8.1 Dimensions

The initial rubric contains seven dimensions:

1. **Intent clarity** — purpose, actor, desired outcome, constraints, and measurable success are
   explicit where applicable.
2. **Evidence strength** — claims have current, attributable, ACL-visible evidence with an explicit
   source mode and sufficient fidelity confidence.
3. **Requirement coverage** — requirements cover the parent intent and known desired outcomes.
4. **Implementation traceability** — requirements connect to concrete implementation assertions.
5. **Test verification** — required behavior connects to current test evidence, including important
   failure and negative paths where declared.
6. **Consistency** — no unresolved contradictory evidence, invalid topology, or blocking case exists.
7. **Freshness** — relevant evidence remains current relative to newer evidence and implementation
   changes.

### 8.2 Applicability

Dimensions are applied by node type and relation context. A product-intent node, for example, may
inherit implementation and test coverage from its requirement descendants. Non-applicable
dimensions are displayed as `N/A` and are excluded from the overall score rather than counted as
zero.

The applicability matrix is versioned as part of the assessment policy. Changing it changes the
rubric version and invalidates cached reports.

### 8.3 Rubric checks and deductions

Each dimension starts from a fixed rubric maximum and applies bounded, named checks. Scores use
integer values from 0 to 100. Every deduction has a stable rule ID, severity, points, evidence or
topology references, and a human-readable explanation.

Rubric version 1 starts each applicable dimension at 100, subtracts each distinct failed rule once,
clamps the result to 0–100, and rounds only after all deductions are applied. Required and inherited
dimensions contribute to the overall score. Optional dimensions are displayed but do not affect the
overall score unless project policy explicitly promotes them. The default overall score is the
floor of the equal-weight arithmetic mean of all contributing dimensions. A project policy may set
different integer weights, but the report must publish those weights and include them in its policy
digest.

No model output contributes directly to a score. Models may propose candidate gaps, but those gaps
affect scoring only after deterministic validation maps them to an approved rubric rule and grounded
evidence.

### 8.4 Overall score and confidence

The node overall score is a policy-versioned weighted rollup of applicable dimensions. Assessment
confidence is separate and reflects whether enough current, visible data exists to rely on the
score.

Examples:

- `82 robustness / 94 confidence` means the graph is well understood but has known weaknesses.
- `82 robustness / 48 confidence` means the apparent robustness is based on insufficient data.

The project and intent-branch scores roll up critical intent paths. They are not plain averages;
many healthy low-impact nodes cannot hide one broken critical requirement.

Assessment confidence is also deterministic. Every rubric rule declares the input slots required
to evaluate it. Confidence is the floor of the percentage of required input slots that can be
safely resolved from the visible snapshot, averaged with equal weight across contributing
dimensions. A resolved contradiction lowers robustness through the consistency rubric; absent or
inaccessible input lowers confidence without disclosing why it is unavailable.

An initial critical intent path is any active traversal from `PRODUCT_INTENT` or `DESIRED_OUTCOME`
through the approved intent/requirement relations to an active `CONSTRAINT`, `REQUIREMENT`, or
`ACCEPTANCE_CRITERION`. A branch score is the floor of the equal-weight mean of its contributing
node scores. The project score is the floor of the equal-weight mean of branch scores. If any
critical path is red, its branch and the project remain red and their displayed robustness is
capped at 49. This makes the numeric rollup consistent with the worst-gap color instead of allowing
healthy unrelated nodes to conceal a blocking path.

### 8.5 Health colors

The worst significant applicable gap determines color:

- **Green** — every required dimension is at least 75, confidence is at least 75, and no blocking
  conflict exists.
- **Orange** — at least one required dimension is 50–74, evidence is incomplete or stale, or
  confidence is below 75.
- **Red** — a required dimension is below 50, a blocking contradiction exists, or critical
  traceability is absent.
- **Unassessed** — the rubric does not understand the node or the safe snapshot is incomplete.

A blocking red dimension always overrides a high overall average. Colors are always paired with a
text label and icon; color alone never conveys status.

Thresholds and criticality weights are explicit project policy. Policy changes create a new
assessment identity but never rewrite prior graph history.

## 9. Progressive improvement sessions

### 9.1 Starting a session

A user may start from:

- the **Improve graph** action in `intent dev`;
- `intent refine --minutes 5|15|30`;
- `intent refine --focus <node-or-branch-id>`; or
- an agent suggestion that opens the UI or CLI with the user's consent.

The plugin may remind or navigate. It cannot answer, approve, or mutate on the user's behalf.

### 9.2 Question priority

Question selection is deterministic before optional wording assistance. Candidates are ranked by:

1. blocking red gaps on critical intent paths;
2. expected improvement to assessment confidence;
3. expected robustness impact;
4. dependency reach across descendant nodes;
5. staleness and unresolved conflict severity; then

6. stable gap and node IDs.

Already answered questions and information present in current visible evidence are excluded.

### 9.3 Answer and proposal lifecycle

1. The service presents one bounded question and its reason.
2. The human may answer, skip, pause, or stop.
3. An answer is immediately stored as attributed immutable evidence.
4. The current approved assessment remains unchanged until the graph changes.
5. A provisional assessment may show the projected effect of a candidate interpretation.
6. Interpretations accumulate as one or more proposals.
7. The user reviews the exact evidence, graph changes, and projected score delta.
8. Existing approval and ChangeSet governance activates approved changes.
9. The service recomputes the approved assessment from the new canonical snapshot.

If a session ends before approval, answers remain useful evidence but no AI interpretation becomes
canonical truth.

### 9.4 Pause, resume, and concurrency

Session state contains only stable references, progress, budget, and lifecycle status. Raw answers
remain in evidence records, not duplicated in session state.

Resume requires the same repository and actor authority. If graph, evidence, policy, or ACL state has
changed, the service creates a fresh assessment, preserves prior answers, invalidates stale proposed
interpretations, and continues from the next eligible gap.

Concurrent identical operations converge. Conflicting session updates or stale approvals fail
closed without partial graph, evidence, case, proposal, or history mutation.

## 10. Alternative and simpler requirements

### 10.1 Invocation

Alternatives are generated only:

- on explicit user request; or
- when the user opens suggestions for an orange or red requirement.

They are not generated during every prompt or assessment.

### 10.2 Allowed solution changes

An alternative may change the solution approach completely, including recommending a manual
workflow, removing automation, narrowing scope, or reusing an existing capability. It must still
state which underlying intent and desired outcomes it preserves.

### 10.3 Comparison

The UI and CLI comparison show, side by side:

- current and proposed requirement text;
- preserved and weakened intent/outcomes;
- constraints added, changed, or removed;
- estimated implementation and operational complexity;
- risks and trade-offs;
- evidence and assumptions;
- current and projected dimension scores; and
- graph nodes and edges that would change.

Projected scores are explicitly hypothetical. They are recomputed from the exact proposed graph
overlay and never replace the approved assessment until the proposal is activated.

## 11. Graph and table experience

### 11.1 Synchronized views

The `intent dev` assessment page contains one shared selection model:

- selecting a graph node selects and scrolls to its table row;
- selecting a table row focuses the graph node;
- filters apply identically to both views; and
- the URL may contain bounded non-secret filter and selection state for reload/navigation.

### 11.2 Graph view

Each node displays:

- accessible health color, icon, and label;
- overall robustness score;
- assessment confidence;
- worst dimension; and
- approved or projected state marker.

Edges may show traceability gaps or contradictions without implying that either endpoint is true.
Users can toggle approved state, proposal preview, and score-delta overlays.

### 11.3 Table view

The sortable table contains:

- node ID, type, label, owner when available, and criticality;
- overall score, confidence, color, and worst dimension;
- all applicable dimension scores;
- evidence strength and freshness summary;
- visible open cases;
- next recommended action; and
- projected score delta when previewing a proposal.

Filters include node type, health, dimension, source, owner, intent branch, changed-since-base, and
approved/projected state.

### 11.4 Rendering bounds

Large graphs use bounded server-side projections, pagination for the table, and deterministic graph
subsets. Rendering never loads hidden nodes merely to compute layout. Markdown and Mermaid exports
remain non-canonical cache artifacts.

## 12. CLI, MCP, and CI interfaces

### 12.1 CLI

Proposed public commands:

```text
intent assess --project . [--focus ID] [--format text|json|markdown]
intent refine --project . [--minutes 5|15|30] [--focus ID]
intent render --project . --assessment [--output PATH]
```

`intent assess` is read-only. `intent refine` records answers only after explicit human input and
uses the existing proposal/approval boundary for graph changes.

### 12.2 MCP

Read-only tools expose:

- current assessment summary;
- bounded scorecard lookup;
- ordered visible gaps;
- deterministic next-question candidates; and
- optional requirement alternatives.

MCP responses contain no authorization token and cannot apply an alternative or graph mutation.

### 12.3 CI

The consolidated check path may enforce project policy such as:

- fail on newly introduced red gaps;
- fail below minimum assessment confidence on critical paths;
- warn on orange gaps;
- reject a robustness regression relative to the merge base; or
- emit a versioned assessment artifact without gating.

CI compares repository states directly and does not require developers to check out an intent branch
beside their feature branch.

## 13. Security, privacy, and trust

- Assessment runs on an ACL-filtered snapshot and must not reveal hidden evidence through scores,
  counts, colors, topology, explanations, alternatives, or timing-sensitive error distinctions.
- Missing and inaccessible evidence are indistinguishable to unauthorized callers.
- Model reasoning receives only bounded detached data and no capability token.
- Raw human answers remain immutable evidence and retain authorship; summaries never replace them.
- Assessment caches are keyed by complete snapshot and principal-projection identity.
- Cache output is non-canonical and safe to delete.
- Model suggestions cannot satisfy a human-answer requirement, approve a ChangeSet, resolve a
  conflict, or write externally.
- Fixed redacted errors replace raw parser, provider, path, and model exceptions at public
  boundaries.
- Cancellation preserves signal identity while scrubbing secret or private payloads from repository
  traceback locals.

## 14. Staleness and failure behavior

- **Offline model provider:** deterministic scores, visualization, and rubric-generated questions
  continue; alternative generation reports unavailable.
- **Stale snapshot:** discard derived output and reassess before preview, resume, or approval.
- **Incomplete safe snapshot:** emit `unassessed` with a fixed explanation; do not infer health.
- **Unknown rubric or node type:** emit a bounded unsupported diagnostic rather than awarding points.
- **Contradictory evidence:** preserve both positions and mark the affected critical path red until
  reviewed.
- **Hidden evidence:** exclude it without revealing existence.
- **Reasoner output fails validation:** discard the suggestion without changing assessment or graph.
- **Concurrent assessment:** identical snapshot requests may share a cache entry; different snapshot
  identities never share results.
- **Concurrent enrichment:** exact replay converges; divergent updates fail without partial writes.

## 15. Testing strategy

### 15.1 Assessment unit and contract tests

- exact scores and deductions for fixed-time fixtures;
- applicability and `N/A` behavior by node type;
- health thresholds and worst-significant-gap coloring;
- critical-path project rollups;
- confidence independent from robustness;
- stable IDs, ordering, report digests, and byte-identical repeat assessment;
- unknown rubric/node handling; and
- mutation checks proving every realistic rubric error changes a test result.

### 15.2 Security and snapshot tests

- full graph/evidence/case/clarification/history/policy preimage binding;
- stale and same-version replacement rejection;
- ACL-hidden evidence indistinguishability across scores, counts, topology, and suggestions;
- cancellation identity and traceback secrecy;
- malformed, oversized, cyclic, aliased, or subclassed raw input rejection before behavior; and
- exact cache isolation by repository, actor projection, graph version, and policy.

### 15.3 Enrichment tests

- 5-, 15-, and 30-minute budgets and focus scopes;
- deterministic highest-impact question selection;
- one-question progression, skip, pause, resume, restart, and expiry;
- immediate attributed evidence capture;
- incomplete sessions leave graph/history unchanged;
- proposal preview versus approved assessment separation;
- stale and concurrent session behavior; and
- existing proposal and ChangeSet governance remains the only mutation path.

### 15.4 Alternative-requirement tests

- preserved-intent and evidence grounding;
- explicit trade-offs and complexity reduction;
- solution-approach changes, including manual alternatives;
- invalid or unsupported suggestions rejected;
- deduplication and stable ordering;
- no score contribution from unvalidated model output; and
- no provider dependency for assessment or rendering.

### 15.5 UI, CLI, MCP, and journey tests

- graph/table values and selection remain synchronized;
- accessible icon/label behavior in addition to color;
- approved and projected overlays cannot be confused;
- bounded large-graph filtering and rendering;
- versioned CLI JSON and deterministic Markdown/Mermaid;
- MCP remains read-only and token-free;
- CI detects new red gaps and score regressions; and
- complete journeys cover voluntary improvement, prompt-time clarification, pause/resume, simpler
  alternatives, offline assessment, CI regression, and hidden-evidence non-disclosure.

## 16. Delivery sequence

### Increment 1: deterministic assessment foundation

- assessment models and policy;
- snapshot-bound scoring service;
- per-node, branch, and project scorecards;
- CLI JSON/text assessment;
- CI policy evaluation; and
- full ACL, staleness, determinism, and compatibility coverage.

### Increment 2: visualization and voluntary enrichment

- synchronized graph/table UI;
- accessible health overlays;
- improved Markdown/Mermaid rendering;
- resumable improvement sessions;
- deterministic question prioritization; and
- approved-versus-projected score comparison.

### Increment 3: grounded alternative requirements

- provider-neutral alternative reasoner port;
- strict grounding and validation;
- comparison experience;
- proposal submission through existing governance; and
- offline/unavailable-provider behavior.

Each increment is independently useful and must preserve all previous release journeys.

## 17. Acceptance criteria

The design is complete when all of the following are true:

1. A user can view the same scores in an interactive graph and a sortable table.
2. Node color follows the worst significant gap, while overall score and confidence remain visible.
3. Every score and deduction is deterministic, explainable, and evidence-linked.
4. A user can start a graph-improvement session without submitting a new requirement.
5. Improvement sessions are time-boxed, resumable, and ask one high-impact question at a time.
6. Every human answer is preserved immediately as attributed evidence.
7. No incomplete session or AI interpretation mutates the canonical graph.
8. Users can request simpler or materially different requirements and compare their trade-offs.
9. Alternative requirements identify preserved intent, supporting evidence, assumptions, risks, and
   projected score effects.
10. Existing reviewed ChangeSets remain the only graph-mutation boundary.
11. Deterministic assessment and visualization work without an AI provider.
12. ACL-hidden evidence cannot be inferred from any assessment or suggestion surface.
13. Scheduled/local CI assessment works without the plugin and without a second intent checkout.
14. Existing onboarding, prompt classification, clarification, reconciliation, assurance, rendering,
   and control-plane journeys remain compatible.

## 18. Baseline concerns discovered during design isolation

The original workspace had incomplete Git metadata (`HEAD`, configuration, and index were missing),
so this specification was prepared in a clean clone from GitHub `main` on branch
`codex/intent-graph-assessment`. The damaged directory was not modified during recovery.

The clean clone baseline also exposed two pre-existing environmental concerns unrelated to this
design:

1. the installed Codex binary reports `0.151.0-alpha.7.2`, while an existing contract test pins
   `0.148.0-alpha.9`; and
2. a full-suite baseline run reproduced an existing multiprocessing hang after 472 passing tests.

The owned pytest process tree was interrupted and fully reaped. No production correction for either
baseline concern is part of this design. They must be resolved or explicitly triaged before an
implementation branch can claim a clean baseline.
