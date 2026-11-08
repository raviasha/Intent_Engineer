# Frictionless Onboarding Implementation Plan

**Goal:** Create or defer repository intent graphs conversationally with minimal setup.
**Architecture:** Extend advisory hooks and onboarding CLI; retain existing bootstrap and review.
**Spec:** `docs/superpowers/specs/2026-11-08-frictionless-onboarding-design.md`

## Constraints and review focus

No automatic graph approval; no source overwrites; no prompt content in reminder state;
bounded, safe local files; no repeated completion counts; a deferred reminder cannot mask
readiness failures or a pending graph review. Keep the installed runtime backward compatible.

## Tasks

1. Test then implement a repository-scoped reminder store, explicit-creation routing and
   bounded Stop hook. Cover three completions, retries, subagents, repository isolation,
   malformed cache data and a missing graph without creating `.intent`.
2. Test then implement PRD discovery, inferred-source capture and CLI bootstrap submission.
   Cover ambiguous/missing PRDs, exclusions and links, invalid/stale proposals and retained
   human approval. Preserve existing onboarding command contracts.
3. Update the advisor skill and quick start for complete conversational setup, code-derived
   PRDs, deferral and on-demand creation. Test real hook/CLI flows, run lint/type checks and
   the full suite, then obtain a fresh branch review and fix material findings.

Implementation is inline in the existing isolated worktree, on a feature branch. Use tests
first for production behavior. The prior main commit has a passing full GitHub test run.
