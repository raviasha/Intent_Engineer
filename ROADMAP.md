# Intent Engineering Roadmap
Version: 0.1.0  
Date: 2026-08-25

## Roadmap principle

Build outward from the reconciliation loop.

Do not optimize first for connector count, UI polish, or graph sophistication. The framework earns the right to grow only after it can reliably answer:

> What changed, what intent/requirement/code/test evidence disagrees, and what should a human or coding agent examine next?

---

## Phase 0 — Foundational repository / dogfood

### Goal
Turn the design into an executable open-source skeleton and use the framework to describe itself.

### Deliverables
- foundational docs;
- meta-model;
- framework intent graph;
- Python package skeleton;
- domain models;
- YAML graph store;
- evidence model;
- ChangeSet model;
- reconciliation model;
- validation;
- initial CLI;
- test fixtures;
- dogfood graph CI.

### Exit criteria
- framework graph validates;
- graph changes are auditable;
- evidence is versioned;
- same input sync is idempotent;
- `CODE_LAG` and `REQUIREMENT_LAG` are demonstrably distinguishable.

---

## Phase 1 — Local OSS MVP

### Goal
A developer can install the tool into an existing repository and get meaningful intent/code drift analysis without a hosted service.

### Sources
- Markdown;
- Git;
- GitHub issues/PRs/commits.

### Capabilities
- `intent init`;
- source ingestion;
- Git history indexing;
- basic requirement-to-code mappings;
- implementation evidence;
- nightly/manual sync;
- five initial drift classes;
- reconciliation report;
- task context packs;
- GitHub Action;
- generated Mermaid views.

### Initial drift classes
- CODE_LAG;
- REQUIREMENT_LAG;
- TEST_LAG;
- UNDOCUMENTED_CODE;
- AMBIGUOUS_DIVERGENCE.

### Exit criteria
- useful on at least three real repositories;
- false-positive drift rate measured;
- users can inspect evidence behind every case;
- no cloud dependency;
- installation documented.

---

## Phase 2 — Coding-agent integration

### Goal
Make the graph useful during active software development.

### Deliverables
- MCP server;
- `intent context --task`;
- `intent explain <path|symbol>`;
- `intent impact <requirement>`;
- agent instruction templates;
- Codex integration example;
- Claude Code integration example where appropriate;
- PR context generation.

### User experience
A developer asks a coding agent to implement a task. The agent retrieves:
- intent;
- requirements;
- constraints;
- acceptance criteria;
- mapped code;
- tests;
- unresolved drift;
- evidence warnings.

### Exit criteria
- context packs reduce irrelevant context loading in benchmark tasks;
- developers report improved requirement adherence;
- agent integration remains vendor-neutral.

---

## Phase 3 — Collaboration connectors

### Goal
Capture the major human sources where intent changes before formal requirements catch up.

### Priority
1. Jira
2. Confluence
3. Notion
4. Slack

### Deliverables
- connector SDK;
- connector-specific provenance;
- revision tracking;
- thread/page context;
- source ACL metadata;
- incremental checkpoints;
- source freshness metrics.

### Reconciliation expansion
Add:
- DOC_LAG;
- CONFLICTING_SOURCES;
- POSSIBLE_INTENT_CHANGE;
- ORPHAN_REQUIREMENT;
- INTENT_LAG.

### Exit criteria
- requirement-lag cases can be traced to human decision evidence;
- source ingestion is permission-aware;
- connector failure does not corrupt graph state.

---

## Phase 4 — Hosted beta

### Goal
Offer convenience and team collaboration without weakening the local open-source core.

### Managed features
- hosted graph/evidence store;
- OAuth connector setup;
- reliable scheduling;
- webhooks;
- team workspace;
- reconciliation inbox;
- dashboards;
- graph exploration;
- managed model execution;
- secrets management;
- cloud backups.

### GitHub App
Add a GitHub App for:
- installation;
- PR annotations;
- drift reports;
- repository onboarding;
- webhook delivery.

### Exit criteria
- a small team can onboard without local infrastructure;
- OSS export/import remains supported;
- cloud data boundary is documented;
- tenants are isolated.

---

## Phase 5 — Organization-scale intent layer

### Goal
Connect multiple repositories and product systems into a cross-project intent fabric.

### Capabilities
- organization graph;
- cross-repository impact;
- shared capability/requirement nodes;
- dependency tracing;
- centralized policy;
- RBAC/SSO;
- audit controls;
- ownership and escalation;
- portfolio drift views.

### Exit criteria
- changes in shared intent can identify impacted repositories;
- users can restrict evidence by authorization;
- enterprise governance is practical.

---

## Phase 6 — Advanced reconciliation intelligence

### Goal
Improve prioritization and suggestions once enough real drift data exists.

### Candidate capabilities
- learned mapping confidence;
- semantic drift scoring;
- change-impact ranking;
- recurring drift pattern detection;
- stale-source prediction;
- recommendation quality feedback;
- confidence calibration;
- automated evidence summarization.

### Constraint
Do not train or deploy opaque "truth scores." Reconciliation must remain explainable and evidence-backed.

---

## Phase 7 — IDE and workflow experiences

### Goal
Bring intent into the developer's flow only after core value is proven.

### Options
- VS Code extension;
- JetBrains extension;
- PR review UI;
- architecture graph explorer;
- inline "why does this exist?" navigation;
- requirement/test trace lens.

The extension should be a client of the same core APIs/MCP interfaces, not a second implementation of the graph.

---

## Open-source boundary

Keep open:
- semantic model;
- file formats;
- schemas;
- graph validation;
- CLI;
- local storage;
- local Git/Markdown support;
- core drift/reconciliation engine;
- context-pack protocol;
- MCP interface;
- basic GitHub integration;
- examples;
- export/import.

Potential managed value:
- hosted infrastructure;
- managed connectors;
- enterprise auth;
- reliable event processing;
- organization-wide graph;
- dashboards;
- collaboration workflows;
- hosted inference;
- compliance/governance;
- support.

---

## Adoption ladder

### Level 1 — Repository hygiene
Run locally and generate drift reports.

### Level 2 — Agent context
Give coding agents task-specific intent context.

### Level 3 — Team reconciliation
Connect tickets/docs/conversations and resolve drift collaboratively.

### Level 4 — Managed platform
Use hosted connectors, scheduling, dashboards, and GitHub App.

### Level 5 — Organization intent fabric
Trace intent and implementation across multiple products and repositories.

---

## Metrics by stage

### Foundation
- validation pass rate;
- idempotent sync;
- test coverage of invariants.

### Local MVP
- useful drift cases per run;
- false-positive rate;
- mean evidence trace completeness;
- requirements with code/test mappings.

### Agent integration
- context precision;
- context token reduction;
- task rework/retry reduction;
- acceptance-criteria adherence.

### Collaboration
- reconciliation age;
- stale requirement detection;
- undocumented change detection;
- source freshness.

### Cloud
- setup time;
- active projects;
- sync reliability;
- connector failure rate;
- reconciliation workflow completion.

---

## First public release recommendation

Target a narrow **0.1 alpha**:

> "A local-first intent graph for Git repositories that can explain why code exists and surface when intent, requirements, code, and tests have drifted."

That claim is strong enough to be useful and narrow enough to prove.
