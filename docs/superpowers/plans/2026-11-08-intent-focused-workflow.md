# Intent-focused Workflow Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task by task.

**Goal:** Deliver focused assessment, contextual clarification, visible coverage and readable intent pages.

**Architecture:** Extend existing snapshot-bound services and local dashboard. Derived projections
remain read-only; the canonical graph and authenticated review remain authoritative.

**Tech Stack:** Python 3.12, Pydantic, pytest, existing vanilla JavaScript dashboard.

**Spec:** ../specs/2026-11-08-intent-focused-workflow.md

## Global Constraints

- Preserve local-first operation, stable IDs, provenance and explicit reconciliation.
- No new dependencies or code/test matching functionality.
- Reuse existing authenticated approval; conversational assent does not mutate the graph.
- Derived content must use permission-filtered snapshots and remain non-canonical.

## Review Focus

- Legacy full-lifecycle reports and historical sessions remain readable.
- Scope differences cannot be mistaken for score improvement.
- Hidden or unrelated graph nodes/evidence must not leak into reading pages.
- Cycles and inactive links must not manufacture outcome coverage.
- Recorded answers awaiting review must not be presented as approved meaning.

### Task 1: Scope-aware assessment

Files: assessment/{models,policy,rubric,service,snapshot,gate}.py; core/models/project.py;
cli/assessment.py; tests/unit/assessment; tests/e2e/test_cli_assessment.py.

Interfaces: ProjectConfig.assessment_scope and AssessmentSnapshot.assessment_scope carry
`intent_requirements` or `full_lifecycle`. AssessmentPolicy.for_scope(scope) constructs policy;
AssessmentReport.assessment_scope labels reports. Existing v1 policy preserves lifecycle semantics.

- [x] Write failing tests for scope exclusions, cache separation and incompatible comparisons.
- [x] Run targeted pytest and confirm missing behavior.
- [x] Implement scope selection, propagation, report identity and CLI override.
- [x] Run assessment unit/integration and CLI tests; commit.

### Task 2: Contextual clarification and playback

Files: intent_workflow/enrichment.py; new intent_workflow/reading.py; plugin skill;
control_plane/service.py and assets/app.js; enrichment tests.

Interfaces: reading.build_reading_page(snapshot, node_id) returns a bounded derived dictionary;
enrichment questions include snapshot-backed context, without changing durable session hashes.

- [x] Write failing tests for cited context, labeled assumptions and repeated known answers.
- [x] Confirm failures, implement context and question selection, update agent guidance.
- [x] Run enrichment integration tests; commit.

### Task 3: Visible coverage review

Files: new intent_workflow/coverage.py; control_plane/service.py and assets/app.js;
new tests/unit/intent_workflow/test_coverage.py; proposal integration tests.

Interfaces: build_coverage(graph, cases) returns deterministic outcome/constraint rows with
supporting requirement IDs, relationship IDs, status and reason. Used on authorized current
and proposed graphs at the existing preview boundary.

- [x] Write tests for linked, missing, deferred, disputed, inactive and cyclic paths.
- [x] Confirm failures; implement bounded traversal and review rendering.
- [x] Run unit and proposal review tests; commit.

### Task 4: Readable pages and end-to-end integration

Files: intent_workflow/reading.py; control_plane/service.py, http_models.py, assets/app.js;
docs/assessment.md; docs/quick-start.md; graph/framework-intent-graph.yaml; UI tests.

Interfaces: existing assessment-node response includes a read-only reading page for the same
authorized snapshot. Existing dashboard selection opens this page without another command.

- [x] Write tests for related topics, evidence attribution, permissions and graph immutability.
- [x] Confirm failures; complete reading view, scope labels and score explanation.
- [x] Update documentation and framework graph through existing models/ChangeSets.
- [x] Run full pytest, ruff and strict mypy; obtain one independent whole-branch review.
- [x] Fix material findings and commit verified work.
