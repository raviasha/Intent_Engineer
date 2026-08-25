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
claim hosted OAuth, GitHub Apps, webhooks, pull-request comments, external
writes, or MCP write-back as shipped functionality.

## Test-driven changes

Write a focused regression or feature test before production code. Run it and
record the expected failing result, make the smallest implementation change that
passes it, then run the focused test again. Run the full suite before committing.
Tests use fixed timestamps, explicit inputs, and no network or model provider.

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
