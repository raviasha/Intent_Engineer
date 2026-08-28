# Task 8 report — agent-host contract and Codex capability gate

## Status

`PRECOMMIT_REVIEW_READY`, unstaged and uncommitted at exact base
`d91beef4b0ab0b2caba8bbfebd1f4999f1b8b2a2`. The final outcome is the approved unsupported
mandatory-mode branch. Provider-neutral host lifecycle contracts are preserved for reuse; Codex
mandatory activation fails with the fixed `MandatoryHookUnavailable` error.

## Binary installed-contract ruling

The installed executable `/Applications/ChatGPT.app/Contents/Resources/codex` reports
`codex-cli 0.148.0-alpha.9`; local `features list` reports stable/enabled `hooks`, `plugins`, and
`unified_exec`. The official [Codex hooks contract](https://developers.openai.com/codex/hooks),
audited on 2026-08-27, documents synchronous command `PreToolUse` denial and argument rewriting for
known native tool paths. It also says hooks are a guardrail rather than a complete enforcement
boundary, permits specialized tool paths to opt out of the default hook path, and does not deliver
another `PreToolUse` event for `write_stdin` interaction with an existing unified-exec session.

Those limitations fail the approved requirement that every repository mutation be denied before
effect unless it has an exact live Task 7 authorization. Enumerating known native tools cannot prove
the absence of a mutating opt-out path. Therefore the detector records
`specialized_paths_may_bypass_hooks=True`, `continuation_pretool_hook_complete=False`, and
`complete_mutation_coverage=False`; mandatory activation raises the same fixed error before any
repository effect. No advisory skill, prompt, post-tool notice, or partial hook is represented as
enforcement.

## Retained implementation

- Strict, frozen, deeply detached host-neutral task, result, and mutation-decision records.
- A provider-neutral `AgentHostAdapter` protocol and `IntentAgentHostAdapter`. Enabled mode delegates
  to the versioned Task 7 preflight and exact live authorization-verification surface without adding
  graph semantics. Workflow request/actor responses and later mutations are bound to the exact
  issued detached task; completion revokes private state even when the result is malformed. Disabled
  mode makes no workflow call and preserves ordinary host behavior.
- Bounded UTC timestamps, UTF-8 identities and requests, canonical relative paths, item counts, and
  private in-memory token handling. Raw request and capability material are not model fields.
- Exact cancellation identity preservation with traceback-local scrubbing at the reusable adapter
  boundary.
- A small local Codex version/feature detector and strict capability record. It preserves the
  detected positive capabilities while recording the official completeness failure explicitly.
- A binary `CodexIntentAdapter` gate. Nonmandatory construction is transparent and detached;
  mandatory construction rejects any incomplete contract with fixed `MandatoryHookUnavailable`.

## Rejected prototype and scope cleanup

A supported command-hook/IPC bridge prototype reached focused GREEN, including official SessionStart
shape recovery and conservative shell-path regressions. Independent review then identified the
official complete-coverage limitation plus concrete classifier/startup risks. The prototype was
rejected at the architectural boundary rather than relabeled as mandatory enforcement.

The untracked `plugins/intent-preflight` prototype was removed completely, as required for an
unsupported outcome. All prototype changes to Task 7 MCP registration/lifecycle and the CLI were
restored byte-for-byte to the exact base. No marketplace, global install, trust, Codex configuration,
provider/model/network call, credential write, or human approval was performed.

## TDD and verification evidence

- Required initial RED before production: the exact agent-host command failed collection with two
  `ModuleNotFoundError` errors for the missing package in `0.05s`.
- Unsupported-branch RED: `17 failed in 0.57s`, covering the missing completeness fields/detector
  ruling and still-present plugin bundle.
- Unsupported-branch focused GREEN after grouped review fixes: `42 passed in 0.24s`. The final
  regression round covers caller-forged capability completeness, substituted workflow request/actor
  bindings, modified issued tasks, malformed completion revocation, exact scalar/container types,
  canonical UTC `Z` JSON roundtrips, and direct capability reachability from cancellation traceback
  frames.
- Required Task 8/Task 7/MCP/wire compatibility gate: `117 passed in 3.33s`.
- Scoped Ruff: clean.
- Full mypy: `Success: no issues found in 115 source files`.
- Plugin validation: not applicable because the unsupported branch must not ship a plugin directory.
- Fresh post-fix full offline warnings-as-errors suite: `1464 passed in 50.57s`.
- `git diff --check`: clean.
- Final finding-only independent review: `0 Critical / 0 Important / 0 Minor`, Ready. The exact
  cancellation-reachability/canonical-timestamp selector was `7 passed`.

Protected untracked artifacts remain untouched. No files are staged or committed.
