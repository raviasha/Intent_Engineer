# GitHub integration (local public alpha)

Intent Engineering can ingest GitHub repository evidence using credentials that
already exist on your machine. It is optional: Markdown and Git sources remain
local-only and do not require a network connection or a secret.

## Install and initialize

Use Python 3.12 or newer. In a clean checkout, create the ignored local
workspace before validation:

```bash
python -m pip install -e '.[dev]'
intent init --project .
intent validate --project .
```

Set the repository scope explicitly with the built-in-string environment value;
it is an identifier, not a credential:

```bash
export GITHUB_REPOSITORY=owner/repository
```

For a local credential, Intent Engineering chooses a nonblank `GH_TOKEN` first.
If it is unset or blank, it uses an existing GitHub CLI login established with:

```bash
gh auth login
```

The token remains in memory only. It is never written to `.intent`, evidence,
checkpoints, reports, logs, or error messages. Grant the least read permission
needed for repository contents, issues, and pull requests; do not grant write
permission for evidence ingestion alone. The separately confirmed team-state setup
below has a different, explicitly write-authorized credential contract.

## Reviewed GitHub team-state setup

`intent team enable github` currently requires an organization-owned repository,
a classic setup token with `repo` and `admin:org`, repository admin/read/write
permission, and an independent CI recipient. The generated CI job uses read-only
permissions; it does not inherit this setup credential. Follow the complete
[dedicated CI recipient and operator setup](ci-recipient.md).

Before local staging, the default branch must already enforce admins, PR approval,
stale-review dismissal, no bypass, and disabled force pushes/deletions. The first
WebAuthn decision stages all three suggestions only. Commit and merge the exact state
workflow, code-check workflow, and
CODEOWNERS through protected default-branch review. Setup verifies the remote bytes
and code-owner enforcement before a second protection authorization.

The organization runner group must be named `intent-state`, non-default, visible
only to selected repositories, and restricted to this repository's exact
`.github/workflows/intent-state.yml@refs/heads/<default-branch>` and
`.github/workflows/intent-check.yml@refs/heads/<default-branch>` workflows, with no
additional workflow entries. The
registered runner name must equal the CI descriptor's runner ID and have both
`self-hosted` and `intent-state` labels; the workflow selects the group explicitly.

An active, no-bypass required-workflow ruleset must target `refs/heads/intent-state`
and source that exact workflow from the default-branch ref. Set
`do_not_enforce_on_create: true` only for the initial reviewed empty orphan; later
updates remain governed. Default-branch-only enforcement does not protect state PRs.
The required `Intent Engineering / state` status and GitHub Actions app ID `15368`
are supplementary—not substitutes for this workflow-source rule or runner isolation.
Default CODEOWNERS protects tooling, while state PRs use generic human review and
retain exactly three encrypted/signed artifacts.

The default branch needs its own active, no-bypass required-workflow rule sourcing
`.github/workflows/intent-check.yml` from the exact protected default-branch ref,
without a creation exemption. Its strict required `Intent Engineering / check` status
is also bound to Actions app ID `15368`. Setup verifies both effective rules and
canonical workflow bytes, not just their status names.

Keep public bootstrap/publication receipts when a provider response is ambiguous.
Cancel is unavailable once recovery authority is required; retry/inspect instead.
After a PR opens, merge in GitHub and refresh to verify its exact merged artifacts
before local trust is established. No recipient private key belongs in GitHub Secrets.

## Diagnose and sync

Confirm access before syncing:

```bash
intent doctor github --project . --format json
```

The result reports the credential source (`environment` or `github_cli`), the
canonical repository scope, access status, and safe rate-limit fields: limit,
remaining, used, reset time, and resource. Authentication, permission,
rate-limit, and protocol failures use fixed redacted diagnostics; provider
bodies, headers, and tokens are not displayed.

Then sync GitHub alone or in one combined run with local sources:

```bash
intent sync --project . --sources github --format json
intent sync --project . --sources markdown,git,github --format json
```

Repeated identical syncs are idempotent: they add no duplicate evidence, graph
changes, cases, or checkpoint mutation. A GitHub-only failed sync exits with
code 1. In a mixed-source run, surviving local connectors plus a failed GitHub
connector produce a partial result and exit with code 3; durable earlier work is
kept and the failed checkpoint remains unchanged. Retry after the provider is
available. Respect a reported rate-limit reset time before retrying.

Create a deterministic authorization-filtered review report with:

```bash
intent drift --project . --format markdown --output intent-drift.md
```

The Markdown report contains authorized open cases, evidence sides, affected
references, impact, and deterministic recommendations. Ordinary open cases exit
0. Require a review gate explicitly when automation needs it:

```bash
intent drift --project . --format markdown --require-review
```

That explicit mode exits 4 when an authorized open case exists. The report is
ACL-filtered and does not include a credential.

## Repository Action

`.github/workflows/intent-sync.yml` runs on `workflow_dispatch` and the nightly
cron `17 2 * * *`. Its exact read-only permissions are `contents: read`,
`issues: read`, and `pull-requests: read`. The ordered command sequence is:

```bash
python -m pip install .
python -m intent_engineering.integrations.github_action restore
intent sync --project . --sources markdown,git,github
intent validate --project .
intent drift --project . --format markdown --output intent-drift.md
```

Because `.intent` is ignored, a clean checkout restores its approved baseline from the fetched
protected `intent-state` ref. The older sync workflow uses a legacy environment-trust
adapter and is not the production key-provisioning path. Do not place recipient private
keys in GitHub Secrets. New team setup requires a [dedicated CI recipient](ci-recipient.md)
whose key remains in its self-hosted runner OS keyring. Missing keys, invalid signatures
or incompatible state fail closed. The workflow
never runs `intent init` and cannot report an empty graph version 0 as clean. See
[CI setup and trust prerequisites](intent-aware-agent.md#required-github-check-setup).
After uploading the report, the final step runs `intent check --require-review`. Pending review
therefore fails the workflow with exit 4 only after the artifact is available. Restore, capture,
validation and rendering failures still stop the workflow; no failing command is masked.

The sync step supplies `GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}` and
`GITHUB_REPOSITORY: ${{ github.repository }}`. The workflow uploads the exact
`intent-drift.md` path as the `intent-drift` artifact with seven-day retention. The separate
`.github/workflows/intent-check.yml` PR job runs reviewed repository tests and the required
`Intent Engineering / check` status without any plugin dependency. Its GitHub Actions
`GITHUB_TOKEN` is ephemeral and distinct from the local CLI credential sources
above. It performs no external write.

The code workflow excludes `intent-state` PRs; the separate `Intent Engineering / state`
job accepts only those PRs and validates their three release artifacts without executing
candidate code. Both checked-in workflows match their packaged canonical assets, run
only the exact protected `github.workflow_sha`, and use the same restricted self-hosted
keyring runner. Code checks restore signed approved state before disposable sandbox tests
and independent final assurance. Neither required workflow accepts GitHub-hosted private
key JSON, uploads artifacts, uses Actions caches, comments, or auto-merges. Per-PR
concurrency cancels superseded runs. See [runner provisioning](ci-recipient.md).

## Scope and testing boundary

Hosted OAuth, GitHub App installation, webhooks, and pull-request annotations or
comments are non-goals for evidence ingestion. Its commands remain read-only;
the separately reviewed team-state setup above performs only its bounded authorized
provider writes. MCP write-back and Slack, Notion, Jira, and Confluence integrations are
not claimed as complete here.

All shipped tests use a deterministic fake GitHub API. An optional live smoke
test is user-run only and is never release-completion evidence.
