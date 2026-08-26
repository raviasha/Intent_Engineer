# MCP and Guarded Write-Back Progress

Base: `b3dd8a4`
Branch: `feat/public-alpha`
Plan: `docs/superpowers/plans/2026-08-25-intent-engineering-mcp-writeback.md`

| Task | Status | Product commit | Report commit | Review |
| --- | --- | --- | --- | --- |
| 1. Typed provider profiles and safe selectors | Completed | `00e41c7` | This commit | Clean after 2 fix rounds |
| 2. Shared MCP client runtime | Pending | — | — | Pending |
| 3. Reference provider profiles | Pending | — | — | Pending |
| 4. Read-side MCP connector | Pending | — | — | Pending |
| 5. Write plans, approvals, receipts | Pending | — | — | Pending |
| 6. Approved write execution | Pending | — | — | Pending |
| 7. Connector/write CLI | Pending | — | — | Pending |
| 8. Read-only Intent MCP server | Pending | — | — | Pending |
| 9. Guarded MCP mutation tools | Pending | — | — | Pending |
| 10. Public-alpha release proof | Pending | — | — | Pending |

## Repository invariants

- Work entirely offline with deterministic fixtures; never use live provider credentials or accounts.
- Preserve the five pre-existing untracked artifacts byte-for-byte.
- Never inspect raw Git object files or launch GUI/browser applications.
- Every production change follows RED → GREEN and receives independent read-only review before the task is accepted.
- No write may execute without an immutable preview, an exact content-hash-bound interactive approval, and an unchanged target version.
- No Intent MCP tool may create an approval.
- Secrets and resolved environment values may not enter canonical state, evidence, plans, approvals, receipts, reports, errors, logs, or traceback renderings.

## Controller rulings

- Ruling: use this controller-created `2026-08-26-intent-engineering-mcp-writeback` workspace as
  the single recovery map. The stock SDD scripts derive `2026-08-25-intent-engineering-mcp-writeback`
  from the plan filename and would split one plan across two ledgers. Cost if wrong: review packages
  and later task briefs must continue to be created explicitly in this workspace rather than through
  the unmodified helper scripts.
- Ruling: Task 1's brief strengthens the plan's illustrative Pydantic snippets: all public models
  reject extras, frozen models detach mutable inputs into immutable public values, and all persisted
  profile reads use the existing descriptor-safe boundary. Cost if wrong: profile authors receive
  stricter early validation instead of permissive forward-field acceptance.
- Ruling: CI installs directly from `pyproject.toml`; no tracked lock/export artifact exists. Task 1
  adds `mcp>=2,<3` there and records the resolved SDK version only if it is available from the
  offline environment; it will not invent an unused lock file or contact an index. Cost if wrong:
  dependency resolution remains range-based in CI, matching the repository's existing practice.

## Preflight conflict and interface scan

| Task(s) | Shared file/interface | Finding and ruling |
| --- | --- | --- |
| 1 self | profile models, selector grammar, loader, generated schema | Internally consistent after applying the brief's strict/deep-immutable requirements over illustrative snippets; Task 2 runtime/session behavior is excluded. |
| 2 self | session port, SDK adapters, redacted errors | Internally consistent; transport configuration and environment resolution remain Task 2. |
| 3 self | four reference profiles/bindings/fixtures | Internally consistent; examples consume rather than redefine Task 1 contracts. |
| 4 self | MCP Connector, ACL authorization, checkpoints | Internally consistent; authorization is conservative and read-side only. |
| 5 self | write plans, approvals, planner, append-only store | Internally consistent; interactive confirmation is a caller precondition and not exposed through MCP. |
| 6 self | mutation executor and receipts | Internally consistent; exact revalidation precedes one non-retried provider mutation. |
| 7 self | connector/write CLI and `cli/app.py` | Internally consistent; approval creation is local interactive CLI only. |
| 8 self | read-only Intent MCP server | Internally consistent; it consumes shared services and keeps stdout protocol-clean. |
| 9 self | proposal/preview/execution MCP tools | Internally consistent; no approval-creation tool or prompt is permitted. |
| 10 self | docs, examples, public-alpha harness | Internally consistent; harness composes production services and only substitutes fake external boundaries. |
| 1 ↔ 3 | `ProviderProfile`, `ProviderBinding`, selectors, schema | Task 3 must express all provider profiles strictly through Task 1's versioned contracts. |
| 1 ↔ 4 | operation/object models, `select_value`, transforms, binding validation | Task 4 consumes Task 1 directly; strict missing/null and capability semantics are load-bearing. |
| 1 ↔ 5 | write profiles and `bind_arguments` | Task 5 consumes the allowlist/schema/precondition contracts; Task 1 must not implement planning. |
| 1 ↔ 7 | profile loader and bindings | Task 7 diagnostics consume validated profiles; configuration/runtime assembly remains later. |
| 1 ↔ 10 | checked-in profile schema and documentation | Task 10 documents the exact generated schema and selector limits without changing them. |
| 2 ↔ 4 | shared runtime/session port | Task 4 uses the runtime; it must not open a parallel SDK path. |
| 2 ↔ 6 | runtime error and call boundary | Task 6 performs writes through the same provider-neutral session behavior. |
| 2 ↔ 7 | capability inspection/runtime factory | Task 7 reports readiness without duplicating transport logic. |
| 3 ↔ 4 | profile operations and fixture payloads | Task 4 normalization is driven entirely by Task 3 data plus Task 1 selectors. |
| 3 ↔ 5 | guarded write mappings | Task 5 builds plans from profile allowlists/preconditions; profiles never execute writes. |
| 3 ↔ 6 | result-version selectors and write arguments | Task 6 revalidates and executes the exact mapped operation. |
| 3 ↔ 7 | example local bindings | Task 7 inspects/tests bindings without claiming universal server compatibility. |
| 3 ↔ 10 | reference-profile docs/examples | Task 10 publishes only contract-tested examples. |
| 4 ↔ 10 | read sync and ACL release proof | Task 10 must use the production connector and conservative authorization path. |
| 5 ↔ 6 | immutable plan/approval records | Task 6 consumes hash-, actor-, version-, and expiry-bound records unchanged. |
| 5 ↔ 7 | preview and interactive approval services | Task 7 calls the production planner/store rather than recreating record logic. |
| 5 ↔ 9 | proposal/preview services | Task 9 may create plans but must not create approvals. |
| 5 ↔ 10 | approval/write smoke path | Task 10 substitutes only a terminal abstraction around production approval. |
| 6 ↔ 7 | execution service and receipts | CLI execution never implies approval and renders the production receipt. |
| 6 ↔ 9 | guarded execution service | Intent MCP delegates to the same executor with a separately persisted approval. |
| 6 ↔ 10 | changed-target/success release proof | Task 10 verifies both no-mutation rejection and approved success through production services. |
| 7 ↔ 10 | CLI commands and executable docs | Task 10 documents and runs the exact command contracts. |
| 8 ↔ 9 | MCP server registration | Task 9 extends the Task 8 server without weakening its authorization or protocol boundaries. |
| 8 ↔ 10 | stdio MCP server and smoke harness | Task 10 launches and probes the production server. |
| 9 ↔ 10 | mutation MCP surface | Task 10 proves execution cannot manufacture its required approval. |

## Task review notes

- Task 1: minor (deferred): `TRANSFORMS` is a mutable module-level dictionary even though profile
  selection exposes only the fixed named registry; final branch review should decide whether an
  immutable public view is worthwhile.
- Task 1: minor (deferred): the initial wrong-kind binding regression was confounded by an unknown
  mapping and the optional null/missing regression returned the same value for both branches; final
  branch review should require isolated assertions if later fixes do not naturally touch them.
- Task 1 review 1: one Critical and five Important findings. Required secret-free selector/
  transform/binding traceback locals, serialization-round-trippable source-shaped bindings, an
  exact strict-JSON boundary, schema-visible semantic constraints, nonblocking FIFO-safe reads,
  and bounded/redacted deep-YAML failure handling. Round 1 GREEN reached 72 focused tests and 740
  full offline tests.
- Task 1 scoped re-review left three test/schema completeness findings and found one Important
  transform-error classification regression. Round 2 added observable hostile-mapping assertions,
  a schema-visible 32-segment selector bound and typed redaction paths, a subprocess FIFO watchdog,
  and preserved fixed `TransformError` semantics. Final focused tests were 74 passed; secure paths
  3 passed; full offline suite 742 passed; schema regeneration was byte-identical at 11,508 bytes;
  Ruff and mypy were clean. Final independent re-review found all findings addressed and no new
  Critical or Important breakage.
