# Codex Implementation Instructions
Version: 0.1.0  
Date: 2026-08-25

## Mission

Build the initial open-source **Intent Engineering** repository from the accompanying foundational specification.

The system must capture and manage intent with provenance, compare it against requirements/code/tests, and create evidence-backed reconciliation cases when those artifacts diverge.

The implementation must **not** assume that requirements are always correct. A mismatch may indicate stale code, stale requirements, stale intent, stale tests, undocumented implementation, or ambiguity.

---

## Read order

Before writing code, read in this exact order:

1. `INTENT_ENGINEERING.md`
2. `schemas/intent-meta-model.yaml`
3. `graph/framework-intent-graph.yaml`
4. `ROADMAP.md`
5. this file

Treat `INTENT_ENGINEERING.md` as the foundational framework specification and `schemas/intent-meta-model.yaml` as the initial machine-readable semantic contract.

The framework graph must dogfood the same model implemented by the repository.

---

## Non-regression constraints

Do not remove or collapse these concepts:

- source provenance;
- authorship;
- source mode: explicit / inferred / derived;
- intent fidelity confidence;
- confidence history;
- intent versus requirement distinction;
- implementation status as a separate semantic concern;
- evidence-backed implementation claims;
- stable graph IDs;
- same-author evolution;
- cross-author incompatibility;
- explicit reconciliation cases;
- graph change history / ChangeSets;
- generated views as non-canonical;
- local-first operation;
- agent-agnostic interfaces.

If an implementation simplification conflicts with these invariants, preserve the invariant and simplify something else.

---

## Initial implementation objective

Build **MVP 0 / foundation**, not the entire roadmap.

The first implementation should establish:
- repository skeleton;
- domain models;
- YAML graph store;
- schema validation;
- ChangeSet application;
- provenance/evidence store;
- Markdown connector;
- Git connector;
- deterministic sync skeleton;
- reconciliation case model;
- basic drift detectors;
- CLI;
- tests;
- framework graph validation.

Do not begin Slack, Confluence, Notion, Jira, cloud hosting, or a full IDE extension in the initial implementation.

---

## Required repository layout

Use this as the starting layout unless a clearly justified implementation detail requires a small change:

```text
intent-engineering/
├── INTENT_ENGINEERING.md
├── CODEX_IMPLEMENTATION.md
├── ROADMAP.md
├── AGENTS.md
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── pyproject.toml
├── src/intent_engineering/
│   ├── __init__.py
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
│   │   ├── interfaces.py
│   │   └── yaml/
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
│   └── minimal-project/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── .github/workflows/
```

---

## Architecture requirements

### 1. Domain layer has no connector dependency

Core models must not import GitHub, Slack, Notion, or cloud SDKs.

### 2. Storage is interface-driven

Implement YAML first behind interfaces so later adapters can use SQLite/PostgreSQL/Neo4j without changing domain semantics.

### 3. Evidence is immutable/versioned

A changed source object creates a new version-addressed evidence record. Do not mutate old evidence into the new value.

### 4. LLMs propose; deterministic code applies

Define a structured proposal/ChangeSet boundary even if the first MVP uses a mock semantic extractor.

### 5. Sync is idempotent

Running sync twice with no input change must not create duplicate evidence, graph mutations, or reconciliation cases.

### 6. Reconciliation does not auto-pick truth

A drift detector creates a case with evidence sides and a proposed classification.

### 7. Explainability

Every generated case must be able to show which evidence and graph nodes caused it.

---

## Suggested implementation technology

Use:
- Python 3.12+
- `pydantic` for domain validation
- `typer` for CLI
- `PyYAML` or `ruamel.yaml`
- `jsonschema` where appropriate
- `pytest`
- `gitpython` or direct git subprocess wrapper
- optional `tree-sitter` later for symbol extraction

Avoid bringing in a graph database for MVP 0.

---

## Domain models to implement first

At minimum:

- `Graph`
- `Node`
- `Edge`
- `EpistemicState`
- `ConfidenceChange`
- `EvidenceRecord`
- `EvidenceRef`
- `ImplementationClaim`
- `ChangeSet`
- `ReconciliationCase`
- `ReconciliationEvidenceSide`
- `ProjectConfig`
- `SyncCheckpoint`
- `ContextPack`

Use enums for:
- node type;
- source mode;
- implementation status;
- reconciliation case type;
- reconciliation status;
- resolution action;
- change kind.

Unknown/future node types should have a controlled extension mechanism rather than requiring unsafe free-form parsing.

---

## Graph invariants to test

Write tests before or alongside implementation for:

1. node IDs are unique;
2. edge IDs are unique;
3. edges reference existing nodes unless explicitly external;
4. confidence stays in 0.0–1.0;
5. an `implemented_baseline` claim requires repository evidence and verification date;
6. semantic mutations require a ChangeSet;
7. evidence records are immutable;
8. stable IDs survive non-semantic edits;
9. cross-author contradiction can create a reconciliation case without overwriting either assertion;
10. generated views do not mutate canonical YAML;
11. a second identical sync produces no semantic change;
12. a stale code mapping can become `stale_evidence`;
13. a requirement/code mismatch can be classified as either `CODE_LAG` or `REQUIREMENT_LAG` depending on supplied evidence.

The final two are especially important: they prove the engine is not hardcoded to blame code.

---

## MVP drift detectors

Implement deterministic scaffolding for these first:

### CODE_LAG

Candidate condition:
- active requirement changed after mapped implementation evidence;
- no newer implementation evidence;
- no stronger contradictory current source.

### REQUIREMENT_LAG

Candidate condition:
- newer explicit intent/decision evidence;
- implementation/test evidence agrees with that newer evidence;
- formal requirement is older and semantically incompatible.

The first version may use fixture-provided semantic labels rather than sophisticated embeddings. Prove the state machine before adding fuzzy inference.

### TEST_LAG

Candidate condition:
- implementation changed after last verifying test evidence;
- acceptance criterion remains active.

### UNDOCUMENTED_CODE

Candidate condition:
- material code addition/change has no mapping to requirement/decision/intent after configured grace rules.

### AMBIGUOUS_DIVERGENCE

Fallback when:
- meaningful inconsistency exists;
- current evidence is insufficient to confidently assign a more specific case type.

---

## CLI behavior for foundation

Implement:

```text
intent init
intent validate
intent ingest
intent sync
intent drift
intent status
intent explain <id-or-path>
intent context --task "<task>"
intent reconcile list
intent reconcile show <case-id>
intent render
```

For MVP 0, commands may have limited functionality, but their contracts should be stable enough to extend.

---

## `.intent/` project workspace

`intent init` should create something like:

```text
.intent/
├── config.yaml
├── graph.yaml
├── evidence/
├── reconciliation/
├── history/
└── cache/
```

Never put secrets in this directory.

Allow teams later to choose which parts are committed to Git.

---

## Context pack contract

`intent context --task` should output JSON and optionally Markdown containing:

```yaml
task:
relevant_intent:
relevant_requirements:
decisions:
constraints:
acceptance_criteria:
code_refs:
test_refs:
open_reconciliation_cases:
evidence_refs:
warnings:
```

Each item should preserve IDs and confidence where relevant.

Keep the context concise; this is an agent context layer, not a dump of the whole graph.

---

## Connector interfaces

Create the base connector protocol before concrete connectors.

MVP 0:
- Markdown;
- Git.

MVP 1:
- GitHub.

A connector should expose:
- discovery;
- fetch;
- normalization;
- checkpoint;
- stable external IDs;
- version or timestamp;
- content hash.

Connector failures should be isolated and reported without corrupting canonical state.

---

## LLM abstraction

Create an interface such as:

```python
class SemanticReasoner(Protocol):
    def extract_assertions(self, evidence_delta): ...
    def map_to_graph(self, assertions, graph): ...
    def propose_reconciliation(self, case): ...
```

Provide a deterministic/mock implementation for tests.

Do not couple core logic to OpenAI or any one model provider.

---

## Reconciliation workflow

Implement a case lifecycle:

```text
open
  -> proposed
  -> needs_human
  -> resolved
```

Also allow:
- deferred;
- false_positive.

A case must preserve:
- original evidence;
- later evidence;
- classification history;
- resolution;
- resolving actor;
- timestamp.

Resolution should be a ChangeSet, not an untracked direct edit.

---

## Framework dogfooding

Load `graph/framework-intent-graph.yaml` as a test fixture and validate it using the same graph loader and validation engine.

Add a CI test that fails when:
- the graph is invalid;
- its edges point to missing nodes;
- required provenance is missing;
- confidence is out of range.

Later, material changes to the specification should be expected to update this graph.

---

## Test fixtures to build

Create at least these fixture projects:

1. `aligned_project`
   - intent, requirement, code, and tests aligned.

2. `code_lag`
   - newer approved requirement, old code.

3. `requirement_lag`
   - newer decision evidence + code/tests agree, old formal requirement.

4. `test_lag`
   - code newer than test evidence.

5. `undocumented_code`
   - new module with no design trace.

6. `cross_author_conflict`
   - incompatible explicit contributor positions.

7. `idempotent_sync`
   - second run produces zero mutations.

---

## Logging

Use structured logging.

Every sync run should emit:
- run ID;
- project;
- source checkpoints;
- evidence added;
- graph proposals;
- changes applied;
- reconciliation cases created;
- validation failures;
- duration.

Do not log secret tokens or full sensitive source content by default.

---

## Documentation requirements

README should explain:
- the problem;
- the Capture → Manage → Sync → Reconcile loop;
- local quick start;
- a small example;
- how this helps coding agents;
- why the engine does not assume requirements are always correct;
- open-source versus future cloud direction.

Create a short architecture document only if needed; avoid duplicating the foundational spec.

---

## Commit discipline for Codex

Use small, coherent commits.

Recommended sequence:

1. repo skeleton + packaging;
2. domain models + validation tests;
3. YAML graph store;
4. evidence store + immutability;
5. ChangeSet applier;
6. reconciliation model;
7. Markdown connector;
8. Git connector;
9. sync orchestrator;
10. drift detectors + fixtures;
11. CLI;
12. framework graph dogfood validation;
13. context pack;
14. docs + GitHub Action skeleton.

Do not combine the entire implementation into one opaque change.

---

## Definition of done for the first Codex pass

Do not claim the foundation is complete unless:

- tests run and pass;
- framework graph validates;
- YAML can round-trip without semantic loss;
- evidence versioning is demonstrated;
- identical sync is idempotent;
- `CODE_LAG` fixture is detected;
- `REQUIREMENT_LAG` fixture is detected;
- those two classifications are produced from different evidence patterns;
- reconciliation cases include evidence refs;
- no LLM provider is required to run tests;
- CLI help works;
- README quick-start commands are executable.

If any item is incomplete, report it explicitly in the handoff.

---

## What not to build yet

Do not build in the first pass:
- cloud SaaS;
- organization billing;
- production Slack connector;
- production Confluence connector;
- production Notion connector;
- Jira connector;
- Neo4j dependency;
- full IDE extension;
- autonomous code rewriting;
- autonomous requirement rewriting;
- complex ML drift scoring;
- universal ontology.

The initial objective is to prove the semantic foundation and reconciliation loop cleanly.
