# Intent Engineering quick start

This guide covers the normal developer journey. Intent Engineering keeps an evidence-backed graph
of the product intent, requirements, decisions, implementation, and tests. It highlights gaps for
human review instead of silently changing the approved graph.

## 1. Install the CLI and Codex plugin

Python 3.12 or newer is required. Install from a source checkout when you want both the CLI and the
repository-shipped Codex plugin:

```bash
git clone https://github.com/raviasha/Intent_Engineer.git /absolute/path/to/Intent_Engineer
python -m pip install /absolute/path/to/Intent_Engineer
codex plugin marketplace add /absolute/path/to/Intent_Engineer
codex plugin add intent-advisor@intent-engineering-local
```

Restart the ChatGPT desktop app after installing the plugin. Open **Plugins Directory**, select
**Intent Engineering Local**, enable `intent-advisor`, and review and trust its prompt hook.

A wheel-only installation provides the CLI and MCP server, but not the Codex plugin assets:

```bash
python -m pip install intent-engineering
```

## 2. Onboard a project

Change to the repository and point Intent Engineering at its product requirements document:

```bash
cd /absolute/path/to/your-repository
intent dev --project . --prd docs/PRD.md --offline
```

`intent dev` initializes the local `.intent` workspace, captures the PRD as versioned evidence,
and opens the local review UI. In the UI:

1. Register this device with WebAuthn.
2. Review the proposed intent graph and its evidence.
3. Activate the baseline only when it accurately represents the project.

Capturing a PRD does not approve it. The agent proposes graph content; the human confirms the
exact proposal. Omit `--offline` when the repository already uses shared GitHub team state.

Useful variants:

```bash
intent dev --project . --status   # Inspect the repository-bound service
intent dev --project . --no-open  # Start or reuse it without opening a browser
```

## 3. Develop normally

With `intent-advisor` enabled, submit ordinary development prompts in Codex. You normally do not
run another Intent command. Before work begins, the plugin checks readiness, restores approved team
state when configured, captures the prompt as untrusted evidence, and classifies it:

| Classification | What happens |
| --- | --- |
| `no_semantic_impact` | Work continues without graph ceremony. |
| `aligned` | Work continues with the relevant approved intent and requirements. |
| `new_or_ambiguous` | Implementation pauses while the agent asks the missing questions. |
| `conflicting` | Implementation stops until the evidence-backed conflict is reviewed. |

Answers and graph-changing proposals are confirmed in the local `intent dev` UI with WebAuthn.
The plugin is advisory and cannot approve or silently mutate canonical intent.

If the plugin is unavailable, check readiness manually:

```bash
intent ensure --project . --preset developer
```

## 4. Check alignment before committing

Run the consolidated local assurance command:

```bash
intent check --project .
```

It performs readiness, evidence capture, validation, assurance, and bounded drift reporting. Tests
are evidence only when they come from reviewed test configuration or a supplied canonical result.
To run one reviewed test command by ID:

```bash
intent check --project . --run-test <reviewed-command-id>
```

Unresolved divergence becomes a review case; it does not automatically rewrite code,
requirements, or the graph.

## 5. View and improve graph quality

Open `intent dev` to see the visual graph and score table. Green means robust, orange needs
attention, and red identifies a blocking or weak critical path. For a read-only CLI scorecard:

```bash
intent assess --project .
intent assess --project . --focus <node-or-branch-id>
```

You can improve the graph even when you are not adding a requirement. Choose **Improve graph** in
the UI, or start a short question-at-a-time session:

```bash
intent refine --project . --minutes 5
```

Sessions can be paused and resumed. Answers become evidence immediately, but a canonical graph
change still requires a reviewed proposal and explicit human approval. See the
[assessment and enrichment guide](assessment.md) for scoring dimensions and session controls.

## 6. Join an existing team

Developers use their normal code checkout. Nobody checks out the protected `intent-state` branch;
Intent Engineering fetches and verifies that branch automatically.

The existing sponsor creates a public invitation:

```bash
intent team invite --project . \
  --github-account-id <new-developer-github-id> \
  --github-login <new-developer-login> \
  --output new-developer.intent-invite.json
```

The new developer runs this from their own checkout and returns the public response file:

```bash
intent team join --project . \
  --invite new-developer.intent-invite.json \
  --output new-developer.intent-join.json
```

The sponsor reviews and approves that exact response:

```bash
intent team approve-join --project . \
  --response new-developer.intent-join.json
```

Merge the resulting protected state pull request in GitHub. The new developer's next prompt or
`intent dev` run verifies and restores the approved state. Each developer keeps an independent
device credential and private key in their own OS keyring; only public invitation/response files
are exchanged.

Initial organization, protected-branch, runner, and CI-recipient setup is an operator task. Follow
the [GitHub guide](github.md) and [dedicated CI recipient guide](ci-recipient.md) before inviting
additional developers.

## 7. Use the CI backstop

The shipped pull-request workflow runs reviewed tests and then performs the equivalent of:

```bash
intent check --project . --ci --require-review \
  --test-results .intent-ci/test-results.json
```

Configure branch protection to require **Intent Engineering / check**. The CI check is independent
of the local plugin, so disabling the plugin does not bypass repository assurance.

## Everyday command summary

| Goal | Command or action |
| --- | --- |
| First onboarding | `intent dev --project . --prd docs/PRD.md --offline` |
| Normal feature work | Use ordinary Codex prompts with `intent-advisor` enabled |
| Open the review UI | `intent dev --project .` |
| Check code, tests, and intent | `intent check --project .` |
| View graph robustness | `intent assess --project .` |
| Improve the graph for five minutes | `intent refine --project . --minutes 5` |
| Diagnose readiness | `intent ensure --project . --preset developer` |

For recovery procedures, provider integrations, and the full security model, see the
[intent-aware agent adoption guide](intent-aware-agent.md).
# Read and improve existing intent

In the existing Intent Dev dashboard:

1. Open **Graph assessment**. Its default scope is **Intent vs requirements**.
2. Select a graph scorecard or table row, then **Read intent and requirements** to inspect its
   purpose, assumptions, related requirements and source references.
3. Use the coverage table to find missing links, disputes and deliberate deferrals. Scores measure
   structural completeness; review the meaning of a requirement separately.
4. Choose **Improve graph** for a five-minute session, even when you have no new feature to add.
   Resume a prior session to retain its answers. Review the cited understanding and answer the
   next relevant question, or pause when convenient.
5. Inspect current and proposed coverage during the existing proposal review before approval.

No extra command sequence is needed. For an optional report that also examines implementation
and tests, run `intent assess --scope full_lifecycle`; this does not change the project setting.
