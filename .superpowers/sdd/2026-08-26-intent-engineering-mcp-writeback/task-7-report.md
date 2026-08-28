# Task 7 — Connector and guarded-write CLI workflows

Status: COMPLETE. Tasks 8–10 have not started.

Base: `9f5ac75`.
Product/tests commit: `7c4e966`.

## Outcome

Added the user-facing bridge from the completed MCP/read/write domain services to the local CLI.
`intent init` now creates a descriptor-rooted `.intent/connectors` configuration directory. Strict
local YAML bindings contain only environment references. `intent connectors list|inspect|test`
renders credential-free semantic configuration, validates the exact profile/binding against the
shared MCP runtime, inspects bounded capabilities, and performs declared read probes without
calling a write capability.

`mcp` is now a stable sync source. `intent sync --sources markdown,git,mcp` constructs one MCP
connector per configured object type and sends every selected local/provider connector through one
`SyncOrchestrator` invocation. This preserves provider authors, versions, ACL decisions, immutable
evidence, and cross-source drift detection on one final graph rather than running per-source
last-write-wins updates.

Added a strict local mutation policy with separate contributor, approver, and executor roles plus
an authoritative cross-source person-alias registry. A plan's actor-scoped connector fingerprint is
recomputed from the current profile/server/binding/scope and original proposer before approval or
execution. This prevents a second local/provider identity belonging to the same person from
constituting independent approval.

`intent write preview` can reload a stored plan or fetch the exact current provider object and
create a Task 5 immutable plan for a human-review case. `intent write approve` refuses non-TTY
input, displays the complete preview before prompting, requires exact `approve <plan-id>`, and
persists a separate hash/version/time-bound approval. `intent write execute` requires an explicit
approval ID, instantiates the Task 6 at-most-once executor, and atomically records the production
receipt, write evidence, case resolution, graph version, and ChangeSet history. Sync and preview
never imply or execute approval.

The production MCP mutation gateway invokes only profile-declared fetch/write capabilities, binds
only declared arguments, selects exact IDs/versions/content, discards provider envelopes, and
normalizes public failures behind context-free boundaries. Result success is trusted only after the
Task 6 executor refetches and compares exact approved post-write semantics.

Read-side CLI projections now resolve one immutable authorization-principal snapshot per command,
combining the configured local actor, authenticated provider principals, and the policy alias
registry. Authorized teammates can therefore see their ACL-scoped conversation evidence and graph
effects without reparsing mutable policy for every record. Authorship remains the immutable provider
principal attached to each evidence version; aliases affect authorization and separation of duties,
not provenance identity.

Sync and guarded writes now share one crash-recovery domain over graph, history, cases, evidence,
and receipts. Evidence append and receipt claim/complete operations participate in that coordinator,
authenticate the exact held target descriptor, and cannot be redirected by directory replacement.
The write gateway performs a genuinely live pre/post fetch rather than requesting the approved
historical version, so changed targets fail closed before mutation. Diagnostic/read capabilities
must also resolve to physical provider tools disjoint from all declared write tools.

## TDD evidence

The initial tests-only RED was two failures because the connector and write CLI modules did not
exist. The first GREEN registered the command groups and exact noninteractive/confirmation
contract. Subsequent RED/GREEN slices added descriptor-safe catalog loading, combined MCP sync,
production approval, exact remote preview, profile-bound provider execution, and a full two-person
success transaction.

Security regressions cover symlinked and special-file bindings, nonblocking FIFO rejection,
descriptor-rooted profile loading after workspace replacement, strict YAML, environment-reference
redaction, cross-source creator/approver aliases, actor-scoped connector authentication, ACL-aware
team projections, read/write physical-tool alias rejection, live-version refetch, exact transaction
target identity, and malformed provider payload absence from repository traceback locals. Crash
tests prove that recovery cannot erase a later evidence append or provider-mutation receipt claim.
An existing release-proof assertion was updated to include the newly initialized connector
directory.

## Files

- `src/intent_engineering/cli/{app,runtime,connectors,writes}.py`
- `src/intent_engineering/capture/mcp/{profile_loader,profile_models}.py`
- `src/intent_engineering/context/provider.py`
- `src/intent_engineering/core/policy/{access,project}.py`
- `src/intent_engineering/storage/jsonl/{evidence_store,receipt_store}.py`
- `src/intent_engineering/storage/{secure,transaction}.py`
- `src/intent_engineering/validation/service.py`
- `tests/e2e/{test_cli_connectors,test_cli_write_approval}.py`
- `tests/e2e/test_cli_local.py`
- `tests/integration/github/test_no_secret_persistence.py`

## Verification

| Gate | Result |
| --- | --- |
| focused Task 7 CLI tests | 19 passed in 0.85s |
| broadened MCP/mutation/CLI selection | 330 passed in 20.72s |
| full offline suite with warnings as errors | 974 passed in 31.16s |
| tracked plus Task 7 Ruff | clean |
| tracked source mypy plus Task 7 additions | 93 source files, clean |
| connector/write/sync help | exit 0; 15/15/13 lines |
| `git diff --check` | clean |
| independent review | Ready; 0 Critical, 0 Important, 0 Minor |

The five protected untracked artifacts remain untouched. Tests use no live provider, credential,
network call, GUI, or raw Git object access.
