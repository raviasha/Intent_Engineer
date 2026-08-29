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

## Guided developer journey

Requires Python 3.12 or newer.

```bash
python -m pip install intent-engineering
codex plugin marketplace add .
codex plugin add intent-advisor@intent-engineering-local
```

Restart the ChatGPT desktop app and start a new task rooted in the target repository. Codex
launches the plugin-owned MCP server from `plugins/intent-advisor/.mcp.json`; do not start an
additional server process for the plugin. First obtain explicit human consent and confirm the
exact PRD path; only then run the advancing
`intent onboard ... --yes` command below. It captures that PRD as immutable declared-intent evidence
and returns the public bootstrap-proposal next action. Running the same command without
`--yes` is only an offer/no-op diagnostic and does not advance onboarding. Onboarding never invents
or activates canonical intent by itself:

```bash
intent onboard --project . --prd docs/PRD.md --yes
```

### Approve the baseline

Inspect the exact proposal before confirming it:

```bash
intent proposals list --project . --format json
intent proposals show <proposal-id> --project . --format json
intent proposals confirm <proposal-id> --project . --format json
```

Confirmation is interactive and digest-bound; neither `--yes` on onboarding nor the plugin can
manufacture it. Existing automation may still use `intent init --project .`,
`intent bootstrap --prd docs/prd.md --project . --format json`, and
`intent sources add markdown docs/prd.md --role declared_intent --project .`.

### Use ordinary prompts

The validated advisory bundle installed above is `plugins/intent-advisor`. Open **Plugins
Directory**, select **Intent Engineering Local**,
enable `intent-advisor`, and review/trust its `UserPromptSubmit` hook. The marketplace keeps the
bundle path explicit as `source.path: "./plugins/intent-advisor"`. The plugin MCP configuration
intentionally omits `cwd`: on a local Codex host, `--project .` binds to the active repository's
working directory. A remote executor must explicitly reproduce or configure that repository
working directory; it must not assume `.` refers to the user's checkout.

The bundle automatically offers onboarding when no approved baseline exists. After approval it
asks for `intent_context`, then routes the agent's bounded classification draft through token-free
`intent_advisory_preflight`.

The advisory classifications are `no_semantic_impact`, `aligned`, `new_or_ambiguous`, and
`conflicting`. Continue normal work for the first two. The plugin never receives a mutation
capability and does not provide mandatory mutation enforcement. Mandatory Codex mode remains
truthfully unavailable through `MandatoryHookUnavailable` with the message
`Codex mandatory mutation hook is unavailable`.

To opt out, decline the onboarding offer or disable/uninstall `intent-advisor`. A decline leaves
the repository unchanged for that task; disabling the plugin leaves ordinary coding behavior
unchanged. The CLI and MCP services remain usable independently.

Clients other than Codex that launch and connect stdio themselves may run
`intent mcp --project .`; that is a client-managed process, not an extra Codex setup step.

### Clarification and review

For `new_or_ambiguous`, answer every persisted question before the agent calls
`intent_clarification_answer`, `intent_clarification_propose`, and
`intent_clarification_confirm`. Answers stay attached to their active session and are not
recursively classified as new requirements. For `conflicting`, stop implementation and require the
configured independent human review before any graph change.

### Scheduled CLI assurance

The plugin is not needed for assurance. Run the deterministic CLI sequence locally, from cron, or
through `.github/workflows/intent-sync.yml`:

```bash
intent status --project . --format json --require-baseline
intent validate --project .
intent sync --project . --sources markdown,git,github
intent drift --project . --format markdown --output intent-drift.md
```

The sequence captures implementation and test-file evidence and opens review cases; it does not
issue authorization or silently rewrite the approved graph. See the executable
[intent-aware agent adoption guide](docs/intent-aware-agent.md) for source roles, compatible
Slack/Jira/Confluence/Notion setup, clarification, independent review, post-task evidence, and
scheduling.

The workflow deliberately fails with `onboarding_required` on a clean checkout unless an approved
`.intent` baseline is restored first or the job runs in a persistent/self-hosted workspace. It
never initializes and reports a clean graph version 0.

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
authorizes canonical changes. The shipped `intent-advisor` plugin is advisory only. Disabling it
leaves ordinary coding behavior unchanged, while mandatory Codex mutation coverage remains
unavailable.

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
