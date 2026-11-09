# Task-start authoritative source refresh

The user asked to implement the agreed workflow on November 9, then refined it to
avoid rereading unchanged sources. Selected bounded source material is authoritative
for downstream intent. Refresh before the next development task. When authoritative
sources disagree, show evidence and a recommended resolution; the user decides.

## Behavior

The existing UserPromptSubmit hook instructs the host agent to begin a refresh before
development. Ordinary questions and unrelated tasks do not require provider reads.
The hook stays prompt-only; it cannot access the host's authenticated provider tools
and must not pretend to enforce arbitrary shell or editor operations.

Persist explicit source selections, their authority, bounded scope, and provenance.
Do not silently promote every old captured document into an authoritative source.
At task start return a deterministic work list: fetch an uninitialized or signaled
source; otherwise check its scope-wide revision marker. A verified unchanged marker
reuses captured content. A marker must cover edits, deletions, scope membership and
permission changes; a Slack channel's latest-message timestamp is insufficient.
Where a provider lacks such a marker, reread the bounded scope. Receipts are
host-reported evidence, not independent provider authentication.

Provider events can invalidate a source through a local signal interface. Signals
are hints that only make a source dirty. They cannot make it clean, change intent,
or execute instructions. Do not claim provider subscriptions are installed: the
current host connectors expose reads but no subscription capability. Future verified
event bridges can call the same interface. Best-effort event silence never proves
freshness; fall back to lightweight metadata checks.

Record task-bound successful checks, source generations, complete captured manifests
and revision markers. A failed/partial read cannot mark a source fresh. A signal or
source reconfiguration during refresh invalidates an older result. Scope collections
must include new and deleted objects, not only already captured pages/messages.

Changed content requires a current background interpretation citing the checked
evidence. Readiness remains pending while relevant conflicts or questions are
unresolved. Use the existing evidence-backed conflict/question/answer service, retain
both positions, and record the user's decision. Recheck source generations before
returning ready. Never resolve a conflict merely by picking the newest timestamp.

Core state and credentials stay local. Source selection and task/check receipts are
scoped to the local actor and stored through descriptor-rooted transactions. Original
source evidence is immutable. Existing legacy proposals, approvals and external-write
contracts are preserved. Code/test matching remains deferred.

## Validation

Use offline real-store tests for initial refresh, unchanged metadata reuse, changed
revisions, deletions/empty collections, stale receipts, duplicate and mid-refresh
signals, scope changes, unavailable/partial reads, inaccessible evidence, task replay,
and conflicting-source resolution. Exercise CLI, MCP and actual hook output. Run the
full suite and linters before updating the installed CLI/plugin; smoke-test the
installed runtime in an isolated fixture.

## Provider capability evidence

- Notion: https://developers.notion.com/reference/webhooks
- Slack: https://docs.slack.dev/apis/events-api/
- Confluence: https://developer.atlassian.com/cloud/confluence/modules/webhook/

These provider mechanisms need separate app subscriptions/delivery infrastructure.
They are not automatically supplied by installing the Intent Advisor host plugin.
