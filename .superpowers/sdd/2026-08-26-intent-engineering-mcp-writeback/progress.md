# MCP and Guarded Write-Back Progress

Base: `b3dd8a4`
Branch: `feat/public-alpha`
Plan: `docs/superpowers/plans/2026-08-25-intent-engineering-mcp-writeback.md`

| Task | Status | Product commit | Report commit | Review |
| --- | --- | --- | --- | --- |
| 1. Typed provider profiles and safe selectors | Completed | `00e41c7` | `09419d4` | Clean after 2 fix rounds |
| 2. Shared MCP client runtime | Completed | `60dfd4a` | `e8c4090` | Clean after 4 fix rounds |
| 3. Reference provider profiles | Completed | `ab6e85b` | `b54c209` | Clean after 2 fix rounds |
| 4. Read-side MCP connector | Completed | `9954c73` | `f1960b1` | Clean after 2 fix rounds |
| 5. Write plans, approvals, receipts | Completed | `5ba7f9b` | `54e1c6c` | Clean after 3 fix rounds |
| 6. Approved write execution | Completed | `7b9309b` | This commit | Clean after 2 review rounds |
| 7. Connector/write CLI | Completed | `7c4e966` | This report commit | Ready; 0 Critical/Important/Minor |
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
- Ruling: Task 2 resolved the declared dependency to official MCP Python SDK `2.1.1` in the ignored
  virtual environment. Production SDK imports stay behind the runtime factory, and public models,
  the session port, and injected-fake tests remain importable without the SDK. Stdio delegates its
  baseline child environment to the SDK's safe inherited-variable allowlist and adds only explicitly
  resolved configured references; HTTP redirects remain disabled for credential-bearing sessions.
  Cost if wrong: SDK-minor API changes inside the declared `>=2,<3` range may require a narrow
  adapter compatibility update, while provider-neutral consumers remain unchanged.
- Ruling: Task 2 gives each production session one dedicated asyncio lifecycle owner. The owner
  enters and exits the complete official-SDK context stack in the same task, while callers wait
  only to the single absolute operation deadline and any shielded cleanup completes in the
  background with a consumed, redacted result. Stdio launch values are scrubbed from retained SDK
  parameters immediately after process creation, and a session accepts exactly one start. Cost if
  wrong: another async backend would require an equivalent task-owner adapter rather than moving
  AnyIO cancel scopes between tasks.
- Ruling: Later Tasks 3+ implement the user-approved hybrid authorship policy. Every authorized
  contributor may add authenticated evidence; compatible graph projection may be automatic.
  Conflicting, overlapping, superseding, or destructive changes require independent authorized
  approval, and a conflicting author cannot self-approve by default. Preserve original
  provider/workspace/account author identity, timestamps, object/version lineage, content hashes,
  evidence references, and per-author semantic/text diffs without last-write-wins. Review cases
  retain competing changes side by side, and append approver identity, time, evidence, and
  resolution. Contributor and approver authorization remain distinct; external system/code
  write-back still requires preview plus explicit approval. Cost if wrong: later ingestion,
  projection, case, approval, and history contracts must be revised together rather than silently
  collapsing author provenance.

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
- Task 2 established the official SDK v2.1.1 stdio and Streamable HTTP adapter, strict frozen
  configs/session port, detached JSON decoding, fixed redacted errors, explicit owned/borrowed
  leases, bounded capability pagination, and deterministic fakes. Review found lifecycle,
  stderr-fd, pagination, decoder, classification, shell, and environment-retention gaps. Four
  TDD fix rounds added a same-task lifecycle owner, absolute caller deadlines with eventual SDK
  cleanup, original-failure preservation, a real subprocess `/dev/null` stderr sink, MCP/HTTP
  classification, a 128-page bound, resolver/stdio-env scrubbing, one-start ownership, and the
  delayed start/close publication guard. Final gates: 60 runtime tests, 134 MCP tests, 802 full
  offline tests, Ruff/mypy/schema/package/secure checks clean. Final independent re-review found
  0 Critical and 0 Important findings. Product commit: `60dfd4a`.
- Task 3 added strict Slack, Notion, Jira, and Confluence reference profiles, complete illustrative
  bindings, and deterministic object/discovery/write/revision fixtures. Review found cross-target
  Slack writes, permissive nested Notion schemas, vacuous revision/ACL coverage, and ambiguous
  primary/revision version collisions. Two TDD fix rounds bound Slack identities only to guarded
  targets, made nested schemas exact, added same-object cross-author version history, separated
  ACL decisions, and made all version identities unambiguous. Final gates: 24 focused contracts,
  162 MCP/package/secure tests, 826 full offline tests, Ruff/mypy/schema/diff checks clean. Final
  independent re-review found 0 Critical, 0 Important, and 0 Minor. Product commit: `ab6e85b`.
- Task 4 added the ACL-aware, profile-driven MCP read connector; full profile/source/scope/actor
  replay identities; canonical exact-prefix checkpoints; official MCP v2 resource-template
  capability validation; reserved-boundary URI expansion; partial-failure durable replay; and deep
  MCP evidence/checkpoint validation. Independent review drove null ACL/author, source provenance,
  cursor redaction, duplicate, resource-template, URI-collision, and uncheckpointed-association
  fixes. Final gates: 208 focused tests, 861 full offline tests, Ruff clean, mypy clean across 83
  source files, and diff checks clean. Final independent review found 0 Critical and 0 Important
  findings and marked the slice Ready.
- Ruling: MCP read evidence persists full profile-contract, source-contract, and scope digests in its
  immutable payload and binds them to full connector identity segments. Distinct local source
  contracts that produce the same provider object/version therefore fail closed instead of
  silently aliasing provenance. Cost if wrong: compatible duplicate capture configurations require
  an explicit source migration/deduplication rule rather than sharing one evidence identity.
- Task 5 precommit: immutable hash-bound write previews preserve exact before/after state, guarded
  target version, provider arguments, evidence references, and conflicting authors. Approval is a
  separate interactive record made by an authorized actor who is neither the proposer nor a
  conflicting evidence author. Plan/approval ledgers are descriptor-safe and append-only. Provider
  execution, receipts, CLI, and MCP mutation tools remain explicitly excluded until later tasks.
- Task 5 review ruling: contributor and approver authorization are separate allowlists, while
  person identity is an explicit alias set spanning local actor names, Git/email identities, and
  provider principals. Plans and approvals persist those aliases, so cross-source conflicts cannot
  be approved by the same person merely by switching identity namespaces. Each write contract also
  declares its target object type and hashes its exact local provider capability mapping.
- Task 6 added a durable claim-before-mutation ledger, exact immutable receipt replay, one non-
  retried provider write, post-write semantic revalidation, fixed provider errors, authorship-
  preserving write evidence, and one atomic receipt/evidence/case/graph/history transaction.
  Independent review drove shared person-level separation-of-duties reauthentication, canonical
  case-target binding, first-create parent-directory fsync, real cross-process contention, locally
  normalized result evidence, and cancellation-safe plan/approval/receipt boundaries. Final gates:
  93 focused tests, 265 broadened MCP/mutation/storage tests, 954 full offline tests, Ruff/mypy/
  format/diff checks clean. Final review found 0 Critical, 0 Important, and 0 Minor findings.
  Product commit: `7b9309b`.
- Task 7 wires strict local MCP bindings into credential-free, read-only connector diagnostics and
  one combined local/provider sync. A strict role/person-alias policy powers exact live remote
  previews, non-TTY refusal, full-preview exact interactive approval, and explicit-approval Task 6
  execution. One immutable authorization snapshot makes ACL-scoped provider evidence visible to
  the authorized teammate while preserving the original provider author on every version. Sync and
  write execution share one five-target crash-recovery domain; coordinated evidence/receipt stores
  authenticate their exact held targets. Review also drove descriptor-rooted nonblocking profile
  reads, special-file rejection, physical read/write capability disjointness, and truly live
  pre/post-write refetches. Final gates: 19 focused, 330 broadened, 974 full offline; Ruff clean;
  mypy clean across 93 source files; help/diff checks clean. Independent review found 0 Critical,
  0 Important, and 0 Minor findings and marked the slice Ready.
