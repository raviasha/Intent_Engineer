# Intent Engineering quick start

Intent Engineering maintains a local evidence-backed graph of intent, requirements and code in the
background. First use offers source-connected setup in a separate chat; subsequent questions
are limited to relevant meaning that is missing, unclear or conflicting.
Requirements-to-code matching is included in initial reconciliation. Test-to-code tracing is deferred.

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
**Intent Engineering Local**, enable `intent-advisor`, and review and trust its prompt and completion hooks.

A wheel-only installation provides the CLI and MCP server, but not the Codex plugin assets:

```bash
python -m pip install intent-engineering
```

## 2. Develop normally

Open a chat rooted in your repository and give the agent a task. The advisor makes a lightweight
relevance check, consults relevant intent when needed, and proceeds silently for unrelated or
aligned work. If no graph exists, it offers to create one in a separate chat using sources
such as Notion, Confluence or Slack. Your original task keeps moving. The setup chat asks
which sources and spaces, pages or channels to read and helps you connect them there.
If you choose “Later,” it offers again after three completed work turns.

When your task needs intent that is missing or unclear, the agent shows its captured understanding
and asks one consequential question. For a conflict it shows the existing intent and the specific
clash. Reply in the chat. Known answers are reused, and clear meaning is maintained automatically
through validated ChangeSets with immutable source evidence and history.

Your statements remain distinguishable from agent inferences. Chat evidence is not an authenticated
approval. Existing pending proposals and unresolved disagreements survive the update. No routine
WebAuthn, graph approval, PRD draft, graph export or report is required.

## 3. CLI fallback and optional review

The agent can read context and maintain intent before MCP connects to a new workspace:

```bash
intent background --project . --task "Add private reports" --session-id chat:example
intent background --submission-schema
intent background --project . --submission background-draft.json
```

The submission schema describes clear interpretations, consequential questions, exact quoted
answers and typed nodes/edges. Context reads and unrelated work do not create `.intent`.
A relevant capture initializes the workspace automatically. If MCP started before initialization,
the CLI remains usable; a new chat can reconnect the tools later.

The host uses `intent background-onboarding --prepare` to obtain a setup-chat prompt,
creates the chat, and records its actual ID with `--chat-id`. It captures real authorized
source reads with `intent background-source --submission <relative-json-path>` (inspect
`--submission-schema` first). This retains source authors, revisions, locations and content
as local evidence. Record the user's bounded scope and authority designation using
`intent background-sources --submission <relative-json-path>`. Inspect the live schema with
`--submission-schema`; old captured evidence is not automatically an authoritative selection.
Use `intent background-onboarding --defer` to postpone setup for three completed work turns.

Before a new development task, the agent starts `intent_background_refresh` (CLI:
`intent background-refresh --submission-schema`). It uses connected host tools to check
scope-wide revision markers. Verified unchanged markers reuse captured content; changed or
unverifiable markers require a complete bounded source read. Collection checks must include
new/deleted objects and edits, not just a latest message timestamp. Failed or partial reads
stay pending. Changed content is interpreted against current intent before dependent work.

Provider events can mark a selected source dirty through `intent_background_source_signal`
(CLI: `intent background-source-signal`). Installing the plugin does not configure provider
subscriptions, a webhook listener, or continuous polling. A separate verified provider bridge
is needed for push notifications; event silence alone never establishes freshness.

Selected authoritative sources govern downstream intent. If they disagree, the agent shows
both sources and recommends a resolution; you decide in the current chat. It preserves the
sources and records your decision locally. Independent work can continue while affected work
waits. The task-start hook guides the cooperating agent; it does not intercept arbitrary shell
or editor operations. Read receipts are host-reported, not independent provider authentication.

After source changes, the agent checks affected intent/requirement paths in both directions
and compares their meaning with the source. Existing structural coverage checks do not prove
that every source need is captured or satisfied. A refresh `ready` result verifies freshness
and recorded interpretation, not a gap-free graph or implementation completeness.

### Initial reconciliation

The setup chat compares the PRD, captured intent and production code before marking setup
finished. It takes the time needed, persists checkpoints and resumes in the same host chat.
Your original work continues. The CLI and MCP tools store the assessment; they do not launch
a worker on their own.

The host inventories relevant files, reads actual behavior and checks source freshness. It
then uses `intent_initial_reconciliation` with `action=start` to snapshot an explicit file
scope and obtains evidence references and required comparisons. The CLI exposes the same service:

```bash
intent initial-reconciliation --submission-schema
intent initial-reconciliation --project . --submission assessment.json
intent initial-reconciliation --project .
```

Each submission uses a stable `session_id` and a distinct `turn_id`. Start supplies `scope`
and `files` with relative `path` and role `requirements` or `code`. Checkpoint supplies the
returned `run_id` and evidence-backed `comparisons`. Inspect the live schema and the plugin's
[complete workflow](../plugins/intent-advisor/skills/intent-advisor/references/initial-reconciliation.md)
for a start example and comparison fields. Use `action=status` to resume and `action=finish`
to validate and publish the scoped assessment.

Required comparisons cover intent→requirements, requirements→intent, requirements→code,
code→requirements and intent→code. This catches missing intent, unrepresented needs, missing
implementation, unsupported behavior, partial coverage and PRD/code agreement that violates
intent. Source intent governs recommendations. Conflicting sources remain explicit for your
decision. Assessed code and source documents are not edited.

`incomplete` means missing comparisons, unknown evidence or pending source reads; `stale`
means a graph, source or file snapshot changed. Continue the work or report an unavailable
scope, and reassess changed inputs. Neither state can mark setup complete. `ready` or
`ready_with_findings` publishes verified file nodes, trace relationships and open issues
through a validated ChangeSet. Present the consolidated findings and recommended changes,
then mark the setup chat finished with `intent background-onboarding --finished`.

Readiness means a host-reported semantic assessment with validated evidence within the
declared scope, not universal proof of correctness. Missing PRDs or external sources remain
explicit limitations. Intended future work stays distinguishable from an observed mismatch.
Existing issues are not automatically closed. Governed bootstrap activation likewise exposes
the pending reconciliation step; activating a graph alone does not compare it with code.

The old review UI is optional, for explicitly requested governed review or team operations:

```bash
intent dev --project . --offline
intent ensure --project . --preset developer
```

Those legacy proposals and team publication still require their existing independently
authenticated approvals. They are not automatically activated by background maintenance.

## 4. Optional full-lifecycle assurance

When explicitly working on full-lifecycle assurance, run:

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

## 5. Optional graph views and enrichment

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
