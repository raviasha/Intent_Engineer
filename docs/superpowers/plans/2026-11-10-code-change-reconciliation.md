# Code-change reconciliation implementation plan

Spec: `docs/superpowers/specs/2026-11-10-code-change-reconciliation.md`

Execute natively in the existing source-refresh worktree. The user already
authorized implementation and pushing to main. Preserve all canonical validation
and authorization contracts; no test-to-code tracing or assessed code changes.

## Task 1: snapshot and publication support

Add verified absence snapshots and a public, ACL-filtered assessment input view
to `initial_reconciliation.py`. Add tests for removed files, reappearance during
review, source revocation and directory membership changes before publication.
Retain stable file IDs and supersede obsolete service-owned mappings.

Interface: assessment files carry `missing: bool = False`; start verifies absence,
snapshot evidence records it, and completion verifies it again. Existing callers
need no new fields. Input view returns exact nodes/evidence for an accessible run.

Run initial reconciliation integration tests: expect all pass after observing the
new tests fail. Commit the cohesive snapshot change.

## Task 2: durable automatic worker

Implement `CodeReconciliationService` and a read-only `CodexReviewRunner`.
State is user-local and checkout-bound. Nonblocking worker leases, debounce,
snapshot membership, checkpoint/resume and final revalidation are required.
Tests use a fixed-time fixture and a fake semantic runner; no model/network in CI.
Exercise edits/additions/deletions, duplicate events, no changes, invalid output,
unavailable CLI, stale graph/sources/code, retry, checkpoint resume and no code writes.

Interface: `intent reconcile-code --project .` reports status; `--run` signals and
drains background work; `--disable`/`--enable` control automatic work locally.
New CLI adapters call public services. Initial ready assessments enable detection
by default unless explicitly disabled. One worker owns semantic execution at a time.

Run worker integration tests and strict lint/type checks; expect pass. Commit.

## Task 3: hook integration and documentation

Add a silent async reconciliation hook and wire supported events. Test malformed
inputs, unsupported/subagent events, missing runtime and normal dispatch. Document
hook trust, local CLI auth, scope, background usage, statuses and next-event
detection of edits made outside Codex. Bump plugin version and contract tests.
Update foundational scope, roadmap, onboarding guidance and quick start.

Run focused CLI/plugin/onboarding tests, then full offline suite, ruff and mypy.
Expect all pass with only existing explicit release-gate skips. Commit.

## Review Focus

- Lease handoff and an edit arriving during publication or shutdown.
- Symlink and excluded paths, removed parent directories and new membership.
- ACL revocation and stale provider evidence during a long semantic review.
- Codex subprocess isolation, recursive hooks and child-process cleanup.
- No false success when a runner is absent or structured output is incomplete.

Use one fresh-context final reviewer, fix material findings and verify again.
Push normal HEAD to GitHub main, verify the remote SHA and CI outcome.
