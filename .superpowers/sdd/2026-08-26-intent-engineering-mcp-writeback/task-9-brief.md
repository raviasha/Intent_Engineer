# Task 9 Brief — Guarded MCP mutation tools

Base: `e2f74cc`.

Extend the reviewed Task 8 server with exactly four mutation-facing tools:
`intent_changeset_propose`, `intent_reconciliation_propose`, `intent_write_preview`, and
`intent_write_execute`. Keep all eight read tools unchanged. Do not register any tool, prompt,
resource, completion, or indirect code path that creates an `ApprovalRecord` or calls
`approve_plan()`.

- `intent_changeset_propose` accepts one exact structured `ChangeSet`, rebinds nothing silently,
  requires its actor to be the freshly authenticated local actor, its baseline to equal the current
  graph version, every evidence reference to exist and be readable, and pure graph application to
  succeed. Persist the content-addressed proposal only in a separate append-only descriptor-safe
  proposal ledger; never append canonical history or mutate graph/case state.
- `intent_reconciliation_propose` may invoke only the first proposal phase of the production local
  resolution service for an authorized OPEN case. It may persist the PROPOSED/NEEDS_HUMAN case
  versions as the durable reconciliation proposal and return its deterministic ChangeSet, but it
  must not apply the ChangeSet or manufacture the
  approval record required by an external write. It may return the local resolution service's exact
  review hash so a person can separately inspect and run the existing explicit CLI resolution.
- `intent_write_preview` delegates to the production Task 7 `WriteWorkflow.create_preview`, thereby
  fetching live state and persisting the complete hash-bound plan. Return the full redacted preview
  plus canonical plan hash; preview is never approval.
- `intent_write_execute` accepts only exact persisted plan and approval IDs and delegates to the
  same Task 6 executor assembled by `WriteWorkflow.execute`. A missing, expired, mismatched, stale,
  unauthorized, or already-ambiguous pair fails closed. Return only the immutable redacted receipt;
  never retry a provider mutation.

All four tools use bounded manual arguments and fixed schema-versioned public results. Missing local
provider/policy configuration leaves the stdio server available but makes mutation requests return
one fixed rejection with no path, credential, provider payload, cause, context, or repository-frame
local. Cancellation and interrupts remain detached and exact. Tool annotations must accurately
distinguish proposal/preview persistence from destructive provider execution.

Tests must exercise the production server through the official SDK in-memory and stdio paths,
prove the exact surface contains no approval creation, prove proposals/previews make no canonical
graph mutation, prove execution rejects without a separately persisted interactive approval before
provider access, prove a valid pre-existing approval delegates once to the production executor, and
prove malformed/secret-bearing inputs are fixed and non-retaining. Remain offline, preserve all five
authorized artifacts, follow RED → GREEN, run full/static gates, and obtain independent read-only
review before commit. Task 10 remains out of scope.
