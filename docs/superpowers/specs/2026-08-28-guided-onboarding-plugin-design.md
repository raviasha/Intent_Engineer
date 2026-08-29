# Guided Onboarding and Prompt-Time Intent UX

Status: Proposed for user review

Date: 2026-08-28

Scope: Guided CLI onboarding, advisory plugin orchestration, prompt-time classification, clarification, and scheduled assurance

## 1. Purpose

Intent Engineering already provides the durable graph, evidence, bootstrap, preflight,
clarification, reconciliation, MCP, and assurance services. This design makes those services easy
for developers to use without requiring them to remember a sequence of commands.

The developer should be able to open an existing repository and interact normally with a coding
agent. The integration detects whether onboarding is complete, offers guided onboarding when it is
not, and classifies later prompts against the approved intent baseline.

## 2. Product contract

The user experience has three phases:

1. **Onboarding:** the CLI creates and human-confirms the first intent baseline from a PRD and
   optional supporting sources.
2. **Interactive development:** a thin plugin classifies each new human prompt and directs the
   existing MCP workflows for aligned, new, ambiguous, or conflicting work.
3. **Independent assurance:** CLI commands invoked locally or by CI periodically compare captured
   code and test evidence with the approved intent graph.

The plugin is an orchestration and presentation layer. It does not implement graph validation,
write canonical state directly, or expose authorization capabilities.

## 3. Repository lifecycle

### 3.1 Uninitialized repository

At the start of a task, the plugin performs a read-only repository-state check. If the project has
no valid approved baseline, it displays:

> This repository has not been onboarded into Intent Engineering. Start guided onboarding now?

The plugin must obtain explicit confirmation before capturing sources or creating a proposal. A
decline leaves the repository unchanged and disables intent-aware classification for that task.

### 3.2 Guided onboarding

The durable entry point is:

```bash
intent onboard --project . --prd docs/PRD.md
```

The command orchestrates existing operations rather than introducing a second bootstrap engine:

1. initialize the local Intent Engineering workspace if necessary;
2. descriptor-safely capture the PRD as immutable evidence;
3. assign or confirm its `DECLARED_INTENT` source role;
4. optionally collect supporting Markdown and configured connector sources;
5. submit a typed bootstrap proposal through the existing deterministic service;
6. display the proposed core graph, provenance, confidence, conflicts, and open questions;
7. collect clarification answers as attributed conversation evidence;
8. display the exact graph change for confirmation;
9. activate the confirmed baseline atomically; and
10. return a stable onboarding summary without exposing internal capabilities.

Non-interactive automation may use explicit flags and submitted typed proposals, but it may not
manufacture human confirmation.

### 3.3 Initialized repository

Once a valid baseline exists, the plugin classifies each new human prompt using the current graph
snapshot and the existing preflight service:

| Classification | Interaction |
| --- | --- |
| `no_semantic_impact` | Continue without graph ceremony for non-requirement or formatting-only work. |
| `aligned` | Provide relevant intent context and continue. |
| `new_or_ambiguous` | Pause implementation, ask focused questions, and propose an attributed graph update. |
| `conflicting` | Block implementation and create a human-review case. |

Answers to an active clarification session are routed to that session and are not recursively
classified as unrelated new requirements.

## 4. Plugin boundary

The plugin may:

- detect repository onboarding state;
- offer to launch guided onboarding;
- invoke public MCP tools;
- render context, questions, proposals, and review outcomes conversationally;
- remind the agent that clarification or review must finish before implementation; and
- report when mandatory host enforcement is unavailable.

The plugin must not:

- duplicate bootstrap, classification, validation, reconciliation, or authorization logic;
- write graph, evidence, case, approval, or history stores directly;
- display or persist authorization tokens;
- claim that prompt injection alone prevents every repository mutation; or
- silently onboard a repository or approve a graph proposal.

Current Codex integration is advisory because the audited host contract cannot prove interception
of every mutating path. `MandatoryHookUnavailable` remains the truthful result when mandatory host
enforcement is requested. A future host may enable mandatory mode only after demonstrating complete
synchronous mutation coverage against the provider-neutral host contract.

## 5. Prompt-time flow

For each new human prompt:

1. identify the repository and actor without mutating state;
2. check for a valid approved baseline;
3. if absent, offer onboarding and stop semantic mutation;
4. if present, capture the human prompt as attributed evidence;
5. request a typed classification from the agent;
6. validate that classification against the current graph and authorized evidence;
7. route the validated result according to the classification table;
8. invalidate and repeat preflight when the prompt, graph, actor, or requested scope materially
   changes; and
9. after completed work, capture implementation and test evidence and run post-task reconciliation.

Failures are fixed, bounded, and fail closed for graph-changing or conflicting work. Advisory-host
limitations are stated explicitly rather than represented as successful enforcement.

## 6. Scheduled assurance

Scheduled assurance remains CLI-driven and independent of the plugin. GitHub Actions, another CI
system, cron, or a developer may invoke capture, sync, validation, and drift commands. Assurance:

- captures fresh Markdown, Git, provider, implementation, and test evidence;
- compares it with the approved intent and requirements graph;
- creates evidence-backed review cases for divergence;
- never silently changes the graph; and
- is idempotent when evidence and intent are unchanged.

The plugin may display assurance results but is not required for assurance to run.

## 7. Compatibility and migration

Existing commands remain supported:

```bash
intent init --project .
intent bootstrap --prd docs/PRD.md --project . --format json
intent sources add markdown docs/PRD.md --role declared_intent --project .
intent preflight --task "Implement CSV export" --project . --format json
```

`intent onboard` is a guided composition over these established services. Existing repositories and
automation do not need to migrate immediately.

## 8. Testing and acceptance

The implementation is accepted when tests prove:

- an uninitialized repository produces a read-only onboarding offer;
- declining onboarding performs no durable writes;
- accepting onboarding uses the real capture and bootstrap services;
- no baseline is activated without exact human confirmation;
- repeated onboarding of the same approved state is a semantic no-op;
- a baseline-enabled prompt is classified exactly once;
- clarification answers are routed to the active session without recursive classification;
- aligned prompts continue while new, ambiguous, and conflicting prompts pause appropriately;
- graph proposals retain exact prompt, answer, author, source, and timestamp provenance;
- the plugin never receives or renders an authorization token;
- advisory mode clearly reports its enforcement limitation;
- scheduled CLI assurance operates with no plugin installed; and
- the existing end-to-end PRD-to-code-to-assurance workflow remains green.

## 9. Documentation

README and adoption documentation will lead with the developer journey:

```text
Install -> intent onboard -> approve baseline -> use the coding agent normally
        -> clarify/review new intent when prompted -> scheduled CLI assurance
```

Documentation will distinguish convenience, canonical authority, and enforcement so users know
which guarantees come from the plugin, the local services, and CI.
