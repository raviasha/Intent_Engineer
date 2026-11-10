# Setup chat results implementation plan

> **For agentic workers:** Use superpowers:executing-plans for inline implementation.

**Goal:** Automatically return first-setup findings and recommendations to the original active agent.
**Architecture:** Add a read-only presentation adapter at the public initial-assessment
CLI/MCP boundaries and an explicit host output recipe. Preserve assessment semantics.
**Tech stack:** Existing Python, Typer, MCP and plugin skill.
**Spec:** docs/superpowers/specs/2026-11-10-setup-chat-results.md

## Global constraints

- No assessed code/source edits, fabricated alignment or source authority changes.
- No progress broadcasts from ordinary code-change workers.
- No claims of actual message delivery by the local service.
- Preserve ACL redaction, freshness, incomplete states and stable evidence.

## Review focus

- Graph exists but reconciliation has not started: never claim setup is complete.
- All comparisons reviewed but some unknown: report incomplete explicitly.
- Ready with no findings: provide a final scope-qualified result.
- Repeated status and resumed setup: stable IDs suppress duplicate updates.
- Access revoked or files changed: do not present cached findings as current.

### Task 1: Public setup presentation contract

Files: new intent_workflow/setup_presentation.py; CLI initial_reconciliation.py and
background_onboarding.py; integrations/mcp_server/background.py; public workflow tests.
Interface: `with_setup_chat_update(result: dict[str, Any]) -> dict[str, Any]`.

- [x] Add failing CLI/MCP tests for progress, final findings, retry identity and incomplete results.
- [x] Add the presentation adapter and integrate only public setup boundaries.
- [x] Verify redacted/unavailable and no-findings output; run focused tests and type checks.

### Task 2: Host behavior and release

Files: plugin skill/reference, handoff prompt, quick start, plugin version contracts.

- [x] Record baseline setup-agent behavior; add a positive milestone/final-summary recipe.
- [x] Verify agent scenarios automatically report progress and recommendations.
- [x] Capture original routing IDs and completed run; produce current return packet and acknowledged delivery.
- [x] Add original-session-only next-prompt retrieval fallback and verify stale/duplicate isolation.
- [x] Verify updated host return behavior against no-return baseline.
- [x] Update product documentation and version; review the branch and run full checks.
- [ ] Push source and publish verified native marketplace builds; verify installed behavior.
