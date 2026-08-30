# Milestone 1 final-review fix report

## Status

Complete. All final-review findings are addressed in one cohesive browser/control-plane fix wave.
No provider execution path was coupled to approval, no handler gained semantic-store access, and no
private answer plaintext is persisted.

## Required reading and scope

Before editing, the worktree instructions and required sources were read completely in this order:

1. `AGENTS.md`
2. `INTENT_ENGINEERING.md`
3. `schemas/intent-meta-model.yaml`
4. `graph/framework-intent-graph.yaml`
5. `ROADMAP.md`
6. `CODEX_IMPLEMENTATION.md`
7. `docs/superpowers/specs/2026-08-29-intent-dev-control-plane-design.md`
8. `docs/superpowers/plans/2026-08-30-intent-dev-control-plane.md`
9. `.superpowers/sdd/2026-08-30-intent-dev-control-plane/progress.md`

The work remained confined to the requested control-plane models, service, HTTP boundary, shipped
browser asset, and directly adjacent tests.

## TDD evidence

The seven regression files were changed before production code. The grouped tests-only RED command
was:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/control_plane/test_models.py tests/unit/control_plane/test_webauthn_service.py tests/contract/control_plane/test_http_api.py tests/contract/control_plane/test_web_assets.py tests/integration/control_plane/test_service.py tests/e2e/test_intent_dev_web.py tests/e2e/test_intent_dev_web_runtime.py -q -W error
```

Its final clean RED output was:

```text
25 failed, 127 passed, 1 skipped in 6.33s
```

The failures mapped to the six requested product findings plus the actual shipped-asset/launched
journey gaps. The new direct production WebAuthn adapter forwarding tests passed in the RED run,
showing that the production adapter already forwarded the required bindings and needed coverage,
not a semantic change.

After implementation, the identical grouped selection against the worktree source was:

```text
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/control_plane/test_models.py tests/unit/control_plane/test_webauthn_service.py tests/contract/control_plane/test_http_api.py tests/contract/control_plane/test_web_assets.py tests/integration/control_plane/test_service.py tests/e2e/test_intent_dev_web.py tests/e2e/test_intent_dev_web_runtime.py -q -W error
```

Final GREEN output:

```text
152 passed, 1 skipped in 2.78s
```

The skip is the existing explicitly manual platform-authenticator probe.

## Findings resolved

### Canonical browser-encoded proposal and case IDs

- Kept shipped `encodeURIComponent` behavior.
- The strict middleware now reconstructs the one canonical raw proposal path from the decoded ID
  only after it passes the existing bounded ID grammar.
- A colon must be uppercase `%3A`; raw colons, lowercase percent spelling, double encoding,
  encoded separators, encoded unreserved characters, and non-grammar characters fail before
  service behavior.
- HTTP contract tests exercise raw ASGI paths, and both shipped-JS and launched-server tests use
  encoded case/proposal IDs.

### Browser-authoritative clarification answers

- Added strict bounded request and response models and public POST endpoints for answer preview and
  idempotent discard.
- Added a public service-owned, ACL-filtered Inbox projection. It reconstructs authenticated
  question prompts from immutable conversation evidence, verifies prompt digests, hides missing,
  invalid, answered, or inaccessible questions, and bounds sessions/questions through existing
  workflow and HTTP limits.
- Inbox renders session/task/question data and the answer form only through DOM construction and
  `textContent`.
- The answer preview returns the existing `answer_clarification` payload, which flows unchanged
  through `/api/v1/decisions/options` and `/api/v1/decisions/verify`; WebAuthn remains the sole human
  authority boundary.
- HTTP handlers call only public service methods and never access semantic stores.

### Action-aware selected-node behavior

- Canonical models and UI require a non-empty selection only for `confirm_baseline` and
  `confirm_proposal`.
- `answer_clarification`, `resolve_conflict`, and `approve_external_write` require the canonical
  empty selection and are no longer blocked by the browser.
- Every review visibly repeats the bound action, subject, and result digest before the explicit
  destructive WebAuthn control.
- Actual shipped-JS tests cover both selection-required actions and all three empty-selection
  action families.

### Same-origin GET requests without Origin

- Exact loopback API GETs may omit `Origin`, matching normal browser behavior.
- A supplied GET Origin must still exactly match the configured canonical origin.
- Exact Host, HTTP scheme, method, query-free canonical path, content boundary, and loopback
  listener constraints remain in force.
- Every POST still requires exact Origin, exact CSRF header/cookie, and exact JSON content type.
- The launched asset journey performs all GETs without injecting Origin and asserts that fact.

### Bounded private answer lifecycle

- Pending answers expire at the five-minute decision lifetime and are purged by status, Inbox,
  preview, options, verify/apply, and discard operations.
- The in-memory table has a hard cap of 64 and a deterministic fixed failure at capacity; it never
  evicts an active item. Cleanup removes only a newly inserted failed/cancelled preview, preserving
  a pre-existing exact item.
- Successful apply, explicit browser cancel/discard, and service close remove the private answer.
- Plaintext remains memory-only; fixed-time expiry/cap/discard tests and cancellation traceback
  tests prove cleanup and secrecy.

### Credential identity consistency

- Local-only credentials require both GitHub identity fields to be absent.
- Future team-mode credentials require both fields to be present.
- Exact model tests cover all complete, absent, partial, and mode-mismatched combinations while
  preserving current local-only enrollment.

### Deferred integration gaps

- Added direct tests proving `PythonWebAuthnVerifier` forwards the exact challenge, RP ID, origin,
  credential public key, sign counter, user-presence, and user-verification bindings supported by
  the production adapter.
- Added a real launched uvicorn + `_ControlPlaneSite` + fetched packaged `app.js` journey. It catches
  encoded-path, absent-GET-Origin, empty-selection, clarification-answer, decisions-options, and
  decisions-verify integration failures in one path.
- Approval continues to create only the authenticated approval record; provider execution remains
  a separate existing operation.

## Files

Production:

- `src/intent_engineering/control_plane/models.py`
- `src/intent_engineering/control_plane/http_models.py`
- `src/intent_engineering/control_plane/service.py`
- `src/intent_engineering/control_plane/web.py`
- `src/intent_engineering/control_plane/assets/app.js`

Tests:

- `tests/unit/control_plane/test_models.py`
- `tests/unit/control_plane/test_webauthn_service.py`
- `tests/contract/control_plane/test_http_api.py`
- `tests/contract/control_plane/test_web_assets.py`
- `tests/integration/control_plane/test_service.py`
- `tests/e2e/test_intent_dev_web.py`
- `tests/e2e/test_intent_dev_web_runtime.py`

Evidence:

- `.superpowers/sdd/2026-08-30-intent-dev-control-plane/final-fix-report.md`

## Verification

Complete control-plane unit/contract/integration/browser/lifecycle selection:

```text
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/unit/control_plane tests/contract/control_plane tests/integration/control_plane tests/e2e/test_intent_dev_web.py tests/e2e/test_intent_dev_web_runtime.py tests/e2e/test_cli_intent_dev.py -q -W error
190 passed, 1 skipped in 12.47s
```

Affected existing onboarding, clarification, proposal-governance, reconciliation, guarded-write,
and granular CLI workflows:

```text
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/intent_workflow/test_bootstrap.py tests/integration/intent_workflow/test_clarification.py tests/integration/intent_workflow/test_proposal_governance.py tests/unit/reconcile tests/e2e/test_cli_write_approval.py tests/e2e/test_cli_local.py -q -W error
222 passed in 31.34s
```

Exact full offline warning-as-error suite:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
1922 passed, 1 skipped in 114.26s (0:01:54)
```

Static gates:

```text
.venv/bin/ruff check <all 11 changed Python paths>
All checks passed!

.venv/bin/ruff format --check <all 11 changed Python paths>
11 files already formatted

PYTHONPATH=src .venv/bin/mypy src
Success: no issues found in 127 source files

git diff --check
[no output; exit 0]
```

Offline wheel/package gate:

```text
uv build --wheel --offline --out-dir <temporary-directory> .
Building wheel...
Successfully built .../intent_engineering-0.1.0-py3-none-any.whl
```

The wheel archive contains `app.js`, `index.html`, and `styles.css` with deterministic timestamps.
Installing that wheel with `pip install --force-reinstall --no-deps` succeeded, and importlib
resources reported exactly those three packaged assets. An earlier `pip wheel --no-build-isolation`
attempt could not import Hatchling from this pre-existing virtualenv; the cached offline `uv build`
path completed the package proof without network access.

Help/diff gates:

```text
.venv/bin/intent --help
.venv/bin/intent dev --help
.venv/bin/intent init --help
.venv/bin/intent bootstrap --help
.venv/bin/intent onboard --help
.venv/bin/intent proposals --help
.venv/bin/intent sources --help
.venv/bin/intent reconcile --help
.venv/bin/intent status --help
```

All exited zero. Root help lists `dev`; `dev --help` retains `--project`, `--prd`, `--no-open`,
`--offline`, and `--status`.

## Self-review

- Re-read every production diff against the final-review list and verified that every mutation
  still passes through the existing application service and semantic workflow services.
- Confirmed no external-provider execute call was added to approval.
- Confirmed browser rendering has no `innerHTML`, persistence, console logging, or manually set
  Origin header, and that cancellation cannot create authority.
- Confirmed the canonical raw-path check compares exact bytes after the decoded bounded grammar,
  so percent-spelling variants cannot alias one service identifier.
- Confirmed pending plaintext has bounded count and lifetime, successful/cancelled/closed cleanup,
  and no durable representation.
- Confirmed the worktree contains no unrelated modification and `git diff --check` is clean.

## Concerns

No product concern remains. The sole skipped test is the pre-existing deliberately manual real
platform-authenticator probe; deterministic fake-authenticator, actual shipped-JS, launched HTTP,
production adapter-forwarding, and full offline coverage all passed.

## Exception-approved corrective pass: failed answer-preview traceback scrubbing

### Approval and scope

After final re-review, the user explicitly approved one exceptional narrow corrective pass for the
remaining `ControlPlaneService.answer_preview()` failure-path traceback retention. The pass changed
only `service.py`, its real-store integration regression, this report, and the progress ledger. It
did not change capacity, expiry, discard, answer authority, HTTP, browser, or provider behavior.

### Root cause

The 65th pending answer correctly failed the fixed hard cap, but only after
`ClarificationCoordinator.preview_answer()` constructed an `EvidenceRecord` whose content contained
the submitted private plaintext. `answer_preview()` already cleared its argument and pending-item
locals, but left that `record` (and related preview intermediates) in its repository traceback
frame when it raised the fixed `ControlPlaneError`.

### Exact RED/GREEN

The real fixed-time regression filled all 64 pending slots, submitted
`PRIVATE-CAP-ANSWER-MARKER-43127`, asserted the fixed exact error/cause/context contract and table
size, and scanned every repository traceback-frame local.

RED command:

```text
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/control_plane/test_service.py::test_pending_answer_cap_failure_scrubs_private_preview_traceback -q -W error
```

RED output:

```text
FAILED tests/integration/control_plane/test_service.py::test_pending_answer_cap_failure_scrubs_private_preview_traceback
E assert 'PRIVATE-CAP-ANSWER-MARKER-43127' not in "... 'content': 'PRIVATE-CAP-ANSWER-MARKER-43127' ..."
1 failed in 1.75s
```

The minimal fix initializes and clears `coordinator`, `record`, `pending`, `existing`, `payload`,
and `material` in `finally`. Finding-only GREEN used the identical command:

```text
1 passed in 2.15s
```

### Corrective-pass verification

Focused control-plane service and browser/API selection:

```text
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/integration/control_plane/test_service.py tests/contract/control_plane/test_http_api.py tests/contract/control_plane/test_web_assets.py tests/e2e/test_intent_dev_web.py tests/e2e/test_intent_dev_web_runtime.py -q -W error
113 passed, 1 skipped in 2.76s
```

Static gates:

```text
.venv/bin/ruff format --check src/intent_engineering/control_plane/service.py tests/integration/control_plane/test_service.py
2 files already formatted

.venv/bin/ruff check src/intent_engineering/control_plane/service.py tests/integration/control_plane/test_service.py
All checks passed!

PYTHONPATH=src .venv/bin/mypy src
Success: no issues found in 127 source files

git diff --check
[no output; exit 0]
```

Fresh exact full offline warning-as-error gate:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src .venv/bin/python -m pytest -p anyio.pytest_plugin -q -W error
1923 passed, 1 skipped in 108.24s (0:01:48)
```

The first full attempt had one unrelated, non-reproducing identical-proposal concurrency failure;
that exact unchanged test then passed once and ten consecutive repetitions. The second full attempt
hit an unrelated two-second advisory-hook child-start deadline, whose abandoned process produced a
downstream resource warning; both reported tests passed together unchanged in isolation, and a
process audit found no remaining pytest, uvicorn, `intent dev`, or multiprocessing process. No
out-of-scope workflow or timing code changed. The third exact full run above passed cleanly.

### Corrective-pass self-review and concerns

The regression would fail if `record` or any newly initialized answer-preview intermediate stopped
being cleared. The fixed exact `ControlPlaneError` type/message and `None` cause/context are pinned,
the active table remains exactly 64 items, and the existing cap/expiry/discard/apply tests remain
green. No product concern remains; the sole skip is still the deliberately manual real
platform-authenticator probe.
