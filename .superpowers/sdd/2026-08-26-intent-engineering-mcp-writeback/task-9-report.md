# Task 9 — Guarded MCP mutation tools

Status: PRECOMMIT READY after clean independent review. Task 10 has not started.

Base: `e2f74cc`.
Product/tests commit: `2563390`.

## Outcome

Extended the reviewed Task 8 MCP server with exactly four mutation-facing tools:
`intent_changeset_propose`, `intent_reconciliation_propose`, `intent_write_preview`, and
`intent_write_execute`. The existing eight read tools, two prompts, and five resource surfaces are
unchanged. No MCP tool, prompt, resource, completion, or handler creates an approval. Public server
instructions state that mutation tools may persist proposals and previews, while execution requires
an independently persisted approval.

`intent_changeset_propose` accepts one exact structured ChangeSet no larger than 1 MiB. It
descriptor-reads the current project configuration, reauthenticates the startup local actor and
their configured provider/person aliases, requires the current graph baseline, rejects hidden
evidence and hidden mutation targets, validates authorship on every added/replaced graph object,
and proves pure graph application before persistence. It stores only a content-addressed immutable
proposal in a separate descriptor-safe append-only ledger. Identical concurrent puts deduplicate;
corrupt ledgers fail closed and are never rewritten. Canonical graph, history, cases, evidence, and
checkpoints remain byte-identical.

`intent_reconciliation_propose` admits only an authorized OPEN case whose evidence, subject, and
affected objects are visible in one current authorization snapshot. It invokes only the first phase
of the production local resolution service, durably appending PROPOSED/NEEDS_HUMAN case versions
and returning the deterministic ChangeSet plus local review hash. It excludes terminal-only
actions, applies no graph mutation, and creates no ApprovalRecord.

`intent_write_preview` delegates to the production Task 7 `WriteWorkflow.create_preview`, including
its live provider refetch and complete hash-bound plan persistence. It caps structured requested
fields before provider/workflow access and returns the complete redacted plan plus canonical hash.
Preview is not approval.

`intent_write_execute` accepts only canonical `write-plan:sha256:<64>` and
`approval:sha256:<64>` IDs. It requires the named approval to exist before entering the production
Task 6 execution path, then delegates once to `WriteWorkflow.execute` and returns only the immutable
redacted receipt. Missing approval is proven to make zero provider mutation calls; a separately
persisted interactive approval succeeds exactly once through the production executor.

Each provider-facing preview or execution reopens the policy, connector binding, profile, plan,
and approval state from the held project descriptors. The live connector contract must match the
authenticated server-start snapshot before provider access. Policy deletion/corruption, role
revocation, connector deletion, or connector drift therefore rejects the request without using a
stale long-lived workflow. Short-lived workflow stores are explicitly closed after every request.

All mutation results use fixed `schema_version: "1"` envelopes. Invalid and secret-bearing inputs,
unavailable configuration, corrupt proposal state, cancellation, and interrupts expose no caller
value, provider payload, path, cause/context, or repository-frame payload local. Cancellation and
interrupt objects remain exact. The stdio server remains available without write configuration and
returns a fixed per-call rejection.

## TDD evidence

The exact tests-only RED failed collection with
`ModuleNotFoundError: intent_engineering.integrations.mcp_server.mutations`. The first production
GREEN passed 12 focused tests. Hardening then proceeded through additional observed RED/GREEN
slices:

- exact plan/approval schemas, malformed-ID pre-handler rejection, and service-side canonical-ID
  checks initially failed four tests;
- preview cancellation retained secret requested fields in a production traceback frame;
- proposal interrupts retained the structured ChangeSet encoding and candidate;
- proposal-store interrupts retained proposal content;
- oversized ChangeSet and preview-field objects reached persistence/workflow access;
- a production missing-approval preview/execute flow needed an explicit zero-provider-mutation
  assertion.

The final focused slice covers exact surface/annotations, no approval creation, durable proposal
idempotence and corruption, hidden evidence/targets, actor drift, reconciliation first phase,
preview persistence, exact approval gating, real execution, bounded input, fixed errors, and
non-retaining cancellation/interrupt behavior.

Initial independent review found one Critical and five Important defects: stale startup
policy/catalog state could authorize a provider write after live revocation; canonical JSONL
without its terminal newline was accepted; a FIFO ledger could block; preview was incorrectly
advertised as idempotent; nested ChangeSet evidence could escape the declared evidence scope; and
node/edge replacements could rewrite immutable creation timestamps. Tests-only RED reproduced all
six. Re-review added three boundary findings: valid live contributor revocation had to reject before
provider refetch, an approval-ledger FIFO had to reject without blocking, and real preview
cancellation had to scrub detached requested fields from every production traceback frame. The
fix adds per-request live workflow assembly plus startup connector-snapshot comparison,
nonblocking strict ledger reads and terminal-newline enforcement, accurate preview annotations,
complete evidence-scope validation, and immutable creation-provenance checks. The post-fix focused
suite passes 39 tests. Final independent re-review found 0 Critical, 0 Important, and 0 Minor
findings and marked the task Ready.

## Files

- `src/intent_engineering/integrations/mcp_server/mutations.py`
- `src/intent_engineering/integrations/mcp_server/server.py`
- `src/intent_engineering/cli/writes.py`
- `tests/contract/mcp/test_intent_server_mutations.py`
- `tests/e2e/test_mcp_write_guard.py`
- `tests/e2e/test_mcp_server.py`

## Verification

| Gate | Result |
| --- | --- |
| focused Task 9 contract/stdio | 39 passed in 3.50s |
| combined Task 8/9 protocol | 47 passed in 3.20s |
| broadened MCP/write/reconciliation selection | 400 passed in 6.26s |
| full offline suite with warnings as errors | 1,039 passed in 36.79s |
| tracked plus Task 9 Ruff | clean |
| tracked source mypy plus Task 9 addition | 101 source files, clean |
| Task 9 formatting | changed files clean |
| `intent mcp/write/connectors --help` | all exit 0 |
| `git diff --check` | clean |
| independent review | Ready; 0 Critical/Important/Minor after fixes |

One first full-suite attempt hit the repository's pre-existing fork-time evidence-store lock-file
initialization race. Task 9 changes no evidence-store or lock implementation. The exact failed test
immediately passed 1/1, and a fresh full suite passed 1,021/1,021 normally.

The five protected untracked artifacts remain byte-identical and untouched.
