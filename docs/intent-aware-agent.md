# Intent-aware agent adoption

Intent Engineering adds a local review layer to an existing repository. It captures immutable
source versions, builds a reviewed intent/requirements provenance graph, gives an active agent
task-specific context, and records disagreements for humans. It does not replace Git, your coding
agent, or provider permissions.

## 1. Install and onboard an existing repository

Python 3.12 or newer is required. Install from an explicit Intent Engineering source checkout,
then change to the repository you want to govern:

```bash
git clone https://github.com/raviasha/Intent_Engineer.git /absolute/path/to/Intent_Engineer
python -m pip install /absolute/path/to/Intent_Engineer
codex plugin marketplace add /absolute/path/to/Intent_Engineer
codex plugin add intent-advisor@intent-engineering-local
cd /absolute/path/to/target-repository
```

Wheel-only installs such as `python -m pip install intent-engineering` provide the CLI and MCP
server, but not the repository-shipped plugin marketplace or advisor assets. Restart the ChatGPT
desktop app and start a new task rooted in the target repository. Codex launches the plugin-owned MCP server from
the bundle configuration; do not start an additional process for it. First obtain explicit human consent
and confirm the exact PRD path; only then run the advancing `intent onboard ... --yes` command
below. A decline leaves the repository byte-unchanged and disables intent-aware classification only
for that task. Running onboarding without `--yes` is only an offer/no-op diagnostic: it does not
initialize `.intent/`, read the PRD, capture evidence, or assign `declared_intent`. The consented
first run returns `proposal_required` and names the bootstrap-proposal action; it does not manufacture
an agent proposal or human approval.

```bash
intent onboard --project . --prd docs/PRD.md --yes
```

## 2. Approve the baseline

The active agent reads the captured evidence and submits a typed `BootstrapSubmission` through
`intent_bootstrap_propose`. Inspect and confirm the exact proposal locally:

```bash
intent proposals list --project . --format json
intent proposals show <proposal-id> --project . --format json
intent proposals confirm <proposal-id> --project . --format json
```

Confirmation is interactive, shows the complete proposal, and requires its exact digest. The agent,
`intent onboard --yes`, and the advisory plugin cannot manufacture that human act. Repeating
onboarding after activation returns `ready` without changing canonical state.

The compatible legacy path remains available and still returns `agent_submission_required` before
an agent proposes graph content:

```bash
intent init --project .
intent bootstrap --prd docs/prd.md --project . --format json
intent sources add markdown docs/prd.md --role declared_intent --project .
```

## 3. Install the advisor and use ordinary prompts

The validated plugin bundle installed above is `plugins/intent-advisor`. Open **Plugins Directory**,
select **Intent Engineering Local**,
enable `intent-advisor`, and review/trust the bundled `UserPromptSubmit` hook. The shipped entry's
`source.path` is exactly `"./plugins/intent-advisor"`.

The bundle intentionally omits an MCP `cwd`. On a local Codex host, its plugin-owned server process
inherits the active repository working directory, binding the held
runtime to that checkout. This is a local-host-only ruling: a remote executor must explicitly
reproduce or configure the repository working directory and must not assume `.` is the user's
checkout.

Clients other than Codex that launch and connect stdio themselves may run
`intent mcp --project .`; Codex users rely on the plugin-owned server configuration instead.

For every submitted prompt, the hook performs a read-only onboarding check. With an approved
baseline it captures the exact hook stdin as untrusted `agent:codex` evidence and supplies opaque
conversation and request-evidence references before the agent submits its bounded draft to
token-free `intent_advisory_preflight`. The hook is callable by the coding agent, so neither its
stdin nor its host fields establish local-human provenance. The agent may use public read-only
Intent tools for bounded repository context, but never sends raw prompt text through MCP. The live
held runtime and captured record, not caller fields, resolve repository, graph, text, time, ACL,
principals, and persistence authority; its author remains `agent:codex`.

- `no_semantic_impact`: continue without graph ceremony.
- `aligned`: continue with the relevant intent and requirement context.
- `new_or_ambiguous`: pause implementation and ask every returned question.
- `conflicting`: stop implementation and open or reuse a human-review case.

The plugin never requests, receives, displays, persists, or infers a capability token. It is
advisory guidance, not complete mutation interception. Mandatory construction continues to raise
`MandatoryHookUnavailable` with `Codex mandatory mutation hook is unavailable`. Do not document or
operate it as mandatory enforcement.

Explicit opt-out is always available: decline the onboarding offer, or disable/uninstall the
plugin. When disabled, ordinary coding behavior is unchanged and no prompt-time workflow call is
made. The local CLI, MCP server, and scheduled assurance remain independent.

Operators can still inspect the bounded diagnostic views manually:

```bash
intent context --task "add CSV export" --project . --format json
intent preflight --task "add CSV export" --project . --format json
```

The CLI diagnostic does not classify the request and does not mint a capability; it reports
`authorization_issued: false`.

## 4. Clarification and review

For `new_or_ambiguous`, the advisory preflight opens a persisted clarification session and the hook
asks each question. A later hook submission is still agent evidence: it does not satisfy a required
human answer and is not treated as approval. The route returns `human_confirmation_required`, and
the public MCP answer and confirmation tools return that same fixed status without changing state.
An independently authenticated non-MCP local human integration over the existing
`ClarificationCoordinator` and proposal-confirmation service must record the answer, then approve
the exact proposal digest and selected node IDs. Intent Engineering ships no CLI command for those
two authority steps; without a configured host integration, the session or proposal remains
pending. The agent may show the token-free persisted proposal preview but cannot activate it.

If there is insufficient evidence, the result remains `new_or_ambiguous` and asks focused
questions. A `conflicting` request cannot be self-reviewed when policy requires an independent
actor. Ordinary reconciliation remains two-phase:

```bash
intent reconcile show <case-id> --project . --format json
intent reconcile resolve <case-id> --action update_requirement --project . --format json
intent reconcile resolve <case-id> --action update_requirement --approve <review-hash> \
  --project . --format json
```

## 5. Scheduled CLI assurance

Prompt-time advice and assurance have separate lifecycles. Capture at the source cadence and run
validation/drift at the review cadence, whether or not the plugin is installed or enabled:

```bash
intent status --project . --format json --require-baseline
intent validate --project .
intent sync --project . --sources markdown,git,github,mcp
intent drift --project . --format markdown --output intent-drift.md --require-review
```

The default CLI uses deterministic checks and optional configured connectors; it issues no
authorization. Repeating the same capture is a semantic no-op. The repository's
`.github/workflows/intent-sync.yml` uses the same guard without installing or invoking
`intent-advisor`. Because `.intent` is ignored, a clean checkout deliberately fails with
`onboarding_required` unless an approved baseline is restored first or a persistent/self-hosted
workspace supplies it. The workflow never initializes and reports a clean graph version 0.

Scheduled semantic inference is optional and may only propose grounded review cases. Confidence is
not a claim of truth and never approves a proposal, resolves a conflict, or authorizes a canonical
or provider write.

## 6. Add compatible sources

Copy both the reviewed provider profile and one project binding. This Slack example is identical in
shape for the shipped Jira, Confluence, and Notion profiles:

```bash
export INTENT_ENGINEERING_SOURCE=/path/to/intent-engineering
mkdir -p profiles/mcp .intent/connectors
cp "$INTENT_ENGINEERING_SOURCE/profiles/mcp/slack.yaml" profiles/mcp/slack.yaml
cp "$INTENT_ENGINEERING_SOURCE/examples/mcp-bindings/slack.yaml" \
  .intent/connectors/slack.yaml
export SLACK_TOKEN='provided-by-your-secret-store'
intent connectors inspect slack-local --project . --format json
intent sources add '<source-role-connector-id>' \
  'https://workspace.example/channels/export/thread-42' \
  --role proposed_intent --project .
intent connectors test slack-local --project . --format json
intent sync --project . --sources mcp
```

`connectors inspect` and `connectors list` return `source_role_connector_ids`, keyed by profile
object type. Copy the exact value for the object that produced the evidence into `sources add`.
That canonical identity is derived locally from the reviewed profile, binding, scope, actor, and
principal mapping; inspecting it does not resolve credentials or contact the provider. A configured
alias that names several object types, such as `slack-local`, is intentionally too ambiguous for a
source-role assignment.

Set `local_actor` in `.intent/config.yaml`. Put contributor, approver, and executor aliases in
`.intent/approvals/policy.yaml`, and map provider principals in the binding. Bind credentials only
as references such as `env:SLACK_TOKEN`; never put a credential value in a profile, binding, graph,
or evidence record. The shipped profiles cover reviewed compatible shapes, not every Slack, Jira,
Confluence, Notion, or MCP deployment.

Roles affect interpretation, never authorship. Every evidence version retains its provider author,
source version, predecessor, timestamp, locator, content hash, and ACL. Once the relevant roles are
configured, an active agent inspects the captured evidence and submits a typed
`BootstrapSubmission` through the public MCP tool `intent_bootstrap_propose`. Candidate nodes may
start with lower confidence, assumptions, unanswered questions, and provisional status.

## 7. Link completion evidence

A supported host calls `IntentAgentHostAdapter.after_task`, which delegates to the public
`PostTaskService`. A completion is recorded only when the private grant still binds the exact task,
repository, graph version, changed paths, and Git commit, and when immutable passing test-result
evidence covers the claimed test references. It then adds only supported requirement/code/test
links and an implementation claim. There is no standalone post-task CLI today; without a supported
host, sync Git and test evidence and let assurance report the missing links.

## 8. Keep external writes three separate acts

External providers are never changed merely because a case exists. Preview as an authorized
contributor, approve interactively as an independent reviewer, then execute the unchanged plan:

```bash
intent write preview <case-id> --connector-id jira-local --operation update_issue \
  --fields '{"summary":"Keep exports local"}' --project . --format json
intent write approve <plan-id> --project . --format json
intent write execute <plan-id> --approval-id <approval-id> --project . --format json
```

A missing approval, changed target version/content, expired plan, actor-role mismatch, or changed
profile/binding rejects before provider mutation. Scheduled jobs do not preview, approve, execute,
confirm proposals, or resolve conflicts. Hosted ingestion, OAuth brokering, webhooks, universal
provider support, and unattended external writes are not shipped.
