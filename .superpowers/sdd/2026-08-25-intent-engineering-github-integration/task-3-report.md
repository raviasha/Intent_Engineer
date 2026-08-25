# Task 3 report — normalize GitHub evidence through the Connector port

## Status

Complete. Product/tests commit: `7b3a91e` (`feat: ingest github repository evidence`).
Dispatch base: `e004856`.

## TDD evidence

- Initial focused RED:
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p anyio.pytest_plugin tests/contract/capture/test_github_connector_contract.py tests/integration/github -q`
  failed during collection with
  `ModuleNotFoundError: intent_engineering.capture.github.connector`.
- Subsequent RED/GREEN cycles directly proved cursor UTC decoding, newest-first commit
  selection, repository-cache normalization scope, deep checkpoint association validation,
  unexpected locator fragments, endpoint-atomic duplicate rejection, and case-insensitive
  provider repository URLs before each production fix.

## Delivered behavior

- Added a repository-scoped `GitHubConnector` with canonical instance IDs
  `github:<owner>/<repo>` and provider-neutral `connector_type="github"` evidence.
- Captures issues, pull requests, commits, issue comments, and review comments through the
  approved `GitHubClient`; filters pull-request rows from `/issues`.
- Strict typed provider decoding recursively places unknown fields in immutable provider-local
  `extra` mappings and excludes them from normalized semantic payloads and hashes.
- Produces deterministic stable IDs, canonical UTC mutable versions, lowercase commit SHAs,
  fixed missing-commit-time sentinel behavior, canonical semantic hashes, nullable actors,
  and validated/canonical GitHub HTML locators.
- Added a strict, versioned, repository-bound canonical JSON checkpoint containing per-endpoint
  ETags, newest mutable timestamp, and newest commit SHA. It rejects duplicate/non-finite JSON,
  wrong scalar types, extra/unknown fields, foreign repositories, malformed timestamps/SHAs/
  ETags, noncanonical encodings, and oversized inputs before HTTP.
- Preserves endpoint-level partial durability: a later endpoint failure returns only prior valid
  endpoint objects, retains a fixed boolean failure flag, and fails at `next_checkpoint` after
  the orchestrator has durably processed those objects. Prior checkpoint bytes remain exact.
- Exact fetch-cache and single-use checkpoint boundaries reject missing, mismatched, duplicate,
  stale, out-of-order, and cross-repository values with fixed redacted connector errors.
- Extended deep validation for canonical GitHub ledgers/checkpoints, exact consumption prefixes,
  association scope/type/identity, canonical semantic hashes, cursor presence, mutable timestamp,
  commit SHA, and repository binding while preserving unknown-connector rejection.
- Integration tests use production connector/client/orchestrator and real local evidence,
  checkpoint, graph, case, history, and transaction stores against `httpx.MockTransport` only.
  Multi-repository instances have isolated ledgers, version chains, and checkpoints.

## Verification

- Focused GitHub contract/integration/failure/deep-validation plus existing GitHub auth/client
  under `-W error`: **134 passed**.
- Relevant capture contracts, capture/sync integrations, package, validation, evidence-store,
  and checkpoint-store regressions under `-W error`: **115 passed**.
- Full offline suite under `-W error`: **494 passed in 18.99s**.
- Tracked Python Ruff check: **passed**.
- Task-scoped Ruff format check: **13 files already formatted**.
- Mypy: **success, 72 source files**.
- `git diff --check`: **passed**.
- Runtime versions: Python 3.12 environment; `httpx 0.28.1`, `pydantic 2.13.4`,
  `pytest 9.1.1`.

The whole-tree Ruff format check still reports 30 pre-existing untouched files that would be
reformatted. Task 3 files pass format checking; this task intentionally did not create an
unrelated repository-wide formatting diff.

## Safety and scope

- No live GitHub request or credential lookup ran; all credentials and HTTP responses are fake.
- Tokens, response bodies, request/response objects, validation inputs, and raw provider extras
  are absent from evidence, checkpoints, public errors, and sync results.
- No Task 4 CLI/Action/reporting work, docs quick start, MCP, browser, GUI, or raw Git object access
  was performed.
- The five protected untracked artifacts remain the only untracked files.
