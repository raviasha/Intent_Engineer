# Intent-Aware Coding-Agent Workflow Design

Status: Approved design

Date: 2026-08-26

Scope: Existing repositories, PRD bootstrap, mandatory agent preflight, clarification, and scheduled assurance

## 1. Purpose

Intent Engineering must participate in the work conversation before code changes, not only report
drift afterward. When enabled for a repository, an agent must interpret each user request through
the current intent graph:

- Is this repository work semantically meaningful or mechanical?
- Which active intent, requirements, constraints, decisions, and acceptance criteria govern it?
- Does the request align, conflict, or introduce something new or ambiguous?
- What must the human clarify or approve before work may continue?
- Which evidence, author, conversation, code, and tests prove what happened?

The design extends the existing graph, evidence, connector, context, reconciliation, approval, MCP,
CLI, sync, and drift foundations. It does not replace them.

## 2. Product contract

When the agent plugin is enabled:

1. Every human message receives an intent classification.
2. Repository-changing work requires a successful preflight.
3. Mechanical work uses a lightweight semantic-impact classification.
4. Aligned work receives bounded intent context and may proceed.
5. New or ambiguous work pauses for clarification and an exact graph proposal.
6. Conflicting, weakening, superseding, deleting, or destructive semantic work pauses for human
   reconciliation.
7. Conversation messages and answers become immutable evidence with their original authorship.
8. Model output is never canonical state. It is a typed proposal validated by deterministic code.
9. Scheduled capture and assurance remain separate from the interactive task gate.

Disabling the plugin is the explicit opt-out. There is no silent per-task bypass while it is
enabled.

## 3. Goals

- Bootstrap a useful intent and requirements provenance graph from an ordinary existing PRD.
- Accept supporting Markdown, Git, GitHub, Jira, Slack, Notion, and Confluence-compatible sources.
- Preserve exact source role, provenance, author, timestamp, version, locator, ACL, and content
  hash.
- Keep inferred detail visibly provisional and lower-confidence until confirmed.
- Make task preflight mandatory for semantic repository work.
- Ask focused questions when intent is absent or ambiguous.
- Present an exact graph diff before activating conversationally introduced intent.
- Let authorized contributors confirm non-conflicting additions.
- Require an independent authorized reviewer for conflicts and destructive semantic changes.
- Associate completed implementation and test evidence with governing intent and requirements.
- Detect code/requirement/intent/test drift on a separate schedule.
- Remain local-first, provider-neutral, and agent-host-neutral at the core interfaces.

## 4. Non-goals

- Treating a PRD, ticket system, conversation, requirement, or codebase as universal truth.
- Allowing an LLM to edit canonical graph storage directly.
- Making confidence mean approval, priority, implementation progress, or business value.
- Automatically resolving cross-author disagreement.
- Requiring a second model provider for interactive use.
- Performing unattended external writes or manufacturing human approval.
- Building a hosted webhook service, collaboration UI, or organization-wide graph in this slice.
- Claiming mandatory enforcement on an agent host that cannot provide a pre-mutation hook.

## 5. Reused foundation

The following existing capabilities remain authoritative:

- strict graph, node, edge, evidence, ChangeSet, case, approval, and receipt models;
- immutable/version-addressed evidence and ingestion associations;
- YAML graph and checkpoint stores plus JSONL evidence/history/case/approval/receipt stores;
- atomic shared transaction and crash recovery;
- Markdown, Git, GitHub, and profile-driven MCP capture;
- author, source locator, content hash, predecessor, ACL, and confidence preservation;
- deterministic sync orchestration, reasoner boundary, drift detectors, and validation;
- context, explain, impact, drift, status, and reconciliation queries;
- official MCP v2 server and guarded mutation tools;
- interactive human approval and exact provider-write execution;
- manual and scheduled CLI/GitHub Action operation.

New work is an orchestration and interpretation layer over these capabilities.

## 6. Source roles

Every configured document, page, channel, project, or other scope has one explicit role:

| Role | Meaning |
| --- | --- |
| `DECLARED_INTENT` | Approved PRD, charter, specification, or equivalent declaration |
| `DECISION` | Approved ADR, decision record, accepted ticket decision, or confirmed outcome |
| `PROPOSED_INTENT` | Draft, exploratory prompt, discussion, or unapproved proposal |
| `IMPLEMENTATION_EVIDENCE` | Code, tests, commits, pull requests, builds, or runtime evidence |
| `OPERATING_CONTEXT` | Supporting documentation and contextual conversation |

Roles are configured per document or channel, with inherited defaults from the parent source.
Connector type may suggest a default but never determines authority. Authority weights review and
confidence; it never silently selects truth or overwrites incompatible evidence.

## 7. Confidence and authority

Extraction confidence answers how likely a candidate assertion represents the source's intended
meaning. It is independent of activation and authority.

- Explicit, recently human-confirmed graph assertions may receive high confidence.
- PRD-derived assertions begin with evidence-based confidence appropriate to their clarity.
- Detailed inferred assertions remain provisional even if model confidence is high.
- Sparse or ambiguous conversation produces low-confidence proposals and questions.
- A human confirmation changes activation/approval state and may produce an audited confidence
  change; it does not erase the original inferred provenance.

An active node therefore always distinguishes source mode, confidence, and approval history.

## 8. Onboarding and bootstrap

### 8.1 Entry condition

On first use in an existing repository, the plugin checks for a valid active intent baseline. If no
baseline exists, semantic repository work pauses and the user enters bootstrap. Mechanical
inspection needed to locate sources remains permitted.

### 8.2 Source selection

The user selects:

1. a primary PRD, initially a local Markdown document;
2. optional supporting Markdown documents;
3. optional Git/GitHub evidence;
4. optional Jira, Slack, Notion, or Confluence-compatible connector scopes;
5. an explicit role for each document/channel, accepting or overriding suggested defaults.

### 8.3 Evidence capture

Bootstrap first captures immutable evidence. Extraction never operates on an unrecorded source.
Each candidate retains evidence references resolving to source object/version and, when available,
the exact span or structured field that supports it.

### 8.4 Draft extraction

The active coding agent submits typed candidate assertions for:

- context and product purpose;
- needs and desired outcomes;
- users, actors, and stakeholders;
- product intent and principles;
- capabilities and requirements;
- constraints, invariants, policies, and non-goals;
- decisions and assumptions;
- acceptance criteria;
- dependencies and graph relationships;
- ambiguities and unanswered questions.

The deterministic bootstrap service validates type, stable identity, evidence scope, authorship,
source role, confidence basis, and graph invariants. Candidates are stored outside canonical graph
state.

### 8.5 Hybrid review

The onboarding review presents only the proposed core foundation:

- purpose and desired outcomes;
- primary actors;
- major capabilities and requirements;
- hard constraints, invariants, and non-goals;
- material conflicts and unanswered questions.

After human confirmation, a validated ChangeSet activates baseline version 1. More detailed inferred
nodes remain provisional. They are surfaced for confirmation only when a task makes them relevant.
This avoids both an exhausting initial review and unreviewed AI inference becoming mandatory policy.

### 8.6 Bootstrap result

The user receives a stable report containing:

- active baseline nodes and relationships;
- provisional candidates and confidence;
- source-role assignments;
- conflicts and reconciliation cases;
- unanswered questions;
- excluded or inaccessible sources;
- the exact baseline graph version and ChangeSet.

## 9. Per-message preflight

### 9.1 Task envelope

For every message, the plugin constructs a detached, bounded task envelope:

- repository identity;
- authenticated local actor and known aliases;
- conversation/session reference;
- normalized user request;
- current active graph version;
- timestamp;
- requested or inferred repository scope.

The raw conversation remains evidence at the agent boundary; the core receives only bounded typed
input needed for classification and provenance.

### 9.2 Classifications

`NO_SEMANTIC_IMPACT`

: Formatting, typo correction, mechanical movement, or demonstrably behavior-preserving work. The
  lightweight classifier records its basis and permits only the corresponding scope. Uncertainty
  escalates to full preflight.

`ALIGNED`

: The task matches active intent and requirements without unresolved blocking conflict. The result
  includes a bounded context packet and authorization.

`NEW_OR_AMBIGUOUS`

: Relevant intent is missing, provisional, underspecified, or admits materially different
  interpretations. Repository mutation pauses and clarification begins.

`CONFLICTING`

: The request contradicts, weakens, supersedes, deletes, or destructively changes active intent,
  requirements, constraints, decisions, or cross-author positions. Work blocks and a reconciliation
  case is created or reused.

### 9.3 Aligned context

An aligned result contains only relevant:

- intent, requirement, constraint, decision, and acceptance-criterion IDs;
- source mode, confidence, provenance, and warnings;
- mapped code and tests;
- open reconciliation cases;
- implementation expectations and permitted scope.

The agent must associate the resulting code, tests, and commits with these stable IDs.

### 9.4 Authorization token

A successful mechanical or aligned preflight produces a short-lived opaque authorization token
bound to:

- repository and actor;
- normalized task digest;
- active graph version;
- classification;
- relevant graph IDs;
- permitted file/tool scope where the host can enforce it;
- issue and expiry time.

The token is an opaque random capability issued and retained only by the long-lived local Intent MCP
process. Its server-side record contains the bindings above. It is never written to graph, evidence,
history, logs, or project configuration; restarting the service invalidates every token. Material
task changes, graph-version changes, expiry, actor changes, repository changes, or scope expansion
also invalidate it and require a fresh preflight. `intent preflight` reports the same classification
for diagnostics but does not issue a mutation-authorizing token.

### 9.5 Mandatory host behavior

An enabled plugin must intercept repository-mutating tool calls before execution and require a valid
token. Read-only exploration needed to classify a task may proceed. A host without a reliable
pre-mutation hook cannot advertise mandatory mode; it may use the same core only in explicitly
labelled advisory mode.

If the plugin is disabled, ordinary agent behavior is unchanged.

## 10. Clarification and requirement proposals

For `NEW_OR_AMBIGUOUS`, the clarification coordinator asks the smallest useful sequence of focused
questions covering applicable dimensions:

- intended outcome and rationale;
- affected users or actors;
- desired behavior;
- boundaries and non-goals;
- constraints and invariants;
- acceptance criteria;
- relationship to existing intent;
- ownership and expected review policy.

Every answer retains its human actor and conversation evidence reference. The agent then submits a
typed proposal containing an exact graph diff, confidence basis, assumptions, evidence refs,
affected active nodes, and predicted code/test impact.

An authorized contributor may confirm a non-conflicting addition. Their confirmation activates the
proposal through a validated ChangeSet. If the proposal conflicts with, weakens, supersedes,
deletes, or destructively changes active semantics, its author cannot approve it alone. It becomes a
reconciliation case requiring an independent authorized reviewer.

Unconfirmed conversation remains evidence or proposed intent. It never silently becomes an active
requirement.

## 11. Post-task evidence

After repository work, the plugin compares the resulting diff with the authorized task and graph
version. It records or proposes:

- changed files and symbols;
- linked intent and requirement IDs;
- implementation claims;
- added or changed tests;
- acceptance criteria addressed;
- commit or worktree evidence;
- deviations, scope expansion, and unresolved warnings.

If the result materially exceeds the authorized scope, completion is withheld and preflight or
reconciliation runs again. The agent may not relabel an unauthorized semantic change as mechanical
after the fact.

## 12. Scheduled capture and assurance

### 12.1 Frequent capture

The capture job:

1. fetches configured sources incrementally;
2. appends immutable evidence versions;
3. records source authorship, ACL, locator, version, and predecessor;
4. invokes an optional configured semantic extractor;
5. applies only deterministic and already-authorized low-risk additions;
6. stores uncertain additions as provisional;
7. creates cases for material contradictions;
8. advances only successful source checkpoints.

Without a background model adapter, capture and deterministic detection still run. New
conversations remain unclassified evidence until an interactive agent session processes them.

### 12.2 Scheduled assurance

The assurance job reads one consistent graph/evidence/code/test snapshot and detects:

- intent without requirements;
- requirements unsupported by intent;
- implementation lagging active requirements;
- material code without declared intent or decision trace;
- tests lagging implementation or acceptance criteria;
- conflicting requirements or decisions;
- provisional intent that has become implementation-relevant;
- stale evidence and source/configuration failures.

It publishes an evidence-backed report. Background jobs cannot approve conflicts or perform
external writes. CI fails only for project-configured blocking invariants; ordinary review cases may
remain report-only.

## 13. Reasoning and authority boundary

Interactive bootstrap, classification, clarification, and proposal formulation reuse the active
coding agent. This avoids requiring a second model account and preserves agent neutrality.

The active agent may:

- interpret evidence;
- identify candidate graph identities;
- classify tasks;
- formulate questions;
- submit typed candidate assertions and ChangeSets;
- propose reconciliation.

The active agent may not:

- directly edit canonical graph, evidence, approval, receipt, or history storage;
- issue its own authorization token;
- activate an invalid or unauthorized proposal;
- approve a conflict it authored;
- hide contradictory evidence;
- claim that confidence constitutes authority.

A provider-neutral optional `SemanticReasoner` adapter supports scheduled extraction later. The core
contract never depends on OpenAI or another model vendor.

## 14. Components and boundaries

### 14.1 BootstrapService

Consumes captured evidence and typed candidate assertions. Produces a detached draft, validation
diagnostics, a core-review summary, and an exact candidate ChangeSet. It never writes canonical graph
state directly.

### 14.2 IntentProposalStore

Stores canonical proposal records outside the graph. Records are immutable/content-addressed,
version-bound, author-bound, evidence-bound, and append-only. Confirmation references the exact
proposal digest.

### 14.3 PreflightService

Consumes a task envelope plus a consistent authorized project snapshot. Produces exactly one fixed
classification result. It delegates semantic interpretation through a typed reasoner port and
deterministically validates all cited IDs, evidence, policy, graph version, and authorization.

### 14.4 ClarificationCoordinator

Tracks bounded clarification sessions and their attributed answers. It produces no canonical change;
it only assembles a proposal for deterministic validation and human confirmation.

### 14.5 AuthorizationIssuer

Issues and verifies short-lived locally authenticated capabilities after successful preflight. It
owns token canonicalization, expiry, graph/task binding, replay rules, and scope checks. Tokens and
signing material are never graph evidence.

### 14.6 Agent host adapter

Translates host lifecycle events into bootstrap, preflight, clarification, context, post-task, and
pre-tool calls. It contains no graph semantics. The first adapter targets Codex. Mandatory mode must
use a real Codex pre-mutation tool hook; if the installed Codex plugin contract does not expose one,
the adapter must refuse mandatory-mode activation rather than relabel instructions or MCP prompts as
enforcement. Later compatible hosts use the same core interfaces.

### 14.7 Existing sync and reconciliation services

Remain the only path for durable evidence ingestion, graph mutation, cases, validation, and scheduled
drift. New services compose these existing ports rather than duplicate storage or policy.

## 15. Interfaces

Initial CLI additions:

```text
intent bootstrap --prd <path>
intent sources add <type> <locator> --role <role>
intent preflight --task <text>
intent proposals list
intent proposals show <proposal-id>
intent proposals confirm <proposal-id>
```

Existing `intent sync`, `intent drift`, `intent reconcile`, `intent context`, `intent validate`, and
`intent mcp` commands remain.

The MCP server adds bounded typed tools for:

- bootstrap draft submission and inspection;
- task preflight;
- clarification session submission;
- intent/requirement proposal submission and inspection;
- post-task evidence submission.

No MCP tool creates independent human approval. Confirmation and conflict review reuse the existing
interactive/local authorization boundary.

## 16. Persistence and transaction rules

- Drafts, proposals, clarification sessions, and token metadata are separate from canonical graph
  state.
- All stored records are strict, bounded, canonical, immutable or version-addressed, and
  provenance-backed.
- Proposal confirmation, graph mutation, history, cases, and evidence associations use the existing
  shared transaction domain.
- A graph version advances only after durable validation and commit.
- Identical proposal or sync replay is a semantic no-op.
- Partial source or reasoner failure cannot advance the affected checkpoint or mutate the graph.
- Generated onboarding and assurance reports are views, never canonical state.

## 17. Failure behavior

- Missing active baseline: block semantic mutation and start bootstrap.
- Invalid graph: block semantic mutation and return fixed recovery diagnostics.
- Intent service unavailable: fail closed for repository mutation while the plugin is enabled.
- Unsupported mandatory host hook: refuse mandatory-mode activation; do not imply enforcement.
- Source unavailable: report partial capture and preserve its prior checkpoint.
- Invalid or ungrounded extraction: store no proposal or graph mutation.
- Missing/unauthorized evidence: omit it and fail closed when required for a decision.
- Material task or graph change: invalidate authorization and re-run preflight.
- Conflict: create/reuse a case without choosing truth.
- Model unavailable: deterministic capture/context/drift continue; semantic extraction waits.
- Plugin disabled: do not intercept the agent; scheduled CLI/CI operation remains available.

Errors exposed to agents and users are typed, bounded, and free of raw source bodies or credentials.
Cancellation preserves the original control-flow signal while owned resources close.

## 18. User experience

The default interaction is quiet when intent is clear:

```text
User: Add CSV export to the report page.
Plugin: Aligned with REQ-42 and CONSTRAINT-LOCAL-FIRST. Supplying context.
Agent: [implements within authorized scope]
```

Ambiguity becomes a focused conversation:

```text
User: Add team sharing.
Plugin: This is not present in active intent. Who may share, what leaves the local device, and what
        outcome should sharing achieve?
User: Workspace admins may share read-only reports; raw source content must stay local.
Plugin: Proposed one capability, two requirements, one constraint, and acceptance criteria. Confirm?
```

A conflict is explicit:

```text
User: Upload all raw conversations to the cloud.
Plugin: Blocked by CONSTRAINT-LOCAL-DATA and conflicting author evidence. Review case CASE-17 created.
```

Mechanical work remains lightweight:

```text
User: Format the touched Python files.
Plugin: No semantic impact; authorized for the selected files.
```

## 19. Verification strategy

### 19.1 Unit contracts

- strict/frozen task, source-role, draft, proposal, clarification, classification, and token models;
- stable IDs and canonical hashes;
- confidence/activation separation;
- task and graph version binding;
- token expiry, replay, actor, repository, and scope rejection;
- source-role inheritance and per-document/channel override;
- clarification completeness and bounded inputs;
- proposal evidence/authorship/identity validation;
- fixed failure and cancellation boundaries.

### 19.2 Integration contracts

- ordinary Markdown PRD to attributed low-confidence draft;
- hybrid core review to active version-1 baseline;
- provisional detail remains non-authoritative until relevant;
- mechanical, aligned, new/ambiguous, and conflicting preflight;
- context pack contains only relevant authorized graph/evidence;
- clarification answers preserve each human author;
- contributor confirms non-conflicting addition;
- author cannot independently approve conflict/supersession;
- token invalidates after graph or task changes;
- post-task implementation/test evidence links to requirements;
- repeated identical bootstrap/preflight/sync is a semantic no-op;
- partial connector/reasoner failure preserves exact prior state.

### 19.3 Agent adapter contracts

- enabled plugin calls preflight before every repository-mutating tool;
- read-only exploration is allowed before classification;
- missing, expired, mismatched, or revoked token blocks the tool;
- a material mid-task request change triggers fresh preflight;
- disabled plugin does not alter normal agent behavior;
- unsupported host cannot report mandatory enforcement.

### 19.4 End-to-end release proof

One temporary existing repository must prove:

1. install/init and ordinary PRD selection;
2. source-role configuration and immutable source capture;
3. agent-proposed draft and human-reviewed baseline activation;
4. aligned task authorization and implementation/test evidence;
5. new requirement clarification and exact proposal confirmation;
6. cross-author conflicting request blocked for independent review;
7. repeated run no-op;
8. scheduled source update and code-versus-intent drift report;
9. validation, context, and drift all read the same project state;
10. no credential or excluded source content persists in outputs or logs.

## 20. Implementation phases

### Phase 1: PRD bootstrap

Add source roles, draft/proposal models and store, bootstrap service, CLI/MCP draft flow, and one
ordinary-Markdown PRD onboarding proof. Reuse existing evidence capture and ChangeSet application.

### Phase 2: Preflight and clarification

Add task classifications, typed reasoner protocol, clarification sessions, exact proposal
confirmation, authorization issuer, CLI/MCP contracts, and governance tests.

### Phase 3: Mandatory agent adapter

Verify the current Codex plugin lifecycle contract, then implement the Codex adapter with
pre-mutation interception, token verification, context injection, material task-change detection,
post-task evidence, opt-out, and adapter E2E. If Codex lacks a pre-mutation hook, complete the core
adapter protocol and report Phase 3 as externally blocked; do not ship an advisory adapter under the
mandatory-mode name.

### Phase 4: Scheduled semantic assurance

Add optional provider-neutral background extraction, provisional relevance detection, expanded
intent/requirement/code/test cases, scheduling documentation, and the complete release proof.

Each phase is independently reviewable and must preserve all prior deterministic, offline, and
credential-free test gates.

## 21. Success criteria

The feature is complete when:

- a user can point an existing repository at an ordinary PRD and review a provenance-backed draft;
- the confirmed core becomes an active baseline while inferred details remain provisional;
- every enabled-plugin coding task receives a classification;
- repository mutation cannot proceed without valid preflight on a supported mandatory host;
- aligned work receives relevant context and produces linked implementation/test evidence;
- new intent produces focused questions and an exact human-confirmed proposal;
- conflicting or destructive intent requires an independent reviewer;
- scheduled capture and assurance detect source and code drift without silently changing truth;
- the same graph, evidence, cases, and history serve CLI, MCP, plugin, and scheduled operation;
- disabling the plugin restores ordinary agent operation; and
- all invariants, idempotency, provenance, authorization, and failure contracts pass offline tests.
