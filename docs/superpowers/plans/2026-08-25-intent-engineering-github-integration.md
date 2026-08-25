# Intent Engineering GitHub Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add locally authenticated GitHub evidence ingestion and a GitHub Action that validates, syncs, and publishes deterministic drift reports without requiring a hosted Intent Engineering service.

**Architecture:** Implement GitHub as another adapter behind the core Connector protocol. A small `httpx` client owns authentication, pagination, conditional requests, and rate-limit conversion; the connector normalizes GitHub objects into immutable EvidenceRecords and advances its checkpoint only after the core sync transaction succeeds.

**Tech Stack:** Python 3.12+, existing core engine, httpx, local `GH_TOKEN` or GitHub CLI authentication, pytest, pytest-httpx, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-08-25-intent-engineering-public-alpha-design.md`

## Global Constraints

- Complete `docs/superpowers/plans/2026-08-25-intent-engineering-core-engine.md` first.
- Use local credentials only: `GH_TOKEN` first, then an existing `gh auth token`; never store token values.
- Ingest issues, pull requests, commits, comments, and relevant metadata with stable GitHub identities and versions.
- GitHub failures must not corrupt graph/evidence state or advance a failed checkpoint.
- Pagination, conditional requests, retries, and rate-limit handling must be deterministic and observable.
- Default tests must use a fake HTTP API and require no network credentials.
- The Action may report ordinary drift but must fail only for configured invariant violations.
- No hosted OAuth, GitHub App installation flow, or webhook receiver belongs in this slice.

---

## File Structure

```text
src/intent_engineering/capture/github/auth.py       Local credential resolution
src/intent_engineering/capture/github/client.py     REST requests, pagination, and error conversion
src/intent_engineering/capture/github/models.py     Provider-local response models
src/intent_engineering/capture/github/connector.py  Connector normalization and checkpoints
src/intent_engineering/cli/github.py                GitHub-specific CLI diagnostics
src/intent_engineering/render/drift_report.py       Markdown Action report
tests/unit/capture/github/                           Auth and normalization tests
tests/contract/capture/                              Shared connector conformance
tests/integration/github/                            Fake-API sync tests
.github/workflows/intent-sync.yml                    Manual/nightly repository workflow
```

### Task 1: Resolve local GitHub credentials safely

**Files:**
- Modify: `pyproject.toml`
- Create: `src/intent_engineering/capture/github/auth.py`
- Create: `src/intent_engineering/capture/github/errors.py`
- Create: `tests/unit/capture/github/test_auth.py`

**Interfaces:**
- Consumes: environment and optional `gh` executable.
- Produces: `GitHubCredentials.resolve(env, run) -> GitHubCredentials` and redacted `GitHubAuthError`.

- [ ] **Step 1: Add the HTTP test/runtime dependencies**

Add `httpx>=0.27,<1` to project dependencies and `pytest-httpx>=0.30,<1` to the `dev` extra. Reinstall with `.venv/bin/pip install -e '.[dev]'`.

- [ ] **Step 2: Write failing authentication-order tests**

```python
def test_gh_token_wins_over_cli_token() -> None:
    calls: list[list[str]] = []

    def run(args: list[str]) -> str:
        calls.append(args)
        return "cli-token"

    credentials = GitHubCredentials.resolve({"GH_TOKEN": "env-token"}, run)
    assert credentials.token.get_secret_value() == "env-token"
    assert credentials.source is CredentialSource.ENVIRONMENT
    assert calls == []


def test_cli_token_is_used_when_environment_is_empty() -> None:
    credentials = GitHubCredentials.resolve({}, lambda args: "cli-token\n")
    assert credentials.token.get_secret_value() == "cli-token"
    assert credentials.source is CredentialSource.GITHUB_CLI


def test_error_never_contains_token() -> None:
    credentials = GitHubCredentials(token=SecretStr("secret-value"), source=CredentialSource.ENVIRONMENT)
    assert "secret-value" not in repr(credentials)
    assert "secret-value" not in credentials.model_dump_json()
```

- [ ] **Step 3: Run auth tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/capture/github/test_auth.py -v`
Expected: FAIL because GitHub auth modules are missing.

- [ ] **Step 4: Implement safe credential resolution**

```python
class CredentialSource(StrEnum):
    ENVIRONMENT = "environment"
    GITHUB_CLI = "github_cli"


class GitHubCredentials(BaseModel, frozen=True):
    token: SecretStr = Field(exclude=True, repr=False)
    source: CredentialSource

    @classmethod
    def resolve(
        cls,
        env: Mapping[str, str],
        run: Callable[[list[str]], str],
    ) -> "GitHubCredentials":
        if token := env.get("GH_TOKEN"):
            return cls(token=SecretStr(token), source=CredentialSource.ENVIRONMENT)
        try:
            token = run(["gh", "auth", "token"]).strip()
        except (OSError, subprocess.CalledProcessError) as error:
            raise GitHubAuthError("Set GH_TOKEN or run 'gh auth login'.") from error
        if not token:
            raise GitHubAuthError("Set GH_TOKEN or run 'gh auth login'.")
        return cls(token=SecretStr(token), source=CredentialSource.GITHUB_CLI)
```

The production runner invokes `subprocess.run(args, check=True, capture_output=True, text=True)` without a shell. Never attach the token or process stderr to a public exception.

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/unit/capture/github/test_auth.py -v`
Run: `.venv/bin/ruff check src/intent_engineering/capture/github tests/unit/capture/github`
Expected: PASS.

```bash
git add pyproject.toml src/intent_engineering/capture/github tests/unit/capture/github
git commit -m "feat: resolve local github credentials"
```

### Task 2: Build the GitHub REST client

**Files:**
- Create: `src/intent_engineering/capture/github/models.py`
- Create: `src/intent_engineering/capture/github/client.py`
- Create: `tests/unit/capture/github/test_client.py`
- Create: `tests/unit/capture/github/conftest.py`

**Interfaces:**
- Consumes: `GitHubCredentials`, repository owner/name, optional ETag.
- Produces: `GitHubClient.get_pages() -> PageResult`, typed issue/PR/commit/comment records, and provider-neutral exceptions.

- [ ] **Step 1: Write failing pagination and conditional-request tests**

```python
@pytest.mark.anyio
async def test_get_pages_follows_link_header(
    httpx_mock: HTTPXMock,
    client: GitHubClient,
) -> None:
    httpx_mock.add_response(
        url="https://api.github.com/repos/acme/demo/issues?state=all&per_page=100",
        json=[{"id": 1, "number": 1}],
        headers={"Link": '<https://api.github.com/repositories/1/issues?page=2>; rel="next"'},
    )
    httpx_mock.add_response(
        url="https://api.github.com/repositories/1/issues?page=2",
        json=[{"id": 2, "number": 2}],
    )
    result = await client.get_pages(
        "/repos/acme/demo/issues", {"state": "all", "per_page": "100"}
    )
    assert [item["id"] for item in result.items] == [1, 2]


@pytest.mark.anyio
async def test_not_modified_returns_no_items(
    httpx_mock: HTTPXMock,
    client: GitHubClient,
) -> None:
    httpx_mock.add_response(status_code=304)
    result = await client.get_pages("/repos/acme/demo/issues", {}, etag='"v1"')
    assert result.not_modified is True
    assert result.items == ()
```

- [ ] **Step 2: Run client tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/capture/github/test_client.py -v`
Expected: FAIL because the REST client is missing.

- [ ] **Step 3: Implement response models and pagination**

Define frozen Pydantic models for GitHub user, issue, pull request, commit, and comment fields used by normalization. Preserve unknown response fields only in a provider-local `extra` mapping; do not leak these models into the domain layer.

Implement:

```python
class PageResult(BaseModel, frozen=True):
    items: Sequence[dict[str, object]]
    etag: str | None
    not_modified: bool = False


class GitHubClient:
    async def get_pages(
        self,
        path: str,
        params: Mapping[str, str],
        etag: str | None = None,
    ) -> PageResult:
        headers = {"Accept": "application/vnd.github+json"}
        if etag is not None:
            headers["If-None-Match"] = etag
        url: str | None = path
        page_params: Mapping[str, str] | None = params
        items: list[dict[str, object]] = []
        first_etag: str | None = None
        while url is not None:
            response = await self._client.get(url, params=page_params, headers=headers)
            if response.status_code == 304:
                return PageResult(items=(), etag=etag, not_modified=True)
            self._raise_for_status(response)
            if first_etag is None:
                first_etag = response.headers.get("ETag")
            items.extend(response.json())
            url = response.links.get("next", {}).get("url")
            page_params = None
            headers.pop("If-None-Match", None)
        return PageResult(items=tuple(items), etag=first_etag)
```

Initialize `httpx.AsyncClient` with the GitHub API base URL, bearer token, fixed User-Agent, and explicit connect/read/write/pool timeouts.

- [ ] **Step 4: Convert failures and rate limits without exposing secrets**

Map `401/403` authentication failures to `GitHubPermissionError`, exhausted primary/secondary rate limits to `GitHubRateLimitError(retry_at)`, `404` to `GitHubNotFound`, `5xx` to `GitHubTransientError`, and other non-success responses to `GitHubApiError(status_code, request_id)`. The public error text may include endpoint and GitHub request ID, but never request headers or response bodies.

```python
def _raise_for_status(self, response: httpx.Response) -> None:
    request_id = response.headers.get("X-GitHub-Request-Id")
    if response.status_code in {401, 403} and response.headers.get("X-RateLimit-Remaining") != "0":
        raise GitHubPermissionError(response.request.url.path, request_id)
    if response.status_code in {403, 429} and response.headers.get("X-RateLimit-Remaining") == "0":
        raise GitHubRateLimitError(parse_reset_time(response.headers), request_id)
    if response.status_code == 404:
        raise GitHubNotFound(response.request.url.path, request_id)
    if response.status_code >= 500:
        raise GitHubTransientError(response.status_code, request_id)
    if response.is_error:
        raise GitHubApiError(response.status_code, request_id)
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/unit/capture/github/test_client.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/capture/github/models.py src/intent_engineering/capture/github/client.py tests/unit/capture/github/test_client.py
git commit -m "feat: add deterministic github rest client"
```

### Task 3: Normalize GitHub evidence through the Connector port

**Files:**
- Create: `src/intent_engineering/capture/github/connector.py`
- Create: `tests/contract/capture/test_github_connector_contract.py`
- Create: `tests/integration/github/test_github_sync.py`
- Create: `tests/integration/github/test_github_failure.py`
- Create: `tests/integration/github/conftest.py`

**Interfaces:**
- Consumes: `GitHubClient`, core `Connector`, `EvidenceRecord`, and `SyncCheckpoint`.
- Produces: `GitHubConnector` with source types `issue`, `pull_request`, `commit`, `issue_comment`, and `review_comment`.

- [ ] **Step 1: Write failing normalization tests**

```python
def test_issue_normalization_preserves_provenance(github_connector: GitHubConnector) -> None:
    source = github_connector.normalize(issue_payload(updated_at="2026-08-25T10:00:00Z"))
    assert source.external_object_id == "github:acme/demo:issue:42"
    assert source.external_version == "2026-08-25T10:00:00Z"
    assert source.author == "octocat"
    assert source.source_locator == "https://github.com/acme/demo/issues/42"
    assert source.payload["kind"] == "issue"
    assert source.content_hash.startswith("sha256:")


@pytest.mark.anyio
async def test_pull_request_is_not_duplicated_as_issue(
    github_connector: GitHubConnector,
) -> None:
    discovered = await github_connector.discover(cursor=None)
    ids = [item.external_object_id for item in discovered]
    assert ids.count("github:acme/demo:pull_request:7") == 1
    assert "github:acme/demo:issue:7" not in ids
```

- [ ] **Step 2: Run connector tests to verify they fail**

Run: `.venv/bin/pytest tests/contract/capture/test_github_connector_contract.py tests/integration/github -v`
Expected: FAIL because `GitHubConnector` is missing.

- [ ] **Step 3: Implement discovery and normalization**

Use `/repos/{owner}/{repo}/issues`, `/pulls`, `/commits`, issue comments, and review comments. Filter pull-request-shaped records from the issues result. Construct IDs as `github:<owner>/<repo>:<kind>:<provider-id>`. Use `updated_at` as the version for mutable objects and commit SHA for commits. Calculate the content hash from canonical JSON containing only normalized semantic fields.

Payloads must include `kind`, title/subject, body/message, state, labels, milestone, base/head refs where applicable, merge commit SHA, linked issue/PR number, and selected repository metadata. Store no authentication headers.

```python
class GitHubConnector:
    connector_id = "github"

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        checkpoint = GitHubCheckpoint.decode(cursor)
        pages = []
        for endpoint in self._endpoints:
            pages.append(
                await self._discover_endpoint(endpoint, checkpoint.etags.get(endpoint))
            )
        return tuple(sorted(chain.from_iterable(pages), key=lambda item: item.external_object_id))

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        return await self._fetch_by_stable_id(object_id, version)

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        semantic = normalize_github_payload(raw.payload)
        return EvidenceRecord.from_semantic_payload(
            connector_type="github",
            external_object_id=raw.external_object_id,
            external_version=raw.external_version,
            source_locator=raw.locator,
            author=raw.author,
            observed_at=raw.observed_at,
            payload=semantic,
        )
```

- [ ] **Step 4: Integrate checkpoints and failure isolation**

Define a checkpoint value containing the newest observed `updated_at`, newest commit SHA, and ETags by endpoint. Serialize it as canonical JSON. Advance it only through `SyncOrchestrator` after all discovered evidence for this connector has been durably stored. Verify a rate-limit error yields a partial sync and leaves the prior checkpoint bytes unchanged.

```python
class GitHubCheckpoint(BaseModel, frozen=True):
    newest_updated_at: datetime | None = None
    newest_commit_sha: str | None = None
    etags: dict[str, str] = Field(default_factory=dict)

    def encode(self) -> str:
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def decode(cls, value: str | None) -> "GitHubCheckpoint":
        return cls() if value is None else cls.model_validate_json(value)
```

`tests/integration/github/conftest.py` defines the fake endpoint payloads, `github_connector`, and `GitHubSyncHarness.run()`; the harness enters `httpx.AsyncClient` and invokes the async core orchestrator through `anyio.run` for synchronous E2E assertions.

- [ ] **Step 5: Run contracts, integration tests, and commit**

Run: `.venv/bin/pytest tests/contract/capture/test_github_connector_contract.py tests/integration/github -v`
Expected: PASS.

```bash
git add src/intent_engineering/capture/github/connector.py tests/contract/capture/test_github_connector_contract.py tests/integration/github
git commit -m "feat: ingest github repository evidence"
```

### Task 4: Expose GitHub diagnostics and scheduled drift reporting

**Files:**
- Create: `src/intent_engineering/cli/github.py`
- Modify: `src/intent_engineering/cli/app.py`
- Create: `src/intent_engineering/render/drift_report.py`
- Create: `tests/e2e/test_cli_github.py`
- Create: `tests/unit/render/test_drift_report.py`
- Create: `.github/workflows/intent-sync.yml`

**Interfaces:**
- Consumes: GitHub auth/client/connector, sync service, case store.
- Produces: `intent doctor github`, `intent sync --sources github`, deterministic Markdown drift report, and manual/nightly Action.

- [ ] **Step 1: Write failing CLI and report tests**

```python
def test_github_doctor_reports_credential_source_without_token(cli: CliHarness) -> None:
    result = cli.run("doctor", "github", env={"GH_TOKEN": "secret-token"})
    assert result.exit_code == 0
    assert "environment" in result.stdout
    assert "secret-token" not in result.stdout


def test_drift_report_is_stably_sorted(
    case_fixture: Sequence[ReconciliationCase],
) -> None:
    report = render_drift_report(tuple(reversed(case_fixture)))
    assert report.index("CODE_LAG") < report.index("TEST_LAG")
    assert "Evidence" in report
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/e2e/test_cli_github.py tests/unit/render/test_drift_report.py -v`
Expected: FAIL because GitHub diagnostics and report rendering are missing.

- [ ] **Step 3: Implement CLI wiring and report rendering**

`doctor github` resolves credentials, calls a lightweight authenticated endpoint, reports repository access and rate-limit state, and redacts secrets. `sync --sources github` uses the existing source registry. The report groups open cases by case type, sorts by type and stable ID, and prints subject, evidence sides, affected refs, confidence, and recommended action.

```python
@doctor_app.command("github")
def doctor_github(
    project: Path = typer.Option(Path.cwd(), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    result = anyio.run(build_github_doctor(project).check)
    emit(result, output_format)


def render_drift_report(cases: Sequence[ReconciliationCase]) -> str:
    ordered = sorted(cases, key=lambda item: (item.case_type.value, item.id))
    return "\n\n".join(render_case_section(case) for case in ordered) + "\n"
```

- [ ] **Step 4: Add the GitHub Action workflow**

Create a workflow with `workflow_dispatch` and nightly `schedule`. It checks out full history, sets up Python 3.12, installs the package, runs `intent validate`, `intent sync --sources markdown,git,github`, and `intent drift --format markdown --output intent-drift.md`, then uploads the report artifact. Pass `${{ secrets.GITHUB_TOKEN }}` as `GH_TOKEN`. Ordinary open cases do not fail the job; invariant validation failures do.

```yaml
name: Intent sync
on:
  workflow_dispatch:
  schedule:
    - cron: "17 2 * * *"
jobs:
  drift:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: read
      pull-requests: read
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python -m pip install .
      - run: intent validate
      - run: intent sync --sources markdown,git,github
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      - run: intent drift --format markdown --output intent-drift.md
      - uses: actions/upload-artifact@v4
        with:
          name: intent-drift
          path: intent-drift.md
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/e2e/test_cli_github.py tests/unit/render/test_drift_report.py -v`
Run: `.venv/bin/intent doctor github --help`
Expected: PASS.

```bash
git add src/intent_engineering/cli src/intent_engineering/render/drift_report.py tests/e2e/test_cli_github.py tests/unit/render/test_drift_report.py .github/workflows/intent-sync.yml
git commit -m "feat: report github drift locally and in actions"
```

### Task 5: Document and verify the GitHub slice

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Create: `docs/github.md`
- Create: `tests/integration/github/test_no_secret_persistence.py`
- Modify: `tests/integration/github/conftest.py`

**Interfaces:**
- Consumes: completed GitHub adapter and CLI.
- Produces: an executable GitHub quick start and proof that credentials never persist.

- [ ] **Step 1: Add a failing secret-persistence test**

```python
def test_token_is_absent_from_project_files(github_sync_harness: GitHubSyncHarness) -> None:
    token = "ghp_test_secret_value"
    github_sync_harness.run(env={"GH_TOKEN": token})
    persisted = b"\n".join(
        path.read_bytes()
        for path in github_sync_harness.project.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    assert token.encode() not in persisted
```

- [ ] **Step 2: Run the test and fix every persistence path**

Run: `.venv/bin/pytest tests/integration/github/test_no_secret_persistence.py -v`
Expected before audit fixes: FAIL if any exception, log fixture, checkpoint, or evidence payload contains the token. Remove headers/bodies from serialized errors and configure structlog redaction until it passes.

- [ ] **Step 3: Write GitHub usage documentation**

Document `GH_TOKEN`, `gh auth login`, least required repository permissions, `intent doctor github`, project source configuration, manual sync, scheduled workflow, rate-limit behavior, report interpretation, and the absence of hosted OAuth/webhooks.

- [ ] **Step 4: Run the GitHub release gate**

Run: `.venv/bin/ruff check .`
Run: `.venv/bin/mypy src/intent_engineering`
Run: `.venv/bin/pytest`
Run with fake API: `.venv/bin/intent sync --sources github --format json`
Expected: all checks PASS, credentials are absent from persisted files, and the fake API sync is idempotent.

- [ ] **Step 5: Commit**

```bash
git add README.md CONTRIBUTING.md docs/github.md tests/integration/github/test_no_secret_persistence.py src/intent_engineering
git commit -m "docs: complete locally authenticated github workflow"
```

## GitHub Slice Completion Check

Before starting the MCP/write-back plan, verify:

```bash
.venv/bin/pytest tests/unit/capture/github tests/contract/capture/test_github_connector_contract.py tests/integration/github tests/e2e/test_cli_github.py
.venv/bin/intent doctor github --help
git status --short
```

Expected: tests pass, command help works, and the worktree is clean.
