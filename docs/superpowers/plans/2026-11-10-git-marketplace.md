# Git Marketplace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Publish an installable Git marketplace plugin with its own Python runtime.
**Architecture:** Platform builds assemble relocatable CPython and locked dependencies with the existing plugin. A plugin-relative launcher serves CLI, MCP and hooks. A gated publication job commits complete packages to the marketplace branch.
**Tech Stack:** Python 3.12, POSIX shell, GitHub Actions, python-build-standalone.
**Spec:** docs/superpowers/specs/2026-11-10-git-marketplace-design.md (approved November 10).

## Global Constraints

- Preserve local-first graph semantics, evidence and read-only reconciliation.
- No manual clone, system Python, global intent, Git LFS or first-run dependency downloads.
- Support macOS arm64/x86_64 and Linux x86_64; explicitly reject unsupported platforms.
- Never grant hook trust or change the user's existing plugin installation.
- Publish generated runtime files only to marketplace; source changes go to main.

## Review Focus

- Spaces and unrelated working directories must preserve arguments and project selection.
- Missing runtime must fail visibly; source-development fallback must not conceal broken releases.
- Child services must use packaged Python and resolve packaged CLI commands.
- Desktop Codex discovery must work without a separately installed CLI.
- Partial builds or racing releases must never replace a complete marketplace package.

### Task 1: Portable plugin entry points

**Files:** plugin scripts, MCP configuration, codex_review.py, new runtime tests and existing plugin contracts.
**Interfaces:** `scripts/intent` accepts existing CLI arguments or `--hook NAME`; bundled runtime path is `runtime/PLATFORM/python/bin/python3`.
- [ ] Write failing launch tests for spaces, cwd, missing runtime and unsupported platform, plus desktop executable resolution tests.
- [ ] Implement shell dispatch and Python hook/CLI entry, preserving exec cancellation and source-developer support.
- [ ] Route MCP and hook wrappers through packaged entry; test real existing hook contracts.
- [ ] Verify focused tests, lint and strict types; commit.

### Task 2: Reproducible runtime builds

**Files:** scripts/plugin_distribution.py, packaging/runtime-sources.json, packaging/runtime.lock, tests/unit/test_plugin_distribution.py, scripts/smoke_plugin.py.
**Interfaces:** build accepts target/output/source revision; assemble accepts tested platform folders and output; verify checks manifest hashes and file limits.
- [ ] Write failing tests for archive/hash validation, manifest integrity and platform completeness.
- [ ] Pin standalone archives and dependency versions/hashes; build native packages with licenses and source provenance.
- [ ] Add relocated smoke checks for CLI, MCP, hooks, subprocesses and synthetic reconciliation using controlled fixtures.
- [ ] Build and run native package with Python/intent absent from PATH; commit.

### Task 3: Marketplace publication

**Files:** .github/workflows/plugin-marketplace.yml, docs/quick-start.md, README.md, plugin skill references and plugin version contracts.
**Interfaces:** workflow builds three platforms, verifies all artifacts, and normally commits to marketplace only after every check passes.
- [ ] Add matrix build and gated serialized publication with version/provenance validation and previous-release history.
- [ ] Update install and update commands, platform support, source-developer fallback and bundled CLI instructions.
- [ ] Run full tests, lint and types; obtain independent review and fix material findings.
- [ ] Push main, run release workflow, verify CI and marketplace publication.
- [ ] Install published Git marketplace in an isolated Codex profile and verify the cached package; record results.
