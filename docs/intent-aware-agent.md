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
server, including the packaged local review UI, but not the repository-shipped plugin marketplace
or advisor assets. Restart the ChatGPT desktop app and start a new task rooted in the target
repository. Codex launches the plugin-owned MCP server from the bundle configuration; do not start
an additional process for it. Confirm the exact PRD path, then start the primary local control
plane:

```bash
intent dev --project . --prd docs/PRD.md --offline
```

This single foreground process initializes a missing workspace, records the PRD as immutable
declared-intent evidence, chooses an ephemeral loopback port, and opens the browser review UI.
Register the local device with WebAuthn. PRD capture still does not manufacture an agent proposal
or human approval: the active agent submits a typed `BootstrapSubmission`, and the browser presents
the exact evidence, digest, and selected nodes for explicit activation. Use `--no-open` when needed,
and check or reuse the repository-bound process with `intent dev --project . --status`.

Milestone 1 is deliberately single-user and local. Team identity enrollment and shared review
state are Milestone 3 work; the current device credential and process metadata are not shared-team
identity or synchronization mechanisms.

Existing automation may still use the granular onboarding diagnostic
`intent onboard --project . --prd docs/PRD.md --yes`.

## 2. Approve the baseline

The active agent reads the captured evidence and submits a typed `BootstrapSubmission` through
`intent_bootstrap_propose`. Inspect and confirm the exact proposal locally:

```bash
intent proposals list --project . --format json
intent proposals show <proposal-id> --project . --format json
intent proposals confirm <proposal-id> --project . --format json
```

The browser confirmation is WebAuthn-backed, shows the complete proposal, and binds the exact
digest and selected node IDs. The agent, PRD capture, and the advisory plugin cannot manufacture
that human act.

The granular CLI path remains available under advanced diagnostics and still returns
`agent_submission_required` before an agent proposes graph content:

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
made. The local CLI, MCP server, scheduled assurance, and required GitHub check remain independent.

Operators can still inspect the bounded diagnostic views manually:

```bash
intent context --task "add CSV export" --project . --format json
intent preflight --task "add CSV export" --project . --format json
```

The CLI diagnostic does not classify the request and does not mint a capability; it reports
`authorization_issued: false`.

### Required GitHub check setup

The code-PR workflow in `.github/workflows/intent-check.yml` names its job exactly
`Intent Engineering / check`. It runs on every PR without path filters and cancels older runs for
the same PR. Its only GitHub permission is `contents: read`; checkout does not persist Git
credentials. It never installs or invokes the advisory plugin.

The workflow restores the fetched `refs/remotes/origin/intent-state` without checking out that
branch. Signature, repository/project identity, lineage, ciphertext, canonical ledgers and graph
invariants must all validate before replacement. It then executes every reviewed argv in the
restored project configuration using the bounded `DevObserver`, stages a result only when all
commands pass, and validates its repository, commit, timestamp and ACL before writing
`.intent-ci/test-results.json`. The final command independently restores and verifies state again:

```bash
intent check --ci --require-review --test-results .intent-ci/test-results.json
```

Reviewed execution requires a clean commit snapshot before and after every command and both
result writes. The helper hashes actual tracked bytes against HEAD, checks executable modes and
the staged index, and binds staged evidence to file and ancestor-directory identities and
modification/change times, including the repository root. Swapping a tracked parent directory
and restoring it after execution cannot preserve a passing snapshot.
Index shortcuts, arbitrary ignore rules and Git replacement objects cannot authorize dirty code.
Any mismatch removes staged and final passing evidence. The internal staged snapshot is local to
this checkout; the final canonical result schema is unchanged.

Only untracked `.intent/`, `.intent-ci/`, `.venv/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
directories and exact reviewed `test_result_paths` are permitted as generated outputs. The helper
creates these bounded output directories and reviewed result parents before the initial snapshot.
Outputs must remain in those pre-existing, untracked directories: creating or replacing entries
along tracked ancestors (including loose outputs at the repository root) invalidates the snapshot.
Tracked files are never exempt. Other outputs must be explicitly reviewed, not merely added to
`.gitignore`.

Repository `__pycache__` content and `.pyc`/`.pyo` files are rejected, including committed bytecode;
rejected caches are left untouched for the operator to handle. They cannot shadow failing committed
Python source. Reviewed CI subprocesses inherit `PYTHONDONTWRITEBYTECODE=1` to prevent ordinary
imports from creating new repository caches. This flag prevents writes, not reads: rejection of
existing repository bytecode provides the read-side boundary. Installed dependencies inside the
explicit `.venv` directory remain part of the separately reviewed dependency environment.

Snapshot inspection is bounded to 4,096 tracked files, 16 MiB per file,
128 MiB total, 1 MiB of Git listing output and five seconds; it fails closed beyond these limits.
Tracked symlinks, submodules, hardlinks and checkout filters that change committed bytes are not
supported by this CI execution contract.

Configure these prerequisites before expecting the check to pass:

1. Review and activate the project baseline. Include a nonempty `test_commands` list in the
   approved shared configuration, for example `[["tools/test-runner"]]`. The first argv entry
   must be a regular executable file relative to the repository; shell strings, PATH executable
   lookup and arbitrary prompt commands are rejected. Script interpreters must satisfy the
   existing observer's vetted-interpreter contract. The PR job uses `macos-14` because the
   observer requires a root-owned regular `/bin/sh` or `/usr/bin/python3`; ordinary Ubuntu
   symlinks at those paths are rejected. Installation creates `.venv` with the installed test
   dependencies. A reviewed `#!/bin/sh` wrapper can run `exec .venv/bin/python -m pytest -q`.
   The subprocess receives a minimal environment, so it must not rely on setup-python's PATH
   or ambient credentials. Commands have the existing five-minute and output bounds.
   `test_result_paths` may remain empty because this workflow writes its own combined artifact.
   Review changes to the test runner, its imports, dependencies and workflow together with code.
2. Provision a compatible signed, encrypted state release on a protected `intent-state` branch.
   The checkout fetches full history, including that branch. Preserve the signed parent lineage;
   do not merge the state branch into a code branch. Current restore supports the
   `seal_state_payload` envelope and independently pinned Ed25519 signing keys. Automatic team
   enrollment, publication PR creation and WebAuthn-bound publication are not provided by this
   workflow. The offline tests' fixed keys are fixtures and must never be used for deployment.
3. Create a GitHub environment named `intent-ci`, with required reviewers and deployment rules
   for the intended branches. Store `INTENT_CI_SHARED_STATE_TRUST` as an environment secret.
   Its strict JSON object has `schema_version: 1`, `project_id`,
   `repository_id: "github.com/owner/repository"`, `recipient_key_id`,
   `recipient_private_key_base64`, and `signing_keys`, an array of
   `{ "signature_id": "...", "public_key_base64": "..." }` records sorted by signature ID.
   Keys use unpadded URL-safe base64 for exactly 32 bytes; all configured signers must match the
   release. Provision the recipient private key and trusted signing public keys through an
   independent secure channel. Do not copy keys from a PR or commit them to Git.
4. Treat approval of this environment as approval to execute the entire PR checkout with access
   to decrypted state and the CI secret. Review the exact revision, workflow, package installation
   hooks, test runner and dependencies before releasing it. Merely naming an environment does
   not configure its protection. Fork PRs normally receive no secret and fail closed; review and
   test approved contributions through a trusted branch workflow. Do not change this workflow to
   `pull_request_target` to expose secrets to fork code.
5. After observing a run, configure the code branch's ruleset or branch protection to require
   **`Intent Engineering / check`**, select GitHub Actions as the expected source where available,
   and require the tested revision to be current before merging. Protect workflow and runner
   changes with review. Do not require this code check on the separate state-only branch. The
   repository files do not create rulesets, configure reviewers, provision secrets or merge PRs.

Only the canonical passing test-result file is uploaded by the PR job, with seven-day retention.
It contains commit/project/test identifiers and ACL metadata, not decrypted canonical state or
test stdout. Do not broaden its artifact path to `.intent`, the whole checkout or raw test logs.
The nightly/manual workflow has separate concurrency and read-only source permissions. It uses
the same protected environment to restore state, captures sources, validates canonical state,
then renders and uploads the drift report before the final `intent check --require-review`.
Pending review still fails the run, but its report remains available. Restore, capture, validation
and rendering failures are not masked. It does not claim fresh repository test execution. Its
generated drift report also has seven-day retention.

For an onboarded local checkout, the first ordinary prompt checks readiness automatically; the
developer does not need to type an Intent command per task. Current prompt readiness examines the
local baseline. A fresh team checkout must first receive a verified baseline through the restore
integration; the prompt hook itself does not fetch or enroll team state. The offline release proof
covers a real fresh clone, signed baseline restore, the first prompt, an implementation/test commit,
a passing CI check, and ambiguous/conflicting cases that still block CI when the plugin is absent.
Passing that check means the configured deterministic checks passed; it does not declare all
software semantically complete.

## 4. Clarification and review

For `new_or_ambiguous`, the advisory preflight opens a persisted clarification session and the hook
asks each question. A later hook submission is still agent evidence: it does not satisfy a required
human answer and is not treated as approval. The route and public MCP answer/confirmation tools
return `human_confirmation_required` without changing state. The `intent dev` browser is the
authenticated non-MCP local human integration: via the existing `ClarificationCoordinator`, it
records the answer through WebAuthn and then presents the exact clarified proposal digest and
selected node IDs for a second authenticated confirmation. The agent may show the token-free
persisted preview but cannot activate it, and there is no shipped CLI command for either
human-authority step.

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
