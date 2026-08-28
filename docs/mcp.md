# MCP and coding-agent workflow

Intent Engineering can consume compatible MCP sources and expose its local intent graph to coding
agents. Install and initialize once:

```bash
python -m pip install -e '.[dev]'
intent init --project .
```

Copy both the reviewed provider profile and its example binding into the project, set
`local_actor` in `.intent/config.yaml`, and provide credentials only through named environment
references. `INTENT_ENGINEERING_SOURCE` is the checkout (or release bundle) containing the shipped
profiles and examples:

```bash
export INTENT_ENGINEERING_SOURCE=/path/to/intent-engineering
mkdir -p profiles/mcp .intent/connectors
cp "$INTENT_ENGINEERING_SOURCE/profiles/mcp/slack.yaml" profiles/mcp/slack.yaml
cp "$INTENT_ENGINEERING_SOURCE/examples/mcp-bindings/slack.yaml" .intent/connectors/slack.yaml
export SLACK_TOKEN='provided-by-your-secret-store'
intent connectors inspect slack-local
intent connectors test slack-local
intent sources add '<connector-id-from-inspect>' \
  'https://workspace.example/channels/export/thread-42' --role proposed_intent
intent sync --sources mcp
```

The binding's `profile_path` is repository-relative, so copying only the binding is insufficient.
The same two-file setup applies to the shipped `jira`, `confluence`, and `notion` compatible
profiles. Bind local actor aliases to provider principals in `.intent/connectors/<provider>.yaml`
and `.intent/approvals/policy.yaml`; credential values remain outside both files.

The repository ships polling, not hosted webhooks. Run sync manually, from cron, or from CI at the
cadence appropriate for conversations. Run reconciliation reporting separately at the cadence at
which humans should review divergence. For example:

```cron
*/15 * * * * cd /repo && intent sync --sources markdown,git,github,mcp
17 2 * * * cd /repo && intent drift --format markdown --output .intent/reports/drift.md --require-review
```

Every captured provider object/version is immutable evidence. Original author, mapped local actor,
timestamps, source locator, hashes, predecessor/version lineage, and ACL remain explicit. A second
teammate creates another attributable version. Compatible additions can update derived state;
competing claims remain side by side in a reconciliation case for human resolution.

Launch the local Intent MCP server for a coding agent with:

```bash
intent mcp --project .
```

It exposes authorized context, explain, impact, drift, status, validation, reconciliation views,
and proposal/preview/execution tools. `intent_context` is read-only. An active agent may submit its
typed PRD proposal with `intent_bootstrap_propose` and its attributed task classification with
`intent_preflight`; those submissions are revalidated against current durable state. Aligned tasks
may receive a short-lived process-local capability, while new/ambiguous and conflicting tasks remain
token-free. The diagnostic CLI `intent preflight --task "..."` only renders context and does not mint
authorization.

Active-agent reasoning may submit proposals. Scheduled semantic reasoning is optional; the default
CLI uses no model and deterministic assurance only. Regardless of origin, confidence is not a claim
of truth and never grants authority to confirm a proposal, resolve a conflict, or mutate a provider.
The server cannot approve its own proposal. External writes are deliberately split:

```bash
intent write preview <case-id> --connector-id <id> --operation <semantic-write> --fields '{"field":"value"}'
intent write approve <plan-id>
intent write execute <plan-id> --approval-id <approval-id>
```

Approval is interactive and binds the preview hash, actor identities, provider binding, target
version, and expiry. A changed target, role/configuration drift, missing approval, or same-person
conflict rejects before mutation. Scheduled jobs and coding agents cannot manufacture approval.

The audited Codex host cannot prove complete mandatory mutation interception, so no Codex plugin is
shipped. A future supported host can compose the provider-neutral adapter. Disabled host mode is a
no-op. See [the complete adoption guide](intent-aware-agent.md).

Not shipped: hosted ingestion, OAuth brokering, webhooks, a collaboration UI, universal MCP server
compatibility, or unattended external writes.
