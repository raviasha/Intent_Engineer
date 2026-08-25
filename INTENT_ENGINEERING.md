# Intent Engineering Framework
Version: 0.1.0  
Status: Foundational design specification  
Date: 2026-08-25

## 1. Purpose

Intent Engineering is an open framework for continuously capturing, managing, tracing, and reconciling the **why** behind software with the **what** that is specified and the **how** that is implemented.

The framework creates a living, evidence-backed intent graph that connects:

`SOURCE EVIDENCE → CONTEXT → NEED → INTENT → REQUIREMENT / DECISION → CODE / TEST → RUNTIME / OUTCOME`

The goal is not merely to provide more context to coding agents. The goal is to preserve **intent fidelity over time** as requirements, conversations, documents, code, tests, and implementation decisions evolve independently.

The framework should be useful to:
- individual developers;
- product and engineering teams;
- AI coding agents;
- architects and reviewers;
- organizations with requirements spread across GitHub, Slack, Confluence, Notion, Jira, meeting notes, and other systems.

The open-source project should be fully useful locally. A future managed cloud offering may add hosted operation, collaboration, enterprise connectors, dashboards, scale, and governance without making the open-source core artificially dependent on the cloud.

---

## 2. Core problem

Software projects accumulate intent in many disconnected forms:
- PRDs and design documents;
- Slack discussions;
- meeting notes;
- Confluence or Notion pages;
- Jira issues;
- GitHub issues and pull requests;
- code comments;
- commit messages;
- architecture decisions;
- tests;
- production behavior.

These sources drift.

A requirement document can be stale even when the code is correct.  
Code can diverge from both requirement and intent.  
A ticket can reflect an implementation shortcut rather than the original product need.  
A meeting can legitimately change intent without the formal requirement being updated.

Therefore the framework MUST NOT assume:

`requirement = truth`

Instead, mismatches create **reconciliation cases** that are evaluated using provenance, recency, confidence, authorship, code evidence, tests, and explicit decisions.

---

## 3. Product thesis

A useful engineering context system needs four capabilities:

### 3.1 Capture
Collect evidence from human and machine sources and normalize it without losing provenance.

### 3.2 Manage
Maintain an explicit graph of intent, requirements, decisions, assumptions, constraints, ownership, confidence, history, and conflicts.

### 3.3 Sync
Continuously compare the design-time graph against code, tests, documentation, tickets, and other implementation evidence.

### 3.4 Reconcile
When sources disagree, determine what kind of drift exists and propose the smallest evidence-backed action rather than automatically changing the code or documentation.

These four capabilities form the core loop:

`Capture → Manage → Sync → Reconcile → Update → Capture ...`

---

## 4. Design principles

1. **Provenance first**  
   Every material assertion must be traceable to source evidence.

2. **Intent and requirement are distinct**  
   A requirement states what should happen. Intent explains why it matters.

3. **Implementation status is distinct from design truth**  
   A requirement can be valid but unimplemented. Code can be implemented but unsupported by intent.

4. **Confidence is epistemic, not completion**  
   `intent_fidelity_confidence` measures confidence that the graph reflects current intended meaning. It does not mean priority, feasibility, implementation progress, or probability of business success.

5. **No silent reconciliation**  
   The system must not overwrite meaningful disagreement merely to make the graph internally clean.

6. **Same-author evolution is normal**  
   People can change their minds. Preserve history without manufacturing a conflict.

7. **Cross-author incompatibility is explicit**  
   When contributors hold incompatible positions, create a review/reconciliation item.

8. **Deterministic core, agentic reasoning at the edges**  
   Hashing, source versioning, schema validation, graph mutation, state transitions, and audit logging should be deterministic where practical. LLMs propose interpretation and reconciliation.

9. **Stable identity**  
   Nodes and edges should retain stable IDs when their semantic identity remains unchanged.

10. **Local-first open source**  
    The core must work against a local repository without requiring a hosted service.

11. **Agent agnostic**  
    Codex, Claude Code, Copilot, Cursor, or future agents should consume the same intent context through common interfaces.

12. **Evidence over eloquence**  
    A beautifully worded explanation with weak evidence must rank below an awkward but well-supported assertion.

---

## 5. Conceptual layers

The graph is logically separated into layers. They may be stored in one graph database later, but their semantics must remain distinct.

### 5.1 Evidence layer
Immutable or version-addressed observations from external systems.

Examples:
- Slack message;
- meeting-note paragraph;
- Confluence revision;
- Jira issue version;
- GitHub issue;
- commit;
- pull request;
- code diff;
- test result;
- production incident;
- user feedback.

Evidence is never rewritten to make the graph consistent.

### 5.2 Intent layer
Explains why the system exists and why design decisions matter.

Typical nodes:
- CONTEXT
- NEED
- PRODUCT_INTENT
- DESIRED_OUTCOME
- ASSUMPTION
- PRINCIPLE
- CONSTRAINT

### 5.3 Specification layer
The current design-time contract.

Typical nodes:
- REQUIREMENT
- CAPABILITY
- ACCEPTANCE_CRITERION
- DECISION
- ARCHITECTURE
- INTERFACE
- DATA_CONTRACT
- POLICY

### 5.4 Implementation layer
What exists in the repository.

Typical nodes:
- REPOSITORY
- MODULE
- FILE
- SYMBOL
- ENDPOINT
- SCHEMA
- TEST
- BUILD_ARTIFACT
- DEPLOYMENT

### 5.5 Outcome layer
Evidence that the implemented behavior produces the intended result.

Typical nodes:
- METRIC
- OBSERVED_OUTCOME
- INCIDENT
- USER_FEEDBACK
- EXPERIMENT_RESULT

---

## 6. Canonical truth model

There is no single universally trusted external source.

The framework maintains:
1. source evidence;
2. a normalized graph representing the current interpreted state;
3. audit history for how that state was produced;
4. unresolved reconciliation cases.

For the initial open-source implementation:
- YAML is the canonical machine-readable graph representation;
- Markdown is the preferred human-readable authoring and handoff format;
- Mermaid is a generated view only;
- source systems remain authoritative for their own raw evidence;
- graph assertions are authoritative only as the framework's current interpreted state.

Later graph databases may be supported through an adapter while retaining export to canonical YAML.

---

## 7. Provenance model

Every material graph assertion should carry or resolve to:

- `created_by`
- `created_at`
- `last_modified_by`
- `last_modified_at`
- `source_mode`: explicit | inferred | derived
- `evidence_refs`
- `source_system`
- `source_locator`
- `source_version`
- `content_hash`
- `intent_fidelity_confidence`
- `confidence_basis`
- `last_reassessed_at`

For source-backed evidence, preserve:
- external object ID;
- revision/version if available;
- timestamp;
- author;
- channel/page/repository context;
- immutable content hash.

The system should be able to answer:

> Why does the graph believe this?

with a compact evidence chain.

---

## 8. Confidence semantics

`intent_fidelity_confidence` is a value from 0.0 to 1.0 answering:

> How confident are we that this assertion reflects the contributors' current intended meaning?

Suggested bands:
- 0.95–1.00: explicit and recently confirmed;
- 0.80–0.94: explicit but aging or mildly ambiguous;
- 0.65–0.85: inferred from multiple consistent signals;
- 0.40–0.64: inferred from sparse or indirect evidence;
- below 0.40: speculative.

Every material confidence change must be auditable:
- prior confidence;
- new confidence;
- evidence;
- reason;
- timestamp;
- actor;
- change kind.

Confidence must never be used as:
- implementation percentage;
- requirement priority;
- delivery confidence;
- feasibility;
- business-case probability;
- test coverage;
- user preference strength.

---

## 9. Capture architecture

Capture is connector-based.

### 9.1 Connector contract

Each connector must implement conceptually:

```text
discover(scope, cursor) -> source objects
fetch(object_id, version?) -> canonical evidence payload
checkpoint() -> cursor
normalize(raw_object) -> evidence record
```

Each evidence record includes:
- connector type;
- external object ID;
- external version;
- author;
- timestamp;
- text or structured content;
- parent/thread/page context;
- ACL metadata where available;
- hash;
- source URL or locator.

### 9.2 Initial connector priority

MVP:
1. local Markdown/docs;
2. local Git repository;
3. GitHub issues, PRs, commits.

Next:
4. Jira;
5. Confluence;
6. Notion;
7. Slack.

Later:
8. Google Docs/Drive;
9. Linear;
10. Figma comments/design metadata;
11. email/meeting systems where permission and privacy models are appropriate.

### 9.3 Capture modes

Support:
- manual import;
- CLI sync;
- scheduled sync;
- webhook/event driven sync.

MVP should support manual + scheduled operation. Event-driven updates can be added after reconciliation behavior is stable.

---

## 10. Intent extraction

Capture does not immediately mutate the graph.

Pipeline:

1. ingest source artifact;
2. calculate content hash and compare with prior version;
3. create immutable evidence record;
4. identify changed spans;
5. extract candidate assertions;
6. classify assertion type;
7. resolve identity against existing graph nodes;
8. propose graph delta;
9. validate schema and invariants;
10. auto-apply only low-risk deterministic changes;
11. queue ambiguous/material changes for reconciliation or approval.

Candidate assertions may include:
- new context;
- new need;
- new intent;
- requirement;
- constraint;
- assumption;
- decision;
- supersession;
- rejection;
- architecture choice;
- acceptance criterion;
- ownership;
- explicit conflict.

The LLM should return structured proposals. It should not directly edit canonical graph files.

---

## 11. Manage capability

The Manage layer is the durable project memory.

It provides:
- typed graph entities;
- stable IDs;
- provenance;
- authorship;
- confidence history;
- graph change log;
- branches or change sets;
- review/reconciliation items;
- supersession history;
- generated views;
- validation rules.

The graph should make these questions easy:

- Why does this requirement exist?
- Who introduced it?
- What evidence supports it?
- What changed its meaning?
- Which code implements it?
- Which tests verify it?
- What would be affected if it changed?
- Is any source currently in conflict?
- Which assertions are weakly supported?
- Which implementation areas have no traceable intent?

---

## 12. Sync capability

Sync continuously compares graph state with implementation evidence.

### 12.1 Key rule

Sync is **comparison**, not automatic correction.

A mismatch creates a drift observation and, when material, a reconciliation case.

### 12.2 Repository indexing

For MVP, index:
- repository;
- directories/modules;
- files;
- symbols where parser support exists;
- test files and test names;
- commits;
- pull requests;
- issue references;
- dependency relationships where cheaply obtainable.

Prefer deterministic parsers:
- tree-sitter or language-native AST tooling;
- git diff;
- test manifests;
- build metadata.

Use embeddings/LLMs only where deterministic mapping is insufficient.

### 12.3 Requirement-to-code mapping

Mappings can originate from:
1. explicit references in commit/PR/issue;
2. code annotations;
3. test names;
4. directory ownership rules;
5. historical change co-occurrence;
6. semantic inference.

Each mapping gets:
- provenance;
- mapping method;
- confidence;
- verification date.

### 12.4 Sync checks

At minimum:

- every active requirement has implementation mapping or an explicit `not_started` state;
- every acceptance criterion has verifying test evidence or an explicit gap;
- implemented claims cite dated repository/test evidence;
- deleted or heavily changed code invalidates stale implementation claims;
- code mapped to superseded requirements is surfaced;
- significant code areas with no intent/requirement linkage are surfaced as orphan implementation;
- active requirements with no code or test movement beyond configured age are surfaced;
- source documents whose claims conflict with newer graph decisions are surfaced;
- graph edges with broken source/evidence references fail validation.

### 12.5 Scheduler

Initial deployment:
- local CLI;
- GitHub Action;
- configurable nightly run.

Later:
- webhooks for PR merge, issue update, document update;
- cloud scheduler;
- near-real-time incremental reconciliation.

A model must not "run continuously" merely by staying active. The deterministic scheduler triggers sync jobs and invokes reasoning only when needed.

---

## 13. Reconciliation capability

Reconciliation is the framework's key differentiator.

When intent, requirement, code, test, or source evidence disagree, classify the divergence.

### 13.1 Reconciliation case types

- `CODE_LAG`  
  Requirement/intent changed; implementation has not caught up.

- `REQUIREMENT_LAG`  
  Stronger/newer intent and implementation evidence agree; formal requirement appears stale.

- `INTENT_LAG`  
  Requirement and implementation changed but the intent graph still reflects an older rationale.

- `TEST_LAG`  
  Implementation appears aligned but acceptance/test evidence is missing or stale.

- `DOC_LAG`  
  Secondary documentation disagrees with the accepted graph state.

- `UNDOCUMENTED_CODE`  
  Material implementation exists with no traceable requirement or decision.

- `ORPHAN_REQUIREMENT`  
  Active requirement has no implementation owner, code mapping, or deliberate backlog status.

- `CONFLICTING_SOURCES`  
  Two or more credible sources contain incompatible current claims.

- `AMBIGUOUS_DIVERGENCE`  
  Evidence is insufficient to decide which side is stale.

- `POSSIBLE_INTENT_CHANGE`  
  New human evidence suggests the underlying goal changed and requires confirmation.

### 13.2 Reconciliation decision packet

Every material case should show:

- subject;
- affected graph nodes;
- affected code/tests;
- conflicting claims;
- evidence on each side;
- timestamps/recency;
- authorship;
- confidence;
- likely impact;
- recommended action;
- alternative actions;
- whether auto-apply is permitted;
- human decision required;
- audit trail.

### 13.3 Resolution actions

Possible resolutions:

- update implementation;
- update requirement;
- update intent;
- update tests;
- update documentation;
- accept implementation as new design and backfill intent/decision;
- supersede old requirement;
- merge compatible interpretations;
- preserve explicit disagreement;
- defer with owner/date;
- mark false positive.

### 13.4 Safety invariant

No reconciliation action that materially changes product intent, requirements, architecture, or cross-author position should be automatically applied without a policy that explicitly permits it.

---

## 14. Drift scoring

A drift score should prioritize review, not declare truth.

Illustrative components:
- semantic divergence;
- evidence recency gap;
- source authority policy;
- intent confidence;
- mapping confidence;
- implementation change magnitude;
- test failure or coverage gap;
- unresolved conflict age.

The score should be explainable.

Do not hide a low-confidence mapping behind a single high precision-looking number.

---

## 15. Authorship and conflict model

Support arbitrary contributors, not only two named authors.

### 15.1 Same-author evolution

When an author revises their own prior position:
- preserve history;
- supersede or update the assertion;
- record the change;
- do not create a conflict merely because the person changed their mind.

### 15.2 Cross-author incompatibility

When different contributors make materially incompatible assertions:
- preserve both positions;
- create a reconciliation/review case;
- do not average confidence;
- do not silently pick one;
- allow explicit resolution.

### 15.3 Source authority policy

Projects may configure authority rules, for example:
- approved ADR outranks informal notes for architecture;
- signed-off requirement outranks an old ticket;
- a newly approved meeting decision can supersede a prior requirement if the project policy allows it.

Authority is evidence weighting, not absolute truth.

---

## 16. Framework graph versus project graph

Two graph instances must be kept conceptually separate.

### 16.1 Framework graph

Describes Intent Engineering itself:
- vision;
- needs;
- design decisions;
- capabilities;
- architecture;
- roadmap;
- open-source/cloud boundary.

This repo should dogfood the framework.

### 16.2 Project graph

Created for each user repository:
- project intent;
- requirements;
- architecture;
- code mappings;
- tests;
- source evidence;
- reconciliation cases.

The same meta-model powers both.

---

## 17. Agent integration

The framework should not start by building a proprietary IDE plugin.

Recommended integration order:

1. CLI;
2. machine-readable context command;
3. MCP server;
4. agent skill / instructions;
5. GitHub Action;
6. IDE extension if user value justifies it;
7. hosted GitHub App for managed cloud.

### 17.1 Core agent queries

Expose queries such as:

```text
intent explain <path-or-symbol>
intent requirements <path-or-symbol>
intent impact <requirement-id>
intent drift
intent reconcile <case-id>
intent context --task "<task description>"
```

### 17.2 Context pack

For a coding task, return a compact context pack:
- relevant intent;
- requirements;
- decisions;
- constraints;
- acceptance criteria;
- mapped code;
- tests;
- unresolved reconciliation cases;
- evidence references;
- confidence warnings.

This should reduce the need to load the entire project history into a coding agent.

---

## 18. Proposed open-source architecture

```text
intent-engineering/
├── INTENT_ENGINEERING.md
├── CODEX_IMPLEMENTATION.md
├── ROADMAP.md
├── LICENSE
├── CONTRIBUTING.md
├── AGENTS.md
├── pyproject.toml
├── src/intent_engineering/
│   ├── cli/
│   ├── core/
│   │   ├── models/
│   │   ├── graph/
│   │   ├── provenance/
│   │   ├── confidence/
│   │   ├── history/
│   │   └── policy/
│   ├── capture/
│   │   ├── base.py
│   │   ├── markdown/
│   │   ├── git/
│   │   └── github/
│   ├── extract/
│   ├── sync/
│   ├── reconcile/
│   ├── context/
│   ├── storage/
│   │   ├── yaml/
│   │   └── interfaces.py
│   ├── render/
│   └── integrations/
│       ├── mcp/
│       └── github_action/
├── schemas/
│   ├── intent-meta-model.yaml
│   ├── graph.schema.json
│   ├── evidence.schema.json
│   └── reconciliation.schema.json
├── graph/
│   └── framework-intent-graph.yaml
├── examples/
│   ├── minimal-project/
│   └── property-matchmaking/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── .github/
    └── workflows/
        └── intent-sync.yml
```

Language recommendation for MVP: Python, because connector work, YAML/schema tooling, repository analysis, CLI development, and LLM integration are straightforward. Keep core interfaces language-neutral enough for future services in other languages.

---

## 19. Core service interfaces

### 19.1 GraphStore

```python
class GraphStore(Protocol):
    def get_node(self, node_id: str): ...
    def query(self, query): ...
    def apply_changeset(self, changeset): ...
    def history(self, subject_id: str): ...
```

### 19.2 EvidenceStore

```python
class EvidenceStore(Protocol):
    def put(self, evidence): ...
    def get(self, evidence_id: str): ...
    def versions(self, external_object_id: str): ...
```

### 19.3 Connector

```python
class Connector(Protocol):
    def discover(self, cursor=None): ...
    def fetch(self, object_id: str, version=None): ...
    def normalize(self, raw): ...
```

### 19.4 Reconciler

```python
class Reconciler(Protocol):
    def detect(self, graph, evidence_delta): ...
    def build_case(self, divergence): ...
    def propose_resolution(self, case): ...
```

### 19.5 ContextProvider

```python
class ContextProvider(Protocol):
    def for_task(self, task: str, repository_scope=None): ...
    def for_symbol(self, symbol_ref: str): ...
```

---

## 20. Storage strategy

### MVP
- YAML graph files committed to Git;
- JSONL or YAML evidence index;
- SQLite for local indexes/caches if needed;
- Git history for repository-level versioning;
- schema validation on every mutation.

### Later
Adapter support for:
- Neo4j;
- PostgreSQL + graph/vector extensions;
- managed graph service.

The domain model must not depend on one database vendor.

---

## 21. Change-set model

LLMs and connectors propose a `ChangeSet`.

A ChangeSet includes:
- ID;
- actor;
- timestamp;
- baseline graph version;
- evidence refs;
- nodes added/updated/superseded;
- edges added/updated/superseded;
- confidence changes;
- implementation status changes;
- reconciliation cases created/resolved;
- validation result.

A deterministic applier validates and commits accepted changes.

---

## 22. Implementation-status model

Implementation state must remain separate from requirement state.

Suggested statuses:
- `implemented_baseline`
- `partial`
- `not_started`
- `requirements_incomplete`
- `unknown`
- `stale_evidence`

An implementation claim must include:
- requirement refs;
- current behavior;
- code evidence;
- test evidence when applicable;
- verified commit SHA;
- verification timestamp.

A UI label, comment, or unexecuted test is insufficient evidence by itself.

---

## 23. Sync job lifecycle

```text
START
  ↓
Load project config + last checkpoint
  ↓
Fetch changed source artifacts
  ↓
Fetch git/code/test changes
  ↓
Normalize immutable evidence
  ↓
Compute deterministic deltas
  ↓
Extract candidate semantic changes
  ↓
Resolve graph identity
  ↓
Validate proposed mappings
  ↓
Run drift detectors
  ↓
Create/update reconciliation cases
  ↓
Auto-apply permitted low-risk metadata changes
  ↓
Emit report + context index
  ↓
Persist checkpoint
END
```

A failed connector must not corrupt the graph. Sync jobs should be idempotent.

---

## 24. GitHub workflow

### MVP workflow

Nightly and on manual dispatch:

1. checkout repository;
2. install Intent Engineering CLI;
3. run `intent validate`;
4. run `intent sync --sources local,git,github`;
5. run `intent drift --format markdown`;
6. upload or comment the report;
7. optionally fail CI only for configured invariant violations, not for ordinary unresolved design ambiguity.

### Pull request workflow

On PR:
- infer impacted requirements;
- show relevant intent/constraints;
- identify missing tests;
- show unresolved reconciliation cases;
- detect if PR introduces meaningful code without requirement/decision trace.

Do not block PRs by default in early versions.

---

## 25. Open source versus managed cloud

### Open-source core should include

- meta-model and schemas;
- YAML graph store;
- local evidence store;
- local Markdown connector;
- Git connector;
- GitHub connector using user credentials;
- CLI;
- drift detection;
- reconciliation case model;
- context pack generator;
- GitHub Action template;
- MCP server;
- agent instruction/skill templates;
- example projects;
- graph renderer;
- import/export.

### Managed cloud may add

- hosted graph/evidence store;
- OAuth connector management;
- webhooks and reliable schedulers;
- Slack/Confluence/Notion/Jira managed connectors;
- multi-project search;
- organization-wide intent graph;
- RBAC and SSO;
- audit retention;
- dashboards and health trends;
- team reconciliation workflows;
- hosted LLM reasoning;
- enterprise policy controls;
- secrets management;
- cross-repository impact analysis;
- SLA and support.

### Commercial principle

Cloud should deliver convenience, collaboration, scale, and governance. It should not remove basic local usefulness from the open-source framework.

---

## 26. Privacy and security

The framework may ingest sensitive company information.

Requirements:
- least-privilege connectors;
- source ACL metadata retained where practical;
- configurable exclusion rules;
- secrets never stored in graph evidence;
- local mode must be possible;
- cloud mode must support tenant isolation;
- deletion/tombstone handling;
- auditable access;
- configurable LLM providers;
- ability to run with no external LLM for deterministic-only functions.

Do not assume that all captured evidence can be shown to every developer who can access the code repository.

---

## 27. Configuration

Example project configuration:

```yaml
project:
  id: demo
  graph_path: .intent/graph.yaml

sync:
  schedule: nightly
  auto_apply:
    metadata_only: true
    confidence_only: false
    semantic_changes: false

sources:
  - type: markdown
    paths: ["docs/**/*.md"]
  - type: git
  - type: github
    repository: "org/repo"

policies:
  require_evidence_for_implemented: true
  material_semantic_changes_require_review: true
  orphan_code_detection: true
  orphan_requirement_detection: true
```

---

## 28. CLI MVP

Required commands:

```text
intent init
intent validate
intent ingest
intent sync
intent drift
intent status
intent explain <id|path|symbol>
intent context --task "<task>"
intent reconcile list
intent reconcile show <case-id>
intent reconcile resolve <case-id>
intent render
```

Optional later:
```text
intent server
intent mcp
intent doctor
intent connectors
```

---

## 29. Health metrics

The framework should measure useful engineering alignment, not vanity graph size.

Candidate metrics:
- percentage of active requirements with intent trace;
- percentage with implementation evidence;
- percentage of acceptance criteria with test evidence;
- stale implementation evidence count;
- orphan implementation count;
- unresolved reconciliation cases;
- median reconciliation age;
- low-confidence intent assertions;
- source freshness;
- context-pack precision/recall feedback;
- number of changes caught before merge;
- false-positive drift rate.

Avoid one universal "intent health score" until component metrics are validated.

---

## 30. MVP definition

MVP proves the full loop on one Git repository without enterprise integrations.

### MVP source set
- Markdown;
- Git;
- GitHub.

### MVP capabilities
- initialize graph;
- validate graph/meta-model;
- ingest evidence;
- create/update intent and requirement nodes through structured proposals;
- map requirements to code/tests;
- nightly/manual sync;
- detect at least five drift classes;
- create reconciliation cases;
- generate a Markdown drift report;
- answer `intent explain` and `intent context`;
- expose context through MCP;
- dogfood the framework on its own repo.

### Required drift classes for MVP
- CODE_LAG;
- REQUIREMENT_LAG;
- TEST_LAG;
- UNDOCUMENTED_CODE;
- AMBIGUOUS_DIVERGENCE.

### MVP non-goals
- perfect semantic understanding;
- autonomous requirement rewriting;
- autonomous code modification;
- enterprise-scale authorization;
- every SaaS connector;
- real-time sync;
- graph database dependency;
- IDE extension.

---

## 31. Acceptance criteria

The first public alpha is complete when:

1. A user can run `intent init` in an existing repository.
2. The tool creates a valid `.intent/` workspace and starter graph.
3. Markdown and Git changes can be ingested with provenance.
4. GitHub issue/PR/commit metadata can be captured.
5. A requirement can trace backward to intent/evidence and forward to code/tests.
6. Code changes invalidate or refresh affected implementation evidence.
7. A mismatch can produce a reconciliation case rather than a binary "code wrong" result.
8. At least one fixture demonstrates `REQUIREMENT_LAG`, where code and newer intent evidence agree against a stale requirement.
9. At least one fixture demonstrates `CODE_LAG`.
10. The graph stores confidence separately from implementation status.
11. Cross-author incompatible positions remain separately visible.
12. Every graph mutation is a validated ChangeSet with audit information.
13. `intent context --task` returns a compact, relevant context package.
14. An MCP-compatible coding agent can retrieve that context.
15. The repo's own framework graph validates using the same engine.
16. The project runs locally with no managed cloud account.
17. Tests cover graph invariants, idempotent sync, reconciliation classification, and provenance preservation.
18. A GitHub Action can produce a nightly drift report.

---

## 32. Reference scenarios

### Scenario A — Code is stale

Evidence:
- approved requirement changed yesterday;
- intent unchanged;
- mapped code last changed two months ago;
- tests still reflect old behavior.

Result:
- `CODE_LAG`;
- recommend implementation + tests update.

### Scenario B — Requirement is stale

Evidence:
- recent meeting decision explicitly changes behavior;
- PR implements that decision;
- tests verify it;
- requirement document still contains old wording.

Result:
- `REQUIREMENT_LAG`;
- recommend requirement update;
- do not roll back code automatically.

### Scenario C — Intent changed

Evidence:
- requirement and code agree;
- new customer evidence reveals that the underlying desired outcome has changed;
- no approved requirement change yet.

Result:
- `POSSIBLE_INTENT_CHANGE`;
- require product/design confirmation.

### Scenario D — Undocumented code

Evidence:
- significant new module merged;
- no issue, requirement, ADR, or linked intent;
- tests exist.

Result:
- `UNDOCUMENTED_CODE`;
- request design/decision backfill rather than automatically deleting code.

### Scenario E — Contributor disagreement

Evidence:
- contributor A explicitly requires local-only storage;
- contributor B explicitly requires cloud-only centralized storage;
- both are current.

Result:
- preserve both;
- create cross-author reconciliation case;
- do not average into "hybrid storage" unless contributors decide that.

---

## 33. Dogfooding rule

The Intent Engineering repository must maintain a framework intent graph representing this specification.

Any material change to:
- core principles;
- meta-model;
- open-source boundary;
- sync semantics;
- reconciliation behavior;
- architecture;
- roadmap

should update the framework graph in the same pull request.

This is the primary reference implementation and proof that the framework can manage its own intent.

---

## 34. Evolution strategy

Start opinionated and small.

Do not prematurely build:
- a universal ontology;
- a large graph database deployment;
- dozens of connectors;
- a full IDE;
- organization-wide autonomous agents.

First prove:
1. provenance survives capture;
2. traceability is useful;
3. drift detection finds real mismatches;
4. reconciliation correctly avoids assuming the requirement is always right;
5. coding agents receive better task context.

Only then widen the connector and platform surface.

---

## 35. Naming

Working project name: **Intent Engineering**.

Potential package/CLI names can be decided later. Do not block MVP on branding.

Core conceptual vocabulary should remain stable:
- Evidence
- Intent
- Requirement
- Implementation
- Sync
- Drift
- Reconciliation
- ChangeSet
- Context Pack
- Provenance
- Intent Fidelity Confidence

---

## 36. Foundational invariant

The framework exists to preserve this chain:

> What are we trying to achieve, why do we believe that, what did we decide, what did we build, what proves it, and where do those things disagree today?

Any feature that does not strengthen that chain should be treated as optional.
