# Task 2 report: deterministic GitHub REST client

## Commits

- Base commit: `9b099a64ef59c096a6070c295d3ca9281e21bfac`
- Product/test commit: `e257c323a9d1baff62fdea0061ba1c6681063766`
- Product/test commit subject: `feat: add deterministic github rest client`
- This report is committed separately from product and tests.

## TDD evidence

The required focused RED ran after the tests were written and before any Task 2 production file
was created:

```bash
.venv/bin/python -m pytest tests/unit/capture/github/test_client.py -q
```

Observed result: collection stopped with the expected
`ModuleNotFoundError: No module named 'intent_engineering.capture.github.client'` (exit 2). The
failure was the missing REST boundary, not a typo in an existing implementation.

The first GREEN for that focused suite was `39 passed in 0.10s`. Three later findings each used a
separate focused RED/GREEN cycle:

- immutable `MappingProxyType` provider data initially failed JSON serialization with
  `PydanticSerializationError`; the serializer regression then passed;
- an injected `AsyncClient` initially retained Authorization after wrapper close and forwarded it
  on a later cross-origin request; the ownership regression then passed with caller headers never
  mutated;
- a token reflected in the server-controlled request-ID header initially appeared in the public
  error representation; the redaction regression then passed by discarding reflected IDs;
- a strict model initially rejected the deeply frozen mapping returned by `PageResult`; the model
  boundary regression then passed while invalid timestamps remained rejected.

## Final verification evidence

Fresh focused GitHub gate after final formatting:

```bash
.venv/bin/python -W error -m pytest \
  tests/unit/capture/github/test_auth.py \
  tests/unit/capture/github/test_client.py -q
```

Result: `66 passed in 0.13s` with warnings promoted to errors.

Relevant capture/package gate:

```bash
.venv/bin/python -m pytest tests/unit/capture tests/unit/test_package.py -q
```

Result: `67 passed in 0.28s`.

Tracked Python lint and package type checking:

```bash
.venv/bin/ruff check $(git ls-files '*.py')
.venv/bin/mypy src/intent_engineering
```

Results: `All checks passed!` and `Success: no issues found in 71 source files`.

Full regression suite:

```bash
.venv/bin/python -m pytest -q
```

Result: `426 passed in 18.42s`.

`git diff --cached --check` completed with no output before the product commit. All HTTP behavior
was tested with `httpx.MockTransport`; no live GitHub credential was read and no network request
was made.

## Implemented boundary

- Internal clients use `https://api.github.com`, explicit connect/read/write/pool timeouts, fixed
  GitHub Accept and Intent Engineering User-Agent headers, bearer authentication, and redirects
  disabled.
- Injected clients are caller-owned and never have their default headers mutated. Authentication
  is supplied only on wrapper requests and is not available to later caller requests, including
  cross-origin requests. Internally created clients alone are closed by `aclose`/async context
  management.
- Pagination preserves page order, sends params and `If-None-Match` on page one only, captures the
  first response ETag, returns the exact empty 304 result, rejects unsafe or malformed next links,
  detects cycles, and enforces a deterministic page cap.
- Each page must decode to a JSON array containing only objects. Returned sequences, item objects,
  nested mappings, and nested arrays are recursively immutable and serialize without warnings.
- Idempotent GET transport failures and 5xx responses alone receive bounded deterministic retries.
  Exhaustion produces a detached `GitHubTransientError` with attempt count and safe scalar
  metadata only.
- Permission, primary/secondary rate limit, not-found, generic API, transient, and protocol errors
  are provider-local. Retry times safely parse reset epochs, delta seconds, and HTTP dates in UTC.
  Malformed values yield no retry time or parse context.
- Error rendering length-bounds and sanitizes endpoint paths/request IDs, discards IDs reflecting
  the active credential, and never retains request/response/header/body objects.
- Frozen strict models cover the GitHub user, issue, pull request, commit, issue comment, and review
  comment fields needed by Task 3. Unknown fields are accepted only through an explicit deeply
  immutable provider-local `extra` mapping.

## Dependency versions

- Python `3.12.13`
- `httpx==0.28.1`
- `pytest==9.1.1`
- `pytest-httpx==0.36.2`
- `pydantic==2.13.4`
- `anyio==4.14.2`

## Preserved local artifacts

The five pre-existing untracked files remain untracked and were excluded from the product and
report commits:

- `.coverage 2`
- `.coverage 3`
- `.coverage 4`
- `README 2.md`
- `src/intent_engineering/core/policy/dogfood 2.py`

## Scope boundary

This task adds no connector discovery or normalization, project configuration, CLI command,
workflow, or live GitHub check. Task 3 was not started.
