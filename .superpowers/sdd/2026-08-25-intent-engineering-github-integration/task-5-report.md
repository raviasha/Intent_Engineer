# Task 5 report — GitHub documentation, persistence audit, and release proof

## Status

- Base: `327467122f706e22889287624caefbc68c5f84a7` on `feat/public-alpha`.
- Product/docs/tests commit: `05d0acf9f7af8bd8f10198edc4a2a58629da29ac`.
- Initial report/ledger commit: `34c43b264f2c339200f39dd95abdcd32304d10d6`.
- Case-insensitive hexadecimal reflection fix: `ba719a789dddb78ce9897ad4e1f495f27e716aef`.
- The final external re-review approved the amended Task 5 product/test diff with no Critical,
  Important, or Minor findings. This amended report and ledger are committed separately.
- No live GitHub API, real credential, `gh` login, GUI/browser, raw Git object access, MCP work,
  hosted OAuth, GitHub App flow, webhook, PR write, or external write was used.

## RED then GREEN

The first focused RED preceded test-support changes:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/integration/github/test_no_secret_persistence.py -q -W error
```

It failed as intended: `1 failed`, with
`TypeError: GitHubSyncHarness.run() got an unexpected keyword argument 'token'`. This proved the
missing deterministic unique-token injection seam rather than a product failure. The minimal test
harness extension creates an owned fake-HTTP client for that token; production code was not changed.

After the initial application-path audit, executable documentation tests, combined release proof,
and workflow-contract assertions were added, the focused result was `3 passed`.

The first read-only pre-commit review then exposed a real scanner race, a vacuous semantic path,
missing provider-reflection probes, and ambiguous documented failure exits. The first review-fix
RED was:

```text
2 failed, 3 passed in 0.60s
```

The scanner test failed because `_scan_project` had no final-open identity seam. The reflected
issue-title test expected a failed sync but got success with five persisted EvidenceRecords,
confirming a production credential-persistence defect. A later exact/full and base64 JSON-key RED
was `2 failed, 6 passed`; both cases also persisted five records. That fix round reached
`21 passed in 0.33s`.

The second pre-commit review found one Critical false positive: the overlap helper matched arbitrary
four-character windows, while the connector also scanned locally constructed `github` metadata.
A valid fine-grained PAT therefore made ordinary sync fail, and an ordinary GitHub pagination URL
could collide with the public token-format prefix. The required normal-path RED was:

```text
2 failed, 1 passed, 21 deselected in 0.49s
```

Doctor succeeded, but ordinary unpaginated and paginated real-orchestrator syncs both failed with
zero evidence. A separate public-marker request-ID regression failed once because the safe
`github_pat_` marker was discarded. The final policy accepts exact full raw/sanitized/base64/hex
credentials at any length and otherwise rejects only conservative fragments of at least 12
characters. Raw GitHub JSON keys/values and ETag/Link headers are checked in `GitHubClient` before
normalization; constructed connector metadata is not treated as provider provenance. The final
normal-plus-malicious matrix is `17 passed, 7 deselected in 0.08s`, and the full focused audit is
`24 passed in 0.44s`.

After the initial Task 5 report, external final review found one Important persistence bypass:
uppercase hexadecimal encoding of the credential was not recognized by the lowercase-only
production matcher or audit oracle. The independently derived upper/mixed JSON value/key and
ETag/Link RED was:

```text
8 failed, 24 deselected in 0.40s
```

The JSON cases completed a real GitHub sync and persisted five EvidenceRecords, ETag returned
normally, and Link was followed. A first case-insensitive hex-run fix made those eight cases green.
Read-only review then found that skipping an odd-length enclosing hex run still permitted one
provider hex nibble before or after a full encoding. The persisted prefix/suffix matrix RED was:

```text
16 failed, 4 passed, 28 deselected in 0.49s
```

The final matcher searches each contiguous hexadecimal run case-insensitively for the exact
even-length credential encoding or a meaningful even 12-character credential fragment, regardless
of unrelated enclosing nibble parity. It does not case-fold ordinary raw, sanitized, or standard
base64 material. The parity selector is now `20 passed, 28 deselected in 0.41s`; the complete audit
is `48 passed`, and valid fine-grained PAT doctor/unpaginated/paginated paths remain successful.

## Delivered behavior

- `README.md`, `CONTRIBUTING.md`, and new `docs/github.md` document Python 3.12, clean-checkout
  initialization, strict non-secret `GITHUB_REPOSITORY=owner/repository`, credential precedence,
  least read permissions, doctor fields/fixed failures, sync/retry/idempotency, drift semantics and
  exits, Action behavior, and slice non-goals.
- The guide corrects the former stale claim that public alpha lacked a network connector/scheduling:
  GitHub is opt-in with local credentials and the Action is repository-local scheduling, not a
  hosted service.
- `tests/integration/github/test_no_secret_persistence.py` drives the real resolver, client,
  connector, `SyncOrchestrator`, secure local stores, doctor, and drift CLI against
  `httpx.MockTransport`. Its scanner holds the project root and every recursive directory by fd,
  opens files descriptor-relative with `O_NOFOLLOW`, authenticates the enumerated/opened identity,
  exact regular type, and `nlink == 1` before reading, and skips only top-level `.git`.
  Deterministic final ordinary-swap, symlink-swap, and hardlink-swap regressions prove the boundary.
- The audit explicitly decodes the successful GitHub checkpoint, verifies five GitHub evidence
  records, proves a byte-identical no-op, repeats strict report replacement and verifies every
  created rollback tombstone is zero bytes, then proves a later reflected rate failure preserves
  earlier durable work and the old checkpoint before a successful retry.
- It covers deterministic result serialization, stdout/stderr/CLI exception, structured logs for
  success and reflected failure, doctor success plus auth/permission/rate/protocol failures, direct
  public error repr/args/cause/context/repository traceback locals, and owned-client closure on
  every exercised path. Provider probes cover pagination Link/next endpoint, ETag-adjacent state,
  resource and all numeric rate scalars, source values, and provider-controlled keys using exact
  full values plus 12-or-longer raw, sanitized, standard-base64, and hex fragments. Hex probes cover
  lower, upper, and deterministic mixed case, including one-nibble prefix/suffix enclosure. The
  scanner oracle derives those representations and every meaningful fragment independently in
  memory without calling the production overlap helper. No sentinel leak was observed.
- A separate fresh real Git repository release proof performs init → validate → injected-fake
  doctor → one combined `markdown,git,github` run through the shared orchestrator → validate →
  byte-identical no-op → Markdown drift output. Its explicit front-matter fixture produces exactly
  eight evidence records, at least one graph mutation plus ChangeSet history, and at least one
  reconciliation case through the production reasoner/detector/executor. The audit requires the
  real history/case files and proves initialized approvals/cache/journal absent/empty/present state.
- Offline injected CLI results prove a GitHub-only failed sync exits 1 while a mixed local/GitHub
  partial exits 3. The guide and documentation contract state the same distinction.
- `tests/integration/github/test_github_docs.py` executes all documented Intent command help,
  proves real credential precedence, and parses the Action YAML to assert the exact trigger,
  cron, permissions, command order, source selection, env expressions, artifact name/path, and
  documentation contract.

## Verification

Final commands were run from the linked worktree with no network access:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/integration/github/test_no_secret_persistence.py \
  tests/integration/github/test_github_docs.py -q -W error
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin \
  tests/unit/capture/github tests/contract/capture/test_github_connector_contract.py \
  tests/integration/github tests/e2e/test_cli_github.py -q -W error
git ls-files -z -- '*.py' | xargs -0 .venv/bin/ruff check
.venv/bin/ruff check tests/integration/github/test_no_secret_persistence.py \
  tests/integration/github/test_github_docs.py
.venv/bin/mypy src/intent_engineering
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
COVERAGE_FILE=/private/tmp/intent-engineering-task5-hex-parity-coverage \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  -p anyio.pytest_plugin -p pytest_cov.plugin --cov=intent_engineering \
  --cov-config=/private/tmp/intent-engineering-task5-coveragerc \
  --cov-report=term -q -W error
.venv/bin/intent doctor github --help
.venv/bin/intent sync --help
.venv/bin/intent drift --help
git diff --check
```

```text
Focused persistence audit: 48 passed in 0.62s
Focused audit + docs contracts: 51 passed in 1.26s
GitHub unit/contract/integration/CLI selection: 257 passed in 4.66s
Tracked Python Ruff: All checks passed
New Python files Ruff: All checks passed
Mypy: Success, no issues found in 74 source files
Full offline suite: 668 passed in 22.76s
Full offline suite with tracked-source coverage: 668 passed in 24.40s
Tracked-only coverage: 89% (6,015 statements; 680 missed)
```

Coverage used isolated data outside the workspace, tracked source
`src/intent_engineering`, and the protected duplicate explicitly omitted. There is no configured
coverage threshold; the exact tracked-source result above is the release measurement. Because
plugin autoload is disabled, the final coverage command explicitly loaded `pytest_cov.plugin`; an
initial invocation without that explicit plugin rejected the `--cov` arguments before collecting
or running any tests.

These commands also exited successfully: `.venv/bin/intent doctor github --help`,
`.venv/bin/intent sync --help`, `.venv/bin/intent drift --help`, and `git diff --check`.

## Files changed

- `README.md`
- `CONTRIBUTING.md`
- `docs/github.md`
- `src/intent_engineering/capture/github/client.py`
- `src/intent_engineering/capture/github/errors.py`
- `tests/integration/github/conftest.py`
- `tests/integration/github/test_no_secret_persistence.py`
- `tests/integration/github/test_github_docs.py`
- `tests/unit/capture/github/test_client.py`
- `tests/unit/capture/github/test_repository_status.py`

## Rulings and limitations

- Fake HTTP remains test-only through injected `httpx.MockTransport`; no public fake-mode switch or
  API-base override was added.
- The security scanner intentionally does not recurse into `.git`, but refuses links and checks all
  regular files created in the temporary project, including report rollback tombstones.
- Credential-overlap validation is fail-closed only at material public/persistence boundaries:
  doctor fields that are actually returned and raw sync JSON keys/values plus Link/ETag state.
  Dropped doctor extras retain prior behavior. Exact full raw/sanitized/standard-base64/hex
  credentials are rejected at any length; partial overlap requires at least 12 characters.
  Hex comparisons alone are case-insensitive and recognize qualifying credential windows even
  inside an odd-length enclosing hex run. The public
  `github_pat_` format marker, normal GitHub URLs, and locally constructed connector/repository/kind
  fields therefore do not create short-fragment false positives.
- The encoded audit and production contract names standard RFC 4648 base64 only. No URL-safe
  base64 behavior is claimed or added in this proven hexadecimal-bypass fix.
- Documentation and workflow describe read-only GitHub evidence ingestion only. Hosted OAuth,
  GitHub Apps, webhooks, PR comments/annotations, external writes, MCP write-back, and Slack,
  Notion, Jira, and Confluence completion are explicit non-goals.
- Task 4's independently approved strict-output ownership policy remains unchanged: pre-existing
  originals require exact single-link identity before scrub; process-owned report/temp inodes are
  scrubbed through raced links; zero-byte tombstones are retained because safe conditional unlink is
  unavailable.
- Existing repository tests own deep cancellation/race coverage. This Task 5 audit adds the
  end-to-end unique-sentinel proof and checks the public error/traceback boundary directly for the
  reflected rate path.

## Preserved user artifacts

The following untracked artifacts remained uncommitted and byte-preserved. SHA-256 values recorded
after verification:

```text
1544dde7f20fe70edd83c28735219a6c7c01a34ce09465dedfb143e97b1b1664  .coverage 2
1544dde7f20fe70edd83c28735219a6c7c01a34ce09465dedfb143e97b1b1664  .coverage 3
1544dde7f20fe70edd83c28735219a6c7c01a34ce09465dedfb143e97b1b1664  .coverage 4
26ffb68b27d2f10d2923eef1140b1039d4f5fa98401e668489b3dfcec422ec45  README 2.md
072a6691254482cb7b03aa242577269808cae70f1661dc0a07a7812fb3a64b20  src/intent_engineering/core/policy/dogfood 2.py
```

## Review state

The first read-only pre-commit review findings were reproduced and fixed: descriptor-authenticated
scanning, a non-vacuous semantic ChangeSet/case path with exact canonical state expectations,
provider reflection rejection with traceback-local clearing, and exact CLI exit 1/3 documentation.
Broad regression runs exposed and corrected two over-broad designs: an ignored doctor payload extra
must not fail a safe probe, and static connector contract clients must not acquire credential APIs.
The second review's Critical valid-PAT false positive was reproduced by normal doctor,
unpaginated-sync, paginated-Link-sync, and public request-ID-marker tests. The final boundary is
applied only by the real client while the contract fakes remain unchanged; malicious exact and
12-or-longer raw/sanitized/encoded reflections still fail before normalization or persistence.
The final reviewer found no Critical, Important, or Minor issues, independently ran 107 focused
tests, and probed ETag handling to confirm full and 12-character reflections are rejected without
traceback retention while the public `github_pat_` marker is accepted. Product/docs/tests were
committed as `05d0acf9f7af8bd8f10198edc4a2a58629da29ac` only after that approval.

External final review after that commit found the uppercase-hex Important described above. The
first narrow fix passed all requested gates, but its read-only review found the odd-enclosing-run
Important before commit. Prefix/suffix affix regressions reproduced it across JSON values, JSON
keys, ETag, and Link. The amended matcher and independently derived oracle were then re-reviewed;
the external final verdict was zero Critical, Important, or Minor findings. The two-file product/
test fix was committed as `ba719a789dddb78ce9897ad4e1f495f27e716aef` only after that verdict.
