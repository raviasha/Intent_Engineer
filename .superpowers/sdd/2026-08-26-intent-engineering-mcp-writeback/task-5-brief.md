# Task 5 brief — Immutable plans and independent approvals

Base: `f1960b1`. Scope is MCP write planning and local approval persistence only. Task 6 provider mutation execution is excluded.

## Required behavior

- Add strict, deeply immutable `RemoteObject`, `WritePlan`, `ApprovalRecord`, `ExecutionReceipt`, and `WriteResult` models.
- A write plan is an exact preview of target, before version/content, after content, bound provider arguments, evidence references, conflicting authors, creator, timestamps, and expiry.
- The canonical plan hash covers every semantic and authorization-relevant field except its derived ID. The plan ID is derived from that hash and validated on every load.
- Planning accepts only a nonterminal human-review reconciliation case, exact built-in JSON requested fields, allowed profile fields, a schema-valid bound argument object, and a current remote object matching the operation context.
- Preserve all case evidence references and every competing evidence author. Planning may be performed by a contributor, but it never constitutes approval.
- Require a separately configured contributor allowlist and an explicit person-level identity alias registry spanning local actors, repository/email authors, and provider principals. Persist proposer aliases in the plan.
- Bind the plan to the validated provider profile, local binding hash, exact provider capability/write-contract hash, and declared target object type. Every operation must bind both target identity and optimistic before-version sources.
- Approval requires an interactive exact confirmation of the hash-bound plan ID, a separately configured authorized approver, an unexpired plan, and a positive bounded expiry.
- By default an approver may be neither the plan creator nor any author on a conflicting evidence side. This is the user-approved independent-approval rule; contributor and approver authorization are distinct.
- Resolve approver identity through the same person-level alias registry and persist the aliases used for approval, so switching local/provider/source namespaces cannot bypass independence.
- Approval binds plan ID/hash, target version, approver identity, approval time, and expiry. Any plan or target change invalidates it.
- Provide separate descriptor-safe append-only JSONL stores for plans and approvals. Exact duplicates are no-ops; reused IDs with different bytes, duplicate JSON keys, unknown fields, malformed records, symlink/hardlink paths, and model-copy validation bypasses fail closed.
- Public errors are fixed and do not retain requested field values, before/after content, or provider schema data in args, cause/context, or repository traceback locals.
- No approval API is exposed through MCP. No provider write, receipt persistence, case resolution, scheduling, CLI, or server surface is implemented in this task.

## Verification

- Establish focused RED before production code.
- Run focused mutation/store tests, relevant profile/secure-storage tests, Ruff, mypy, schema/package checks, and the full offline suite.
- Obtain independent read-only review with no Critical/Important findings before commits.
- Preserve the five protected untracked artifacts byte-for-byte.
