# Task 1 — Typed MCP provider profiles and safe selectors

Status: `ACCEPTED` after two independent scoped review rounds

## Scope and design

Implemented only the provider-neutral Task 1 contracts. No session/runtime, transport,
credentials, provider-specific profile, connector, plan, approval, or write execution behavior
was added.

- `ProviderProfile`, read/write/object operations, `Selector`, `ArgumentBinding`, and
  `ProviderBinding` are Pydantic frozen, extra-forbid public models. Every nested mapping is
  copied into a `MappingProxyType`; nested JSON values are recursively detached/frozen and
  serialise back to detached JSON.
- Validation rejects blank/control-bearing public IDs, semantic names, argument/field names,
  selector paths and binding capability names. It checks object operation references, operation
  key/name agreement, paginated selector consistency, strict write selectors, non-empty field
  allowlists, exact object JSON Schema properties, field argument allowlists, and complete
  profile-version/kind-correct binding mappings.
- Profile reads use `SecureDirectory`/`SecureFile` descriptor-rooted no-follow access, thereby
  rejecting symlinks, hardlinks and non-regular files. The YAML event and construction stages
  reject aliases, custom tags, duplicate/non-string mapping keys, multi-document/non-mapping
  input and invalid values. All loader failures have exactly `invalid MCP provider profile`, no
  cause/context and retain no profile text or parser exception.
- Selectors allow root, safe dot keys and canonical decimal indexes only (256-character/
  32-segment bounds). Access preserves explicit JSON null; only a missing optional mapping path
  yields `None`. Type/index failures are fixed redacted errors. The transform registry contains
  only `string`, `integer`, `iso_datetime`, `string_list`, `canonical_json` and `sha256`;
  datetimes canonicalise to UTC `Z`, JSON is stable, and hashes use `sha256:<hex>`.
- `bind_arguments()` accepts only the enumerated model sources, requires present context/fields,
  and returns fresh strict JSON values without mutating/aliasing caller input.
- The checked-in schema is canonical UTF-8 JSON from
  `ProviderProfile.model_json_schema()` (11,508 bytes after review hardening).

## TDD evidence

Tests were written before production modules. The initial focused command was:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v
```

RED result: exit 4 in 0.4s with the expected
`ModuleNotFoundError: No module named 'intent_engineering.capture.mcp'` while importing the
new test conftest. The tests name and catch: removal of a write version precondition;
permissive IDs/fields/bindings; mutable or extras-accepting public models; unsafe/redaction-leaky
loader paths; selector grammar/missing-null/type errors; transform coercion; argument aliasing;
and schema drift. Schema and transform expectations were independently hand-derived (the SHA-256
literal was checked with local `shasum`, not the production helper).

GREEN result after the smallest cohesive implementation:

```text
43 passed in 0.94s
```

## Files changed

- `pyproject.toml`
- `src/intent_engineering/capture/mcp/__init__.py`
- `src/intent_engineering/capture/mcp/profile_models.py`
- `src/intent_engineering/capture/mcp/profile_loader.py`
- `src/intent_engineering/capture/mcp/selectors.py`
- `src/intent_engineering/storage/secure.py`
- `schemas/mcp-provider-profile.schema.json`
- `tests/unit/capture/mcp/__init__.py`
- `tests/unit/capture/mcp/conftest.py`
- `tests/unit/capture/mcp/test_profiles.py`
- `tests/unit/capture/mcp/test_selectors.py`

## Verification evidence

| Command | Result |
| --- | --- |
| `.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v` | 43 passed |
| `git ls-files -- '*.py' \| xargs .venv/bin/ruff check` | clean |
| `.venv/bin/mypy src/intent_engineering` | 78 source files, no issues |
| `.venv/bin/pytest tests/unit/test_package.py -v` | 1 passed |
| `.venv/bin/pytest -q` | 711 passed in 26.94s |
| schema regenerate/read/compare command | byte-identical, 7,774 bytes |
| `git diff --check` | clean |

The untracked protected filename `dogfood 2.py` makes broad `ruff check .` invalid; tracked-file
Ruff was run instead so no protected untracked file was inspected by linting.

## Dependency limitation

`pyproject.toml` declares the required CI range `mcp>=2,<3`. The offline current virtual
environment has no installed `mcp` distribution (`mcp resolved version: unavailable offline`),
so no package index was contacted and no unused lock/export artifact was invented.

## Protected artifact integrity

Opaque SHA-256 integrity checks were recorded without staging any protected artifact:

```text
1544dde7f20fe70edd83c28735219a6c7c01a34ce09465dedfb143e97b1b1664  .coverage 2
1544dde7f20fe70edd83c28735219a6c7c01a34ce09465dedfb143e97b1b1664  .coverage 3
1544dde7f20fe70edd83c28735219a6c7c01a34ce09465dedfb143e97b1b1664  .coverage 4
26ffb68b27d2f10d2923eef1140b1039d4f5fa98401e668489b3dfcec422ec45  README 2.md
072a6691254482cb7b03aa242577269808cae70f1661dc0a07a7812fb3a64b20  src/intent_engineering/core/policy/dogfood 2.py
```

## Self-review and concerns

- Audited public models for `extra="forbid"`, frozen state and deep detachment rather than
  relying on illustrative snippets alone.
- Audited loader error causes/contexts explicitly; tests assert both are `None` and test a secret
  sentinel is absent.
- Audited `ProviderBinding` against all read tool/resource capabilities plus writes, not just the
  two illustrated missing-operation cases.
- The profile schema has no provider profile yet by design; provider-specific profiles remain
  Task 3. Official SDK use is deferred to Task 2; the dependency range is declared but unresolved
  in this offline environment.
- An independent read-only adversarial review is still required before any commit. No files are
  staged or committed, and the progress ledger was not modified.

## Review fix round 1/5 — Critical and Important findings

Status: `PRECOMMIT_REVIEW_READY` after remediation; still unstaged and uncommitted.

### Root causes and fixes

1. Fixed selector/transform/binding errors were raised in frames that still owned provider or
   write input. The public APIs now call non-raising internal result functions, delete
   caller-controlled references before constructing a fixed public exception, and retain only
   inert status codes on the public traceback. Regression tests use
   `TracebackException(..., capture_locals=True)` and inspect all production selector frames for
   a sentinel, while also asserting exact args and absent cause/context.
2. `ArgumentBinding` combined nullable defaults with source-shape validation based on field
   presence. Its serializer now omits irrelevant defaults, its validator rejects explicitly
   supplied irrelevant null fields, and every source round-trips through `model_dump()` and
   `model_validate()`.
3. External selector/binding JSON is now limited to exact built-in `dict`, `list`, finite numeric
   scalars, string, boolean and null values. Tuple and arbitrary `Mapping` subclasses are rejected
   without invoking their protocol. Constants are thawed only from the known frozen
   `MappingProxyType`/tuple representation produced by the model validator.
4. The public schema now exposes text/path constraints, transform literal enum, mapping/item
   minimums and cross-field JSON Schema conditionals for `ArgumentBinding` source shapes and
   explicit false write preconditions. Representative invalid data is rejected by
   `Draft202012Validator`, not merely by Pydantic. The regenerated canonical schema is 11,489
   bytes and byte-identical to `ProviderProfile.model_json_schema()`.
5. `SecureFile.read_bytes_nonblocking()` narrowly adds an `O_NONBLOCK` descriptor read that
   authenticates regular/single-link/no-follow status with `fstat` before read. The profile loader
   uses it, so a FIFO fails closed without waiting for a writer. Loader parsing is bounded to
   1 MiB and 64 collection levels, and an internal result boundary collapses `RecursionError`
   without source/parser traceback frames.

### Round 1 RED → GREEN evidence

Initial review RED command:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v
```

It collected 68 tests, produced 17 intended failing contracts (source-shape round trips,
explicit irrelevant nulls and schema constraints) and then blocked at the deliberately added FIFO
regression, confirming the reviewed blocking-open defect. The process was terminated after the
controller checkpoint; no files were changed by the test. After test-harness correction for an
isolated `$defs` reference, the bounded FIFO regression and the other review tests were valid.

Additional cross-field RED used the same exact focused command: 72 tests collected, 68 passed and
4 failed. The substantive failures were acceptance of `before_version.required: false` and source
shape fields by JSON Schema; the initially isolated `$defs` validator did not carry its root
definitions, so the test was corrected to use a root-ref wrapper before the production conditional
schema change.

Round 1 GREEN:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v
72 passed in 0.17s
```

### Round 1 verification

| Command | Result |
| --- | --- |
| focused profile/selector command | 72 passed |
| `.venv/bin/pytest tests/contract/storage/test_secure_paths.py -v` | 3 passed |
| `git ls-files -- '*.py' \| xargs .venv/bin/ruff check` | clean |
| `.venv/bin/ruff check src/intent_engineering/capture/mcp tests/unit/capture/mcp` | clean |
| `.venv/bin/mypy src/intent_engineering` | 78 source files, no issues |
| schema regenerate / byte identity | 11,489 bytes, identical |
| `.venv/bin/pytest tests/unit/test_package.py -v` | 1 passed |
| `.venv/bin/pytest -q` | 740 passed in 23.88s |
| `git diff --check` | clean |

### Round 1 files additionally changed

- `src/intent_engineering/storage/secure.py`
- `src/intent_engineering/capture/mcp/profile_models.py`
- `src/intent_engineering/capture/mcp/profile_loader.py`
- `src/intent_engineering/capture/mcp/selectors.py`
- `schemas/mcp-provider-profile.schema.json`
- `tests/unit/capture/mcp/test_profiles.py`
- `tests/unit/capture/mcp/test_selectors.py`

The five protected artifacts retained their exact previously recorded SHA-256 values after all
round-1 gates. No protected artifact was staged, no commit was made, and no Task 2 behavior was
introduced.

## Review fix round 2/5 — scoped re-review findings

Status: `PRECOMMIT_REVIEW_READY`; all product, test, schema, and report changes remain unstaged
and uncommitted. No Task 2/session behavior or provider-specific profile was added.

### RED-first regressions and root causes

The review verdict supplied the four scoped findings directly. Before production changes, the
following focused command was run:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v
```

It collected 74 tests and returned **72 passed, 2 failed in 0.30s**. The expected product failures
were:

1. `redacted_paths` accepted an invalid selector because it was merely `frozenset[str]` in the
   public model/schema.
2. A valid selector whose `integer` transform rejected provider text surfaced
   `SelectorError("invalid selector access")` instead of the exported fixed `TransformError`.

The initial 33-segment fixture used longer segment names and therefore could also be rejected by
the independent 256-character bound. Before product changes, it was narrowed to 33 one-character
segments so the regression independently exercises the 32-segment rule rather than length.

The new hostile-`Mapping` regressions carry an observable `invoked` flag for both selector payload
and binding context. They were already green against the exact-type external JSON boundary and now
prove that no mapping protocol method is called, rather than merely proving an eventual redacted
error. The FIFO regression was changed from a same-process call to a subprocess watchdog with a
two-second timeout, empty-output assertions, and `finally` cleanup; it was already green against
the nonblocking descriptor implementation.

### Minimal remediation

1. `SelectorPath` now has a schema-visible `{0,32}` segment quantifier in addition to its existing
   256-character bound and Pydantic validator. `ProviderProfile.redacted_paths` is now
   `frozenset[SelectorPath]`, so model validation and Draft 2020-12 schema validation both reject
   an unsafe redaction path.
2. The public selector error boundary now maps the inert `_TRANSFORM` result to the fixed,
   context-free `TransformError("invalid selector transform")`. The boundary still deletes
   provider inputs, selector, and result before constructing the exception.
3. Tests explicitly verify the `TransformError` type, exact message/args, absent
   `__cause__`/`__context__`, and absence of the provider sentinel from captured production
   traceback locals. The hostile mapping tests verify `invoked is False`; the trusted-constant
   test continues to demonstrate that model-frozen constants thaw into detached ordinary JSON.

### Round 2 GREEN and final gates

Focused GREEN after the remediation:

```text
.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v
74 passed in 0.22s
```

| Command | Result |
| --- | --- |
| focused profile/selector command | 74 passed in 0.22s |
| `.venv/bin/pytest tests/contract/storage/test_secure_paths.py -v` | 3 passed in 0.43s |
| deterministic schema regenerate/read/compare command | byte-identical, 11,508 bytes |
| `git ls-files -- '*.py' \| xargs .venv/bin/ruff check` | clean (rerun after test lint correction) |
| `.venv/bin/ruff check src/intent_engineering/capture/mcp tests/unit/capture/mcp` | clean |
| `.venv/bin/mypy src/intent_engineering` | 78 source files, no issues |
| `.venv/bin/pytest tests/unit/test_package.py -v` | 1 passed in 0.24s |
| `.venv/bin/pytest -q` | 742 passed in 23.73s |
| `git diff --check` | clean |

### Round 2 files additionally changed

- `src/intent_engineering/capture/mcp/profile_models.py`
- `src/intent_engineering/capture/mcp/selectors.py`
- `schemas/mcp-provider-profile.schema.json`
- `tests/unit/capture/mcp/test_profiles.py`
- `tests/unit/capture/mcp/test_selectors.py`

### Security/self-review and integrity

- Independently checked the 33-segment selector (`$.` plus 33 `x` segments) against the public
  `Draft202012Validator`; the schema rejects it without relying on the runtime Pydantic validator.
  The same test independently validates an invalid `redacted_paths` member.
- Rechecked the external boundary policy: `dict`/`list` and scalar acceptance is exact-type only;
  arbitrary mapping subclasses and tuples are not normalized. Trusted constants use the distinct
  `MappingProxyType`/tuple thaw path only after model validation.
- The FIFO watchdog validates the product's descriptor-safe `O_NONBLOCK` behavior from a separate
  process and removes the FIFO even on timeout. The narrow shared secure-file helper remains the
  only descriptor boundary change from review round 1.
- Recomputed opaque SHA-256 values for all five protected artifacts after the final gates; each
  exactly matches the values recorded above. Their contents were not inspected, staged, or
  modified.
- The offline dependency condition is unchanged: `pyproject.toml` declares `mcp>=2,<3`, while the
  current virtual environment has no resolved `mcp` distribution. No index was contacted and no
  lock/export artifact was invented.

### Final independent review verdict

The scoped round-2 re-review marked all four remaining findings **ADDRESSED**: observable hostile
mapping non-invocation, schema-visible selector depth and redacted-path strictness, deterministic
FIFO watchdog coverage, and `TransformError` preservation. It confirmed the appended gate evidence
and found no new Critical or Important breakage or out-of-scope observations.

Product, schema, and tests were committed as `00e41c7` (`feat: add typed MCP provider profiles`).
Task 2 remains intentionally unstarted. The only known limitation is the already-recorded offline
absence of an installed MCP SDK distribution; the required `mcp>=2,<3` range is declared without
inventing a lock artifact.
