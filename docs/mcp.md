# MCP and coding-agent workflow

Intent Engineering can consume compatible MCP sources and expose its local intent graph to coding
agents. The public developer journey is ordered so canonical approval stays distinct from advisory
prompt guidance.

## Install and onboard

```bash
git clone https://github.com/raviasha/Intent_Engineer.git /absolute/path/to/Intent_Engineer
python -m pip install /absolute/path/to/Intent_Engineer
codex plugin marketplace add /absolute/path/to/Intent_Engineer
codex plugin add intent-advisor@intent-engineering-local
cd /absolute/path/to/target-repository
```

Wheel-only installs such as `python -m pip install intent-engineering` provide the CLI and MCP
server, but not the repository-shipped plugin marketplace or advisor assets. Install the Codex
plugin from an explicit source checkout as shown above.

Restart the ChatGPT desktop app and start a new task rooted in the target repository. Codex
launches the plugin-owned MCP server from the bundle configuration; do not start another process
for it. First obtain explicit human consent and confirm the exact PRD path; only then run the advancing
command with `--yes`. It captures the PRD, assigns `declared_intent`, and returns
the bootstrap-proposal next action. Without `--yes`, the call is only an offer/no-op diagnostic and does not
advance onboarding. It does not silently activate a graph. Existing scripted integrations may
continue to use `intent init --project .` and `intent bootstrap`.

```bash
intent onboard --project . --prd docs/PRD.md --yes
```

## Approve the baseline

The agent submits its typed bootstrap proposal through the public MCP tool. The human then reviews
and confirms the exact digest:

```bash
intent proposals show <proposal-id> --project . --format json
intent proposals confirm <proposal-id> --project . --format json
```

## Use ordinary prompts

The validated source bundle installed above is `plugins/intent-advisor`. Open **Plugins Directory**,
select **Intent Engineering Local**,
enable `intent-advisor`, and review/trust its `UserPromptSubmit` hook. The shipped marketplace entry
uses `source.path: "./plugins/intent-advisor"`. The bundle intentionally omits an MCP `cwd`: a local
Codex host binds the plugin-owned MCP process to the active repository working directory. A remote
executor must explicitly reproduce or configure that working directory and must not assume `.` is
the user's checkout.

The hook automatically offers onboarding when the baseline is absent. Otherwise it persists its
exact stdin prompt as untrusted `agent:codex` evidence, allows the agent to use public read-only
Intent tools for repository context, and routes a bounded draft through token-free
`intent_advisory_preflight`. Because the coding agent can invoke the hook, neither the prompt nor
the submitted host fields establish local-human provenance.

The possible classifications are `no_semantic_impact`, `aligned`, `new_or_ambiguous`, and
`conflicting`. The advisory tool request contains only the opaque conversation reference, the
authenticated request-evidence reference, and a bounded classification draft. Repository, graph,
prompt text, time, ACL, principals, and persistence authority come from held local state; the
request author remains `agent:codex`. Neither request nor response contains a mutation capability.

The plugin is not mandatory enforcement. The audited Codex host still returns
`MandatoryHookUnavailable` with `Codex mandatory mutation hook is unavailable`. To opt out, decline
the onboarding offer or disable/uninstall the plugin; ordinary coding and the standalone CLI/MCP
surfaces continue unchanged.

## Clarification and review

For `new_or_ambiguous`, the hook asks the persisted questions but cannot authenticate answers or
approval. Public MCP answer and confirmation calls return the fixed
`human_confirmation_required` result and do not change state. An independently authenticated
non-MCP local human integration over the existing clarification coordinator and confirmation
service must record the answer and confirm the exact proposal digest and selected node IDs. There
is no shipped CLI command for these clarification authority steps; without a configured host
integration, the session or proposal remains pending. The public preview remains available to show
the exact persisted proposal. A `conflicting` request pauses implementation and follows configured
independent review policy.

## Scheduled CLI assurance

Assurance is independent of the plugin and uses the same commands in cron or CI:

```bash
intent status --project . --format json --require-baseline
intent validate --project .
intent sync --project . --sources markdown,git,github,mcp
intent drift --project . --format markdown --output intent-drift.md
```

These commands capture fresh source, code, and test evidence, validate the approved graph, and open
review cases without issuing authorization. `.github/workflows/intent-sync.yml` has no plugin or
hook dependency. It fails with `onboarding_required` on a clean checkout unless an approved
`.intent` baseline is restored first or a persistent/self-hosted workspace supplies it; it never
creates a clean graph version 0.

## Compatible MCP sources

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

## Standalone stdio clients

For clients that launch and connect stdio themselves, rather than Codex with the installed plugin,
the host may
launch the local Intent MCP server with:

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

The audited Codex host cannot prove complete mandatory mutation interception, so the shipped
`intent-advisor` plugin remains advisory. A future supported host can compose the provider-neutral
adapter only after proving complete synchronous coverage. Disabled plugin/host mode is a no-op. See
[the complete adoption guide](intent-aware-agent.md).

Not shipped: hosted ingestion, OAuth brokering, webhooks, a collaboration UI, universal MCP server
compatibility, or unattended external writes.
