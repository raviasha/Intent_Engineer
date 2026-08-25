# Task 4 report — GitHub diagnostics, runtime wiring, drift reports, and Action

## Status

Implementation complete and ready for controller independent review. Product/tests/workflow
commit: `e725e8d` (`feat: report github drift locally and in actions`). Dispatch base:
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
