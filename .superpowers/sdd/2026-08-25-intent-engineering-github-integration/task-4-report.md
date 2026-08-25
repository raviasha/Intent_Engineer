# Task 4 report — GitHub diagnostics, runtime wiring, drift reports, and Action

## Status

Implementation and independent-review fix round 1 complete; ready for controller final review.
Product/tests/workflow commits: `e725e8d` (`feat: report github drift locally and in actions`)
and `d230f42` (`fix: harden github report boundaries`). Dispatch base:
`e12b78a01cea6e1803f9ae1bd05b441ee561e74b`.

## TDD evidence

- Runtime/source-wiring RED began with collection failing because
  `GitHubConfigurationError` did not exist. The initial runtime GREEN was **11 passed**; a
  cancellation/BaseException ownership test then failed and was made GREEN at **12 passed**.
- Repository-status RED failed during collection because `GitHubRepositoryStatus` did not exist.
  The narrow REST-model/client slice became GREEN at **5 passed**.
- GitHub-doctor RED failed during collection because `intent_engineering.cli.github` did not
  exist. Environment and injected-`gh` credential sources, deterministic success, fixed failure
  identities, cleanup, and cancellation were implemented through focused RED/GREEN cycles.
- Drift-renderer RED failed during collection because `drift_report` did not exist. Renderer
  behavior then became GREEN at **11 passed** before later adversarial cases expanded the suite.
- CLI drift RED had **3 failures**: missing `--output`/Markdown behavior, default open-case exit 4,
  and an ACL-empty path using the generic renderer. Each was corrected through the shared runtime
  and authorization projection.
- Workflow RED failed because `.github/workflows/intent-sync.yml` was absent; the offline
  structural workflow test became GREEN at **1 passed**.
- An injected synchronous client-factory `CancelledError` initially did not propagate; the focused
  lifecycle test failed before the ownership boundary was corrected.
- A first full-suite run exposed a test-only global `structlog` capture stream leak with **56
  downstream failures**. The in-process Typer test now isolates logging; the production logging
  contract was not weakened.
- Internal adversarial review RED 1: **7 failed** for six previously unrecognized absolute-path
  forms and a final target swapped to a symlink after validation. The path filter and verified
  output transaction made all **7 pass**.
- Internal adversarial review RED 2: a legitimate HTTPS provenance URL was incorrectly classified
  as a Windows path (**1 failed**). Boundary and separator rules fixed the overmatch while retaining
  path redaction.
- Internal adversarial review RED 3: **2 failed** for the POSIX root path `/` and a hardlink added
  in the new-file install/stat window. Native no-replace rename plus final identity/type/link-count
  authentication made the final focused probe **11 passed**.

## Delivered behavior

### Runtime and source selection

- `parse_sources` accepts stable duplicate-free combinations of `markdown`, `git`, and `github`.
- `sync` and combined local/GitHub selection use one existing `SyncOrchestrator` run. There is no
  provider-only synchronization path.
- GitHub scope comes only from a strict built-in-string `GITHUB_REPOSITORY`, is canonicalized to
  lowercase `owner/repository`, and is rejected before client construction or project-state access
  when absent, blank, ambiguous, malformed, or type-hostile.
- Credentials and clients are resolved only when GitHub is selected. Local-only commands do not
  touch GitHub configuration, credential, or HTTP boundaries.
- CLI-owned clients close on success, ordinary failure, and cancellation. Injected clients remain
  caller-owned; original failures and cancellation take precedence over competing close failures.

### GitHub doctor

- `intent doctor` remains the existing deep workspace validation command and retains its versioned
  v1 result/exit contract.
- `intent doctor github` uses the reviewed credential and HTTP boundaries and makes one bounded
  repository-access request.
- The safe result contains credential source, canonical repository, accessibility, and bounded
  rate-limit scalars only. Authentication, permission, rate-limit, not-found, protocol, malformed
  header, transport, generic API, construction, and close failures map to fixed redacted
  diagnostics.
- Provider bodies, headers, tokens, rejected inputs, and response objects are cleared or excluded
  from public output, result models, exception chains, and repository traceback locals.

### Authorization-safe Markdown drift report

- The renderer receives the CLI's shared `_authorized_cases` projection and independently excludes
  terminal cases. It does not reimplement ACL policy.
- Cases sort by `(case_type.value, case.id)`; evidence sides and deduplicated authors, evidence
  references, and affected references have deterministic ordering.
- Every section includes case identity/type, subject, status, evidence packet, affected references,
  optional impact, and an exhaustive deterministic recommendation. Unknown case vocabulary fails
  closed.
- Every evidence-derived scalar normalizes line/control characters, escapes Markdown and HTML,
  redacts credential patterns, and redacts arbitrary POSIX absolute paths, `file:///` paths, and
  Windows drive paths without suppressing legitimate HTTPS provenance.
- Markdown output has one stable empty form and exactly one terminal newline. Ordinary open cases
  exit 0; explicit `--require-review` preserves exit 4; partial sync remains exit 3.
- `--output` accepts only a contained `.md` project path outside `.git` and `.intent`. It writes the
  exact stdout bytes through a held directory descriptor. New files use native atomic no-replace
  rename; replacements use atomic exchange, authenticate both displaced and installed identities,
  regular-file type, and link counts, and roll back a raced target. Unsupported native primitives
  fail closed.

### Read-only GitHub Action

- `.github/workflows/intent-sync.yml` has only manual and nightly `17 2 * * *` triggers.
- It grants only read permissions for contents, issues, and pull requests; checks out full history;
  uses Python 3.12; installs the checkout; initializes and validates a clean workspace; runs the one
  combined sync; renders Markdown; and uploads `intent-drift.md`.
- The workflow passes `GH_TOKEN` from `secrets.GITHUB_TOKEN` and repository scope from
  `github.repository`. It contains no write permission, `continue-on-error`, or shell workaround.

## Security and lifecycle rulings

- Ruling: GitHub repository scope is explicit environment configuration, not guessed from Git
  remotes and not persisted as a secret. Cost if wrong: local users must provide
  `GITHUB_REPOSITORY` when GitHub is selected.
- Ruling: case recommendations are deterministic reporting guidance only. `ORPHAN_REQUIREMENT`
  recommends `update_implementation`; conflicting/ambiguous evidence recommends
  `preserve_disagreement`; `POSSIBLE_INTENT_CHANGE` recommends `update_intent`. Cost if wrong: the
  displayed recommendation can be revised without changing authorization or applying a mutation.
- Ruling: report targets are `.md` project files only and may not enter `.git` or `.intent`. Cost if
  wrong: users cannot use this option to overwrite canonical/internal state or emit other formats.
- Ruling: atomic report creation requires Linux `renameat2(RENAME_NOREPLACE)` or macOS
  `renameatx_np(RENAME_EXCL)`; replacement requires the corresponding exchange primitive. Missing
  primitives fail closed instead of falling back to a check-then-replace race. Cost if wrong: an
  unsupported POSIX platform cannot use `--output`, though stdout remains available.
- The transaction authenticates the installed entry immediately before success. As with any local
  file, an actor already holding the same OS identity can copy or link a successfully published
  report after publication; that post-publication local-access boundary is not treated as a
  pathname-validation race.

## Verification

- Final Task 4 CLI/report/workflow selection under `-W error`: **57 passed in 2.77s**.
- Task 4 plus secure-storage focused regressions under `-W error`: **72 passed in 2.82s**.
- GitHub unit/contract/integration plus legacy local CLI selection under `-W error`:
  **171 passed in 13.98s**.
- Full offline suite under `-W error`: **579 passed in 20.59s**.
- Tracked Python Ruff check: **passed**; explicit new/untracked Task 4 Python files: **passed**.
- Task-scoped Ruff format check: **12 files already formatted**.
- Mypy: **success, 74 source files**.
- `intent doctor --help`, `intent doctor github --help`, and `intent drift --help`: **exit 0**.
- `git diff --check`: **passed**.
- Final internal adversarial re-review: **clean**, with no Critical or Important findings. The
  reviewer independently re-probed path/URL classification, new and repeated output, symlink swap,
  and hardlink-at-install rejection.

## Safety, scope, and known limitations

- No live GitHub request, real credential, GUI/browser action, `open`, Finder, TextEdit, or raw Git
  object read occurred. All GitHub behavior was exercised with injected fakes and offline HTTP.
- Task 5 documentation/secret-audit work and all MCP work remain unstarted.
- The report intentionally provides a fixed deterministic recommendation profile; it never applies
  a resolution or mutates GitHub.
- The five protected untracked artifacts remain untouched and uncommitted:
  `.coverage 2`, `.coverage 3`, `.coverage 4`, `README 2.md`, and
  `src/intent_engineering/core/policy/dogfood 2.py`.

## Independent review fix round 1

Controller independent review found no Critical findings, five Important findings, and one Minor
finding:

1. strict output installation did not roll back cancellation/BaseException after native
   no-replace rename or exchange;
2. protected output directories were checked only as an exact, case-sensitive first component;
3. doctor and sync cancellation traceback frames retained the raw environment mapping/token;
4. a successful provider rate-resource scalar could reflect all or part of the credential;
5. UNC/device absolute paths survived Markdown report rendering; and
6. `_` and `~` remained active Markdown delimiters.

Every finding was reproduced before its production change. The initial combined review RED was
**15 failed, 9 passed**: four rollback phases, four protected-path variants, two cancellation-local
leaks, three rate-resource overlaps, plain UNC leakage, and Markdown delimiter escaping. The six
direct regressions then became GREEN at **24 passed**.

### Review-fix behavior

- Report output rejects `.git` and `.intent` components at any depth using case-insensitive
  comparison, including case variants that resolve to protected names on default APFS.
- Doctor and sync release raw input mappings before client construction or an async cancellation
  point. Cancellation cleanup/re-raise remains unchanged, while repository traceback locals no
  longer contain `GH_TOKEN`.
- Repository status rejects full, prefix, and sanitized-fragment overlap between the authenticated
  token and `X-RateLimit-Resource`; rejected provider values are cleared before the fixed protocol
  error is raised.
- Markdown scalar protection recognizes UNC, Windows device, POSIX, file-URI, and drive-letter
  absolute paths across every case/evidence field. `_` and `~` are escaped with the rest of the
  Markdown delimiter vocabulary.
- Verified output installation now tracks native filesystem phase by authenticated inode state.
  Before commit, absent-output cancellation removes the installed inode; existing-output
  cancellation exchanges the exact displaced inode back; rollback unlink/exchange interruptions
  are retried; outer cleanup only removes the owned report inode.
- A raced hardlink to the owned replacement inode causes fail-closed rollback. Before removing the
  project/temp name, rollback opens and authenticates that inode, truncates and fsyncs it when
  multiply linked, and therefore leaves no report bytes reachable through the raced external link.
  Both absent and existing installs re-authenticate regular type, exact inode, and single-link state
  after parent-directory fsync.

### Commit-point ruling

- Ruling: existing-target replacement has one irreversible commit point: authenticated
  disappearance of the displaced-original temporary name after native unlink. Before that point,
  any BaseException restores the original bytes and inode, removes owned temporary state, and
  re-raises the original signal. If native unlink completed before raising, exact identity rollback
  is no longer possible; commit wins, the post-commit signal is suppressed, replacement bytes
  remain, and no temporary name remains. No strict-output durability work occurs after this commit
  point. Cost if wrong: a control signal delivered after the irreversible native unlink is reported
  as success rather than cancellation, preventing the CLI from claiming failure after it committed.

### Review-fix TDD evidence

- Initial native phase RED: **4 failed** for absent/existing install cancellation immediately after
  rename/exchange and after parent fsync. GREEN plus prior race/repeat cases: **7 passed**.
- Rollback-interruption RED: **2 failed** when rollback unlink/exchange raised before acting. The
  authenticated retry implementation restored exact state and made both pass.
- Hardlink rollback/final-authentication RED: **4 failed** for absent/existing external-link leakage
  during rollback and absent/existing links inserted during parent fsync. The scrub and post-fsync
  authentication behavior made all four pass.
- Terminal cleanup RED: cancellation before displaced unlink left replacement plus original temp;
  after native unlink, cancellation was incorrectly re-raised after commit. Separate before-effect
  and after-effect tests now prove exact rollback versus commit-wins behavior and safe retry.
- Final transaction race/phase selection: **14 passed**.
- Final internal adversarial re-review: **clean**, with no Critical, Important, or Minor findings.

### Review-fix verification

- Task 4/report/workflow/repository-status plus secure-storage selection under `-W error`:
  **100 passed in 3.41s**.
- GitHub unit/contract/integration plus legacy local CLI selection under `-W error`:
  **174 passed in 13.71s**.
- Full offline suite under `-W error`: **602 passed in 21.31s**.
- Tracked Python Ruff check: **passed**.
- Review-fix Ruff format check: **9 files already formatted**.
- Mypy: **success, 74 source files**.
- `intent doctor --help`, `intent doctor github --help`, and `intent drift --help`: **exit 0**.
- `git diff --check`: **passed**.

No live GitHub request, real credential, GUI/browser action, Task 5 work, MCP work, or raw Git
object read occurred. The five protected untracked artifacts remain untouched and uncommitted.
