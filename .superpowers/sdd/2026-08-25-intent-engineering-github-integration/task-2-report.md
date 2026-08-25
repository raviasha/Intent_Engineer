# Task 2 report: deterministic GitHub REST client

## Commits

- Base commit: `9b099a64ef59c096a6070c295d3ca9281e21bfac`
- Product/test commit: `e257c323a9d1baff62fdea0061ba1c6681063766`
- Product/test commit subject: `feat: add deterministic github rest client`
- Review-fix product/test commit: `7d2015155f5f420e23dfcbbe4cf527604a4a6b83`
- Review-fix commit subject: `fix(github): harden REST client boundaries`
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

The first GREEN for that focused suite was `39 passed in 0.10s`. Four later findings each used a
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

## Review fix round 1

Independent review found four Important and two Minor gaps on report base
`7537182d47471df4ee6d383e5acbaccbd04e17da`. Each finding was verified against the implementation
and fixed in `7d2015155f5f420e23dfcbbe4cf527604a4a6b83` without beginning Task 3.

### Focused RED/GREEN evidence

1. **Injected client isolation.** A hostile injected client with default Basic auth, Authorization,
   ETag, query params, and 99-second timeout changed wrapper requests. The RED showed caller query
   params on page one. The GREEN uses an explicit `httpx.Request` and `send(auth=None,
   follow_redirects=False)`: wrapper bearer/fixed headers win, page-two Link URLs remain exact,
   `If-None-Match` is first-page-only, fixed timeout extensions are present, caller state is
   unchanged, and later cross-origin caller reuse has no wrapper bearer.
2. **Long reflected tokens.** A realistic 94-character `github_pat_...` token reflected through
   `X-GitHub-Request-Id` leaked a 64-character prefix in RED. A second RED showed the standalone
   11-character `github_pat_` prefix also survived. GREEN compares raw and sanitized secret
   fragments before public truncation and discards the request ID.
3. **Strict JSON extras.** Seven RED cases proved sets, bytearrays, arbitrary objects, NaN,
   infinity, non-string keys, and a nested set were accepted. GREEN rejects all seven, accepts only
   string-keyed finite JSON shapes, and preserves detached deep immutability plus warning-free
   serialization.
4. **Deleted actors.** Issue, issue-comment, and review-comment payloads with `user: null` each
   failed RED validation. All three pass GREEN with `GitHubUser | None`; non-null actors remain
   strictly validated.
5. **Pagination fragments.** A same-origin next URL with a fragment caused a second request in RED.
   GREEN rejects it before another request.
6. **Malformed Link headers.** Garbage, an unterminated target, and a missing parameter value were
   silently accepted or followed in RED. GREEN uses a strict complete-header parser; six malformed
   structures fail closed before request two while valid GitHub Link pagination remains accepted.

### Fix-round final verification

Focused GitHub tests with warnings promoted to errors:

```bash
.venv/bin/python -W error -m pytest \
  tests/unit/capture/github/test_auth.py \
  tests/unit/capture/github/test_client.py -q
```

Result: `87 passed in 0.14s`.

Relevant capture/package tests:

```bash
.venv/bin/python -m pytest tests/unit/capture tests/unit/test_package.py -q
```

Result: `88 passed in 0.25s`.

Tracked lint, formatting, and type checking:

```bash
.venv/bin/ruff check $(git ls-files '*.py')
.venv/bin/ruff format --check src/intent_engineering/capture/github tests/unit/capture/github
.venv/bin/mypy src/intent_engineering
```

Results: `All checks passed!`, `8 files already formatted`, and
`Success: no issues found in 71 source files`.

Full regression suite:

```bash
.venv/bin/python -m pytest -q
```

Result: `447 passed in 17.21s`.

`git diff --cached --check` completed without output before the fix commit. All new regressions use
offline `httpx.MockTransport` responses; no GitHub credential was read and no network request was
made.

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
