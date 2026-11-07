# Final readiness remediation — 2026-09-08

## Scope and result

This remediation addresses the final plan review's three readiness findings:

1. A populated approved graph no longer hides an undecided proposal. Readiness returns
   `human_attention_required` at `proposal` when proposal review is the remaining work;
   outstanding cases or unanswered clarification retain their `inbox` route. The initial
   baseline review route remains unchanged. A proposal ledger above the 256-proposal inspection
   budget fails closed, including a decided prefix that would otherwise hide a pending tail.
2. Readiness passes one immutable mapping of the canonical configuration, configured graph,
   evidence, ChangeSet history, reconciliation cases, receipts and checkpoints to the existing
   `validate_canonical_snapshot` API. The proposal/decision/clarification ledger is parsed from
   that same capture. It does not reopen stores or introduce a second interpretation of canonical
   evidence, history replay or checkpoint validity.
3. All readiness inputs are read through nonblocking, no-follow, held regular-file descriptors.
   Per-file size, aggregate size, descriptor identity, link count, timestamps, ancestor identity
   and transaction-journal absence are checked before readiness can return a detached runtime.

The bundled prompt hook now stops at fixed Proposal review or Team state guidance for these
conditions. Production-path regressions verify that it neither captures the prompt nor requests
classification, exposes no prompt/proposal contents, and leaves canonical bytes unchanged on replay.

## Implementation and limits

The capture includes these exact inputs:

| Snapshot input | Canonical file |
| --- | --- |
| Configuration | `config.yaml` |
| Graph | The bounded configured path below `.intent` |
| Evidence | `evidence/evidence.jsonl` |
| ChangeSet history | `history/changesets.jsonl` |
| Reconciliation | `reconciliation/cases.jsonl` |
| Receipts | `approvals/receipts.jsonl` |
| Checkpoints | `cache/checkpoints.yaml` |
| Proposals, decisions and clarification | `history/intent-proposals.jsonl` |
| Transaction exclusion | Absence of `history/.local-transaction.json` |

- The immutable validator and readiness reader share the same **8 MiB per-file** and
  **16 MiB aggregate** constants. Readiness's aggregate also includes its proposal ledger and
  any journal encountered. Oversized inputs are rejected from descriptor metadata before their
  contents are read; each actual read is additionally bounded to the authenticated size.
- The fixed nine-input inventory and the configured graph path limit of 32 components / 4096
  UTF-8 bytes bound descriptor use. An alias between canonical paths is invalid and cannot
  overwrite ownership of an already-open descriptor.
- Every selected file is pinned before the canonical content scan. Configuration is pinned and
  read first to locate the graph. All file and ancestor metadata remains bound through parsing
  and canonical validation, followed by a terminal metadata barrier with no later content reads.
  File ctime detects rewriting identical bytes even when mtime is restored. Named-entry checks
  reject replacement of `.intent`, a canonical subdirectory, or an input file.
- Optional absent files remain bound as absent. An existing transaction journal fails closed
  without recovery, lock-file creation, canonical mutation or initialization. A dangling `.intent`
  symlink is invalid state rather than an onboarding offer.
- Public failures discard parser/path exception context. Cancellation preserves the exact
  exception object, drops captured plaintext traceback frames, and closes owned descriptors.
- Readiness remains a snapshot observation rather than a durable authorization. Later canonical
  changes are observed by the next invocation. No human attribution, semantic approval,
  conflict resolution, publication, provider write or task-completion authority is added.

The inspector intentionally does not retry a detected mutation into a ready result. The caller
receives fixed invalid state and can repeat readiness after the concurrent work has quiesced.
Existing lightweight onboarding inspection keeps its historical bounded summary behavior; the
stricter fail-closed overflow rule belongs to the governed readiness boundary.

## Tests-first evidence

- Initial proposal regressions: **3 failed** before implementation. A populated graph returned
  `ready`, a pending proposal beyond a decided 256-item prefix was omitted, and the bundled hook
  returned `action=classify` while capturing the prompt. The same three tests passed after the
  readiness gate was corrected.
- Canonical capture regressions: **15 failed, 1 passed** before the capture change. Corrupt
  evidence/history/checkpoints, missing referenced evidence, omitted immutable validation and
  a file rewritten during the last read were accepted. All nine FIFO cases either blocked until
  the two-second subprocess deadline or were omitted and incorrectly returned ready.
- Unsafe-input/aggregate regressions: **17 failed, 20 passed** before the capture change.
  Previously omitted evidence/history/receipt/checkpoint files bypassed symlink, hardlink,
  directory and size checks; individually acceptable files exceeded the aggregate budget.
- Cancellation and dangling-workspace regressions: **2 failed** before the change. Cancellation
  retained configuration bytes in inner traceback frames, and a dangling workspace link became
  an onboarding offer.
- A follow-up descriptor-ownership regression failed on a configured graph alias replacing the
  captured config descriptor. Rejecting duplicate canonical paths closed that leak; alias and
  ancestor-substitution tests then passed together.
- Existing ensure, plugin and black-box MCP fixtures that directly manufactured version-1 graphs
  were updated to use real immutable evidence and public ChangeSet application. Guided-onboarding
  E2E expectations now require Proposal review guidance while preserving rejection of forged
  human confirmation. These fixture and route updates do not weaken canonical validation.

## Verification

Commands run from `.worktrees/intent-graph-assessment-design`. Source-path test runs set
`PYTHONPATH` to the absolute worktree `src` and root paths, so a subprocess changing directory
does not silently select an older installed package.

- Focused readiness, canonical snapshot, ensure, advisory prompt and bundled hook gate:
  **144 passed** in 17.64s.
- Broad readiness/workflow/validation/agent-host/MCP/ensure/check/developer-automation gate:
  **946 passed, 1 deselected**, 19 existing Pydantic/fork warnings, in 71.72s.
  The new CLI regression module resets structlog configuration after each test so it does not
  retain closed capture streams for later library tests.
- Black-box MCP and guided-onboarding E2E gate: **4 passed** in 9.87s. These tests were run after
  an offline wheel refresh of the existing worktree virtualenv. The installed runtime file's
  SHA256 was checked against the worktree source and matched. An initial editable-install attempt
  produced a macOS-hidden `.pth` file ignored by Python; the wheel install removed that dependency.
- Full offline gate: **2,311 passed, 1 manual skip, 1 deselected**, 31 existing warnings, in
  **200.86s**. Command: `.venv/bin/python -m pytest -q --import-mode=importlib --tb=short
  -k 'not test_installed_codex_contract_refuses_incomplete_mandatory_coverage'`, with the absolute
  `PYTHONPATH` described above and local loopback permission for control-plane server fixtures.
- Repository Ruff checks passed with the existing unrelated tracked `dogfood 2.py` exclusion;
  the invalid module filename is unchanged. All **14 changed Python files** passed formatting.
- Full mypy passed for **140 source files**. The installed plugin validator passed.
- `intent ensure --help`, `intent check --help` and `git diff --check` passed.

The full offline gate retains the established deselection of
`test_installed_codex_contract_refuses_incomplete_mandatory_coverage`: the installed host reports
Codex 0.153.4, while that existing test pins 0.148.0-alpha.9. No GitHub operation, push or external
provider mutation was performed.

## Review round 1 — boundary corrections

The next review identified three holes in the initial remediation. This round supersedes the
earlier completeness claims at those boundaries; the limits, no-authority policy and approved
proposal routing above remain unchanged.

1. **Receipt semantics:** Capturing `approvals/receipts.jsonl` did not validate its contents.
   Both immutable and local canonical validation now invoke a public, pure validation wrapper
   around the existing receipt store decoder. This preserves its exact canonical-record,
   newline-completeness, unique-claim, claim-before-completion, single-completion and actor-binding
   rules without a second parser. Failures produce the fixed `receipts.invalid` diagnostic.
   Real `ensure` and bundled prompt-hook tests cover truncated receipt contents; the hook returns
   fixed Team state guidance without classification or capture and is a byte-identical replay.
2. **Directory ownership and cancellation:** `SecureDirectory.open` and `subdirectory` now
   close a newly opened descriptor if its `fstat`/directory authentication fails, as well as
   closing the held parent. They no longer translate a `BaseException` cancellation into an
   ordinary unsafe-path failure. The readiness boundary preserves the exact cancellation object
   and scrubs inner plaintext traceback frames. Capture construction acquires its duplicated
   workspace descriptor only after fallible metadata setup. The related `read_relative` traversal
   also closes its duplicated parent and untransferred child when authentication is cancelled.
   Tests count all live `open`/`dup` descriptors and retain real wrappers so cleanup cannot pass
   merely because Python happened to collect them.
3. **Check startup order:** `CheckRuntimeAdapter.restore` no longer calls the ordinary runtime
   opener. Verified shared restoration keeps its existing order and statuses; bounded readiness
   runs next, before mutable stores, capture or reviewed tests. Existing local transaction journals
   fail closed with `readiness_required` and are not recovered or removed automatically. The
   compatibility test and adoption guide now explicitly preserve a prepared/torn transaction for
   the trusted recovery/control-plane workflow. Actual CLI subprocesses enforce a two-second
   deadline for config, graph and journal FIFO inputs and require unchanged local state.

### Review-round tests-first evidence

- Receipt validation: **7 failed, 1 passed** before the fix. Truncated, duplicated, orphaned and
  actor-mismatched receipts were accepted; `ensure` returned ready and the real hook classified.
  Receipt/store focused verification after the fix: **18 passed**.
- Directory readiness: **16 failed, 6 passed** before the fix, demonstrating both leaked
  descriptors and cancellation rewritten as readiness/project-not-initialized errors. Relative
  traversal added **2 failing** authentication-cleanup cases. The complete secure-path and
  readiness-snapshot modules then passed: **93 passed**.
- Check startup: **4 failed** before the fix: all three FIFO subprocesses timed out and the
  prepared journal was replayed into changed canonical state. The complete check CLI and service
  modules then passed: **43 passed**.
- The actual hook additionally passed all **4** canonical-corruption cases on repeated execution,
  with exact fixed output and no canonical writes or prompt leakage.

### Review-round verification history

All runs use the absolute source/root `PYTHONPATH` described above. The existing virtualenv was
rebuilt from an offline regular wheel, and the installed runtime's SHA256 matched the source.

- The broad storage, validation, workflow, shared-state, CLI, hook and automation run initially
  completed with **946 passed, 1 failed, 1 deselected**. The failure was the existing fork-based
  case-store concurrency test: one child received `ENOENT` creating `.cases.jsonl.lock`, leaving
  its peer waiting at a barrier. The whole case-store module immediately passed unchanged
  (**10 passed**). Only the inspected leftover child of that failed pytest run was terminated.
- The first full uninstrumented run completed with **2,333 passed, 13 failed, 1 skipped,
  1 deselected** in 203.73s. All 13 failures were successful external-write commit paths exposing
  their fixed `execution_unavailable` boundary. The same receipt/authorship integration case
  passed alone, with full-suite collection, after the complete check CLI module, and under six
  explicit Python hash seeds. No mutation implementation or test expectation was changed.
- A separate diagnostic full run wrapped only the local committer in a temporary in-process
  trace (no repository file edits). It passed **2,346 tests**, with the same manual skip and host
  deselection, in 201.83s. It observed successful normal commits and only expected injected
  failures. This diagnostic run is not substituted for the ordinary final gate. The precise cause
  of the earlier intermittent failures has not been established.
- The final ordinary, uninstrumented full offline command passed: **2,346 passed, 1 manual skip,
  1 established host-version deselection, 31 existing warnings**, in **203.12s**. No production
  code or mutation-test changes were made between the failing and passing full runs.
- The exact broad command was then repeated unchanged and passed: **947 passed, 1 established
  host-version deselection, 16 existing warnings**, in **122.20s**.
- Repository Ruff checks passed with the same pre-existing `dogfood 2.py` filename exclusion;
  all **10 changed Python files** passed formatting. Full source mypy passed for **140 files**.
  Plugin validation, both `ensure` and `check` help commands, and `git diff --check` passed.
