# Hourly Monitoring Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the independent cache and monitor changes, then integration/review.

**Goal:** Reduce polling overhead with hourly monitoring, shared freshness and batched work.
**Architecture:** Actor/project scoped source-check reuse, single-owner durable monitor cycles, host-owned hourly schedule and reopen freshness invalidation.
**Tech Stack:** Python/Typer/MCP, validated evidence ledger, secure metadata, host scheduling skill.
**Spec:** docs/superpowers/specs/2026-11-11-hourly-monitoring.md

## Global Constraints
No automatic code/source/PRD edits or semantic code reviews. Preserve task alignment and provenance.
One hour is a maximum cache age; no sliding TTL. Force catch-up and explicit invalidations bypass cache.
No new live user schedule or external messages during tests. Existing publishing approval applies.

## Review Focus
- Cached receipts cannot conceal scope/generation/ACL changes or refresh their original timestamp.
- Expiry applies to long-lived tasks as well as new chats; unknown/failed checks cannot be reused.
- Concurrent owners and expired worker finish cannot duplicate or publish stale batches.
- Restart/catch-up survives crashes and does not clear until fresh verification succeeds.
- Existing schedules upgrade in place; task hooks retain alignment and code remains on demand.

### Task 1: Shared source cache
Files: source_refresh.py; integration tests for source refresh.
Interface: RefreshRequest.force_refresh: bool=False; successful check reuse <=one hour, original
receipt/time retained, explicit cached/checked_at/expires_at metadata. Monitor consumes same API.
- [ ] Add failing tests for cross-chat reuse, expiry, forced checks, invalidation and ACL isolation.
- [ ] Implement bounded reuse of current successful checks and preserve task alignment/blockers.
- [ ] Run focused tests and review.

### Task 2: Hourly batched monitor
Files: intent_monitor.py; monitor integration tests. Root owns scripts/hooks/docs/CLI/MCP text.
Interface: MonitorRequest.worker_id optional, force_refresh optional; busy response for another
live owner; 15-minute renewable lease and expired-owner rejection. begin/finish consume worker_id.
Class method request_catch_up_for_project(Path) records pending reopen marker without runtime reads.
- [ ] Test hourly packet, competing/resumed workers, lease takeover, catch-up persistence and cache bypass.
- [ ] Implement batched lease and durable catch-up marker; reuse unfinished snapshots safely.
- [ ] Run focused tests and review.

### Task 3: Integration and release
Files: monitor-hook.py, skill/references, onboarding prompt/MCP instructions, foundation docs,
README/quick-start, plugin version/contract, scripts/smoke_plugin.py.
- [ ] Wire startup/resume marker, hourly schedule migration, worker protocol and cached task checks.
- [ ] Verify hook/transport and packaged smoke; update version to 0.1.12.
- [ ] Review complete change, fix findings, run full suite/lint/types.
- [ ] Push main, publish marketplace, verify fresh isolated installation and record evidence.
