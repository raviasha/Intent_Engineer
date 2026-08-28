# Intent-aware agent adoption

Intent Engineering adds a local review layer to an existing repository. It captures immutable
source versions, builds a reviewed intent/requirements provenance graph, gives an active agent
task-specific context, and records disagreements for humans. It does not replace Git, your coding
agent, or provider permissions.

## 1. Initialize an existing repository

Python 3.12 or newer is required. From the repository root:

```bash
python -m pip install intent-engineering
intent init --project .
intent validate --project .
```

For a source checkout, use `python -m pip install -e .` instead. Initialization creates the ignored
`.intent/` workspace. Do not use `--force` to load an existing workspace.

## 2. Capture a Markdown PRD and assign its authority

Point the capture command at an existing repository-relative Markdown file:

```bash
intent bootstrap --prd docs/prd.md --project . --format json
intent sources add markdown docs/prd.md --role declared_intent --project .
```

This command captures the PRD and returns `agent_submission_required` with evidence references. It
does not invent or activate a graph. The second command marks that exact captured path as declared
intent before an agent proposes any graph content.

## 3. Add compatible sources, then review the bootstrap proposal

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

Review the resulting proposal locally and confirm only the intended core:

```bash
intent proposals list --project . --format json
intent proposals show <proposal-id> --project . --format json
intent proposals confirm <proposal-id> --project . --format json
```

Confirmation is interactive and displays the complete proposal before asking for its exact digest.
The agent cannot manufacture this human confirmation.

## 4. Look through the graph before every task

An operator can inspect the same bounded packet the agent should read:

```bash
intent context --task "add CSV export" --project . --format json
intent preflight --task "add CSV export" --project . --format json
```

`intent preflight` is a manual diagnostic wrapper around context. It does not classify the request,
does not mint an authorization, and returns `authorization_issued: false`.

In a supported active-agent integration, the agent records the human request and its attributed
classification submission, then calls the public MCP tool `intent_preflight`. The deterministic
service rechecks that submission against current graph/evidence state:

- `aligned` or `no_semantic_impact`: the long-lived MCP process may return a short-lived,
  process-local capability bound to the exact repository, actor, task, graph version, and paths.
- `new_or_ambiguous`: ask every returned question before proposing a requirement. When there is
  insufficient evidence, the result remains `new_or_ambiguous` and explains the gap through those
  questions; it is not a separate classification.
- `conflicting`: do not mutate; open or reuse the evidence-backed review case.

The capability is private to that process. It is never written to graph, history, evidence, cases,
configuration, receipts, logs, or CLI output.

## 5. Clarify and review new intent

For `new_or_ambiguous`, the agent asks the returned questions in the same conversation. A supported
host composes the public Python `ClarificationCoordinator.open`, `.answer`, and `.propose` methods to
persist the human and agent turns, each answer's author and ACL, and the proposal chronology. It then
uses `ProposalConfirmationService.confirm` with the authenticated contributor.

There is currently no standalone clarification-answer CLI. Treating ordinary chat text as a silent
canonical update would lose the provenance this workflow is designed to preserve.

A conflict or high-risk replacement cannot be self-reviewed. A different actor listed in the local
approval policy must inspect both evidence sides and confirm through
`ProposalConfirmationService.confirm`. For ordinary reconciliation cases, the local two-phase CLI
shows the exact review hash before applying anything:

```bash
intent reconcile show <case-id> --project . --format json
intent reconcile resolve <case-id> --action update_requirement --project . --format json
intent reconcile resolve <case-id> --action update_requirement --approve <review-hash> \
  --project . --format json
```

## 6. Host enforcement and disabled mode

The provider-neutral `IntentAgentHostAdapter` is the integration seam for a future host that can
prove synchronous coverage before every file or command mutation and invoke post-task processing.
For the currently audited Codex contract, mandatory construction raises
`MandatoryHookUnavailable` with the fixed message `Codex mandatory mutation hook is unavailable`.
No Codex plugin is shipped, and this project does not claim automatic Codex enforcement.

When integration is disabled, the host adapter is a transparent no-op: ordinary coding behavior is
unchanged, no preflight service is called, and no completion is recorded. Operators may still run
the diagnostic CLI and MCP services manually.

## 7. Link completion evidence

A supported host calls `IntentAgentHostAdapter.after_task`, which delegates to the public
`PostTaskService`. A completion is recorded only when the private grant still binds the exact task,
repository, graph version, changed paths, and Git commit, and when immutable passing test-result
evidence covers the claimed test references. It then adds only supported requirement/code/test
links and an implementation claim. There is no standalone post-task CLI today; without a supported
host, sync Git and test evidence and let assurance report the missing links.

## 8. Run capture frequently and assurance separately

Capture source versions at the cadence at which they change:

```bash
intent sync --project . --sources markdown,git,github,mcp
```

The default production sync uses deterministic checks with no model or network inference beyond
configured source connectors. An application embedding `AssuranceService` may provide optional
ACL-filtered semantic reasoning, but its output can only propose evidence-backed cases. A confidence
score never authorizes a canonical intent/requirement change, approval, conflict resolution, or
external write.

At the human review cadence, validate and render drift/assurance separately:

```bash
intent validate --project .
intent drift --project . --format markdown --output intent-drift.md --require-review
```

The repository workflow runs the same ordered read-only sequence manually or on a schedule. A
second identical capture is a semantic no-op, while previously captured authors and revisions remain
visible.

## 9. Keep external writes three separate acts

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
