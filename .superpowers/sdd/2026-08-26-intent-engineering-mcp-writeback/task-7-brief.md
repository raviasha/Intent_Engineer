# Task 7 brief — Connector and guarded-write CLI workflows

Base: `9f5ac75`. Scope is the local CLI assembly for Tasks 1–6. The Intent MCP server
and MCP-exposed mutation tools remain Tasks 8 and 9.

## User workflow

1. `intent init` creates the local workspace. Each teammate configures non-secret MCP
   bindings under `.intent/connectors/*.yaml`; credentials remain environment references. Teams
   declare contributor/approver/executor roles and cross-source person aliases in the strict local
   `.intent/approvals/policy.yaml` registry.
2. `intent connectors list|inspect|test` validates the local semantic profile and server
   capabilities without exposing credentials or invoking a write capability.
3. A user or scheduler runs `intent sync --sources markdown,git,mcp`. All selected local
   and MCP connectors enter one `SyncOrchestrator` run, so authored conversation evidence,
   repository evidence, graph projection, and reconciliation cases share one final graph view.
4. Compatible immutable evidence may update the graph through existing policy. Competing
   authors/versions remain side by side and create human-review cases rather than last-write-wins.
5. External provider changes use `intent write preview`, `intent write approve`, and
   `intent write execute`. Sync never performs an external mutation.

## Required behavior

- Connector configuration is strict, descriptor-rooted, no-follow, duplicate-key rejecting,
  and bounded. Configuration contains only environment references, never resolved secrets.
- Connector listing and inspection expose stable IDs, profile/version, transport, semantic
  operations, object types, mapped capability names, and environment/header names only.
- Connector testing uses the shared `McpRuntime`, validates the exact binding, inspects bounded
  capabilities, and performs only declared read probes. It never invokes a declared write.
- MCP sync constructs one read connector per configured object type and combines those with
  selected Markdown/Git/GitHub connectors in one orchestrator invocation.
- A preview reloads the live remote target through the profile/binding, constructs the exact
  Task 5 `WritePlan`, and durably stores it. Rendering includes target, before/after, bound
  arguments, evidence/authorship, version precondition, expiry, and canonical hash.
- Approval re-renders the complete immutable preview, refuses noninteractive stdin, requires
  exact `approve <plan-id>`, reauthenticates the current actor and provider aliases, and appends
  one Task 5 `ApprovalRecord`. The proposer or any conflicting author cannot approve.
- Execution requires a separately persisted approval ID and delegates to the Task 6 executor.
  It never creates or infers approval, never retries an ambiguous provider write, and emits only
  the production receipt.
- Team identity is preserved through the explicit local actor plus a cross-source alias registry
  that includes provider principals, Git/email identities, and any other authenticated namespace.
  Each authorized team member may contribute; contributor, approver, and executor roles are
  separate, and approval remains independent at the person/alias level.
- All command failures are fixed and redacted. Resolved environment values/provider payloads may
  not enter output, logs, durable state beyond normalized evidence, or repository traceback locals.

## Verification

- Establish the plan's two CLI tests as RED before production code.
- Add offline production-path tests for strict configuration, combined MCP sync, non-mutating
  diagnostics, exact preview, non-TTY/exact approval, independent authorship, changed-target
  rejection, and successful at-most-once execution.
- Run the focused CLI tests, all MCP/mutation suites, Ruff, mypy, the full offline suite, then an
  independent read-only adversarial review before committing.
- Preserve the five protected untracked artifacts byte-for-byte.
