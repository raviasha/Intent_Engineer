# Task 4 report — CLI and MCP intent onboarding

## Status

REVIEW APPROVED after independent-review Fix Round 1. All six Important findings are addressed,
the scoped re-review found no new Critical, Important, or Minor issue, and all required gates are
complete. The one known YAML lexical-preservation Minor remains explicitly deferred.

Execution base: `ef1c2ead86c634f818b78fac8b93d2b5b722c53a`.

Task authority retained with this report:
`.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-4-brief.md`.

## Implementation

- Added `intent bootstrap --prd`, using descriptor-rooted nonblocking reads of regular,
  single-link relative Markdown files. Capture normalizes through the Markdown connector and
  associates immutable evidence through the production evidence store. The response is a bounded
  `agent_submission_required` context packet and contains no invented semantic candidate or
  proposal.
- Added fail-closed path, suffix, exclusion, special-file, bounded descriptor-read, post-read
  identity/content reauthentication, and workspace snapshot checks. Identical content capture is a
  semantic and byte no-op even when only the filesystem mtime changes.
- Added `intent sources add`, validating the real connector catalog/config and connector-specific
  canonical path or URI scope before a preimage-checked atomic config write. Configured MCP IDs,
  configured dynamic GitHub repository IDs, ProjectConfig canonical ordering, exact-scope
  override, idempotent replay, and concurrent replacement rejection are preserved.
- Added ACL-projected, detached, bounded `intent proposals list|show` output and TTY-only
  `proposals confirm`. Confirmation emits the complete reviewed preview before prompting, requires
  the exact digest phrase, rechecks config/proposal/graph state, and delegates activation to the
  Task 3 service. It creates no approval or external write.
- Added provider-neutral `IntentWorkflowPort` registration for only
  `intent_bootstrap_propose`, `intent_proposal_show`, and `intent_proposal_confirm`. Registration is
  optional and additive; omission retains the prior read surface and production stdio composes the
  port over the same held runtime as existing read/mutation services.
- Added strict bounded Pydantic MCP request models, including the exact published Task 3
  `BootstrapSubmission` schema and typed detached port delegation, truthful tool annotations, fixed
  validation and handler boundaries, cancellation preservation, and wire/log/traceback secrecy
  checks. No approval-minting or external-write workflow tool exists.
- Updated runtime config decoding through strict JSON validation so canonical serialized source
  roles round-trip from YAML without weakening strict model validation.

## TDD evidence

### Required RED

The CLI E2E and official installed-SDK MCP contract tests were authored before production edits.
After correcting only a test fixture assumption about an optional fresh-project history file, the
exact required command was run:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py -q -W error
```

Exact intended result:

```text
9 failed in 2.18s
```

The failures showed that `intent bootstrap` and `intent sources` were absent (CLI exit 2) and
`build_server` did not accept `intent_workflow_services`.

Further test-first hardening covered special files, oversize requests, real confirmation,
production port delegation, fixed handler/wire boundaries, cancellation, and stdio composition.

### Focused GREEN

Initial exact focused GREEN:

```text
12 passed in 13.90s
```

After cancellation and wire-boundary hardening, focused plus MCP stdio was:

```text
15 passed in 27.04s
```

Final required focused/legacy command, including explicit swapped-path and concurrent-config
replacement cases:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_cli_local.py tests/e2e/test_mcp_server.py -q -W error
52 passed in 25.50s
```

The two initial strengthened adversarial cases pass independently: `2 passed in 0.31s`.

### Independent-review Fix Round 1

The first independent verdict was 0 Critical / 6 Important / 1 Minor: Not Ready. Before any
fix-round production edit, regressions for post-read path replacement, bounded/growing regular-file
reads, touch-only replay, unchanged-config replacement, configured provider/GitHub URI scopes, and
the exact typed MCP submission/schema boundary were added. The exact Task 4 pair produced:

```text
9 failed, 12 passed in 5.25s
```

The nine failures mapped directly to the six findings. Final fix-round focused evidence:

```text
tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py
21 passed in 4.48s

tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py tests/e2e/test_cli_local.py tests/e2e/test_mcp_server.py
58 passed in 23.28s
```

Adjacent descriptor/evidence/runtime/catalog/GitHub/read-MCP regressions are `119 passed in
5.71s`; mutation-MCP and source-model regressions are `33 passed in 2.21s`.

## Final gates

All required help commands exited 0:

```text
.venv/bin/intent bootstrap --help
.venv/bin/intent sources --help
.venv/bin/intent proposals --help
```

Required Ruff command:

```text
.venv/bin/ruff check src/intent_engineering/cli/intent_workflow.py src/intent_engineering/integrations/mcp_server/intent_workflow.py tests/e2e/test_cli_intent_bootstrap.py tests/contract/mcp/test_intent_workflow_tools.py
All checks passed!
```

Type gate:

```text
.venv/bin/mypy src
Success: no issues found in 108 source files
```

Fresh full offline warnings-as-errors gate:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
1157 passed in 43.41s
```

One preceding full run had a single existing FIFO subprocess test exceed its hard two-second
startup timeout (`1 failed, 1148 passed in 191.42s`); the test passed immediately in isolation and
the fresh full rerun above is clean. An earlier self-authored test-isolation issue that left
structlog bound to closed pytest capture was fixed with explicit test teardown and verified by
running Task 4 CLI tests before a representative sync module (`6 passed in 13.72s`).

`git diff --check` completed with no output. The index is empty.

## Changed files

- `src/intent_engineering/cli/intent_workflow.py`
- `src/intent_engineering/storage/secure.py`
- `src/intent_engineering/integrations/mcp_server/intent_workflow.py`
- `src/intent_engineering/cli/app.py`
- `src/intent_engineering/cli/runtime.py`
- `src/intent_engineering/integrations/mcp_server/server.py`
- `tests/e2e/test_cli_intent_bootstrap.py`
- `tests/contract/mcp/test_intent_workflow_tools.py`
- `tests/e2e/test_mcp_server.py`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-4-report.md`
- `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/progress.md`

## Self-review

- CLI bootstrap captures source evidence but never performs semantic invention; only the typed MCP
  submission boundary can invoke Task 3 proposal creation.
- Descriptor-rooted reads cap bytes at limit-plus-one while reading, clear partial chunks, reject
  growing/oversize input promptly, and reauthenticate the original path identities and exact
  content immediately before evidence association. Fixed public failures do not echo content or
  sensitive paths.
- Content-identical replay reuses the durable immutable record, so an mtime-only touch cannot create
  an association conflict or change evidence bytes.
- Source-role writes compare exact config preimages under the shared path lock and use race-rejecting
  atomic replacement, including the unchanged branch. Unrelated config semantics survive and
  canonical model validation sorts roles. Configured provider URI and GitHub repository scopes are
  validated offline without provider calls.
- Proposal output is produced only after Task 3 ACL review and contains detached typed values. Final
  decisions are hidden like missing proposals.
- CLI and MCP confirmation both recheck the reviewed digest and current workspace state and delegate
  canonical activation; neither can create an approval or call an external provider.
- Optional MCP registration does not change the omitted legacy surface. Raw request objects are
  validated against the exact Task 3 schema, bounded, converted to detached typed models, and
  cleared at fixed pre-handler/handler boundaries; cancellation retains its signal.
- No Task 5 preflight behavior was introduced and no provider/network call is present.
- The five protected untracked artifacts remain unstaged and were not read, modified, renamed, or
  deleted.

## Concerns

No Critical or Important concern is open.

The reviewer’s Minor is deliberately deferred: source-role updates serialize the full semantic
config with `yaml.safe_dump`, so unrelated YAML comments and lexical formatting are not preserved
byte-for-byte. Every unrelated semantic field is preserved. A bespoke comment-preserving YAML
rewriter is not warranted in this fix round.

## Independent review

Initial verdict: 0 Critical / 6 Important / 1 Minor, Not Ready.

Fix Round 1 scoped re-review verdict: **Ready** with 0 new Critical / 0 new Important / 0 new
Minor. The reviewer confirmed that all six Important findings are resolved. Independent evidence
was 21 fix-focused tests, 20 shared descriptor/storage/MCP regressions, a clean diff check, and the
reported fresh full offline result of 1157 passing tests. The known YAML comment/lexical-formatting
Minor remains deferred as documented above.
