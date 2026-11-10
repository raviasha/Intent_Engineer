# Background reconciliation after code changes

The user requires code changes to trigger background comparison of intent,
requirements and code, flagging gaps and recommendations without changing assessed
code or interrupting development. Designated source intent remains highest priority;
test-to-code tracing remains deferred.

Trusted asynchronous Codex hooks run on session start, prompt submission, tool
completion and turn completion. They invoke a silent worker, separate from the
foreground agent. External-editor changes are detected at the next hook event;
this release does not install an always-on filesystem daemon. Hook installation
does not bypass Codex's per-installation review and trust.

After a completed initial assessment, detect changed content and membership in the
declared code directories, plus the exact declared requirement files. Include new
nonignored Git files in those directories; retain removed files as verified absence
evidence. Exclude canonical state, Git internals and configured exclusions. A
non-Git project supports its explicit baseline files. Record the exact assessed
scope; never claim repository-wide coverage from a partial initial scope.

One worker per checkout coalesces bursts and checkpoints semantic comparisons.
An authenticated local Codex CLI performs a separate read-only review of immutable
snapshots, with plugins, apps and hooks disabled. Only the deterministic parent
may publish validated evidence, graph traces and reconciliation cases. No assessed
code, PRD, external source or hook-trust configuration is writable by this workflow.
The worker reviews captured source evidence, not live provider state; selected
sources already marked dirty/unreconciled prevent completion and remain pending
for the existing host source-refresh workflow. An idle event is not a freshness
claim.

Use existing reconciliation validation, ACL checks and canonical transactions.
Before publication, recheck graph/source/file snapshots and directory membership.
Stale reviews are discarded and queued against the latest files. Missing runtime,
authentication, invalid output, unavailable evidence or unreadable files remain
incomplete/retryable, never a false alignment claim. Crashes retain checkpointed
work. Findings are available on demand and through normal task context; hooks
never block a tool, continue a stopped turn, ask questions or emit notifications.

Background execution uses Codex account/model usage. No separate API key is
required when the local CLI is already signed in. Installation instructions must
make the local CLI prerequisite, scope and trigger coverage explicit.
