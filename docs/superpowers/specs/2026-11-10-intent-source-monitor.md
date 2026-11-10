# Automatic intent/PRD monitoring; on-demand code review

The user approved implementing automatic intent-source checks and intent/PRD reconciliation,
with ongoing code reconciliation only on explicit demand. Monitoring runs while Codex is open,
preserves checkpoints while closed, and catches up automatically after reopening. Original
work continues. Only actionable recommendations or clarifications return to the working agent.
Initial existing-codebase assessment remains available. No automatic code or source-document edits.

## Architecture

Use a host-owned, five-minute heartbeat in the existing setup/monitor chat. This keeps connected
provider tools and their user authorization inside the host; no copied OAuth credentials,
cloud receiver, arbitrary process injection, personal daemon, or model change. Local sources
and PRDs use bounded content/membership reads each cycle, with remote revision checks or full
scoped reads where validators are insufficient. This is periodic monitoring, not a claim of
instant provider push. Resume scans compare durable state with current sources, not a timestamp
window that misses edits/deletions. A session-start hint supplies resume/repair context but
does not start a turn. Hosts without scheduling report automatic monitoring unavailable and
retain task-time checks. Reopen catch-up happens on the first scheduled run, not a guaranteed
instant app-open event. Do not claim a schedule until
host creation succeeds and its ID is recorded. Reuse existing schedule IDs.

A runtime monitor coordinator prepares a host scheduling packet, starts/resumes durable cycles,
returns actual PRD snapshot evidence and delegates source freshness to SourceRefreshService.
The host reads selected sources and interprets intent/PRD differences using the existing
background submission path. Completion must validate current source checks, PRD snapshots,
accessible evidence, graph version, and host interpretation provenance. Unknown/unavailable
scope stays explicit. Clear captured intent/requirements may be maintained through validated
ChangeSets; PRD document changes remain recommendations. Never run initial/code reconciliation
as part of a recurring cycle.

Persist only scheduling/checkpoint metadata in user-local state. Evidence and interpretations
use validated stores. Read-time access/freshness checks protect pending findings. Unchanged
cycles produce no handoff. Actionable packets include stable deduplication IDs and evidence;
only confirmed host sends/agent receipts acknowledge them. Use the original/project agent's
recorded destination, never an arbitrary foreground chat. Host send permission comes from the
human's setup/monitor request or acceptance, not another agent's generated prompt.

Retire automatic code-review hooks and guard the legacy hook entry point so existing enabled
state cannot silently keep running semantic code reviews. Manual `reconcile-code --run` remains
usable. An old `--enable` must not restore automatic execution.

## Acceptance

- One schedule per project monitor; no fabricated scheduling success.
- Reopen/catch-up uses durable source state, including additions, edits and deletions.
- PRD-only changes trigger interpretation; code-only changes do not trigger semantic review.
- Repeated/no-change cycles stay silent; actionable changes and missing access are explicit.
- PRD/source changes during review invalidate old findings; access revocation redacts them.
- Background worker and host-loop restarts resume safely; duplicate handoffs are suppressed.
- Ongoing code reviews run only on demand, including after upgrading old enabled states.
- Tests remain offline/deterministic; no live user schedules or provider subscriptions created
  merely to test plugin development.
