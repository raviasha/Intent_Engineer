# SDD ledger — plan: docs/superpowers/plans/2026-08-25-intent-engineering-github-integration.md

## Controller rulings

- Ruling: Add a load-bearing Task 0 before the written GitHub tasks. It must close the core final-review residuals for durable uncheckpointed evidence replay and evidence-derived chronology before GitHub extends the same connector and detector seams. Why: a GitHub adapter built on incorrect retry or chronology semantics would preserve evidence but still skip meaning or manufacture lag. Cost if wrong: this plan begins with one additional reviewed task and may adjust shared ports before provider code is added.
- Ruling: Retry semantics are defined by the durable evidence ledger, not solely by current source discovery. After a failed run, every durable connector record not consumed by the retained checkpoint must be replayed into semantic processing even if the remote object changes or disappears before retry. Newly fetched records are then merged deterministically. Cost if wrong: durable evidence can be permanently omitted while a later checkpoint advances past it.
- Ruling: Declaration-supplied version integers cannot decide CODE_LAG, REQUIREMENT_LAG, or TEST_LAG. Ordering must be derived from resolved immutable evidence provenance—connector version chains and observed timestamps—and inconsistent chronology declarations must fail closed. Cost if wrong: provider metadata or fixtures could fabricate drift classifications.
- Ruling: The plan's prose and approved public-alpha spec control over illustrative snippets where the existing core contracts have evolved. Provider code must reuse the existing Connector, EvidenceRecord, SyncOrchestrator, secure storage, authorization, validation, and transaction boundaries rather than create parallel abstractions. Cost if wrong: examples may require small API adaptations while preserving their stated behavior.
- Ruling: GitHub tests and release verification are offline by default and use a deterministic fake HTTP API. No live credential or network access is required to claim completion. Cost if wrong: live smoke testing remains an optional user-run check, not part of the deterministic gate.
- Ruling: Preserve the pre-existing untracked Desktop artifacts `.coverage 2`, `.coverage 3`, `.coverage 4`, `README 2.md`, and `src/intent_engineering/core/policy/dogfood 2.py`. The last file can stall discovery when hydrated; focused test runs may disable plugin autoload or temporarily isolate it only if restored byte-for-byte. Cost if wrong: static/test commands need explicit tracked-file targeting, but user-owned files remain untouched.
- Ruling: Do not use GUI applications, browser automation, `open`, Finder, TextEdit, or direct reads of `.git/objects` anywhere in this plan. Use isolated shell and code/test tooling only. Cost if wrong: none to product scope; it prevents recurrence of the prior raw-object window flood.
- Ruling: Task 4 resolves GitHub repository scope only from the strict non-secret `GITHUB_REPOSITORY` environment value and never guesses from Git remotes. Cost if wrong: local GitHub commands require one explicit environment value, but local-only commands remain configuration- and credential-free.
- Ruling: Task 4 report recommendations are deterministic guidance, never mutations: `ORPHAN_REQUIREMENT` maps to `update_implementation`, conflicting/ambiguous evidence to `preserve_disagreement`, and `POSSIBLE_INTENT_CHANGE` to `update_intent`. Cost if wrong: the display profile can be revised without altering stored cases or authorization.
- Ruling: Task 4 report files are restricted to contained `.md` paths outside `.git` and `.intent`; secure creation/replacement requires native no-replace/exchange primitives and fails closed when unavailable. Cost if wrong: unsupported platforms retain exact stdout reporting but cannot use `--output`.
- Ruling: Task 4 existing-report replacement commits when the displaced-original temporary name is authenticated absent after native unlink. Before that point, BaseException restores the exact original inode and re-raises; after it, commit wins and the signal is suppressed because identity-preserving rollback is impossible. No strict-output durability work follows the commit point. Cost if wrong: a signal delivered after irreversible commit is reported as success, avoiding a false cancellation result after state changed.

## Preflight conflict and interface scan

| Tasks | Shared file/interface | Finding and ruling |
|---|---|---|
| 0 self | `sync/orchestrator.py`, evidence/checkpoint ports, `reconcile/evidence_detection.py`, fixtures | Two core residuals are independent at the behavior level but share the EvidenceDelta boundary. Implement in one coherent, tests-first task and keep provider vocabulary out of core. |
| 1 self | dependencies and `capture/github/auth.py` | Safe local credential resolution is self-contained. Token values are secret fields and never enter public exceptions, persistence, subprocess arguments, or logs. |
| 2 self | GitHub auth/errors/models/client | Client consumes Task 1 credentials. Keep HTTP/provider errors adapter-local and redact request headers and bodies. |
| 3 self | Connector protocol, evidence/checkpoint semantics, orchestrator | Must conform to Task 0 replay semantics and existing async Connector contract. Checkpoint state cannot be the sole record of durable semantic consumption if it cannot identify ledger rows deterministically. |
| 4 self | `cli/app.py`, source registry, report rendering, workflows | Extend existing Typer/runtime and render services. Do not bypass shared validation, authorization, or transaction services. The scheduled Action is read-only and never performs external writes. |
| 5 self | docs, fake integration harness, secret audit | Documentation must match executable commands and make hosted OAuth/webhooks explicit non-goals. Secret scan excludes nested Git metadata but covers all project state and reports. |
| 0 ↔ 3 | pending durable evidence and GitHub checkpoints | Task 3 must prove source change/deletion after partial GitHub failure does not erase already durable semantic work and does not advance the failed checkpoint prematurely. |
| 0 ↔ 4 | drift detection/reporting | Reports must consume cases whose chronology is evidence-derived; no provider field may directly select a lag outcome. |
| 1 ↔ 2 | credential transport | The token is revealed only at the in-memory Authorization-header construction point and never included in model dumps or errors. |
| 2 ↔ 3 | pagination/ETags/checkpoints | ETags and pagination remain client/connector concerns; immutable EvidenceRecord identities and semantic checkpoints remain provider-neutral at the port boundary. |
| 3 ↔ 4 | source registry and CLI runtime | Register one GitHub connector through the existing runtime. Avoid a second sync path in `cli/github.py`. |
| 4 ↔ 5 | workflow/docs/secret audit | The documented Action and local quick start must exercise the same commands tested by E2E and must not imply live network verification in the default test suite. |

## Task status

- Task 0 — completed after fix round 2; commits `1a15e95`, `314bd99`, `f871681`, `19df58f`; reports `c7aa4fe`, `e4b92cd`, `89c9741`; independent review clean.
- Task 1 — completed after fix round 1; product `6477a7c`, fix `c1756b8`; reports `bd14d5e`, `9b099a6`; independent review clean.
- Task 2 — completed after fix round 2; product `e257c32`, fixes `7d20151`, `c7b034e`; reports `7537182`, `021d6e6`, `e004856`; independent review clean.
- Task 3 — completed after fix round 1; initial product `7b3a91e`, fix `8076f5e`, initial report
  `9a1aa6c`; final internal re-review clean after durable replay, association, lifecycle,
  exception-retention, and cancellation hardening.
- Task 4 — implementation and independent-review fix round 1 complete; product `e725e8d`, fix
  `d230f42`; final internal adversarial re-review clean after cancellation transaction,
  protected-path, secret-local, provider-scalar, UNC/device, and Markdown-escape hardening; awaiting
  controller final review.
- Task 5 — pending.

## Review findings

- Task 0 review 1: no Critical, 3 Important. Same-object chronology trusts caller tuple order; currentness trusts declaration metadata; single-owner `EvidenceRecord.ingested_by` breaks overlapping connector instances and mixes chains. Required fix: durable provider-neutral connector/evidence association, authenticated chain metadata/currentness, and adversarial multi-instance/permutation/stale-version tests.
- Task 0 review 2: no Critical, 3 Important. Fix 1 addressed caller-order chronology, declaration currentness, atomic association, evidence identity, and explicit legacy migration. Remaining: combined delta re-flattens scoped predecessors and permanently wedges divergent instances; consumed IDs need exact-prefix validation; envelope JSON/model fields need strict duplicate/constant/type/nonempty enforcement. Existing permutation test is vacuous and must become a direct non-empty proof.
- Task 0 review 3: all review-2 findings addressed; no Critical/Important/Minor and no new breakage. Reviewer independently reproduced same-provider v1→v2→v3 plus no-op retry, exact prefix rejection, and strict JSON/model failures. Durable replay and evidence-derived chronology approved.
- Task 1 review 1: 2 Critical, 2 Important, 1 Minor. Required: scrub GitHub token override variables from gh child environment; reject hostile str subclasses inside redaction boundary; remove secret-bearing objects from structured-log proof; directly test production subprocess failures; unconfound extra-field test.
- Task 1 review 2: all Task 1 findings addressed; no new Critical/Important/Minor. Credential precedence, subprocess safety, serialization, exception/log redaction, dependency scope, and strict model behavior approved.
- Task 2 review 1: no Critical, 4 Important, 2 Minor. Required fixes: isolate wrapper requests from every injected-client default (auth, params, conditional headers, timeout) while preserving caller ownership; redact long reflected credential prefixes before request-ID truncation; restrict explicit provider `extra` to detached deeply immutable strict JSON values; support valid null/ghost actors. Also reject or canonicalize pagination fragments and fail closed on structurally malformed Link headers.
- Task 2 review 2: all review-1 findings addressed. No Critical; 2 new Important and 1 Minor remain: endpoint sanitization truncates before credential-overlap detection, valid embedded Git commit authors may be null, and registered Link relation names should be matched case-insensitively.
- Task 2 review 3: all prior findings addressed; no Critical/Important/Minor and no new breakage. Reviewer independently re-probed endpoint/request-ID credential overlap, injected-client default isolation, nullable commit authors, Link casing/malformed/duplicate behavior, ETags, retries, and close ownership. Deterministic GitHub REST boundary approved.
- Task 3 review 1: no Critical/Minor, 6 Important. Required fixes: build the finalized GitHub cursor from the exact durable consumed ledger so deleted pending evidence cannot create a self-invalid checkpoint; align commit cursor validation with provider ordering instead of timestamps; accept PR conversation issue-comment locators; validate GitHub associations even before a checkpoint exists; authenticate/abort stateful discovery generations so overlapping/stale calls cannot mix cursors or caches; and raise malformed-cursor errors outside active exception handlers so rejected input is not retained in `__context__`.
- Task 3 fix review: the six ordinary-exception findings were resolved. Two adversarial re-reviews
  then found cancellation cleanup gaps first for one acquired generation and then for sibling
  generations in a multi-connector run. Both were reproduced RED, fixed with authenticated run-wide
  ownership cleanup that re-raises cancellation, and independently re-probed. Final review found no
  Critical, Important, or Minor issues.
- Task 4 internal review 1: no Critical, 2 Important. The renderer recognized only a short
  directory allowlist of local absolute paths, and report output accepted a final symlink swapped
  after target validation. Both were reproduced RED and fixed with general absolute-path redaction
  plus a descriptor-rooted verified install transaction.
- Task 4 internal review 2: the first path fix overmatched HTTPS URLs, `/` remained visible, and the
  new-file link/unlink sequence exposed a hardlink install window. Each was reproduced RED. The
  final implementation preserves HTTPS, redacts root/arbitrary paths, uses native atomic
  no-replace rename for creation, authenticates final identity/type/link count, and validates both
  sides of replacement exchange with rollback. Final internal re-review found no Critical or
  Important findings.
- Task 4 independent review 1: no Critical, 5 Important, 1 Minor. Required phase-aware
  BaseException/cancellation rollback for report installation, protected output-component rejection
  at any depth with case-insensitive matching, removal of raw environment/token mappings from sync
  and doctor cancellation frames, credential-overlap rejection for successful rate-resource
  scalars, UNC/device path redaction, and `_`/`~` Markdown escaping. Initial combined RED was
  **15 failed, 9 passed**; all six boundaries were fixed.
- Task 4 fix internal reviews found further rollback interruption, multiply-linked rollback,
  post-fsync link authentication, and terminal displaced-unlink ambiguity. Each was reproduced RED.
  Rollback now retries authenticated native cleanup, scrubs multiply-linked owned report inodes,
  re-authenticates after parent fsync, and uses the controller-approved commit-wins rule after
  authenticated irreversible unlink. Final internal re-review found no Critical, Important, or
  Minor findings; full offline suite **602 passed**. Controller final review remains pending.
