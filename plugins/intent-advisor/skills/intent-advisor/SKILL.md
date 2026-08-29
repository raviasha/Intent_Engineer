---
name: intent-advisor
description: Route repository work through the advisory Intent Engineering onboarding, preflight, clarification, proposal, and review workflows.
---

# Intent Advisor

Treat prompt-hook guidance as advisory workflow context, not complete mutation enforcement. Never
request, display, persist, or infer a capability token. Do not weaken or work around a
`MandatoryHookUnavailable` result from the host adapter.

## Follow the prompt route

- For `action=offer_onboarding`, ask whether to start guided onboarding. Only after explicit human
  confirmation, ask the user for the PRD path. Confirm that exact path, then run
  `intent onboard --project . --prd <confirmed path> --yes` and follow its proposal/confirmation
  flow. A decline leaves the repository unchanged. A call without `--yes` is only the offer/no-op
  diagnostic and must not be treated as an advancing onboarding command.
- For `action=classify`, use public read-only Intent tools as needed for bounded repository context.
  Form an agent classification draft containing classification, basis, relevant node and evidence
  references, semantic effects, uncertainties, questions, conflicts, and requested scope. Then
  call the named public `intent_advisory_preflight` MCP tool with only the route's
  `conversation_ref`, `request_evidence_ref`, and that draft. The hook has already captured the
  exact human prompt; never send raw prompt text or caller-supplied attribution through MCP. Never call the
  authorization-producing preflight tool for this advisory flow. Ask every returned clarification
  question before implementation. Use only the persisted clarification session returned by the
  advisory result; show exact graph proposals and obtain human confirmation before any graph change.
  Use exactly the established public classifications: `no_semantic_impact`, `aligned`,
  `new_or_ambiguous`, and `conflicting`.
- For `action=answer_clarification`, call the named public `intent_clarification_answer` MCP tool
  with only the supplied `session_id`, `question_id`, and `answer_evidence_ref`. The hook has already
  captured the answer; never send raw answer text, actor, timestamp, or ACL through MCP.
  Do not classify the answer again. Continue with `intent_clarification_propose` only when the
  returned session state permits that exact next step.
- For `action=review_clarification_proposal`, first call the token-free public
  `intent_clarification_show` tool with the supplied proposal ID. Present the complete persisted
  preview. Call `intent_clarification_confirm` only when the current human prompt exactly confirms
  that preview's `proposal_digest` and the selected node IDs. A decline, no, or digest mismatch
  leaves the proposal unapplied; never classify that response as a new task.
- For `action=continue`, follow the returned message without inventing authority or mutating intent
  state directly.

Use only the public MCP workflow tools named by the route. Do not write `.intent` graph, evidence,
case, approval, or history files directly. If MCP tools are unavailable, say exactly: "Intent MCP
tools are unavailable; no intent-aware workflow action was taken." Then stop the intent-aware
workflow without claiming approval or enforcement.
