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

Use only the public MCP workflow tools named by the route. Do not write `.intent` graph, evidence,
case, approval, or history files directly. If MCP tools are unavailable, say exactly: "Intent MCP
tools are unavailable; no intent-aware workflow action was taken." Then stop the intent-aware
workflow without claiming approval or enforcement.
