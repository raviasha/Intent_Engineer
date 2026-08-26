# Task 2 — Shared MCP client runtime

Status: `PRECOMMIT_REVIEW_READY`; no files staged or committed.

## Scope and implementation

Implemented only Task 2's provider-neutral client runtime:

- `McpServerConfig` and `McpConnectorConfig` are frozen, extra-forbid models.
  They use exact public strings/lists/dicts, reject controls, unsafe shell-shaped
  argv, ambiguous transport fields, credential-bearing/fragment/query URLs,
  duplicate case-insensitive headers, non-reference credentials, invalid
  timeouts, non-exact mappings, and non-finite/non-JSON scope.
- `McpSession` is a narrow async port. `McpRuntime` provides explicit `open`,
  bounded live `inspect_capabilities`, `validate_binding`, tool calls and
  resource reads. Each runtime operation creates its own lease and validates
  bounded, control-free capability names before use.
- `SessionLease` makes injected ownership opt-in. Borrowed sessions are never
  closed; production/transferred sessions close once in a cancellation shield.
  The operation deadline encloses start/initialize, inspection/call/decode and
  owned cleanup; cleanup preserves an original failure/cancellation/interrupt.
- Official SDK imports occur only in `_load_official_sdk`, invoked by the
  production factory. Stdio uses `StdioServerParameters`, `stdio_client` and
  `ClientSession`; it supplies only explicit resolved references and lets the
  SDK inherit its safe environment. Streamable HTTP uses the official adapter,
  an owned `httpx2.AsyncClient`, and `follow_redirects=False`.
- SDK output is accepted only after detached finite built-in JSON validation.
  Text resources use a duplicate-key-rejecting JSON parser. Fixed public error
  classes are raised without causes/contexts or retained raw provider failures.

Task 3 profiles and Task 4 connector/write behavior were not implemented.

## TDD evidence

Initial exact RED command:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -v
```

Result: collection stopped at the expected missing production boundary:
`ModuleNotFoundError: No module named 'intent_engineering.capture.mcp.errors'`.
No production files were edited/staged/committed before that checkpoint.

Security expansion RED used the same command after adding contracts for public
open/inspection, strict resource parsing, live capability validation, and URL
credentials. It collected 35 tests: 31 passed and 4 failed exactly because
`open`/`inspect_capabilities` were absent, duplicate encoded JSON was accepted,
and query credentials were accepted. The subsequent deadline/adapter-redaction
contracts were green against the corrected implementation.

Final focused GREEN:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -v
37 passed in 0.07s
```

Tests cover strict/frozen/extra-forbid configs, exact detached JSON,
configuration rejection, complete binding validation, typed redacted errors,
traceback-local redaction, cancellation/KeyboardInterrupt/original-failure
preservation, close-once ownership, concurrent isolation, timeout of start and
owned close, resource reads/duplicate JSON/non-finite JSON, capability/name
bounds, deferred environment resolution, no-shell SDK construction, HTTP
redirect refusal, and serialized configuration round-trips. All SDK seams are
deterministic; no network, provider, GUI, shell invocation, or credential is
used in tests.

## SDK and security design

The installed declared SDK is `mcp 2.1.1`. Its official v2 interfaces used are
`mcp.client.session.ClientSession`,
`mcp.client.stdio.StdioServerParameters`, `stdio_client`, and
`streamable_http_client`; HTTP construction uses the SDK's `httpx2` dependency.
SDK imports remain inside the production loader, so config/error/port and fake
tests import without the dependency. Resolved references live only in local
adapter startup variables and are discarded during close; public config keeps
only `env:NAME` references.

## Files

- `src/intent_engineering/capture/mcp/session.py`
- `src/intent_engineering/capture/mcp/runtime.py`
- `src/intent_engineering/capture/mcp/errors.py`
- `src/intent_engineering/capture/mcp/__init__.py`
- `tests/fakes/mcp_session.py` and its package marker
- `tests/unit/capture/mcp/test_runtime.py`
- `tests/unit/capture/mcp/conftest.py` (narrow repository-root import support)

No lock/export artifact was added. The five pre-existing protected untracked
artifacts were retained and their SHA-256 values match the recorded Task 1
values. `progress.md` was already modified by the controller and was not
changed by this task.

## Verification

| Command | Result |
| --- | --- |
| focused runtime tests | 37 passed in 0.07s |
| Task 1 profile/selector plus runtime tests | 111 passed in 0.28s |
| tracked and targeted Ruff | clean |
| `.venv/bin/mypy src/intent_engineering` | 81 source files, no issues |
| schema byte identity | 11,508 bytes, identical |
| package test | 1 passed |
| full offline pytest | 779 passed in 24.75s |
| `git diff --check` | clean |
| protected artifact SHA-256 | all five match |

## Self-review and remaining review item

Audited all production SDK imports, public error chaining, detached JSON
boundaries, close ownership and redirect handling. Capability pagination is
not added: the Task 2 port exposes a bounded exact capability set supplied by
the session; a production server response with pagination beyond the initial
SDK list response should be independently reviewed before merge if profiles
can bind more than one page. An independent read-only adversarial review is
still required by the brief; it was not dispatched because the controller
explicitly prohibited subagents. No staged changes or commit exist.

## Review fix round 1/5 — lifecycle, SDK, and secret-boundary hardening

Status: `PRECOMMIT_REVIEW_READY`; the reviewer checkpoint is preserved above.
No files are staged or committed.

### Review RED

Before production changes, the exact focused command was:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -v
```

It collected **47 tests: 37 passed, 10 failed**. The failures independently
proved the findings: `McpRuntime.open()` leaked raw start errors (C1); stdio
relied on SDK stderr (C2); only the first SDK capability page was consumed
(I1); JSON null/error/hostile decoder behavior was unsafe (I2); faithful v2
error classification lacked a safe path (I3); and shell basenames/combined
execution switches were accepted (I5). The direct test package name shadows
the installed `mcp` import under pytest, so the I3 regression uses a faithful
v2-shaped `mcp.shared.exceptions` error with the installed v2 wire codes; this
tests the same lazy classifier without importing the SDK at public-test import
time. I4/I6 contracts were added in the same focused pass for public-open
deadline behavior and noncapturing environment state.

### Remediation

1. `open()` now enters and exits its internal session manager through private
   result boundaries, uses a single absolute deadline, translates lifecycle
   failures from safe frames, and preserves caller cancellation,
   `KeyboardInterrupt`, and body errors through shielded cleanup.
2. Stdio explicitly receives an owned write-only discard sink instead of SDK
   stderr. The sink retains no text.
3. Production v2 tool/resource enumeration follows `next_cursor` with
   `PaginatedRequestParams`, exact cursor/name bounds, global duplicate and
   cursor-cycle rejection, and the existing capability cap.
4. Official result decoding invokes `model_dump` only for the exact loaded v2
   result type. It rejects `isError`, missing/ambiguous wrappers, arbitrary
   model objects, duplicate/non-finite encoded JSON, and preserves valid JSON
   null with an explicit validity flag.
5. Runtime classification recognizes faithful v2 MCP error codes, standard
   permission/timeout, AnyIO closed resources, Pydantic schema errors, and
   streamable HTTP transport failures without retaining their raw data.
6. Default production environment access is now a noncapturing one-name
   `os.getenv` lookup. The resolver is cleared before transport construction
   and again on failure/close; resolved values remain local startup values.
   Shell executable basenames and `-lc`/`-cl`/command execution switches are
   rejected in configuration validation.

### Round 1 GREEN and verification

```text
.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -v
49 passed in 0.12s
```

| Command | Result |
| --- | --- |
| focused runtime tests | 49 passed in 0.12s |
| Task 1 + runtime tests | 123 passed in 0.64s |
| targeted and tracked Ruff | clean |
| `.venv/bin/mypy src/intent_engineering` | 81 source files, no issues |
| schema byte identity | 11,508 bytes, identical |
| package test | 1 passed |
| full offline pytest | 791 passed in 25.10s |
| `git diff --check` | clean |
| protected artifact SHA-256 | all five match |

### Round 1 self-review

Checked that no public config/session/error import pulls in the SDK, that the
new discard sink has no buffer/state, and that paginated responses have one
global duplicate/cycle cap rather than per-page validation only. The public
open boundary deliberately preserves caller-body exceptions while making
start/close failures fixed and redacted. No Task 3/4 functionality, lock file,
network call, live credential, staging, or commit was introduced.

## Review fix rounds 2–4 — real SDK teardown, bounded pagination, and owner safety

### Round 2 RED

The final edge review found that the SDK stderr object was not a subprocess
file descriptor, timeout/auth exceptions were under-classified, official SDK
cleanup could outlive the operation deadline, and empty unique-cursor pages
could grow without a page bound. Tests were added before production changes.

```text
.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -v
56 collected; 49 passed, 7 failed in 0.36s
```

The seven failures were the real AnyIO subprocess `fileno()` call; MCP
`REQUEST_TIMEOUT`; `httpx2` timeout; HTTP 401 and 403; failed-start/normal-close
deadline behavior; and the 128-page empty-cursor oracle. The focused command
then reached 56 passed, but independent re-review correctly showed that an
outer AnyIO timeout cannot penetrate MCP SDK 2.1.1's nested shield and that a
cleanup timeout must not replace the original start failure.

### Same-task lifecycle-owner design

The final adapter design gives one dedicated asyncio owner task the complete
SDK `AsyncExitStack`: that task enters every SDK transport/client context,
publishes only fixed readiness categories, and exits the same contexts in the
same task. This preserves AnyIO cancel-scope ownership. Callers wait only to
the one absolute operation deadline; shielded SDK cleanup can safely continue
after a reported timeout, with its already-redacted task result consumed and
forgotten. An ordinary initialization failure remains the public transport
failure even when cleanup continues. The persisted fake reproduces the SDK's
nested task group and shield, verifies the caller deadline, and then waits for
both exact cleanup completions.

The stdio diagnostic sink is now an owned `/dev/null` text descriptor accepted
by a real AnyIO subprocess launch. Capability enumeration rejects more than
128 pages as well as its existing name/cursor bounds. Classification recognizes
MCP `-32001`, `httpx2` timeouts, and HTTP 401/403 without retaining provider
error material.

### Final owner review RED → GREEN

Final lifecycle review found two owner-state issues. The long-lived owner frame
retained a bound environment resolver and launched stdio environment, and a
second/concurrent `start()` could replace and leak the first owner. The exact
tests-only RED was:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py \
  -k 'owner_task_drops or repeated_and_post_failure or only_one_concurrent_start' -v
3 failed, 56 deselected in 0.09s
```

The owner now deletes its resolver immediately after resolution. A short-lived
stdio launch helper proves the child receives the resolved value, then clears
the detached mapping and the SDK parameter environment before readiness. A
synchronous owner guard rejects repeated, concurrent, and post-failure starts.
The selector rerun passed all three tests in 0.05s and the full runtime suite
passed 59 tests.

The last re-review found a start/close publication race. A delayed-initialize
regression first failed because `start()` reported success after `close()` won.
The owner now captures the immutable public config, checks close before setup
and atomically after initialization, and never publishes a client or result
types after close. `close()` clears live SDK state both before and after joining
the owner. The exact regression passed in 0.02s; the final runtime suite passed
60 tests. Final independent re-review reported **0 Critical / 0 Important**.

### Final verification

| Command | Result |
| --- | --- |
| `.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -q` | 60 passed |
| `.venv/bin/pytest tests/unit/capture/mcp -q` | 134 passed in 0.52s |
| tracked Python plus explicit MCP Ruff | clean |
| `.venv/bin/mypy src/intent_engineering` | 81 source files, no issues |
| `.venv/bin/pytest tests/unit/test_package.py tests/contract/storage/test_secure_paths.py -q` | 4 passed |
| schema regeneration/read comparison | byte-identical, 11,508 bytes |
| `.venv/bin/pytest -q` | 802 passed in 24.14s |
| `git diff --check` | clean |
| final independent read-only review | 0 Critical / 0 Important |

An earlier full-suite attempt produced 797 passes and one unrelated
fork-initialization race in the case-store contract, then lingered in the
failed multiprocessing child's atexit join. The exact failing test immediately
passed alone and the untouched storage code was outside this diff; subsequent
fresh full runs passed 798, 801, and finally 802 tests normally.

The five protected artifacts remain byte-identical:

- `.coverage 2`, `.coverage 3`, `.coverage 4`:
  `1544dde7f20fe70edd83c28735219a6c7c01a34ce09465dedfb143e97b1b1664`
- `README 2.md`:
  `26ffb68b27d2f10d2923eef1140b1039d4f5fa98401e668489b3dfcec422ec45`
- `src/intent_engineering/core/policy/dogfood 2.py`:
  `072a6691254482cb7b03aa242577269808cae70f1661dc0a07a7812fb3a64b20`

Task 2 intentionally stops at the shared runtime. Reference profiles,
continuous evidence ingestion, scheduled reconciliation UX, connectors, and
guarded external write-back remain later tasks. The user clarified that later
UX must treat connected conversations as continuously ingested evidence and
reconciliation as a separately schedulable drift check; no Task 3+ assumption
was added here. External system/code write-back remains preview plus explicit
approval. The user approved the later-task hybrid graph policy: authorized
contributors' authenticated evidence retains provider/workspace/account author
identity, timestamps, object/version lineage, content hashes, and per-author
diffs; non-conflicting evidence may project continuously, while overlapping or
contradictory cross-author changes remain side by side in a human-approved
review case. Contributor and approver authorization stay distinct, and every
approval is itself appended with approver identity, time, and evidence.

Product, runtime adapters, fakes, and tests were committed as
`60dfd4aa876c6a0954ff4040a935f1881a3a88d3`
(`feat: add shared MCP client runtime`). This report and the progress ledger are
committed separately; Task 3 has not started.
