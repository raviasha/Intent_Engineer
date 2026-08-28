# Task 7 report — process-local scoped intent capabilities

## Status

`READY`. Implementation, tests, self-audit, required verification, and three independent review
rounds are complete. The final scoped Round 3 review reported `0 Critical / 0 Important / 0 Minor`
and authorized the exact Task 7 commit.

Execution base: `493572b8b9c89b156bceb6bcc8b049e250a8f1d7`.

Task authority: `.superpowers/sdd/2026-08-26-intent-aware-agent-workflow/task-7-brief.md`.

## Implementation

- Added strict/frozen `AuthorizationGrant` and `AuthorizationVerification` records plus one
  process-local `AuthorizationIssuer`. Issuance consumes exact detached Task 5 envelope/result
  models and permits only final mechanical or aligned classifications with matching task, graph,
  canonical path scope, and canonical relevant intent IDs.
- Tokens use `secrets.token_urlsafe(32)`. Only fixed-size SHA-256 digests and immutable grants enter
  the lock-protected ordered registry. The registry holds at most 1,024 live grants, evicts every
  expired grant before capacity checks, never evicts a live grant, and supports atomic full
  revocation. A new issuer invalidates every prior token.
- Grants bind actor, repository, content-addressed task ID, graph version, classification, sorted
  unique permitted paths, relevant node IDs, UTC issue time, and an exclusive expiry exactly five
  minutes later. Verification compares fixed-size digests with `hmac.compare_digest` and requires
  the exact live binding plus a non-expanding path subset; an empty requested operation is accepted
  only for a zero-scope grant.
- Added optional MCP `intent_preflight` and `intent_authorization_verify` tools. One long-lived
  production workflow service owns exactly one issuer. Preflight calls the real Task 5 service over
  the held runtime and persisted conversation evidence, then rechecks descriptor-held config,
  principals, repository, actor, and graph before minting. Ambiguous and conflicting results retain
  their Task 5 context/case semantics and contain no token.
- Verification reauthenticates descriptor-held live config, principals, repository, actor, and
  graph before checking the exact requested operation. Its public response is limited to
  `schema_version`, `authorized`, `classification`, `relevant_node_ids`, and `expires_at`; every
  invalid, expired, unknown, changed, or mismatched request has the same reduced denied shape.
- Added strict raw MCP validation before SDK coercion or unknown-key removal. Exact request
  containers, bounded UTF-8 fields and token, canonical unique relative POSIX paths, integer graph
  versions, and typed bounded Task 5 models are required. A hostile `dict` subclass is rejected
  before any overridden member access.
- Narrowly allowed an initialized repository's not-yet-created append-only evidence, case, and
  proposal ledgers to snapshot as canonical empty inputs. The first conflicting preflight creates
  its case ledger through the existing transaction path; config and graph remain mandatory.
- Fixed error/cancellation boundaries remove task, classification, token, operation, identity,
  digest, and grant references from repository traceback frames while preserving exact
  cancellation identity. An interruption after registry insertion rolls back the unreturned grant.
- Task 8 host interception remains deliberately out of scope. These MCP capabilities exist for the
  host to enforce, but Task 7 does not claim that repository mutations are already intercepted.

## TDD evidence

Before any production edit, only `tests/unit/intent_workflow/test_authorization.py` was created and
the exact required command ran:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/intent_workflow/test_authorization.py -q -W error
```

Required RED: collection failed with
`ModuleNotFoundError: No module named 'intent_engineering.intent_workflow.authorization'`;
`1 error in 0.17s`.

Integration tests were then added before MCP production wiring. Their initial RED was
`4 failed, 9 passed in 1.58s`, all from the missing new tool surface. The first integrated run was
`46 passed, 1 failed`; the sole failure exposed that a fresh initialized project has no case ledger
until its first conflict. Treating absent append-only ledgers as empty restored focused GREEN to
`47 passed in 1.53s`.

Security self-audit remained tests-first:

- Cancellation immediately after registry insertion was RED and now proves the unreturned
  capability is removed.
- Raw unknown/coerced/duplicate/absolute request probes were `2 failed` before raw prevalidation;
  the strengthened matrix then passed `12 passed in 0.66s`.
- A hostile `dict` subclass probe failed `1 failed in 0.52s` because `.get()` ran before the exact
  type check; reordering the check restored `1 passed in 0.41s`.
- A noncanonical relevant-ID order probe failed as the eighth parameter (`1 failed, 7 passed in
  0.19s`); issuance now requires exact canonical order and the matrix is `8 passed in 0.13s`.

The final tests cover mechanical/aligned issuance; every issuance and verification mismatch;
expiry/restart/revoke; exact UTC/TTL; capacity and expired eviction; deterministic concurrent
issue/verify/revoke; strict frozen detached records; digest-only state; token uniqueness; canonical
path and identity bounds; exact/zero/subset/expanded scope; all four real Task 5 classifications;
live config/graph replacement; direct official API and stdio validation; reduced denials; and token
absence from project files, output, logs, errors, and repository traceback locals after success,
denial, fixed failure, and cancellation.

### Independent review Fix Round 1

The first independent review reported `0 Critical / 3 Important / 0 Minor`, Not Ready. Before any
Fix Round production edit, the exact combined issuer/MCP review selection produced the
authoritative tests-only RED: `15 failed, 36 passed in 2.19s`. The failures were the missing graph
content binding API; three issuer Windows-drive/colon paths; same-version semantic graph
replacement; swaps between Task 5 validation and issuance and between live verification snapshot
and issuer verification; UTF-8 token-byte overflow; three raw MCP Windows-drive/colon paths;
noncanonical `+00:00` timestamp spelling; accepted hostile string/list subclasses; and execution of
a hostile nested dictionary override.

Fix Round 1 now derives a canonical SHA-256 graph identity by parsing and deterministically
reserializing descriptor-held graph bytes. Task 5 returns a frozen internal result/digest handoff;
the MCP service compares its final descriptor-held graph to that authenticated identity; the issuer
derives and stores the same digest from graph bytes rather than accepting a caller digest; and
verification requires exact version plus graph identity. Post-issue and post-verify descriptor
re-reads close both tested race windows. A failed post-issue recheck revokes only the just-created
capability before returning the fixed rejected response.

The raw MCP boundary now walks exact built-in JSON dictionaries, lists, keys, and scalar types
before any SDK coercion, serialization, or subclass behavior. It rejects nested subclasses,
unknown/missing/coerced fields through strict typed validation, noncanonical timestamp spellings,
non-UTC offsets, and a token exceeding 256 UTF-8 bytes even when its character count is smaller.
An actual serialization-cancellation probe initially interrupted shared test-runtime setup; after a
test-only ordering correction, its authoritative behavioral RED was `1 failed in 0.48s` because the
task envelope remained in a repository traceback frame. Every raw conversion frame now clears its
input while preserving exact cancellation identity.

Issuer and MCP path validation now combines POSIX canonicality with `PureWindowsPath` drive and
absolute detection and rejects colon ambiguity, covering drive-absolute, drive-relative, UNC,
device, named-pipe-like, backslash, and forward-slash Windows forms while retaining valid relative
POSIX repository paths.

The exact prior review selection is GREEN at `51 passed in 0.98s`. A final tests-first cancellation
audit then found that targeted post-issue revocation could retain the capability in the token-digest
frame (`1 failed in 0.18s`); explicit digest-frame scrubbing restored it to `1 passed in 0.16s`.
Every final gate below was rerun after that production change.

### Independent review Fix Round 2

The second independent review reported `0 Critical / 2 Important / 0 Minor`, Not Ready. Only the
issuer and raw MCP tests were changed before the authoritative Round 2 command. Production remained
untouched at that boundary, and the exact tests-only RED was `30 failed, 46 passed in 4.16s`:
12 issuer reserved-device paths, the same 12 raw MCP paths, three bounded cycle probes, one shared
container alias, and two excessive-shape probes. Nearby valid POSIX names passed throughout.

Issuer and raw MCP path validation now examines every `PureWindowsPath` segment and rejects Windows
reserved devices case-insensitively, including `NUL`, `CON`, `AUX`, `PRN`, `CLOCK$`, `COM1`–`COM9`,
and `LPT1`–`LPT9`, plus extension and trailing-dot/space variants. Valid repository-relative names
such as `com10.py`, `lpt10.txt`, `auxiliary.py`, and `null-device.py` remain accepted.

Raw JSON validation is now an iterative bounded tree walk. Exact dictionary/list containers are
tracked with active and seen identity sets, so direct and mutual cycles and shared aliases fail
deterministically before serialization or typed validation. Depth is limited to 128, total keys and
values to 65,536 nodes, and aggregate exact-string/key UTF-8 content to 1 MiB. All raw references,
pending frames, identity sets, and temporary encoded bytes are cleared in the validator's `finally`
boundary. Bounded child-process tests prove cyclic input cannot hang the server and that fixed errors,
traceback frames, and logs retain neither task nor token markers.

The exact Round 2 selection is GREEN at `76 passed in 1.09s`. The first Ruff pass found only the
new regex flag's discouraged `re.I` alias; changing it to `re.IGNORECASE` restored the required
static gate, after which the focused selection and every final gate below were rerun.

### Independent review Fix Round 3

The third narrow review reported `0 Critical / 1 Important / 0 Minor`, Not Ready: the preflight raw
branch constructed a key set before invoking the exact-tree validator, and the three existing
proposal branches shared the dispatcher without equivalent raw validation. With production
untouched, the exact ten-case hostile key/scalar matrix was RED at `7 failed, 3 passed in 0.89s`.
It proved root-key `str` subclasses could execute `hash`/`eq` in preflight and proposal dispatch,
bootstrap could execute a hostile scalar `repr`, and proposal show/confirm could accept hostile
scalar subclasses through to their handlers.

The raw dispatcher now performs only the exact root-dictionary identity check before the iterative
exact-tree validation. Tool-name comparison, allowed-key set construction, lookup, conversion, and
strict typed validation occur only after every key, container, and scalar has been proven an exact
built-in. All five workflow tools have exact allowed-key and typed raw branches, unknown tool names
fail closed, and bootstrap conversion now clears its raw value, encoded form, and candidate model in
a `finally` boundary. The exact hostile key/scalar matrix is GREEN at `10 passed in 1.59s`, with zero
overridden behavior and zero handler calls in every rejection.

The final finding-only re-review accepted the boundary at `0 Critical / 0 Important / 0 Minor`,
Ready. The user approved a fast-but-safe final cadence: the focused Task 7 gate, targeted Ruff and
mypy, and one fresh full offline suite before commit; then the focused Task 7 gate, targeted Ruff,
mypy, base-to-HEAD diff check, and index/protected-name audit after commit, without a redundant
second full-suite run.

## Final gates

- Required authorization-only gate: `63 passed in 0.28s`.
- Required authorization/MCP/wire gate: `138 passed in 3.18s`.
- Broadened Task 4–7 onboarding, preflight, clarification, governance, MCP, CLI, and write-guard
  compatibility: `423 passed in 32.95s`.
- Required Ruff paths: `All checks passed!`.
- `.venv/bin/mypy src`: `Success: no issues found in 112 source files`.
- Fresh full offline warnings-as-errors: `1422 passed in 53.34s`.
- `git diff --check`: no output.

## Review scope

Changed paths are limited to the Task 7 authorization module, the narrow preflight empty-ledger
handoff, MCP workflow registration/lifecycle, authorization/MCP/wire tests, this report, and the SDD
progress ledger. No provider/network access, external write, host enforcement, staging, or commit
was performed.
