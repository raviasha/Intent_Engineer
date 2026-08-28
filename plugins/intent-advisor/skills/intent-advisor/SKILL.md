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
  confirmation, run `intent onboard --project .` and follow its proposal/confirmation flow. A
  decline leaves the repository unchanged.
- For `action=classify`, call the named public `intent_preflight` MCP tool once, using the current
  human prompt as `task`. Ask every returned clarification question before implementation. Show the
  exact graph proposal and obtain human confirmation before any graph change.
- For `action=answer_clarification`, call the named public `intent_clarification_answer` MCP tool
  with the supplied session metadata and the current human prompt as `answer`.
  Do not classify the answer again. Continue only after the existing clarification flow returns a
  resolved result.
- For `action=continue`, follow the returned message without inventing authority or mutating intent
  state directly.

Use only the public MCP workflow tools named by the route. Do not write `.intent` graph, evidence,
case, approval, or history files directly. If MCP tools are unavailable, say exactly: "Intent MCP
tools are unavailable; no intent-aware workflow action was taken." Then stop the intent-aware
workflow without claiming approval or enforcement.
