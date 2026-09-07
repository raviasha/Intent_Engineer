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
