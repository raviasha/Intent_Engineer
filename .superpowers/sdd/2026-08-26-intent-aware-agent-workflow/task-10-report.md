# Task 10 report — complete operating proof and adoption contract

## Status

`COMPLETE` from exact base `049f158fdde65095f15494a8f4654c672e3adaa3`.
The same independent reviewer returned the final verdict `0 Critical / 0 Important / 0 Minor`,
Ready. The exact product/test/docs/workflow commit is
`1cca7b18dcb813e90a6f7b1d710ffa163e8294aa` (`feat: complete intent-aware agent workflow`). This
report, the controlling brief, and progress are intentionally isolated in the subsequent metadata
commit.

Task 8 remains final: no Codex plugin is shipped, mandatory Codex construction returns the fixed
`MandatoryHookUnavailable`, and disabled host mode is a transparent no-op.

## Implemented operating proof

One `IntentAwareAgentHarness` drives one ordinary existing Git repository and one initialized local
transaction domain through the complete journey. Only external HTTP/MCP/provider, supported-host,
test-process, and interactive-terminal boundaries are fake.

- Real CLI PRD capture returns `agent_submission_required`; real bootstrap proposal/review/activation
  confirms the core, retains provisional content, source author/version/ACL, graph history, and
  byte-stable replay.
- One production MCP conversation connector records two teammate revisions with distinct authors,
  immutable versions, exact predecessor lineage, ACL, and the full canonical connector identity as
  an explicit proposed-intent source role. Git evidence enters the same ledger.
- Real preflight classifies an aligned task, issues a process-local exact-scope capability, and the
  actual host-neutral adapter authorizes before effect. The harness then creates a real commit, runs
  a real passing test with a test-boundary sentinel, captures Git/test evidence, and records exact
  requirement/code/test implementation links through `after_task` and `PostTaskService`.
- A new/ambiguous request persists attributed human/agent turns, two required questions and answers,
  exact open/answer/answer/propose/close chronology, contributor confirmation, and graph/history.
- A conflicting request denies mutation, opens evidence-backed review, rejects proposer self-review,
  and requires a distinct policy-authorized reviewer before activation.
- The audited Codex contract produces exactly `Codex mandatory mutation hook is unavailable`; no
  plugin directory exists. The separately disabled host performs no workflow call or completion.
- Scheduled `markdown,git,github,mcp` capture and assurance use the same state. The first pass adds
  real evidence/cases with stable fingerprints; the identical second pass has zero evidence,
  semantic changes, or cases and preserves checkpoint bytes. Validation, drift, CLI, and official
  MCP context read the same graph.
- Provider-write governance composes real resolution, profile/binding/catalog, plan, approval,
  executor, committer, receipt, and official MCP services. Missing approval is an exact fixed
  rejection with zero calls and byte-stable state; changed target performs one fresh read and zero
  mutation; success performs exactly one mutation and records exact plan/approval/receipt/result
  links in reviewer-authored write evidence.
- Final graph version 7, history, cases, evidence, proposals, decisions, receipts, CLI, MCP, and deep
  validation agree. Rejected/no-op paths preserve their relevant canonical bytes.
- GitHub, Slack/MCP, Jira, disabled-request, test-environment, and generated authorization-token
  sentinels enter their real boundaries. Every regular project file plus captured stdout, stderr,
  structured logs, fixed errors, and repository traceback locals is scanned; the final leak set is
  empty. No capability, digest, proof, credential value, or rejected private request is durable or
  public.

## Minimal production composition seams

The E2E witnessed each seam RED before its focused change:

- `SourceRoleAssignment.connector_id` remains bounded but now accepts the canonical production MCP
  identity containing the configured ID plus four SHA-256 components.
- Persisted YAML configuration is validated through an exact JSON round trip at proposal authority,
  preflight/MCP read, guarded MCP mutation, and validator boundaries, preserving strict enums without
  rejecting their serialized string form.
- Preflight conflict-side authors are canonical sorted tuples.
- Proposal activation carries the exact union of proposal and selected nested mutation evidence.
- Validation recognizes canonical conversation and MCP-write evidence identities, checks MCP-write
  payload hashes, applies evidence-overlap only to genuine conflicting-source sides, and permits
  preflight/proposal-governance review cases that intentionally have no semantic graph-creation row.
- The required diagnostic CLI `intent preflight --task ...` renders the same bounded context with
  `authorization_issued: false`; it never classifies or accesses the process-local issuer.

No model/provider/network implementation, Codex hook, approval shortcut, background write, hosted
service, or universal-provider behavior was added.

## Documentation and scheduled workflow

- `docs/intent-aware-agent.md` is the honest existing-repository guide: install/init, capture-only PRD
  bootstrap, typed active-agent proposal, source roles, profile plus binding, credential references,
  connector checks, compatible Slack/Jira/Confluence/Notion sources, preflight interpretations,
  clarification public API, independent review, fixed Codex refusal, disabled behavior, post-task
  linkage, scheduled capture/assurance, and three-act external writes.
- README links the guide and retains a concise happy path. `docs/mcp.md` copies both artifacts and
  distinguishes active-agent proposals, optional scheduled reasoning, and non-authoritative
  confidence. CONTRIBUTING records TDD, provenance, capability, reviewer, and write invariants plus
  the offline release proof.
- The repository action preserves its established clean-checkout GitHub source selection and exact
  read-only permissions while naming and ordering install, initialize, validate, combined capture,
  drift/assurance report, and artifact upload. A structural test prohibits proposal confirmation,
  conflict resolution, provider write commands, write permission, and credential echo. The complete
  same-project E2E separately proves configured MCP in combined scheduled assurance.

## TDD and debugging evidence

- Required initial two-file-only RED: `1 failed in 0.05s` at the absent harness bootstrap boundary.
- Provider composition first reached `1 failed in 2.32s` at the absent Jira target. A PRD-derived
  Jira artifact plus real preflight/reconciliation lifecycle then reached the production guarded-
  mutation configuration failure: `1 failed in 2.57s`, exact `approval_not_found`. After the narrow
  persisted-config fix and deterministic chronology correction, the provider phase passed.
- Final write-state validation exposed only the producer/validator MCP-write identity mismatch:
  `1 failed in 2.27s`; the bounded identity and payload-hash validation made the complete proof
  `1 passed in 2.88s`.
- Documentation/workflow structural RED was `2 failed in 0.63s` (missing adoption guide and missing
  ordered step contract); GREEN was `2 passed in 1.28s`.
- Affected source-role/bootstrap/clarification/preflight/governance/assurance/validation/MCP/write
  regressions: `284 passed in 6.13s`.
- Public-alpha plus complete journey: `4 passed in 4.60s` after formatting; final workflow/GitHub
  structural selection: `5 passed in 1.56s`.

## Release gates

- Focused Task 1–10 workflow/host/MCP/E2E gate: `473 passed in 8.32s`.
- Broad contract/integration/public-alpha/complete-E2E gate first exposed the established GitHub
  action pin (`1 failed, 725 passed in 20.22s`); after preserving that contract, the exact rerun was
  `726 passed in 19.77s`.
- Fresh full warnings-as-errors first exposed the same exact action step-shape pin
  (`1 failed, 1531 passed in 53.47s`); final authoritative rerun:
  `1532 passed in 53.37s`.
- Coverage collection: `1532 passed in 69.41s`; protected-artifact-excluded report is 15,515
  statements, 1,809 missed, 88% coverage across 116 tracked source files. No configured coverage
  threshold exists. The raw recursive coverage command auto-enumerated the protected untracked
  `dogfood 2.py` filename in its table; it was not opened manually, modified, deleted, staged, or
  included in the authoritative omitted report.
- Repository-wide Ruff check: clean after one import-order-only correction. Scoped Ruff format check
  is clean for all 11 changed/new Python paths. The newly mandated repository-wide format check
  exposes a pre-existing baseline of 66 untouched files that Ruff would reformat (161 already
  formatted); this task intentionally did not create a 66-file unrelated formatting change.
- Mypy with the protected untracked file excluded: `Success: no issues found in 116 source files`.
- `intent bootstrap --help`, `intent preflight --help`, `intent proposals --help`, and
  `intent mcp --help`: all exit 0.
- Scheduled workflow structural gate: `5 passed in 1.56s`.
- `git diff --check`: clean.

## Independent review fix round 1

The independent adversarial review returned `0 Critical / 4 Important / 1 Minor`, Not Ready. All
five findings were grouped into one tests-first round without adjacent expansion. Before production
or documentation fixes, the exact finding selector was `6 failed, 1 passed in 3.99s`; its failures
proved the configured MCP alias/canonical producer mismatch, three previously accepted conversation
identity tamper classes, the second transaction coordinator, and the guide's nonexistent
classification. The already-rejected conversation-ID tamper was the one passing parameter.

- Connector list/inspect now derives a credential-free, per-object
  `source_role_connector_ids` map from the reviewed profile, binding, scope, actor, and principal
  mapping. `sources add` accepts only an exact canonical producer identity when a configured alias
  covers more than one object type, and persists the exact ID used by MCP ingestion. A real Slack
  profile/binding and real fake-boundary captured record prove equality and strict ambiguity
  rejection through the installed public CLI.
- Validation now recomputes the conversation content hash, version material, external version, and
  evidence ID from the canonical payload and attributed producer fields. Equal-length mutations of
  content, claimed hash, external version, and ID all fail validation.
- Official in-memory `intent_status` supplies the E2E MCP graph version. The first grouped fix
  removed the second `load_runtime` and second coordinator, but still constructed an actor-specific
  Runtime wrapper; the final one-point correction below removes that wrapper as well.
- The adoption guide assigns the PRD role and selected canonical conversation-source role before
  `intent_bootstrap_propose` or human confirmation. Clean-project tests execute the documented
  init, bootstrap, role, and connector-inspection path. Insufficient evidence is truthfully
  described as a `new_or_ambiguous` result with explanatory questions, matching the public enum.

The same finding selector is now `7 passed in 5.91s`. Post-fix affected regressions are `51 passed
in 11.03s`; focused Task 1–10 is `473 passed in 7.82s`; broad is `727 passed in 21.04s`; and the sole
fresh full offline warnings-as-errors run is `1537 passed in 53.48s`. Accepted coverage remains
15,515 statements / 1,809 missed / 88% across 116 tracked source files with no configured threshold;
coverage was not rerun. Tracked plus new Ruff checks are clean, all 14 changed/new Python paths are
format-clean, protected-excluded mypy is clean across 116 source files, all four documented help
commands exit 0, both scheduled-workflow structural tests pass, and `git diff --check` is clean.

The first post-review mypy invocation used an ineffective leading-slash exclude regex and therefore
reported 117 source files, inadvertently including the protected untracked duplicate filename. It
printed no file contents and made no change. The command was corrected immediately to
`--exclude 'dogfood 2\\.py$'`, producing the authoritative 116-file clean result; the protected file
remains unmodified, unstaged, and uncommitted.

### Final one-point single-Runtime correction

The same review found one remaining literal composition violation: `dataclasses.replace` still
constructed a second Runtime wrapper even though it shared the first Runtime's stores and
coordinator. A tests-only identity assertion over proposer/reviewer workflow catalogs and both MCP
read/mutation services failed exactly at `all_services_hold_runtime` (`1 failed in 2.62s`).

`WriteWorkflow` now carries an authenticated actor snapshot read from the strict live project
configuration during guarded workflow assembly. Construction verifies unchanged project and graph
identity and requires the actor to exist in the live mutation policy. Preview, interactive approval,
and execution use that snapshot; the MCP mutation boundary independently reloads live configuration
and requires its actor to equal the workflow snapshot before deriving configured and policy aliases.
This preserves contributor/approver/executor roles, provider-principal ACL checks, independent-review
enforcement, and live configuration rejection without mutating Runtime or its cached config.

The harness now calls `write_workflow`, `McpReadServices`, and `McpMutationServices` with the one
original Runtime object for both contributor and reviewer phases. There is no `replace`, second
Runtime, second load, or second coordinator. The exact regression is `1 passed in 3.63s`; the full
finding selector is `7 passed in 5.08s`; Task 10 E2E/public-alpha is `5 passed in 6.12s`; and guarded
CLI/MCP write regressions are `22 passed in 1.83s`. Scoped Ruff is clean, all 19 changed/new Python
paths are format-clean, protected-excluded mypy is clean across 116 files, and diff check is clean.
The still-fresh authoritative full suite remains `1537 passed in 53.48s` and was not repeated because
the affected gates remained green; accepted 88% coverage was likewise not repeated.

The same reviewer's final one-point re-review returned `0 Critical / 0 Important / 0 Minor`, Ready.
No additional fix round was required.

## Handoff state

The product commit is `1cca7b18dcb813e90a6f7b1d710ffa163e8294aa` on the Task 10 branch. The
five protected artifacts remain untracked and were not edited, deleted, staged, or committed. The
final same-reviewer verdict is Ready with no findings; only this metadata commit remains at the time
of writing.
