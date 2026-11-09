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
5. **Reconcile** meaningful divergence in the conversation, preserving disagreements
   and recording clear decisions through validated ChangeSets.

The supplied [framework graph](graph/framework-intent-graph.yaml) dogfoods the
same graph model used for a project.

New to the project? Start with the [five-minute user guide](docs/quick-start.md) for installation,
onboarding, everyday development, graph assessment, team enrollment, and CI assurance.

## Guided developer journey

Requires Python 3.12 or newer.

```bash
git clone https://github.com/raviasha/Intent_Engineer.git /absolute/path/to/Intent_Engineer
python -m pip install /absolute/path/to/Intent_Engineer
codex plugin marketplace add /absolute/path/to/Intent_Engineer
codex plugin add intent-advisor@intent-engineering-local
cd /absolute/path/to/target-repository
```

Wheel-only installs such as `python -m pip install intent-engineering` provide the CLI and MCP
server, but not the repository-shipped plugin marketplace or advisor assets. Use the source
checkout sequence above when installing the Codex plugin.

Restart the ChatGPT desktop app and start a new task rooted in the target repository. Codex
launches the plugin-owned MCP server from `plugins/intent-advisor/.mcp.json`; do not start an
additional server process for the plugin. On first use, the agent offers to create the intent
graph in a separate chat and connect sources such as Notion, Confluence and Slack. Accepting
opens a setup chat for source selection, connection and graph creation while the original
task continues. Deferring schedules another offer after three completed work turns.
Normal work with an existing graph gets a lightweight relevance check.
Unrelated and aligned tasks proceed silently. When relevant intent is missing, unclear or
conflicting, the agent shows its understanding or the specific conflict and asks one
consequential question in the chat. It reuses your answers and maintains clear intent
through validated ChangeSets with source evidence and history. No routine dashboard,
graph approval, WebAuthn, PRD file or export is required. Code/test matching is deferred.

Existing pending proposals remain pending; upgrading never fabricates their approval.
Chat statements are recorded as agent-reported user evidence, separately from inferences.
The CLI fallback works before the MCP server has a project workspace:

```bash
intent background --project . --task "Add private reports" --session-id chat:example
intent background --submission-schema
intent background --project . --submission background-draft.json
```

The schema describes bounded interpretations, questions, quoted answers and graph updates.
Missing workspaces are initialized only when relevant intent is captured. Context reads and
unrelated tasks do not initialize them. Graph reports/exports remain explicit commands.

The host creates the setup chat from `intent background-onboarding --prepare` and records
its returned ID with `--chat-id`. Authorized connector reads can be captured using
`intent background-source --submission-schema` and `--submission <relative-json-path>`.
Source revisions, authors, locations and exact content become immutable evidence; new
authorized reads can update the graph. Provider authentication happens through the host's
connection tools. This does not install a continuous remote polling service.

### Optional governed review

For manual setup from an existing PRD, launch the local review flow:

```bash
intent dev --project . --prd docs/PRD.md --offline
```

The command initializes a missing `.intent` workspace, captures the PRD as immutable
`declared_intent` evidence, starts exactly one repository-bound process on an ephemeral loopback
port, and opens the packaged review UI. It does not invent an agent proposal or approve canonical
intent. Register the local device with WebAuthn in the UI; when the active agent submits the typed
bootstrap proposal, review its complete evidence and selected nodes there before activating it.
Use `--no-open` on a headless terminal and `intent dev --project . --status` to inspect the held
process. Re-running `intent dev` reuses only a live process that proves the same repository
identity. The metadata in `.intent/cache/control-plane.json` is a non-authoritative locator and
contains no WebAuthn credential or decision authority.

`intent dev` is the Milestone 1 single-user local control plane. Team identity enrollment and
shared review state arrive in Milestone 3; do not treat the current local credential or cache as
a team identity system.

The compatible granular onboarding diagnostic remains available for existing automation:
`intent onboard --project . --prd docs/PRD.md --yes`.

### Approve the baseline

The browser flow is the primary confirmation path. The granular commands remain available as
advanced diagnostics for automation and recovery:

```bash
intent proposals list --project . --format json
intent proposals show <proposal-id> --project . --format json
intent proposals confirm <proposal-id> --project . --format json
```

CLI confirmation remains interactive and digest-bound; neither PRD capture nor the plugin can
manufacture it. Existing automation may still use `intent init --project .`,
`intent bootstrap --prd docs/prd.md --project . --format json`, and
`intent sources add markdown docs/prd.md --role declared_intent --project .`.

### Use ordinary prompts

The validated advisory bundle installed above is `plugins/intent-advisor`. Open **Plugins
Directory**, select **Intent Engineering Local**,
enable `intent-advisor`, and review/trust its `UserPromptSubmit` and `Stop` hooks. The marketplace keeps the
bundle path explicit as `source.path: "./plugins/intent-advisor"`. The plugin MCP configuration
intentionally omits `cwd`: on a local Codex host, `--project .` binds to the active repository's
working directory. A remote executor must explicitly reproduce or configure that repository
working directory; it must not assume `.` refers to the user's checkout.

The default hook uses `--preset background`: no dashboard startup, shared-state fetch,
or repository-wide gate for pending reviews. It returns bounded, opaque chat/turn identifiers
and background guidance without capturing the raw prompt. The agent consults
`intent_background_context` when needed and submits clear interpretations or consequential
questions through `intent_background_submit`. Ordinary chat answers can advance this flow.
The deterministic service checks references, access, graph versions and provenance before
atomic persistence; it never creates authentication or approval records.

The legacy `--preset developer` readiness flow, team trust, reviewed proposals and external
writes remain available explicitly. Their independently authenticated approval contracts are
unchanged. Mandatory Codex enforcement remains unavailable through
`MandatoryHookUnavailable`; the advisory hook does not claim to enforce repository mutations.
Disable the plugin to stop background guidance. CLI and MCP remain independently usable.

While `intent dev` is running, it polls bounded Git and test-result evidence at a fixed cadence
without waiting for a browser request. The Home view shows the latest detached observation and
offers an explicit **Run reviewed tests** action for command IDs from reviewed project
configuration. The write uses the same-origin/CSRF boundary; it supplies an identifier, never a
shell command, and neither polling nor test execution asserts implementation completion.
The explicit legacy `--preset developer` path can start a headless service without persisting
its CSRF bootstrap. The default background hook starts no service. Running ordinary `intent dev`
later securely replaces an attested headless owner, opens a browser with a new in-memory bootstrap,
and keeps the protected write actions usable;
`intent dev --status` and `intent dev --no-open` continue to reuse the existing process.

### Required GitHub check

The separate PR workflow, [intent-check.yml](.github/workflows/intent-check.yml), exposes the
required status **`Intent Engineering / check`**. It installs the package, restores the signed and
encrypted approved baseline, runs every reviewed `test_commands` argv from that baseline, writes
canonical evidence for the checkout's exact Git commit, and runs:

```bash
intent check --ci --require-review --test-results .intent-ci/test-results.json
```

Passing tests are evidence, not human approval. Missing trust or baseline, failing tests, invalid
evidence, and unresolved review work fail the job. Disabling the local plugin does not disable
this GitHub backstop. The nightly/manual `intent-sync.yml` remains separate: it captures configured
Markdown, Git, and GitHub sources, validates state, renders and uploads the drift report, then
runs the review-required check. Pending review fails the run after producing its report.

This workflow requires operator setup; committing it does not enable branch protection. Configure
the protected `intent-ci` environment, supply the CI recipient key and pinned signing-key trust,
publish compatible approved encrypted state to protected `intent-state`, and require the exact
status above on your code branches. A bare checkout or a locally manufactured `.intent` directory
cannot satisfy CI. See [CI setup and trust prerequisites](docs/intent-aware-agent.md#required-github-check-setup)
before enabling it. Automated key enrollment and publication setup remain future work.

Clients other than Codex that launch and connect stdio themselves may run
`intent mcp --project .`; that is a client-managed process, not an extra Codex setup step.

### Clarification and review

The default background flow asks in the chat and retains quoted answers as versioned
evidence. Clear meaning is maintained automatically. Agent inferences remain inferred;
existing disputed positions and old pending proposals are preserved. A conversational
conflict can be resolved by the answer to that question, with an auditable ChangeSet and
without a second approval prompt. Cross-author disputes remain separate positions.

The optional governed workflow still uses independently authenticated local review for
its legacy proposals and publication. See the plugin's
[governed workflow reference](plugins/intent-advisor/skills/intent-advisor/references/reviewed-workflow.md).

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

Inspect the current visible graph's deterministic robustness without changing it:

The default assessment focuses on intent versus requirements. In Intent Dev, select a scorecard
and choose **Read intent and requirements** for cited context, assumptions and outcome coverage.
The same coverage review appears alongside proposed graph changes. Scores measure structural
completeness; they do not prove that a requirement satisfies its intended outcome.

```bash
intent assess --project . --format json
intent assess --project . --focus <node-or-branch-id> --format json
```

The scorecard is derived, explainable, ACL-filtered, non-canonical, and model-independent. Green
requires a score and confidence of at least 75 with no blocking conflict; orange covers scores
50–74 or insufficient confidence; red is below 50 or blocked. Non-applicable dimensions are `N/A`,
unsupported inputs remain `unassessed`, and a critical red path caps branch and project robustness
at 49. The CLI, read-only MCP tools, and CI comparison share the same semantic report digest. See
[the assessment guide](docs/assessment.md) for dimensions, exact exit codes, and CI defaults.

Improve the graph when you have time; this does not require a new development prompt:

```bash
intent refine --project . --minutes 5
intent refine --project . --session <session-id> --action pause --format json
intent refine --project . --session <session-id> --action resume --format json
```

The same workflow is available as **Improve graph** in `intent dev`. It asks one deterministic,
gap-linked question at a time. An explicit answer is captured immediately as ACL-scoped evidence;
the session ledger keeps only its evidence reference. Pause/restart/resume is durable. Neither an
answer nor an enrichment session changes the canonical graph. A proposed change still enters the
existing human-governed proposal and approval workflow. MCP exposes only bounded status and
next-question reads—never answer, skip, pause, resume, propose, approve, or mutation authority.
See [the assessment guide](docs/assessment.md) for the CLI and MCP response contract.

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

## Current release boundary

Included now are explainable assessment and visualization, resumable progressive enrichment,
grounded simpler/manual/narrowed/reuse requirement alternatives, CLI/browser/read-only-MCP
journeys, deterministic validation, and the existing reviewed guarded
enrollment foundation. The local assessment and enrichment path works offline; configured remote
evidence sources still require their provider connection. The `intent-advisor` plugin can suggest
when to inspect or clarify intent, but it cannot provide human approval or bypass repository ACL,
proposal, reconciliation, or release controls.

Deferred follow-up work is explicitly not shipped or reachable: requirement-to-code/test matching,
automatic activation beyond the existing reviewed governance boundaries, and the broader
state-hardening backlog.

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
