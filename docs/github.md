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
permission for this read-only slice.

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
protected `intent-state` ref using `INTENT_CI_SHARED_STATE_TRUST` in the protected `intent-ci`
environment. Missing keys, invalid signatures or incompatible state fail closed. The workflow
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

## Scope and testing boundary

Hosted OAuth, GitHub App installation, webhooks, pull-request annotations or
comments, and all external writes are non-goals for this local public-alpha
slice. MCP write-back and Slack, Notion, Jira, and Confluence integrations are
not claimed as complete here.

All shipped tests use a deterministic fake GitHub API. An optional live smoke
test is user-run only and is never release-completion evidence.
