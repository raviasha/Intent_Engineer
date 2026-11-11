# Hourly monitoring, shared freshness, and batching

User-approved scope: replace five-minute monitoring with hourly monitoring, share cached source
checks across chats, batch reconciliation, preserve task alignment and reopen catch-up, publish main
and marketplace. No idle detection, new webhook infrastructure, automatic code review, or document edits.

Use a one-hour maximum age for successful source checks, shared within the same project/local actor.
Cache validity binds exact scope, source generation, complete manifest/validator, original read time,
and current evidence permissions. Known changes, failed/partial checks, scope changes and forced
catch-up invalidate reuse. Never extend age by copying a receipt. Reused evidence remains explicitly
cached, not a new provider observation. Task relevance/alignment and current graph/blocker checks
still run for every development task. Missing information stays unknown.

One hourly host heartbeat per project; upgrade an existing five-minute schedule in place with its
actual automation ID and confirmed host receipt. One bounded owner lease batches all pending source
and PRD updates into the current cycle; competing workers return busy without reading providers or
starting interpretation. Expired workers cannot publish after takeover. Preserve durable checkpoints,
freshness/ACL validation and actionable original-agent delivery. No change stays quiet.

A SessionStart startup/resume hint records a bounded catch-up request in user-local metadata; the
next host monitor run bypasses shared freshness once, then completes/clears that request only after
fresh checks. Hook does not wake an agent; keep the existing first-scheduled-run catch-up guarantee,
and task-time alignment remains independent. No live personal automation created for development.

Ruling: maximum cached freshness is one hour; unsignaled remote updates may remain unknown for up
to that window. Known changes and catch-up bypass it. Cross-project/actor cache sharing is forbidden.
