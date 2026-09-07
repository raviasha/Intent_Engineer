# Final shared-state restore remediation

## Scope

This fix addresses the final review's two Critical restore findings and its Important
unsigned-marker lineage finding. It preserves the existing encrypted envelope, signer policy,
repository/project binding, recipient checks, canonical payload bounds and semantic/ACL validation.
No GitHub mutation, publication, push, enrollment or signing-policy change was performed.

## Changes

- Semantic validation now consumes the exact immutable decrypted bytes. The shared canonical
  snapshot validator reuses the existing graph/evidence/history/case validation logic, requires
  the complete bounded snapshot shape, and caps it at 8 MiB per file and 16 MiB total. The
  existing plan/approval ledger parser is reused directly on authenticated bytes, retaining
  immutable-ID and duplicate-record checks.
- Fresh installation holds the repository, staging and workspace directory descriptors. It
  checks named directory identities and the complete bounded staged inventory, including file
  identities and bytes, before and after exclusive promotion. Only the authenticated payload,
  exact local marker and empty generated runtime files may be installed. A concurrent `.intent`
  creation is not overwritten. Failures and cancellation roll back the installed workspace;
  cleanup uses the owned staging descriptor and does not recursively delete a replacement root.
- Existing restoration compares the full semantic graph, ChangeSet history, decision ledgers and
  configuration inside the same transaction lock scope used for replacement. Local extensions
  or divergence return `diverged` without overwriting local decisions. `intent check` maps this
  to `human_attention_required` / exit 4 before capture. A matching approved ancestor can advance
  to a newer approved release; matching approved local state remains a no-op. Valid append-only
  local evidence is retained when the approved evidence baseline is unchanged. Output bytes and
  file/directory identities are checked before committing an existing-state replacement.
- Lineage verification traverses the complete signed chain to genesis, with a 64-commit bound
  that includes the tip. An unsigned local marker only identifies a release within that fully
  authenticated chain; it cannot terminate verification. Raw Git commit objects establish
  parent topology independently of shallow metadata, grafts or replacement refs. Missing parents,
  merges, invalid genesis/parent links, forks, rollback, repeated bundles and graph-version
  regression fail closed.
- The documented CI route uses a fresh disposable checkout with its own `.intent`. A developer
  workspace with unpublished decisions is not repurposed or cleared. No force-replacement
  switch was added; the existing real CI restoration and required-check journeys remain covered.

## RED evidence

Real temporary Git fixtures reproduced all three review findings before their fixes. The tests
observed `verified` for staged content/identity substitutions, valid unpublished local ChangeSet
extensions and invalid earlier topology hidden behind an unsigned marker. Further RED tests
covered retained writable descriptors, mutation at `existing_precommit`, concurrent fresh
workspace creation, unsigned auxiliary state, repository-root substitution and graph-version
rollback. A separate attack temporarily supplied valid graph bytes during the filesystem
validator's read, then restored the signed but semantically invalid graph before promotion;
that also returned `verified` before snapshot validation was introduced.

The initial test invocation used the environment's installed restore module; its SHA-256 was
checked against the unchanged source at HEAD and both were
`f6ba027a97502a9be2e7dac998dc30d62a914d08a7e0321eb25d7a685f0251eb`.
All implementation verification uses `PYTHONPATH=src` explicitly. Two early extension-fixture
errors were corrected to use the public `NodeUpdate` / `ChangeSet` models and rerun to genuine
behavioral failures before implementing the preservation guard.

The public snapshot validator also has direct tests proving that later local-file changes do
not alter the supplied snapshot's result and that malformed, oversized or aggregate-overflow
snapshots fail with fixed diagnostics. All fixtures remain offline, fixed in time, and use real
temporary Git objects and real local stores.

## Verification

- Focused restore, check-service and canonical-validation gate: **134 passed** in 42.72s.
  Command: `PYTHONPATH=src .venv/bin/pytest -q --import-mode=importlib
  tests/integration/team_state/test_restore.py tests/integration/intent_workflow/test_check.py
  tests/unit/validation/test_service.py`.
- Broader affected-area gate: **503 passed** in 72.15s, with 16 existing Pydantic warnings.
- Complete offline gate: **2,228 passed, 1 manual skip, 1 deselected** in 193.29s. Command:
  `PYTHONPATH=src .venv/bin/pytest -q --import-mode=importlib --tb=short
  -k 'not test_installed_codex_contract_refuses_incomplete_mandatory_coverage'`.
  The first sandboxed invocation confirmed that loopback socket binding was denied; the 17
  local-server-dependent failures passed when the same gate was rerun with local server access.
  No network-backed fixtures or GitHub operations were involved.
- Repository Ruff check passed with the known unrelated `dogfood 2.py` exclusion; Ruff format
  check passed for all nine changed Python files. Full mypy passed for **140 source files**.
- `intent check --help`, `intent ensure --help`, and `git diff --check` passed.

## Operational limits

There is no authenticated checkpoint or release-compaction protocol in this change. A complete
chain longer than 64 releases fails closed even when the local marker matches its tip. Such a
history needs a separately reviewed checkpoint/compaction design. Divergence is surfaced for
explicit reconciliation; this fix does not invent or automatically approve a reconciliation
decision. The complete offline suite excludes the pre-existing installed-Codex version contract
and retains the explicitly manual platform-authenticator skip.

## Review round 1 — 2026-09-08

### Reproductions and changes

The review's late multi-file scan attacks were reproduced with real files and Git objects in
all three restore paths: fresh installation, existing replacement, and verified no-op. A read
of a later canonical file changed an earlier file or directory after its bytes had already
been accepted. All six cases returned `verified` before the whole-tree fence. The fresh
fixture was adjusted for filesystem enumeration order and rerun to a genuine behavioral RED
before implementing that fence.

- Each final scan now holds the relevant file and ancestor-directory descriptors, captures
  device/inode, mode, link count, size, modification time and change time, and rechecks their
  named bindings and complete change-token vector after all content reads. Missing preimage
  files are pinned as absent. This gives a coherent bounded snapshot rather than unrelated
  per-file observations; a writable inode or copied directory cannot stand in for the
  authenticated snapshot.
- Before fresh promotion, authenticated immutable payload bytes are materialized into new
  regular-file inodes. A descriptor retained from the earlier writable stage cannot alter
  installed authority. The retained-descriptor regression now proves that restore succeeds
  with the authenticated bytes on a different inode even when that obsolete descriptor is
  written; corruption of the actual installed tree still fails and rolls back.
- Existing replacement checks run both before commit and at the new `journal_cleaned`
  boundary, while the transaction still holds all locks and can restore its preimage. Real
  mutations during the commit-journal write and its final unlink both reproduced `verified`
  before this terminal guard and now fail closed.
- Fresh rollback reserves a private, collision-safe recovery container. It quarantines the
  actual entry promoted into `.intent`, including an unfamiliar substituted directory, and
  restores the absent preimage. An occupied stage or recovery name does not cause that entry
  to be overwritten or abandoned in the live namespace. Two rename-swap regressions and a
  recovery-container collision exercise these paths; unfamiliar bytes remain recoverable.
- Existing restore pins the bounded canonical and local restore-target preimage, including
  file absence, directory identity, modes and timestamps, within the coordinator's lock
  scope. If an uncooperative writer substitutes a directory or changes a verified no-op,
  rollback quarantines unfamiliar state and reconstructs only those pinned target bytes and
  metadata. The reconstructed canonical preimage is checked again before leaving the lock
  scope. Real directory/root substitutions verify exact canonical bytes, absent files, file
  mode/mtime and directory mode. Reconstruction does not authorize unsigned replacement
  bytes, invent history, or delete unfamiliar entries.
- Git parent parsing operates on byte-delimited headers. Only parent object IDs are decoded
  as ASCII; Unicode author and committer names in an authenticated ancestor no longer make
  a valid chain fail. The real Unicode ancestor fixture was RED before this change.
- Cancellation is tested at `validated`, `fresh_preinstall` and `fresh_installed`. The two
  earlier boundaries reproduced retaining decrypted staging bytes when the cleanup snapshot
  was cleared prematurely. Authenticated cleanup now finishes before clearing that snapshot;
  all three boundaries remove clean plaintext staging and preserve scrubbed cancellation
  propagation.

The prior encryption, signature, recipient, repository/project, semantic and ACL checks are
unchanged. Full 64-release ancestry verification, local divergence preservation and the
explicitly disposable CI checkout route remain in force.

### Recovery and snapshot limits

Unfamiliar recovery material is retained under owner-private `.intent-quarantine-*` containers
for explicit review; it may contain plaintext local state and is never treated as an approved
baseline. Known canonical/local restore targets are reconstructed from their bounded preimage;
unfamiliar or noncanonical bytes remain quarantined rather than being promoted or deleted.
If safe quarantine or exact reconstruction cannot complete, restore fails closed.
This repository ignores generated staging/recovery directories; consuming repositories are
documented to add the same ignore rules. Restore does not mutate their Git configuration.

The change-token fence is an optimistic, point-in-time whole-snapshot check under the existing
transaction locks. It does not claim to freeze a writable filesystem against a later external
operation after the terminal check. No authenticated checkpoint, compaction, automatic
reconciliation, force replacement or remote mutation was added.

### Round 1 verification

- Focused restore, transaction, validation and check-service gate: **175 passed** in 49.97s.
  Command: `PYTHONPATH=src:. .venv/bin/python -m pytest -q --import-mode=importlib
  tests/integration/team_state/test_restore.py tests/unit/storage/test_transaction.py
  tests/unit/validation/test_service.py tests/integration/intent_workflow/test_check.py --tb=short`.
- Broader restore/workflow/storage/MCP/CI gate: **680 passed** in 91.42s, with existing warnings.
  Command: `PYTHONPATH=src:. .venv/bin/python -m pytest -q --import-mode=importlib
  tests/integration/team_state tests/integration/intent_workflow tests/unit/validation
  tests/unit/storage tests/contract/mcp/test_intent_workflow_tools.py
  tests/e2e/test_cli_intent_check.py tests/e2e/test_github_action_workflow.py
  tests/e2e/test_intent_dev_automation.py --tb=short`.
  The first broad invocation had 677 passes and one spawned-process import setup failure;
  explicitly including the repository root made that real multi-process receipt test pass.
- Final complete offline gate: **2,244 passed, 1 manual skip, 1 deselected** in 200.68s.
  Command: `PYTHONPATH=src:. .venv/bin/python -m pytest -q --import-mode=importlib --tb=short
  -k 'not test_installed_codex_contract_refuses_incomplete_mandatory_coverage'`.
  The suite used local loopback server access and retained the existing installed-environment
  exclusion and manual authenticator prerequisite. No network-backed fixtures were added.
- Repository Ruff check passed with the existing unrelated `dogfood 2.py` exclusion; the
  three changed Python files pass the format check. Full mypy: **140 source files**, no issues.
- `git diff --check` and direct `git check-ignore --no-index` checks for generated staging and
  recovery paths passed. No push, publication or GitHub mutation was performed.
