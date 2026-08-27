# Contributing

## Environment

Use Python 3.12 or newer and install the development dependencies in an isolated
environment:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

On a clean checkout, initialize the repository's ignored local workspace once,
then run the release checks. Validation reads the complete `.intent/` state
(configuration, configured graph, evidence, cases, ChangeSets, checkpoints, and
transaction recovery state), so it intentionally fails before initialization.

```bash
.venv/bin/intent init --project .
.venv/bin/ruff check .
.venv/bin/mypy src/intent_engineering
.venv/bin/pytest --cov=intent_engineering --cov-report=term-missing
.venv/bin/intent validate --project .
```

## GitHub slice

GitHub tests and release evidence are offline and use injected fake HTTP; never
use a live token or `gh` login in the test suite. The local GitHub workflow and
the repository Action are documented in [docs/github.md](docs/github.md). Keep
the Action read-only, retain its clean-checkout initialization step, and do not
claim hosted OAuth, GitHub Apps, webhooks, or pull-request comments as shipped functionality.

## MCP and guarded write-back

MCP tests use the official SDK with deterministic fake sessions; never invoke a live provider.
Profiles and bindings are strict semantic contracts and examples contain only environment
references. Preserve source authorship and version chains in evidence. Do not collapse competing
authors into one mutable record. A mutation surface may propose or preview, but no Intent MCP tool
may create its own approval. Provider writes must reuse the production preview, interactive local
approval, executor, target-version check, receipt, and atomic commit services.

The release harness proves a missing approval and a changed target make zero provider mutation
calls, a second identical sync is a no-op, and sentinel credentials occur in no persisted project
file or captured output.

## Test-driven changes

Write a focused regression or feature test before production code. Run it and
record the expected failing result, make the smallest implementation change that
passes it, then run the focused test again. Run the full suite before committing.
Tests use fixed timestamps, explicit inputs, and no network or model provider.

For intent-aware agent changes, preserve these invariants:

- Capture the attributed human request and agent reasoning before preflight; never infer canonical
  intent from an unattributed prompt.
- Keep source author, provider version, predecessor, locator, timestamp, role, and ACL explicit.
- Build every semantic update as an evidence-backed `ChangeSet`; no direct canonical graph edits.
- Treat confidence as review metadata, never as authority to confirm, approve, resolve, or write.
- Mint mutation capability only from a validated aligned/mechanical preflight, bind it to exact
  repository/task/graph/path state, keep it process-local, and revoke it after completion.
- Require a distinct authoritative reviewer for conflicting or high-risk proposals. MCP and
  scheduled jobs cannot create approvals.
- Keep provider preview, interactive approval, and execution separate. Re-read the target and reject
  drift before one at-most-once mutation.
- Preserve the fixed unsupported mandatory-Codex result and disabled host no-op until an audited host
  can prove complete synchronous mutation interception.

## Offline intent-aware release proof

The complete operating proof uses one ordinary initialized repository and fakes only external
HTTP/MCP/model/supported-host/terminal boundaries. Run its focused and broad gates with plugin
autoload disabled:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/e2e/test_intent_aware_agent_workflow.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/unit/intent_workflow tests/integration/intent_workflow tests/contract/agent_host \
  tests/contract/mcp/test_intent_workflow_tools.py \
  tests/e2e/test_intent_aware_agent_workflow.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/contract tests/integration tests/e2e/test_public_alpha.py \
  tests/e2e/test_intent_aware_agent_workflow.py -q -W error
```

Before release, also run the full warnings-as-errors suite, coverage collection, Ruff check and
format check, `mypy src`, the documented CLI `--help` commands, the scheduled-workflow structural
test, and `git diff --check`. All tests remain offline; never substitute a live token, provider, or
model. The proof must include rejected/no-op byte stability and sentinel scans across regular project
files, captured output/logs/errors, and traceback locals.

## Fixture conventions

The reconciliation matrix lives in `tests/fixtures/` and is materialized by
`tests/helpers/fixtures.py`. Fixture repositories are tiny and deterministic:
their generated Git commits have fixed identity and timestamps, source metadata
uses explicit `intent_assertion` and `detection_input` payloads, and no nested
`.git` directory is tracked in fixture source. A fixture must prove one intended
classification (or aligned no-case behavior); idempotency fixtures must prove a
second sync has zero evidence, graph, and case mutations.

## Semantic ChangeSets

Never edit canonical graph YAML to implement a semantic change. Build a validated
`ChangeSet` against the current graph version with stable subject IDs and explicit
evidence references, then apply it through the graph store. A semantic ChangeSet
must have evidence; adds, updates, and supersessions for the same node or edge
cannot coexist. Graph bytes are durable before append-only history. Reconciliation
resolution validates authorized evidence and uses the documented preview/approval
flow before a graph-changing commit.

Generated renderings are non-canonical cache output. Keep the domain layer free
of connector and cloud-provider dependencies, preserve ACL-safe projections, and
do not add cloud or model fallbacks to local commands.
