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
`Intent Engineering / check`. It uses the protected default-branch `pull_request_target` workflow,
without path filters, and cancels older runs for the same PR. Its only GitHub permission is
`contents: read`; checkout does not persist credentials. It never invokes the advisory plugin.
The one checkout is `.intent-trusted` at the exact `github.workflow_sha`, never the proposed
revision. Protected tooling fetches the PR head as Git objects and verifies the requested SHA;
it does not check out, install, build, or import the proposed package on the host. Requests for
an unprotected/non-default base or execution ref fail closed instead of being skipped.

Action code is part of this trusted bootstrap. Every action reference in `ci.yml`,
`intent-check.yml` and `intent-sync.yml` uses a reviewed full commit SHA, not a movable version
tag. The pins resolve to official [checkout v4.4.0](https://github.com/actions/checkout/commit/11d5960a326750d5838078e36cf38b85af677262),
[setup-python v5.6.0](https://github.com/actions/setup-python/commit/a26af69be951a213d495a4c3e4e4022e16d87065)
and [upload-artifact v4.6.2](https://github.com/actions/upload-artifact/commit/ea165f8d65b6e75b540449e92b4886f43607fa02),
verified from their official repositories on 2026-09-08. The offline workflow guard discovers
all `.yml`/`.yaml` workflows and checks every step action and reusable-workflow job against an
exact reviewed allowlist. Updating an action requires verifying its official full commit and
reviewing the upstream changes, then changing the workflow pins and allowlist together in a
protected-tooling review. Pins prevent silent tag movement; they do not automatically receive
future security fixes or replace the trusted runner and environment controls described below.

After installing the protected exact-version/hash wheel lock, a separate read-only-auth fetch
step acquires the proposed commit. Only the protected launcher check step receives the state
decryption trust bundle:

```bash
python -I .intent-trusted/ci/launch.py check
```

Canonical test results use schema version 2. They retain the execution snapshot, the configured
intent graph and semantic history digest, and the complete reviewed test configuration digest
through evidence ingestion. The consumer independently recomputes these bindings before capture
and before reporting success; CI requires a nonempty configured command set and exact coverage.
Artifacts from version 1 or with missing bindings must be regenerated by running the tests.
Local snapshot/baseline bindings include identities and change timestamps as well as bytes, so
local results must be regenerated after code changes, even if original bytes are restored, or in
another checkout. Kernel-verified immutable image bindings instead use domain-separated content
identities, so separate mounts of the same verified image do not depend on inode/device reuse.
Dirty local test runs can report their process outcome but produce no committed-tree
evidence; `intent check --run-test` fails when that evidence is unavailable. Test evidence uses
the stable configured project ID across CLI, control-plane and GitHub producers. The control
plane's separate per-checkout repository identity continues to bind human authentication.

Local and control-plane results remain consumable by local checks and replay without duplicating
evidence. They are not CI-eligible. `intent check --ci` cannot pass on a mutable host;
otherwise-valid inputs return `test_environment_unsupported`. Neither an artifact field nor an
environment flag can enable it.
Passive development observation omits rejected stale/dirty test artifacts while continuing to
return independent Git revision and changed-path evidence.

CI uses a rootful Docker daemon and the protected-base adapter as trusted infrastructure. The adapter
builds its own Dockerfile and entrypoint, materializing the exact HEAD from hash-verified Git
commit/tree/blob objects and the baseline from independently authenticated encrypted Git state.
The host launcher imports only its protected checkout's `src`; isolated image entrypoints prevent
proposed project packages from shadowing the adapter. Signature, repository/project identity,
complete signed lineage, ciphertext and canonical state invariants are independently verified.
It never copies working-tree code, local graph/config bytes, hooks, Git configuration or index
into the image. The code history has an explicit shallow boundary at HEAD; the complete bounded
signed-state lineage is retained. Dirty edits cannot turn a failing committed tree into a pass.

The base image is pinned by manifest digest in protected tooling. `ci-runtime.lock` fixes all
runtime/test dependency versions and distribution hashes from the frozen protected `uv.lock`.
Only wheels may be installed, using `--require-hashes --only-binary=:all:`. The networked
dependency build receives only this protected lock and its generated Dockerfile: no proposed
code, baseline or trust. Its exact image ID becomes the base of the separate `--network=none`
project/baseline build. That build executes only the protected materializer, never proposed
package build hooks. Proposed dependency changes cannot change this toolchain before an explicit
protected-tooling update. A missing wheel or hash mismatch fails closed.

Reviewed tests and final `CheckService` CI consumption use two separate disposable containers
of the same exact image ID. The test container receives only the execution time, never the trust
bundle. The host destroys it and its entire cgroup/PID namespace, including detached descendants,
and verifies removal before starting finalization. Only its strict canonical v2 result crosses
the boundary; no test-created assurance files or writable volumes are reused. The fresh consumer
independently authenticates the baseline and seeds its own assurance workspace. Both containers
are non-root, network-disabled, capability-free,
and has `no-new-privileges` and a read-only root image. The consumer checks the kernel's initial
UID namespace, process capabilities, network interfaces, mount table and held descriptors independently. Source
and baseline bind mounts are rejected, including read-only binds of mutable host files. Root
image immutability prevents ordinary writes and memory-mapped writes throughout execution and
acceptance; repeated metadata scans or file notifications are not treated as an atomicity primitive.
The guarantee certifies the image's exact proposed commit, not the protected tooling checkout or
continued immutability of an external checkout.

Assurance stores use a fresh authenticated copy on a separate 128 MiB tmpfs; result output has a
1 MiB tmpfs and temporary files have 64 MiB. No source/baseline bind mounts or Docker socket are
exposed. Only the exact strict canonical artifact accepted by the final consumer is copied out
for audit; host-side replay cannot establish CI eligibility. Trust is supplied over stdin only
to the fresh consumer, never to reviewed processes, an image, build context, or command arguments.
Docker/build subprocess environments exclude both state trust and GitHub credentials. The
ephemeral image and intermediate layers are removed by their unique per-run label before success.
The bounded legacy no-cache Docker builder is required so private baseline layers do not remain in
a separate BuildKit cache; unavailable Docker, unsupported builders, cleanup failures, and unsafe
container boundaries fail closed. The trusted host/daemon must not modify image storage.

Both local and CI checks run bounded, read-only readiness before opening ordinary mutable
stores, capturing evidence, or running reviewed tests. Unsafe canonical files and any existing
local transaction journal fail closed with `readiness_required`. The check does not repair a
torn graph, replay a journal, or remove recovery state; use the explicit trusted recovery or
control-plane workflow before retrying. Signed shared-state restoration still precedes readiness
in CI.

Restore preserves unpublished local graph decisions, ChangeSet history, proposals and reviewed
configuration. A semantic extension or divergence returns `diverged`; the check reports
`human_attention_required` and exits 4 before capture. Reconcile that state explicitly. Repeating
the same approved release is a no-op and preserves valid evidence appended locally. An approved
remote advance is automatic only when local decisions still match the authenticated baseline
or the new approved release.

CI restoration uses the workflow's fresh, disposable checkout. For a separate CI baseline when
a developer workspace has unpublished decisions, run the workflow in a new checkout with its own
`.intent` directory; do not reuse or clear the developer's state. There is no force-replacement
switch. Existing self-hosted jobs must likewise use an isolated checkout per job.

Every restore verifies the complete signed release chain, up to 64 commits including the tip.
The unsigned local marker cannot shorten that traversal. Missing Git parent objects, merges,
forks, invalid signatures, malformed genesis links and history beyond the bound fail closed.
Use the full fetched history; shallow-boundary metadata cannot establish a signed genesis.

Failed restoration preserves unfamiliar substituted state in owner-private
`.intent-quarantine-*` directories for explicit review. These can contain plaintext local state
and are never approved baselines. Keep `.intent-restore-*/` and `.intent-quarantine-*/` ignored
alongside `.intent/` in each consuming repository; do not commit or publish recovery contents.
Restore does not silently modify a consumer repository's Git configuration or ignore rules.

Local reviewed execution checks a clean commit snapshot before and after every command and both
result writes. The helper hashes actual tracked bytes against HEAD, checks executable modes and
the staged index, and binds staged evidence to file and ancestor-directory identities and
modification/change times, including the repository root. These local drift checks are not atomic
and never establish CI eligibility. CI additionally enforces immutable image material.
Index shortcuts, arbitrary ignore rules and Git replacement objects cannot authorize dirty code.
Any mismatch removes staged and final passing evidence. The internal staged snapshot is local to
this checkout; the final canonical result schema is unchanged.

Only untracked `.intent/`, `.intent-ci/`, `.venv/`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`
directories and exact reviewed `test_result_paths` are permitted as generated outputs. The helper
creates these bounded output directories and reviewed result parents before the initial snapshot.
Outputs must remain in those pre-existing, untracked directories: creating or replacing entries
along tracked ancestors (including loose outputs at the repository root) invalidates the snapshot.
Tracked files are never exempt. Every reviewed executable must be an executable regular HEAD blob,
even when its path is under a generated directory. Other outputs must be explicitly reviewed,
not merely added to `.gitignore`.

Inside immutable CI, only `.intent-ci` and `/output` are writable project-related output locations.
Reviewed tests must not depend on writing source files or the approved `.intent` baseline.
The adapter writes the aggregate result itself; configured local artifact paths are not written by
individual CI executions. Tracked `.intent` or `.intent-ci` material is unsupported in this image path.

Repository `__pycache__` directories (even empty or case-variant names) and `.pyc`/`.pyo` files
are rejected, including committed bytecode. A descriptor-based directory scan covers nested,
untracked and ignored directories that Git's file listing cannot see; it does not follow links.
It skips only root Git metadata and the explicit generated/dependency directories listed above.
The scan is capped at 16,384 entries, 1 MiB of path names and the existing snapshot deadline.
Rejected caches are left untouched for the operator to handle. They cannot shadow failing committed
Python source. Reviewed CI subprocesses inherit `PYTHONDONTWRITEBYTECODE=1` to prevent ordinary
imports from creating new repository caches. This flag prevents writes, not reads: rejection of
existing repository bytecode provides the read-side boundary. Installed dependencies inside the
explicit `.venv` directory remain part of the separately reviewed dependency environment.

Snapshot inspection is bounded to 4,096 tracked files, 16 MiB per file,
128 MiB total, 1 MiB of Git listing output and five seconds; it fails closed beyond these limits.
Tracked symlinks, submodules, hardlinks and checkout filters that change committed bytes are not
supported by this CI execution contract.
Image materialization additionally caps the combined uncompressed Git objects at 128 MiB,
4,096 tree entries, 16 MiB per blob and a 120-second object-capture deadline. The build context is
capped at 256 MiB. Compressed ancestor history cannot bypass these limits.

Configure these prerequisites before expecting the check to pass:

1. Review and activate the project baseline. Include a nonempty `test_commands` list in the
   approved shared configuration, for example `[["tools/test-runner"]]`. The first argv entry
   must be a regular executable file relative to the repository; shell strings, PATH executable
   lookup and arbitrary prompt commands are rejected. Script interpreters must satisfy the
   existing observer's vetted-interpreter contract. The PR job uses `ubuntu-24.04` with rootful
   Docker; its trusted image supplies root-owned regular `/bin/sh` and `/usr/bin/python3` rather
   than relaxing the interpreter checks for distribution symlinks. The image creates `.venv`
   with the locked test dependencies. Tests must explicitly exercise proposed code, not the
   protected adapter installed in site-packages. For a `src` layout, a reviewed `#!/bin/sh`
   wrapper can run `PYTHONPATH=src exec .venv/bin/python -m pytest -q --import-mode=importlib`.
   This source import occurs only in the untrusted test container. The host never installs the
   proposed project or invokes its packaging hooks.
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
3. First promote the reviewed launcher, adapter, hash lock, base digest and workflow to the
   protected default branch through a trusted bootstrap review. A PR cannot bootstrap its own
   trusted tooling. Protect these paths with independent code-owner review, stale-approval
   dismissal, no direct/force pushes and no ordinary bypass. Create a GitHub environment named
   `intent-ci`, with required reviewers and deployment rules allowing only that protected default
   branch. Do not make its secret available to PR refs or alternate workflows. Store
   `INTENT_CI_SHARED_STATE_TRUST` as an environment secret.
   Its strict JSON object has `schema_version: 1`, `project_id`,
   `repository_id: "github.com/owner/repository"`, `recipient_key_id`,
   `recipient_private_key_base64`, and `signing_keys`, an array of
   `{ "signature_id": "...", "public_key_base64": "..." }` records sorted by signature ID.
   Keys use unpadded URL-safe base64 for exactly 32 bytes; all configured signers must match the
   release. Provision the recipient private key and trusted signing public keys through an
   independent secure channel. Do not copy keys from a PR or commit them to Git.
4. Environment approval authorizes only protected tooling to handle the trust secret. Proposed
   code, including fork code, executes solely in its secret-free, network-disabled container.
   Never add a head checkout, PR-authored action, package install, build hook or arbitrary PR
   command to this host job. `pull_request_target` is safe here only with that separation and the
   protected workflow source; merely selecting the event or naming an environment is insufficient.
   [GitHub documents the event's protected/default-branch context and risks](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target).
5. Require this exact protected workflow through an active organization/enterprise workflow
   ruleset targeting the protected default branch. An ordinary status-name requirement alone is
   insufficient: PR-authored Actions can emit the same name. Require current-head checks and
   independent review, and restrict ruleset bypass. GitHub supports `pull_request_target` for
   required-workflow rules. If that enforcement is unavailable, use an independently pinned
   external required-workflow service; do not claim a name-only check is this enforcement boundary.
   [GitHub required-workflow rules](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets#require-workflows-to-pass-before-merging).
   Do not apply this code rule to the separate state-only branch. These repository files do not
   configure rulesets, reviewers, environments, secrets or merges.

Only `.intent-trusted/.intent-ci/test-results.json` is uploaded by the PR job, with seven-day retention.
It contains commit/project/test identifiers and ACL metadata, not decrypted canonical state or
test stdout. Do not broaden its artifact path to `.intent`, the whole checkout or raw test logs.
The nightly/manual workflow has separate concurrency and read-only source permissions. It uses
the same protected environment to restore state, captures sources, validates canonical state,
then renders and uploads the drift report before the final `intent check --require-review`.
Pending review still fails the run, but its report remains available. Restore, capture, validation
and rendering failures are not masked. It does not claim fresh repository test execution. Its
generated drift report also has seven-day retention.

For an onboarded local checkout, the first ordinary prompt checks readiness automatically; the
developer does not need to type an Intent command per task. When approved shared-state trust is
configured, the same prompt path verifies and restores the signed baseline before immutable
readiness, then starts or reuses the repository-bound `intent dev` service with no browser popup.
Verification first performs a bounded, output-capped, credential-free fetch of the one fixed
`origin/intent-state` ref; it never checks out or merges remote code, and an absent/offline ref does
not silently certify an older remote-tracking tip.
A repository with neither local state nor an approved shared baseline still receives the
non-mutating onboarding offer. The offline release proof covers a real fresh clone, automatic signed
baseline restore, automatic service startup, the first prompt, an implementation/test commit,
local check/replay, and ambiguous/conflicting cases that block checks when the plugin is absent.
A separate explicit rootful-Docker release gate proves immutable CI execution and consumption.
Passing that check means the configured deterministic checks passed; it does not declare all
software semantically complete.

The running control plane passively polls the bounded development observer at a fixed cadence even
when no browser page is open. Home reads only its latest detached projection. A user may explicitly
run one reviewed test command through the protected loopback API; the request contains the reviewed
command ID and is subject to exact origin, CSRF, JSON, timeout, output, repository, and executable
bindings. Background polling never executes tests, mutates the graph, or records completion.
Because the headless service cannot share its in-memory CSRF bootstrap, a later ordinary
`intent dev` invocation re-attests and stops that exact owner, acquires the same repository lease,
and opens a replacement using a new ephemeral fragment. Status and explicit `--no-open` calls
still reuse the owner. Bootstrap bytes remain absent from metadata, argv, environment and logs.
If the ACL-filtered Inbox cannot be projected, status returns the fixed unavailable response; it
does not rewrite storage or authority failures as an empty ready Home view.

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
