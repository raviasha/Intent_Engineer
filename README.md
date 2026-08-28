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
4. **Assure** that current code and tests remain aligned with current intent and
   requirements, opening evidence-backed cases for detected gaps.
5. **Reconcile** evidence-backed divergence with a human review step before a
   graph-changing resolution.

The supplied [framework graph](graph/framework-intent-graph.yaml) dogfoods the
same graph model used for a project.

## Quick start

Requires Python 3.12 or newer.

```bash
python -m pip install -e '.[dev]'
intent init --project .
intent bootstrap --prd docs/prd.md --format json
intent sources add markdown docs/prd.md --role declared_intent
intent proposals list --format json
intent sync --sources markdown,git
intent status
intent context --task "add local export"
intent preflight --task "add local export" --format json
intent drift --require-review
```

Bootstrap first captures the PRD and asks an active agent for a typed proposal; it does not silently
invent canonical intent. Review and confirm that proposal before treating its core as active. The CLI
`preflight` command is diagnostic only and never mints a mutation capability. See the executable
[intent-aware agent adoption guide](docs/intent-aware-agent.md) for source roles, compatible
Slack/Jira/Confluence/Notion setup, clarification, independent review, host support, post-task
evidence, and scheduling.

For team conversations and external requirements, configure GitHub and/or a compatible MCP source,
then schedule capture independently from reconciliation:

```bash
# Frequent capture: append new source-authored versions and refresh derived state.
intent sync --sources markdown,git,github,mcp

# Your review cadence: compare intent, requirements, code, and tests.
intent drift --format markdown --output .intent/reports/drift.md --require-review
```

Each source version retains its original author, provider identity, timestamp, locator, content
hash, predecessor, and ACL. Multiple teammates' changes remain separate evidence versions; a
conflict becomes a case with both sides rather than a last-write-wins overwrite. Authorized low-risk
projection can be automatic, while conflicting, superseding, destructive, and external-provider
changes require human review. External writes additionally require `intent write preview`, a
separate interactive `intent write approve`, and `intent write execute` against the unchanged
target. See [the MCP and agent guide](docs/mcp.md) and
[provider-profile guide](docs/provider-profiles.md).

Active-agent reasoning may submit evidence-backed proposals. Scheduled semantic inference is
optional and can only open grounded review cases; confidence is not a claim of truth and never
authorizes canonical changes. Mandatory Codex mutation coverage is not currently available, so no
Codex plugin is shipped. Disabling host integration leaves ordinary coding behavior unchanged.

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
fail-closed for the configured local actor. Hosted OAuth, GitHub App installation, webhooks, a
collaboration UI, and unattended external writes are not shipped. Conversation capture is
polling/manual/scheduled, not a hosted continuous daemon. See [the GitHub guide](docs/github.md).

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
