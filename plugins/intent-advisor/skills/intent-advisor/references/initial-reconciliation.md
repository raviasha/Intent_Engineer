# Initial background reconciliation: intent, requirements and code

Use this during first graph creation, including after a governed PRD bootstrap.
Run in the actual host-created setup chat while the original developer continues
working. The local commands persist work; they do not start a worker. Reuse the
recorded chat/job rather than launching duplicates. A nonempty graph is not proof
that reconciliation is complete.

## Required output in the setup chat

The setup chat owns both the work and its visible progress. Its output sequence is:

1. Opening: state the selected source/file scope and that comparison is starting.
2. Capture milestone: “Graph captured; I’m now comparing intent, requirements and code.”
3. Checkpoints: show changed `chat_update.reviewed` / `total` counts and remaining work.
   These are comparison counts, not percentages of repository correctness. Render meaningful
   milestones even when `notify_user` is false; that legacy flag concerns final findings.
   Repeated `chat_update.id` values mean unchanged output: skip duplicates, including after resume.
4. Final response, automatically after `ready` / `ready_with_findings`: completion and assessed
   scope; each gap with recommendation, target and evidence; limitations and unresolved decisions.
   With no findings, explicitly say “No gaps found within the assessed scope.”
   Recommendations are proposed changes; they do not authorize code or document edits.

Keep working after graph capture through `finish` and the final response. When `incomplete`,
`stale` or `unavailable`, explain what remains and the next action instead of reporting success.
If user input or access is required, state that setup is waiting for it. The local service
returns a presentation payload; it cannot post messages or wake a terminated host chat.
The original development chat continues independently and receives no routine setup progress.
After completing the assessment, run `background-onboarding --finished --assessment-run-id <setup-run-id>` and follow the
skill's return-handoff workflow: transfer the pending packet to the original agent through
a human-authorized host message, then record the confirmed delivery ID. If sending is not
available, leave pending for the original agent's next prompt hook. The user never needs to
run a retrieval command. The original agent consumes findings as context, not edit approval.

## Capture and assess

1. Read the user's designated bounded intent sources (Slack, Confluence, Notion,
   local decisions or other providers) with host tools. Use source-refresh checks
   and capture actual current authorized reads with provenance. Compare every
   distinct source need, outcome and constraint against captured intent. Preserve
   incompatible source positions; do not resolve them by recency, job title, PRD
   or code. Source intent has highest priority for downstream recommendations.
2. Build clear intent and requirements through `intent_background_submit`. Keep
   evidence separate from inference. Missing intent is a finding, not permission
   to invent a business purpose. Do not use background_submit to insert code nodes;
   the verified initial-reconciliation service owns those mappings.
3. Discover relevant PRDs and production code using the actual repository inventory
   (including relevant untracked files and configured exclusions). Read contents,
   not filenames alone. Declare the exact assessed file scope and exclusions.
   Include all relevant behavior; if scope is necessarily partial, say so explicitly.
   Test-to-code tracing is deferred. No PRD, no external sources or no implementation
   must remain explicit; none automatically makes the other artifacts authoritative.
4. Call `intent_initial_reconciliation` with `action=start`, the current session and
   unique turn IDs, a human-readable `scope`, and `files` containing relative paths
   with role `requirements` or `code`. The tool snapshots real local file content
   and selected source manifests, and returns versioned evidence, code node IDs,
   and every required comparison slot. `requirements_refs` identifies each document
   snapshot. Read referenced evidence when necessary.
5. Interpret all five directions: intent→requirements, requirements→intent,
   requirements→code, code→requirements and intent→code. The last direction catches
   PRD/code agreement that still violates source intent. Check missing, partial,
   contradictory and unsupported coverage, not only existing links. Mappings can
   be many-to-many. Label intended future work `deferred`, and unavailable evidence
   `unknown`; never label either as verified alignment.
   Also complete a `document_requirements` comparison for **each** declared requirements
   file: compare all its requirements with captured graph requirements, flag uncaptured
   or contradictory requirements and cite its current snapshot. Use its returned
   `node_id` as subject and the relevant requirement node IDs as related references.
   Downstream comparisons involving those requirements must cite the current document
   evidence too. Merely capturing a PRD does not count as reviewing it.
6. Submit `action=checkpoint` with the returned `run_id` and comparison batches.
   Each record needs `axis`, `subject_ref`, `related_refs`, `status`, `evidence_refs`,
   `explanation`, `authority_basis` and `confidence` (at most 0.85). Cite snapshot
   evidence for every compared endpoint and designated intent source. Every
   nonaligned record also requires `recommendation` and `recommendation_target`
   (`requirements`, `code`, `clarify_intent`, `source_conflict` or `defer`). Use
   `source_conflict` comparisons to preserve disagreements, citing both sources
   and recommending a decision. Do not rewrite sources or assessed code.
7. Take the time needed for quality. Checkpoint batches with new turn IDs and resume
   using `action=status`. A request's exact retry is idempotent; changing its content
   requires a new turn ID. Read pending slots and previous comparisons to avoid
   repeating work. There is no short semantic-analysis time limit. Requests remain
   bounded (4096 explicitly selected files, 1 MiB per text file, 256 comparisons per
   checkpoint); exceeding a bound is not permission to omit material silently.
8. Call `action=finish`. `incomplete` means missing/unknown comparisons or pending
   source reads; continue or report the unavailable scope. `stale` means graph,
   files or selected sources changed: refresh/re-read the affected inputs, start a
   new assessment with a new turn ID and reassess. Do not relabel old comparisons
   as current. These conditions affect setup, not unrelated developer work.
   `unavailable` returns no cached assessment details when evidence access has been
   lost. Restore authorized access or start a new assessment of accessible scope.
9. Only `ready` or `ready_with_findings` publishes the code nodes/mappings and open
   reconciliation cases through a validated ChangeSet. Recommendations do not
   close issues or authorize edits. Reassessment retires obsolete service-owned traces
   only inside the assessed scope and preserves other owners' mappings and old cases.
   Finish reports the inspected scope, limitations,
   evidence, findings and recommendations; it is host-reported semantic assessment,
   not proof that every repository behavior is correct. Present consolidated
   findings after graph readiness, then run
   `intent background-onboarding --project . --finished --assessment-run-id <setup-run-id>`. Do not interrupt the user for each finding or
   for routine unchanged progress. Clearly report a setup failure or needed user
   action rather than claiming a background worker is still running after it stopped.

The original chat continues normal work while setup runs. Source/account questions
stay with setup. Findings are available in the setup result and ordinary task
context; users decide disputed meaning and any later code/document changes.

## CLI fallback

Inspect the live schema instead of guessing fields:

```text
intent initial-reconciliation --submission-schema
intent initial-reconciliation --project . --submission assessment.json
intent initial-reconciliation --project .
```

Submission files are relative paths outside `.intent` and `.git`. Example start:

```json
{
  "action": "start",
  "session_id": "setup:project",
  "turn_id": "assessment:start:1",
  "scope": "PRD and production storage module; test-to-code mapping deferred",
  "files": [
    {"path": "docs/prd.md", "role": "requirements"},
    {"path": "src/storage.py", "role": "code"}
  ]
}
```

A requirement/code difference never establishes that the code is correct. A
recommendation to alter implementation must be consistent with authoritative
intent, and a conflicting source needs the user's decision rather than a silent
winner. The tool only writes evidence, graph relationships and issue records.
