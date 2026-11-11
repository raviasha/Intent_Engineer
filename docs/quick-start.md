# Intent Engineering quick start

Intent Engineering maintains a local evidence-backed graph of intent, requirements and code in the
background. First use offers source-connected setup in a separate chat; subsequent questions
are limited to relevant meaning that is missing, unclear or conflicting.
Requirements-to-code matching is included in initial reconciliation. Test-to-code tracing is deferred.

## 1. Install the CLI and Codex plugin

Use Codex desktop or CLI. Add the Git marketplace once, then install Intent Advisor:

```bash
codex plugin marketplace add raviasha/Intent_Engineer --ref marketplace
codex plugin add intent-advisor@intent-engineering-local
```

No source checkout, Python installation, pip command or global `intent` is required.
The package includes Python 3.12 and pinned dependencies for macOS Apple Silicon/Intel
and Linux x86_64 with glibc. Windows, Linux arm64 and Alpine/musl are not supported yet.
In the desktop plugin directory, choose **Intent Engineering Local** and install/enable
**Intent Advisor** after registering the marketplace. Start a new chat or restart the app
if it has not loaded the new plugin. Review and trust the bundled hooks under
**Review hooks** or **Settings > Hooks**, or use `/hooks` in the CLI.

The marketplace name and versioned cache layout are part of the launcher contract; use the
registration command above rather than renaming the marketplace.

The reviewer uses Codex from the host's PATH, with a fallback to the ChatGPT macOS app's
bundled executable. Use the signed-in Codex host; there is no separate Intent account.
If the host executable cannot be found, reconciliation remains pending instead of claiming
success. The plugin cannot approve its own hooks. Install and trust on each computer.

For updates, run `codex plugin marketplace upgrade intent-engineering-local`, then update
or reinstall the plugin from the directory and review changed hooks when prompted.
Existing installations registered from a local source checkout should remove that marketplace
registration and add the Git source above; this does not authorize deletion of project graphs.

Commands below use `intent` as shorthand for `/bin/sh "<installed plugin root>/scripts/intent"`.
The agent resolves that path from its installed skill. The plugin does not alter your shell PATH.

### Source development (contributors only)

Python 3.12+ remains a development prerequisite. Install the source project in your own
virtual environment and set `INTENT_DEVELOPMENT_PYTHON` to that environment's absolute
Python path when exercising source plugin scripts. Keep its `intent` executable on PATH
for the source hook subprocesses. Distributed packages never use this fallback.

## 2. Develop normally

Open a chat rooted in your repository and give the agent a task. The advisor makes a lightweight
relevance check, consults relevant intent when needed, and proceeds silently for unrelated or
aligned work. If no graph exists, it offers to create one in a separate chat using sources
such as local folders, Notion, Confluence or Slack. Your original task keeps moving. The setup chat asks
which folders, spaces, pages or channels to read and helps you connect them there.
If you choose “Later,” it offers again after three completed work turns.

For a local folder, give its path and choose which files/subfolders are in scope. This can be
inside the codebase or another folder the host can read. Designate whether those documents
represent authoritative intent or supporting context. Local folders need filesystem access,
not a remote-service login. Current file contents and folder membership are checked during
source refresh, including additions and deletions; no continuous filesystem watcher is installed.

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

In that same setup chat, you see the opening scope, graph-capture milestone and changed
comparison counts. Graph creation is followed by reconciliation. The agent automatically
shows the final gaps, recommended changes, evidence and limitations; you do not need to ask
for a status check. With no findings it reports no gaps within the assessed scope. If evidence
is missing or inputs change, it explains the remaining work rather than claiming completion.
When complete, setup returns the findings and recommendations to your original chat's agent,
which explains relevant gaps in context while continuing your work. The setup offer includes
permission for this return message. Delivery is tracked so unchanged results are not sent twice.
If host messaging is unavailable or not authorized, the original agent retrieves the result
automatically at its next trusted prompt hook; you do not run a command. The host must keep
the setup task active; the local runtime cannot wake a terminated chat.

Once that assessment is complete, the separate automatic worker described below handles
subsequent code-change reviews when trusted hooks fire.

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

Every declared PRD also requires a `document_requirements` comparison against captured
requirements, using its current snapshot evidence. This flags newly added or contradictory
requirements absent from the graph. Related downstream comparisons must cite that current
document too; capturing a document without comparing it cannot complete the assessment.

`incomplete` means missing comparisons, unknown evidence or pending source reads; `stale`
means a graph, source or file snapshot changed. Continue the work or report an unavailable
scope, and reassess changed inputs. Neither state can mark setup complete. `ready` or
`ready_with_findings` publishes verified artifact nodes, trace relationships and open issues
through a validated ChangeSet. Present the consolidated findings and recommended changes,
then mark the setup chat finished with `intent background-onboarding --finished --assessment-run-id <setup-run-id>`.

Readiness means a host-reported semantic assessment with validated evidence within the
declared scope, not universal proof of correctness. Missing PRDs or external sources remain
explicit limitations. Intended future work stays distinguishable from an observed mismatch.
Revoked evidence access returns `unavailable` without cached details. Reassessment retires
obsolete service-owned mappings within the assessed scope; unrelated mappings are preserved.
Existing issues are not automatically closed. Governed bootstrap activation likewise exposes
the pending reconciliation step; activating a graph alone does not compare it with code.

### Automatic intent and PRD checks

Accept source-connected setup and background monitoring. Select bounded intent sources (including
local folders), PRD files and source authority. The setup agent assesses the existing codebase,
returns initial findings to the original agent, then creates one hourly heartbeat in the same
setup chat. It confirms the actual host schedule ID before claiming monitoring is active.

The heartbeat checks intent sources and PRDs while Codex is open. After reopening, its first
scheduled run catches up with current state. It does not need a new user prompt. SessionStart
hook context assists resume/repair; the host schedule, rather than the hook, starts background work.
No standalone daemon, provider webhook configuration or copied credentials is needed. Keep the
project available and provider connections authorized. An unavailable scheduler or source is
reported honestly; task-time refresh remains available.

Only new actionable gaps, proposed document changes or consequential clarifications return to the
recorded working agent. Unchanged/aligned checks and already-delivered packets stay quiet. Clear
intent and requirements are maintained in the graph; PRD/source documents and code are not edited.
Source intent has highest priority, while conflicting sources remain explicit for a user decision.
The scope includes missing intent for requirements, missing requirements for intent, partial coverage
and contradictory or outdated PRD statements. Missing access is unknown, not alignment.

For an existing graph, ask: “Monitor my selected intent sources and PRD in the background and return
recommendations here.” Existing source selections are reused. On plugin updates, review any changed
hook trust request in Codex; the plugin does not enable trust on your behalf.

Source-check caching is shared across chats in the same project/local actor for at most one hour
from the original successful read. Task alignment continues on every development task. Known changes,
expired checks, scope/access changes and reopen catch-up bypass cache reuse. All pending source/PRD
changes join one batch; competing monitor workers stay quiet instead of duplicating reads/reviews.
An existing five-minute schedule is updated in place to hourly through the host, preserving its ID;
this is confirmed only after a successful host update. Each scheduled run forces fresh source checks,
even if a task just warmed the cache. Discovery follows the hourly schedule while the app, project
and providers are available; failed or delayed runs can delay findings. SessionStart records catch-up
for the next scheduled monitor run.

### On-demand code reconciliation

Code review runs only when deliberately requested, for example: “Check code against intent and the
PRD.” Initial graph setup still assesses the existing codebase. Later code coverage remains
last-reviewed or unknown until another explicit review; an aligned task does not prove implementation.

```bash
intent reconcile-code --project .       # status and last findings
intent reconcile-code --project . --run # explicitly run/resume code review
intent monitor-intent --project .       # monitor status; normally the agent handles this
```

These commands use the bundled plugin launcher described above. Only the explicit code reviewer
needs a signed-in current Codex CLI. It uses the normal default model configuration in an isolated
read-only snapshot; it never edits assessed code. Legacy `--enable` does not restore automatic
reviews; `--disable` can cancel a running review. Edits, prompt submission, tool completion and
SessionStart do not start code review.

Coverage starts with initial assessment files; Git projects also include recognized nonignored
source/config files under declared code directories, including additions and removals. Non-Git
projects keep their declared scope. Unsafe, unreadable or oversized inputs remain pending. Dirty
intent sources must first be refreshed by the host. Test-to-code tracing remains deferred.

### Optional governed review

The old review UI is available for explicitly requested governed review or team operations:

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
