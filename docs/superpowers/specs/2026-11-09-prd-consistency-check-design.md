# Background reconciliation of intent, requirements and code

Status: user authorized implementation and publication on November 9, 2026.
Date: November 9, 2026.

## Purpose and scope

Intent Engineering matches captured intent, requirements and code, and flags gaps
and mismatches across all three. Source intent has highest priority. Every issue
should have evidence and a recommended change. Pilot outcomes are fewer
requirement misunderstandings and less developer rework.

The user explicitly superseded the earlier requirements-to-code deferral.
Requirements-to-code traceability is now in product scope. Test-case-to-code
traceability remains deferred. This describes requested behavior, not the
capabilities of the currently installed background engine.

## Current failure

The project's inferred PRD was generated from revision `26e7430`, before the
background-maintenance workflow. Subsequent graph maintenance captured that
workflow without updating the PRD. PRD discovery identifies candidate paths;
bootstrap validates evidence and typed assertions. Background onboarding's
completion command checks only that the graph has nodes. Neither path requires
initial three-way reconciliation with evidence-backed findings.

## Chosen approach

The user specified comparison, issue flagging and recommendations across all
three layers. A PRD/code-only comparison misses unsupported requirements and
uncovered intent. Automatically changing code to match a PRD can compound an
outdated requirement. Neither alternative meets this request.

At first graph creation, read designated intent sources such as selected Slack,
Confluence or Notion material when available, PRD requirements and recorded
project decisions, and relevant current code. Preserve exact content, revisions,
authorship, access scope and file/symbol references. The host agent interprets
meaning; deterministic services validate provenance and stored findings.

## Authority and coverage

Source intent governs recommendations for requirements and code. Requirements
express intended behavior; code records implemented behavior. Code does not
establish intended truth. Never invent intent to justify an existing artifact.
If no external intent sources exist, label that limitation and any interpretation
from available documents or user statements. If authoritative sources conflict,
preserve both positions and recommend a resolution for the user's decision.

Check many-to-many coverage in both directions and end-to-end:

| Comparison | Issues to flag |
| --- | --- |
| Intent to requirements | Intent with no addressing requirements; partial outcomes or constraints; contradictions |
| Requirements to intent | Requirements without captured supporting intent; unsupported scope or conflicting purpose |
| Requirements to code | Missing or partial implementation; behavior diverging from requirements |
| Code to requirements | Behavior without supporting requirements; contradictory or extra behavior |
| Intent to implemented behavior | PRD and code agree with each other but fail to serve source intent |

A structural link is not proof of semantic coverage. Distinguish missing,
partial, contradictory, explicitly deferred and unassessed coverage. Missing
captured evidence does not prove intent does not exist. Inaccessible sources and
incomplete reads are unknown, not successful comparisons or proven absences.

Every finding includes affected references, versioned evidence, the gap or
mismatch, why it matters, a recommended change and target, confidence, and any
needed decision. Recommendations follow source intent: propose requirements for
uncovered intent, seek evidence for unsupported requirements, and recommend code
changes only against requirements that themselves agree with intent. Preserve
issues until a subsequent check or explicit decision establishes resolution.

## Nonblocking, quality-first execution

Initial graph creation and reconciliation run in the background while the user
continues working. Use the existing host-managed setup mechanism and reuse a
recorded running setup instead of creating duplicates. Provider authentication
and setup needs stay with setup and do not block the original work.

Allow the time needed for quality. Do not truncate the assessment to satisfy a
short response deadline. Save progress for resumable execution across host limits,
interruptions and provider failures. Incomplete work remains labeled incomplete.
Do not claim a worker is running unless the host actually started it.

Record immutable source and code snapshots. Revalidate affected input revisions
before calling findings current; reassess changed inputs or explicitly label the
assessed revision. Never mix revisions silently or overwrite concurrent user work.

Once the graph is ready, present consolidated reconciliation issues and recommended
changes. Do not interrupt normal work for each finding or send routine unchanged
progress notifications. Clear intent can be captured while disputed positions and
unknown coverage remain visible. A nonempty graph alone proves neither readiness
nor synchronization.

## Mutation boundary

The assessment updates graph, evidence and issue records. It does not modify the
assessed project's code. It proposes PRD and code changes and keeps issues open;
any later artifact edit is a separate authorized action. Remote source intent is
never rewritten to fit downstream artifacts. Synchronizing all three means
capturing actual relationships and reconciliation needs, not hiding differences
through automatic edits.

## Verification

Use fixed offline fixtures for full alignment; obsolete PRD; unsupported
requirements; uncovered intent; partial coverage; code divergence; undocumented
code; PRD/code agreement that violates intent; conflicting sources; absent code
or sources; inaccessible or partial reads; deliberate deferrals; and concurrent
input changes. Exercise both onboarding paths, resume and duplicate-start behavior.
Verify uninterrupted user work, findings after graph readiness, evidence and
recommendations for all issue classes, no false completion, and unchanged assessed
code. Test-to-code traceability stays deferred; regression tests for this feature
remain required.

## Recorded decisions

The user's refinements are preserved in active intent graph version 11 through
validated ChangeSets and immutable evidence. This document specifies requested
behavior; no production rollout or running background assessment is claimed.
