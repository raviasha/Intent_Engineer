# Task 5 report: required-check workflow and release proof

## Implementation

- Added the independent PR workflow `.github/workflows/intent-check.yml`, with the exact job
  status name `Intent Engineering / check`, unfiltered PR/manual triggers, read-only contents
  permission, full-history checkout without persisted credentials, concurrency cancellation,
  twenty-minute job timeout, and the protected `intent-ci` environment.
- The ordered path is checkout, Python/dependency installation, signed/encrypted state restore,
  reviewed repository tests, canonical result writing, then the exact command
  `intent check --ci --require-review --test-results .intent-ci/test-results.json`.
- Added a thin `integrations/github_action.py` adapter around the existing shared-state restorer,
  `DevObserver`, runtime adapter and canonical result validator. All configured commands must
  pass; empty configuration fails; old artifacts are invalidated before a rerun; changed HEAD,
  stale/foreign/unauthorized results and descriptor-unsafe files retain the existing rejection
  boundaries. Workflow errors are fixed and secret-free. No graph approval or publication is
  introduced.
- Used `macos-14` for reviewed execution because the current observer requires a root-owned
  regular `/bin/sh` or `/usr/bin/python3`, rejecting the ordinary Ubuntu interpreter symlinks.
  Installation creates a local `.venv` with the installed dependencies so an approved shell
  wrapper can call `.venv/bin/python` despite the observer's deliberately minimal PATH.
- Kept the nightly/manual `intent-sync.yml` separate. It restores verified approved state,
  invokes consolidated capture/check, then renders its drift artifact. It does not claim fresh
  repository tests. Both artifact paths have explicit seven-day retention; the PR job explicitly
  includes its one hidden-directory result file and never uploads `.intent` or test stdout.
- Updated README, adoption guide and GitHub guide with branch-protection setup, environment
  review, independent trust/key provisioning, fork limitations and current publication/enrollment
  boundaries. Updated the existing public-alpha/GitHub workflow compatibility tests.
- Added `.intent-ci/` to ignored generated artifacts. No GitHub mutation or push was performed.

## TDD evidence

- Initial focused RED: **7 failed, 1 passed**. Failures identified the absent required workflow,
  missing checkout credential setting and absent reviewed-workflow adapter before implementation.
- A rerun regression failed because a previous passing artifact survived a later failed test;
  the adapter now invalidates both staged and final results before executing any reviewed command.
- The hidden artifact structural regression failed until `include-hidden-files: true` was added
  for the exact `.intent-ci/test-results.json` upload path.
- The release fixture uses fixed time and fixed offline keys, real temporary Git repositories and
  clones, real signing/encryption and the production environment trust provider. Encryption keeps
  its production randomness. Reconciliation fixtures use public models and the transaction-level
  ChangeSet executor, so their signed canonical state passes semantic verification.
- The fresh clone restores a verified baseline without checking out the state branch, handles its
  first ordinary prompt through the real hidden CLI hook, executes its committed implementation
  and verifying test, emits HEAD-bound evidence, and passes the real CI command. The plugin-free
  path fails with exit 4 for both `AMBIGUOUS_DIVERGENCE` and `CONFLICTING_SOURCES`; the cases remain
  durable. Missing real trust and failing tests also fail closed.
- Added fixture cleanup after CLI capture so a closed captured stderr cannot contaminate later
  library tests. The corresponding release-proof plus scheduled-assurance run passed **39 tests**.

## Verification

Final gates ran against the unchanged implementation:

- Broad signed-state/readiness/observer/check/plugin/MCP/public-alpha/GitHub compatibility:
  **614 passed, 1 deselected, 16 existing warnings**, 77.35 seconds.
- Complete offline suite: **2156 passed, 1 skipped, 1 deselected, 31 existing warnings**,
  157.08 seconds. Command:
  `PYTHONPATH=src .venv/bin/pytest -q --import-mode=importlib --tb=short -k
  'not test_installed_codex_contract_refuses_incomplete_mandatory_coverage'`.
- The skipped test is the explicitly manual platform-authenticator probe. The deselected test
  is the independent installed-Codex version assertion described below.

Verified static gates:

- Ruff on all files except the pre-existing tracked duplicate `dogfood 2.py`: passed.
- Ruff format check on all five changed/new Python files: passed.
- Full mypy: **140 source files**, no issues.
- Installed plugin validator: passed.
- `intent check --help` and `intent ensure --help`: passed.
- Focused workflow/public-alpha/GitHub guide compatibility: **11 passed**.
- YAML structural workflow contracts and `git diff --check`: passed.

Repository-wide checks also ran without suppressing unrelated baseline issues:

- Plain Ruff reports `N999` for the existing tracked file
  `src/intent_engineering/core/policy/dogfood 2.py`.
- Whole-repository format check reports **51 existing files** that would be reformatted. None
  was changed as part of this task.
- The installed-Codex contract expects `0.148.0-alpha.9`, while this host has `0.153.4`. An
  unfiltered full run demonstrated this independent failure; the final compatibility/full runs
  exclude only `test_installed_codex_contract_refuses_incomplete_mandatory_coverage`.
- Local web/process tests need loopback/process permissions beyond the default tool sandbox.
  The full offline suite was rerun with those permissions. The platform-authenticator probe is
  explicitly manual and remains skipped. Existing Pydantic/fork warnings are retained.

## Boundaries and handoff

The workflow files do not provision secrets, create branch protection, approve an environment,
enroll team identities, publish a baseline, or merge PRs. They deliberately fail until a compatible
approved encrypted state release and independent CI trust exist. No live GitHub Actions execution
was used as release evidence.

The local prompt hook currently inspects an already-restored baseline. The zero-command prompt
proof is for a freshly cloned and onboarded checkout after verified restore; it does not claim
that the hook itself fetches or enrolls team state. CI verification was not weakened to make the
fixture pass. Passing CI is bounded deterministic assurance, not a universal semantic-completeness
or human-approval claim.

Commit message: `feat: automate intent readiness and checks`.

## Review round 1: exact test tree and scheduled review reports

Both Important review findings were reproduced before changing implementation. The initial
review regressions produced **11 failures**: dirty committed/working/index state could acquire
passing evidence, test/result-boundary mutations went undetected, and an open review case halted
scheduled capture before artifact production. A further Git replacement-object regression also
failed RED before disabling replacement objects in the bounded Git environment.

The fix adds an optional CI-only clean-commit snapshot to the existing observer. It compares
descriptor-read tracked file bytes and executable modes with the actual HEAD tree, checks the
staged index and untracked paths without trusting arbitrary ignore rules, and binds evidence to
file identities plus modification/change times. The adapter checks this snapshot before and
after every reviewed command and both staged/canonical writes; any failure invalidates both
passing-result paths. The internal staged envelope carries the fingerprint; the public canonical
result schema and signed shared-state verification remain unchanged.

Inspection retains the existing pinned Git executable, sanitized environment, disabled hooks and
filesystem monitor, streaming output caps and process deadlines. It additionally disables replace
objects and text-conversion helpers. Snapshot bounds are 4,096 tracked files, 16 MiB per file,
128 MiB total and a five-second acceptance deadline. Only explicit generated directories and exact
reviewed output paths may be untracked; tracked files never receive an exemption. Unsupported
links, submodules and transformed checkout bytes fail closed. Documentation lists these limits.

The scheduled workflow now restores, captures, validates, renders and uploads before the final
review-required check. A real signed conflicting-state fixture walks the workflow's CLI steps,
proves a new source is captured and the conflict report exists at upload, then observes exit 4.
Separate capture and render failure fixtures prove operational errors stop the pipeline rather
than being masked. The required PR workflow and its exact status/command are unchanged.

Review regression coverage includes unstaged/staged/assume-unchanged/ignored-untracked and replaced
HEAD attacks; mutation during tests, including restoring original bytes; all result boundaries;
declared output allowance without exempting tracked code; unsafe and oversized inputs; and
scheduled report/failure ordering. No GitHub mutation or push was performed.

Review fix verification:

- Focused observer, CI evidence, workflow, scheduled-pipeline and compatibility gates:
  **63 passed**, 44.42 seconds.
- Broad affected-feature gate (workflow/readiness/check, signed restore, GitHub, agent-host,
  MCP, release proof and public-alpha contracts): **917 passed, 1 deselected, 19 existing
  warnings**, 102.08 seconds. Only the previously documented installed-Codex version assertion
  was deselected.
- Full mypy: **140 source files**, no issues.
- Ruff excluding the previously documented unrelated `dogfood 2.py`: passed.
- Ruff formatting on all six changed Python files: passed.
- Plugin validator, `intent check --help`, `intent ensure --help`, and diff checks: passed.
- Complete offline suite (same installed-Codex-only exclusion as above): **2,172 passed,
  1 failed, 1 skipped, 1 deselected, 31 warnings**, 323.98 seconds. The sole failure was the
  unchanged `test_store_instances_dedupe_the_same_evidence_id_concurrently`: one fork child
  received `FileNotFoundError` while creating `.evidence.jsonl.lock`, leaving its peer waiting
  at the test barrier. The post-summary atexit wait was interrupted. All **8 evidence-store
  contract tests passed** immediately in isolation (0.11 seconds). Neither storage implementation
  nor storage tests changed; this intermittent failure is reported, not suppressed or claimed
  green.

Fix commit message: `fix: bind CI evidence and preserve scheduled reports`.
