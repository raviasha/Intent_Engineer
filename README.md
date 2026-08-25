# Intent Engineering

Intent Engineering keeps a local, evidence-backed graph of why software exists,
what it should do, what code and tests currently show, and where those sources
disagree. A mismatch is not an automatic verdict against code or requirements:
it becomes a reviewable reconciliation case with its supporting evidence.

## The local loop

1. **Capture** versioned Markdown and Git evidence.
2. **Manage** typed intent, requirement, decision, implementation, and test
   assertions in a provenance-backed graph.
3. **Sync** fresh evidence through deterministic validation and mapping.
4. **Reconcile** evidence-backed divergence with a human review step before a
   graph-changing resolution.

The supplied [framework graph](graph/framework-intent-graph.yaml) dogfoods the
same graph model used for a project.

## Quick start

Requires Python 3.12 or newer.

```bash
python -m pip install -e '.[dev]'
intent init --project .
intent sync --sources markdown,git
intent status
intent context --task "add local export"
intent drift --require-review
```

`intent reconcile resolve <case-id>` is deliberately two-phase. The first call
records a deterministic preview and returns an approval hash with review-required
exit status. Re-run it with the exact `--approve <hash>` to apply the approved
ChangeSet and resolve the case. `defer` and `mark-false-positive` are terminal
case actions and do not mutate the graph.

## What classifications mean

`CODE_LAG` means a current, active requirement is newer than mapped
implementation evidence. For example, requirement version 2 says exports must
remain local-first while implementation evidence remains at version 1.

`REQUIREMENT_LAG` means an older requirement is contradicted by a newer explicit
decision that is already reflected by both implementation and test evidence. For
example, a signed decision version 2 changes an export workflow and code/tests
are also version 2; the earlier requirement deserves review rather than blaming
the code.

Other local deterministic classifications include `TEST_LAG`,
`UNDOCUMENTED_CODE`, `AMBIGUOUS_DIVERGENCE`, and `CONFLICTING_SOURCES`.

## Context for coding agents

Use `intent context --task "..." --format json` to provide a compact,
task-specific packet of relevant intent, requirements, decisions, code/test
references, evidence, and unresolved reconciliation cases. `intent explain
<reference>` exposes the authorized supporting evidence for a node, case, or
evidence ID.

## Local and cloud boundary

Local Markdown and Git workflows run entirely from local files and need neither
a network connection nor a secret. GitHub ingestion is opt-in: it uses a local
credential and an explicit, non-secret `GITHUB_REPOSITORY=owner/repository`
scope. The repository-local GitHub Action provides manual/nightly scheduling; it
is not a hosted Intent Engineering service. Evidence ACLs are enforced
fail-closed for the configured local actor. Hosted OAuth, GitHub App
installation, webhooks, collaboration UI, and external MCP writes are not
shipped in this slice. See [the GitHub guide](docs/github.md).

## Development

A clean checkout does not contain project-local canonical state. Initialize its
ignored `.intent/` workspace once before asking the production validator to
inspect it; `intent validate` validates that workspace, not the separately
tracked framework-graph artifact.

```bash
intent init --project .
ruff check .
mypy src/intent_engineering
pytest --cov=intent_engineering --cov-report=term-missing
intent validate --project .
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for TDD, fixture, and ChangeSet rules,
and [AGENTS.md](AGENTS.md) for the required repository reading order.
