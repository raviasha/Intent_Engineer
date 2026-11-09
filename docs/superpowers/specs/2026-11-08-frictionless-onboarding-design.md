# Frictionless intent onboarding

> Routine onboarding, clarification and graph activation are superseded by
> [the November 9 background workflow](2026-11-09-background-intent-workflow.md).
> The old approval path remains an explicitly selected governed workflow.

The installed advisor should offer to create a missing repository intent graph, let the
developer defer, and make creation possible at any time by saying “create intent graph”.
The agent does the preparation; the developer reviews the inferred meaning once.

Use the existing prompt hook, bootstrap service and local review UI. Add a small local
reminder store and a non-blocking Stop hook; avoid an independent graph generator or a
second approval mechanism. Keep the current intent-versus-requirements scope.

- Check real onboarding readiness, including pending proposals, before routing a task.
- Offer **Create now** or **Defer for now**. Deferral continues ordinary work without
  initializing `.intent`. Remember it in user-local state, scoped to the repository.
- Default to reminding on the next prompt after three completed top-level work turns.
  A configurable count is a builder choice for “a few tasks”. A Stop event counts once,
  only if a corresponding prompt was seen during deferral. Ignore subagent events,
  repeated events and hook continuations. This is a turn count, not semantic proof that
  a feature was finished. Reminder data contains identifiers/counters, never prompt text.
- An explicit creation request overrides deferral. Existing approved graphs are reused;
  existing pending proposals go to review. Readiness errors remain errors.
- Discover likely Markdown PRDs without requiring a path. Use one clear candidate;
  ask a short selection question when several are plausible. Honor exclusions and never
  follow source symlinks or enter hidden, dependency or generated directories.
- If no PRD exists, the host agent inspects relevant source, tests and docs read-only and
  writes a new, clearly marked inferred PRD with path/revision citations, observed behavior,
  proposed intent, assumptions and unknowns. Never overwrite an existing PRD or infer
  business goals as established fact. No implementation changes or test execution are
  needed for this discovery. An empty project gets a minimal question about purpose.
- Capture this draft as agent-authored proposed intent rather than declared human intent. All bootstrap
  assertions remain inferred and pending until the existing authenticated review.
  A source-visible first-line marker preserves agent attribution across ordinary Markdown sync:
  `<!-- intent-engineering: inferred-prd; author=agent:codex -->`. This is provenance metadata,
  not authentication or approval; the capture flag rejects an unmarked inferred draft.
- Provide a bounded CLI proposal-submission bridge so onboarding can finish even when the
  plugin MCP server started before `.intent` existed. Reuse BootstrapService validation;
  never write canonical graph files directly or accept activation through this bridge.
- After preparation, open the existing dashboard once for review/activation. Reuse known
  answers, ask only material missing questions, and retain the original development task.

Acceptance covers deferred reminders across sessions, exact timing and replay, immediate
creation, pending/approved graphs, PRD ambiguity and unsafe files, inferred-source provenance,
proposal validation and unchanged approval boundaries.
