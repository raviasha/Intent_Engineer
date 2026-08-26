# Task 8 — Read-only Intent MCP server

Status: COMPLETE. Tasks 9–10 have not started.

Base: `09412aa`.
Product/tests commit: `acaf5fb`.

## Outcome

Added an official MCP Python SDK v2 server exposed by `intent mcp` over stdio. Its exact read-only
tool surface is `intent_context`, `intent_explain`, `intent_impact`, `intent_drift`, `intent_status`,
`intent_validate`, `intent_reconcile_list`, and `intent_reconcile_show`. Every tool carries explicit
read-only, non-destructive, idempotent, closed-world annotations and returns a deterministic
`schema_version: "1"` envelope. No planning, approval, receipt, mutation, or provider capability is
registered.

Registered graph-node, immutable evidence-chain, public-schema, reconciliation-case, and drift-
report resources plus the bounded `prepare_task` and `review_reconciliation` prompts. Evidence
chains preserve the evidence store's append/provenance order. Prompts direct clients to the
versioned read tools and never embed graph dumps or offer approval/write behavior.

Every ACL-scoped, state-bearing read descriptor-reads the current strict project configuration,
authenticates its actor against configured provider principals and the authoritative person-alias
registry, then captures graph, cases, and evidence together under the existing five-target
transaction coordinator. That one immutable authorization snapshot filters evidence, graph
nodes/edges, and cases before their serialization. A case is visible only when its evidence,
subject, and every affected graph object are authorized; terminal, missing, unauthorized, and
mixed-ACL objects are indistinguishable from not found. Validation separately reads the held project
directory, while public schemas and static prompts contain no project state. The server stays bound
to the startup project ID, graph path, and held project directory, so pathname replacement or
configuration drift fails closed rather than mixing workspaces.

The production server applies fixed non-retaining boundaries around SDK tool validation, prompt
lookup/render validation, and resource lookup. Unknown, malformed, traversal-shaped, oversized,
missing-required, and unauthorized requests expose neither caller values nor SDK validation text,
URI error data, causes, contexts, repository-frame locals, worktree paths, or stderr tracebacks.
The Typer startup boundary writes no pre-protocol stdout and preserves cancellation/interrupts.

The production stdio E2E test launches the installed `intent` executable through the official MCP
client, initializes the server, verifies its exact surface, exercises successful and rejected
requests, and confirms protocol-clean output without a live provider, credential, network service,
GUI, or mutation path.

## TDD evidence

The exact tests-only RED failed at collection with `ModuleNotFoundError` for the absent
`intent_engineering.integrations` package. The first GREEN passed seven contract/stdio tests.
Hardening then proceeded through focused RED/GREEN slices for the exact resource/template surface,
byte-identical workspace reads, bounded identifiers, fixed corrupt-workspace errors, per-request
actor refresh, held-directory validation after pathname replacement, bounded prompt/tool inputs,
and terminal-case hiding.

Independent review drove five further focused RED/GREEN fixes:

- unknown/malformed/oversized resource URIs initially echoed caller text; one server-wide resource
  boundary now emits a fixed not-found error, strips wire error data, and retains no request local;
- evidence-chain versions were lexicographically sorted; they now preserve store append order;
- public-evidence cases could reveal ACL-hidden graph IDs; subject and every affected ref must now
  belong to the authorized graph snapshot;
- missing tool parameters failed in SDK/Pydantic before handlers and echoed another supplied value;
  the server now maps all SDK `ToolError` results to one fixed argument error;
- unknown/missing prompts failed in the SDK manager and echoed names/validation details; the server
  now applies the same fixed, non-retaining prompt boundary.

The mixed MCP selection also exposed a pre-existing test namespace ambiguity:
`tests/unit/capture/mcp` could shadow the installed top-level SDK package. Adding
`tests/unit/capture/__init__.py` makes the test module's full name unambiguous.

## Files

- `src/intent_engineering/integrations/{__init__,mcp_server/*}.py`
- `src/intent_engineering/cli/{app,connectors,writes}.py`
- `src/intent_engineering/validation/{__init__,service}.py`
- `tests/contract/mcp/test_intent_server_reads.py`
- `tests/e2e/test_mcp_server.py`
- `tests/unit/capture/__init__.py`

## Verification

| Gate | Result |
| --- | --- |
| focused Task 8 contract/stdio | 26 passed in 1.74s |
| broadened MCP/CLI/validation selection | 301 passed in 5.01s |
| full offline suite with warnings as errors | 1,000 passed in 32.97s |
| tracked plus Task 8 Ruff | clean |
| tracked source mypy plus Task 8 additions | 99 source files, clean |
| Task 8 formatting | 12 files, clean |
| `intent mcp --help` | exit 0; 11 lines |
| `git diff --check` | clean |
| independent review | Ready; 0 Critical, 0 Important, 0 Minor |

The five protected untracked artifacts remain byte-identical and untouched.
