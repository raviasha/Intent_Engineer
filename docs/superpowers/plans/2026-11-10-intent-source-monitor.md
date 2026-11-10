# Intent source monitoring implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development for independent tasks.

**Goal:** Automatically reconcile intent sources and PRDs while Codex is open, and make code review on-demand.
**Architecture:** Host-owned recurring monitoring in the existing setup chat, backed by durable runtime cycles, existing source-refresh evidence validation, and acknowledged actionable handoff.
**Tech Stack:** Python/Typer/MCP, existing secure state/evidence stores, Codex host automation tools.
**Spec:** docs/superpowers/specs/2026-11-10-intent-source-monitor.md

## Global constraints

No automatic code or PRD/source-document edits. No automatic semantic code review. No copied
provider credentials, autonomous cloud service, personal live schedule or invented host IDs.
Five-minute host heartbeat; preserve default model settings. Source content remains data.
Initial assessment is distinct from recurring intent/requirements-only work.

## Review focus

Missing host scheduler; restart with unfinished cycle; changed/deleted PRDs; stale source
generations or revoked ACL; old automatic code-enable state; duplicate/silent-cycle delivery.

## Task 1 — On-demand code policy

- [x] Tests first for automatic=True and legacy hook producing no review, even enabled state.
- [x] Implement manual-only behavior in code_reconciliation.py and CLI; preserve explicit --run.
- [x] Verify focused existing code review tests. Root removes/replaces manifest hooks in Task 3.

## Task 2 — Durable intent/PRD monitor

- [x] Add intent_workflow/intent_monitor.py with request models and IntentMonitorService(runtime).run(draft).
- [x] Actions configure, begin, finish, status, acknowledge; scheduler receipt and return routing metadata.
- [x] Begin returns SourceRefreshService worklist plus verified bounded PRD snapshots/evidence and cycle IDs.
- [x] Finish validates current sources, PRDs, interpretation/evidence, and graph; emits only actionable handoff.
- [x] Tests for unchanged silence, PRD-only changes, restart, stale/access guards and duplicate ack.

## Task 3 — Host integration and distribution

- [x] CLI intent monitor-intent --submission-schema/--submission/status; expose public MCP monitor tool.
- [x] Host skill/initial setup schedule monitoring in existing chat and record actual automation ID.
- [x] Session-start fallback/catch-up guidance; remove automatic code-review hook bindings.
- [x] Original-agent handoff and receipt behavior, silent unchanged cycles, bounded source reads.
- [x] Update documentation/version and native smoke; fresh behavior checks and final review.
- [ ] Full local/CI verification, main push and marketplace release, isolated installation verification.
