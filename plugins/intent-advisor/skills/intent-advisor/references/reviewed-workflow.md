# Optional governed workflow

Use only when the user explicitly requests the legacy review workflow or team publication.
The default background workflow is defined in ../SKILL.md.

# Intent Advisor

Treat prompt-hook guidance as advisory workflow context, not complete mutation enforcement. Never
request, display, persist, or infer a capability token. Do not weaken or work around a
`MandatoryHookUnavailable` result from the host adapter.

## Create or defer a missing intent graph

The prompt hook checks repository readiness. A pending graph proposal goes to the existing
review, and an approved graph is reused. Never replace either by starting a second graph.
If the hook is unavailable, `intent onboard --discover --project . --format json` is a
read-only check; do not interpret a command error as proof that the graph is absent.

For `action=offer_onboarding`, ask one short question offering **Create now** or **Defer for
now**. If the user is answering an offer already made, act on that answer without repeating
the question. Keep the original development request so work can resume after setup.

- **Defer:** run `intent onboard --defer --project . --format json`, briefly acknowledge,
  and continue ordinary development. This saves a user-local preference, without creating
  `.intent`. The default reminder is after three completed top-level work turns; use
  `--after-tasks N` for a different user-requested interval. The completion hook counts these
  turns automatically. Do not count prompts yourself or remind while the hook says deferred.
- **Create:** acceptance of the offer, `action=start_onboarding`, or an explicit request such
  as **“create intent graph”** authorizes preparation now, including during deferral. Do not
  ask again whether to start or make the user run setup commands. Follow the steps below.
- **Existing graph:** show/open it instead of recreating it. **Pending proposal:** resume
  its review. **Readiness error:** explain it; deferral cannot bypass a pending review.

### Prepare the PRD and graph

1. Run `intent onboard --discover --project . --format json`. Read the likely PRDs returned.
   Use one clear, relevant PRD and state its path; a path supplied by the user is already
   selected. If several are plausible, ask which one to use in one question. If discovery
   returns no candidate, check relevant repository docs before deciding no PRD exists.
2. **No PRD:** inspect relevant implemented source, test definitions and existing docs,
   without modifying implementation or running the application/tests. Draft a new Markdown
   PRD, normally `docs/inferred-prd.md`; choose an unused name and never overwrite a file.
   Respect ignore rules, source exclusions and secret/dependency/generated directories.
   Include: observed behavior; proposed user need and intent; proposed requirements;
   constraints; assumptions/unknowns; and source file paths with commit/blob hashes or
   content hashes for uncommitted files. The first line must be exactly
   `<!-- intent-engineering: inferred-prd; author=agent:codex -->` so later capture/sync
   preserves attribution. Also label it **Inferred draft — pending human review**.
   Cite the basis for each material assertion. Observed behavior is not evidence of its
   business purpose: leave that purpose unknown when unsupported. For an empty repository,
   ask the single most useful purpose/outcome question instead of fabricating features.
3. Capture the selected PRD with `intent onboard --project . --prd <path> --yes --format json`.
   Add **`--inferred`** for a code-derived or previously generated draft. This records the
   marked draft as agent-authored proposed intent. The flag rejects unmarked drafts rather
   than silently altering an existing source. `--yes` authorizes preparation, not activation.
   Use the returned `evidence_refs`, `source_role` and `graph_version`; never invent these.
4. Prepare the bootstrap draft using the schema from
   `intent onboard --submission-schema --format json`. Use `actor: agent:codex`, a current
   UTC timestamp, stable descriptive node/edge IDs, the returned evidence and source roles.
   Candidate nodes have `status: proposed`, `source_mode: inferred`, matching author/time
   fields, confidence and its evidence basis. Include the clear intent and requirements in
   `core_node_ids`; uncertain details go in `provisional_node_ids`, `assumptions` and
   `unanswered_questions`. Keep questions to gaps that materially affect this first graph;
   optional detail can be enriched later. Ask one material question at a time and reuse
   answers already available. Do not add implementation/test coverage requirements.
5. Save the typed submission to an ordinary temporary JSON file inside the repository,
   outside `.intent` and `.git`, and run
   `intent onboard --project . --submission <relative-json-path> --format json`.
   This validates and persists only a pending proposal through the existing bootstrap
   service. Fix rejected draft fields using the schema; never edit canonical stores.
6. Open/reuse `intent dev --project . --offline` for a new local project, without `--prd`
   (the source is already captured). Present a short summary of proposed intent,
   requirements and material assumptions, then direct the user to the pending proposal in
   **Proposal review**. The existing authenticated local review activates the graph.
   Do not simulate approval or claim a pending graph is active. After activation, follow
   [initial reconciliation](initial-reconciliation.md) in the setup chat. The governed
   `ready` state means a baseline exists; check `initial_reconciliation_status` separately.
   The original task can continue while the setup chat compares intent, requirements and code.

These onboarding CLI operations work even if the plugin MCP server could not start before
`.intent` existed. Do not require MCP availability to prepare the PRD, submit the bootstrap
draft or open the review. Once created, reconnect the plugin/start a new chat if its MCP tools
have not become available; never pretend those tools were called.

## Follow the prompt route

Before drafting a classification, read the relevant approved intent, requirements, decisions,
constraints and any existing clarification answers through the public read-only context tools.
Use a short playback: **who needs what and why**, the supporting node/evidence references,
and any **proposed assumptions**. Omit a slot when the sources do not establish it; never invent
the beneficiary or outcome. Invite corrections through the existing clarification/review flow.

Draft a question only when an unanswered point could change an outcome, requirement or constraint.
Use known answers rather than asking for them again. A clear, aligned request can have no questions;
an ambiguous request starts with its most consequential question. Do not infer scheduling,
delivery channels or extra features from a narrow request. In intent-versus-requirements mode,
missing code or tests is not a clarification gap. Keep structural scores separate from reviewed
meaning: a high score or a graph link does not establish that a requirement satisfies intent.

After preflight returns a persisted question, follow that session's required question flow.
If an answer is already recorded there, reuse its evidence; do not manufacture a second answer
or bypass a pending authenticated human step. Label unapproved answers and alternatives as pending.

- For `action=offer_onboarding` or `action=start_onboarding`, follow **Create or defer a missing
  intent graph** above. A call without `--yes` is only an offer/no-op diagnostic.
- For `action=classify`, use public read-only Intent tools as needed for bounded repository context.
  Form an agent classification draft containing classification, basis, relevant node and evidence
  references, semantic effects, uncertainties, questions, conflicts, and requested scope. Then
  call the named public `intent_advisory_preflight` MCP tool with only the route's
  `conversation_ref`, `request_evidence_ref`, and that draft. The hook captures the exact
  host-submitted prompt only as untrusted `agent:codex` context; it is not local-human evidence.
  Never send raw prompt text or caller-supplied attribution through MCP. Never call the
  authorization-producing preflight tool for this advisory flow. Ask every returned clarification
  question before implementation. Use only the persisted clarification session returned by the
  advisory result; show exact graph proposals and obtain independently authenticated local-human
  evidence before any graph change.
  Use exactly the established public classifications: `no_semantic_impact`, `aligned`,
  `new_or_ambiguous`, and `conflicting`.
- For `action=human_attention_required`, do not capture, classify, implement, or resolve the work
  automatically. Direct the developer to complete the exact bounded local view named by the route,
  then wait for a later prompt after that human workflow has changed the durable state.
- For `action=human_confirmation_required`, do not submit the prompt as an answer or approval over
  MCP. Explain that the hook cannot authenticate a local human and leave the clarification pending.
  An independently authenticated non-MCP local human integration must record the answer before the
  session may advance. Intent Engineering does not ship a CLI command for this step; if the host has
  not configured such an integration, do not invent one and do not propose a graph change.
- For `action=review_clarification_proposal`, first call the token-free public
  `intent_clarification_show` tool with the supplied proposal ID. Present the complete persisted
  preview and its exact `proposal_digest`. The public MCP surface cannot authenticate approval and
  must not apply it. An independently authenticated non-MCP local human integration must confirm the
  digest and selected node IDs. A decline, no, or digest mismatch leaves the proposal unapplied;
  never classify that response as a new task.
- For `action=continue`, follow the returned message without inventing authority or mutating intent
  state directly.

## Compare requirement alternatives

Only enter this workflow when the user explicitly asks for alternatives, or explicitly chooses the
suggestion action for a visible orange or red requirement. An ordinary feature request continues
through `action=classify`; do not reinterpret it as an alternative request.

Map the user's words to the exact public modes: `simpler` to `simplify`, `manual` or `different approach` to `change_approach`,
`smaller scope` to `narrow_scope`, and `reuse existing` to `reuse_capability`. First call
`intent_alternative_context`. Draft no more than four candidates, then call
`intent_alternative_validate` for each draft and present only validated
comparisons. If the server is known to have a configured reasoner, call `intent_alternatives`
instead of the context-and-host-validation sequence.
If that reasoner is unavailable, keep the approved assessment available and say that no
alternatives were generated.

Label every comparison as projected, not approved. Show the preserved intent and desired outcomes,
changed constraints, complexity, risks, tradeoffs, assumptions, and score deltas. Never submit or approve an alternative over MCP.
When the user selects a host-validated candidate, give them its exact candidate JSON and direct them
to select the requirement in Intent Dev, click **Validate host candidate**, paste the exact JSON,
then click **Validate host candidate locally**. After the same candidate is re-grounded there, they
can create and inspect its pending proposal in the local Proposal review view.
Do not implement the selection or claim it is approved until the existing
independently authenticated local review completes.

## Improve existing intent without a new feature

An explicit request to improve existing intent is an enrichment session, not a new requirement.
Direct the user to **Improve graph** in the existing Intent Dev dashboard, where they can choose
5, 15 or 30 minutes or a topic. Resume an existing session when available: it retains answered
and skipped questions. With that session's ID, use `intent_enrichment_status` and
`intent_enrichment_next_question` to read its current state and single question. These read-only
tools are also permitted for this explicit enrichment request, even without a classification route.
Summarize the question's cited context and mark assumptions before presenting it. Answers,
pause/resume and proposal creation use the local dashboard; do not invent MCP mutation tools.
When time runs out, preserve the session and pending evidence. Do not require a new feature or
claim that a recorded answer has changed the approved graph. The usual local review applies.

Outside the onboarding CLI workflow above, use only the public MCP workflow tools named by the route.
Do not write `.intent` graph, evidence,
case, approval, or history files directly. If MCP tools are unavailable, say exactly: "Intent MCP
tools are unavailable; no intent-aware workflow action was taken." Then stop the intent-aware
workflow without claiming approval or enforcement.
