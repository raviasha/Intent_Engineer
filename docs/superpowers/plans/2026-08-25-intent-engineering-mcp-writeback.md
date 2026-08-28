# Intent Engineering MCP and Guarded Write-Back Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Intent Engineering consume typed external MCP sources, preview and execute explicitly approved writes, and expose intent/context/reconciliation capabilities through its own MCP server.

**Architecture:** A shared MCP client runtime is isolated behind a small session port. Versioned provider profiles and local bindings map provider semantics to tools/resources using a constrained selector grammar. External writes use immutable WritePlans, interactive content-hash-bound ApprovalRecords, optimistic target-version checks, and immutable execution receipts; the Intent MCP server can request previews and execute separately approved plans but cannot create approvals.

**Tech Stack:** Python 3.12+, completed core and GitHub slices, official MCP Python SDK v2, Pydantic 2, Typer, PyYAML, pytest, anyio

**Spec:** `docs/superpowers/specs/2026-08-25-intent-engineering-public-alpha-design.md`

## Global Constraints

- Complete the core-engine and GitHub plans first.
- Support local MCP client sessions through stdio and Streamable HTTP using the official MCP Python SDK v2.
- Provider profiles are typed, versioned data; local bindings map semantic operations to server tool/resource names.
- Profile selectors and transforms must not execute arbitrary Python, shell, templates with code execution, or `eval`.
- Ship Slack, Notion, Jira, and Confluence reference profiles and fake-server contract suites.
- Users supply compatible MCP servers, credentials, and binding overrides; do not claim universal server compatibility.
- External writes always require an exact preview and interactive human approval.
- Approval is bound to plan ID, canonical plan hash, target version, actor, and expiry.
- Any target change invalidates approval and performs no mutation.
- No MCP tool exposed by Intent Engineering may create its own human approval.
- Restricted evidence is excluded unless the configured actor-to-provider mapping explicitly authorizes it.
- Secrets never enter `.intent/`, evidence, approvals, receipts, context packs, or logs.
- No hosted OAuth, webhooks, unattended external writes, or enterprise RBAC.

---

## File Structure

```text
src/intent_engineering/capture/mcp/profile_models.py   Typed profile schema
src/intent_engineering/capture/mcp/selectors.py        Constrained field extraction/transforms
src/intent_engineering/capture/mcp/profile_loader.py   YAML loading and validation
src/intent_engineering/capture/mcp/session.py          Provider-neutral MCP session port
src/intent_engineering/capture/mcp/runtime.py          Official SDK session adapters
src/intent_engineering/capture/mcp/connector.py        Read-side Connector implementation
src/intent_engineering/mutations/models.py             Plans, approvals, receipts
src/intent_engineering/mutations/planner.py            Exact operation preview construction
src/intent_engineering/mutations/approval.py           Interactive approval and persistence
src/intent_engineering/mutations/executor.py           Revalidation and write execution
src/intent_engineering/integrations/mcp_server/         Intent MCP server tools/resources/prompts
profiles/mcp/                                           Provider semantic profiles
tests/fakes/mcp_server.py                               Deterministic in-process fake session
tests/contract/mcp/                                     Profile and server contracts
tests/e2e/test_mcp_server.py                            Stdio server behavior
```

### Task 1: Define typed provider profiles and safe selectors

**Files:**
- Modify: `pyproject.toml`
- Create: `src/intent_engineering/capture/mcp/profile_models.py`
- Create: `src/intent_engineering/capture/mcp/selectors.py`
- Create: `src/intent_engineering/capture/mcp/profile_loader.py`
- Create: `schemas/mcp-provider-profile.schema.json`
- Create: `tests/unit/capture/mcp/test_profiles.py`
- Create: `tests/unit/capture/mcp/test_selectors.py`
- Create: `tests/unit/capture/mcp/conftest.py`

**Interfaces:**
- Consumes: YAML provider profile files.
- Produces: `ProviderProfile`, `ProviderBinding`, `load_profile()`, `select_value()`, and `apply_transform()`.

- [ ] **Step 1: Add MCP/runtime test dependencies**

Add `mcp>=2,<3` to project dependencies; `anyio>=4,<5` already comes from the core plan. Reinstall with `.venv/bin/pip install -e '.[dev]'` and record the resolved MCP SDK version in the lock/export artifact used by CI.

- [ ] **Step 2: Write failing profile-validation tests**

```python
def test_profile_rejects_write_without_version_precondition(tmp_path: Path) -> None:
    payload = valid_profile_payload()
    payload["writes"]["update_issue"]["before_version"] = None
    path = write_yaml(tmp_path, payload)
    with pytest.raises(ProfileValidationError, match="before_version"):
        load_profile(path)


def test_binding_requires_every_profile_capability(profile: ProviderProfile) -> None:
    binding = ProviderBinding(
        profile_id=profile.id,
        profile_version=profile.version,
        tools={},
        resources={},
        actor_principals={},
    )
    with pytest.raises(BindingValidationError, match="missing operation"):
        binding.validate_against(profile)
```

`tests/unit/capture/mcp/conftest.py` defines `write_yaml(path, payload) -> Path`, `valid_profile_payload() -> dict[str, JsonValue]`, and a complete `profile` fixture. `write_yaml` uses `yaml.safe_dump(sort_keys=True)`; the valid profile contains discover/fetch operations and required identity, version, author, time, locator, and content selectors.

- [ ] **Step 3: Run profile tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v`
Expected: FAIL because profile modules are missing.

- [ ] **Step 4: Implement exact profile models**

```python
class Selector(BaseModel, frozen=True):
    path: str
    transforms: Sequence[str] = ()
    required: bool = True


class ArgumentBinding(BaseModel, frozen=True):
    source: Literal[
        "constant",
        "scope",
        "cursor",
        "object_id",
        "object_version",
        "target_id",
        "before_version",
        "field",
    ]
    value: JsonValue | None = None
    field: str | None = None


class ReadOperation(BaseModel, frozen=True):
    semantic_name: str
    kind: Literal["tool", "resource"]
    pagination: Literal["none", "cursor", "page"]
    arguments: dict[str, ArgumentBinding] = Field(default_factory=dict)
    item_selector: Selector
    next_cursor_selector: Selector | None = None


class ObjectProfile(BaseModel, frozen=True):
    discover_operation: str
    fetch_operation: str
    external_id: Selector
    external_version: Selector
    author: Selector
    observed_at: Selector
    locator: Selector
    parent_ref: Selector | None = None
    acl: Selector | None = None
    content: dict[str, Selector]


class WriteOperationProfile(BaseModel, frozen=True):
    semantic_name: str
    target_id: Selector
    before_version: Selector
    allowed_fields: frozenset[str]
    arguments: dict[str, ArgumentBinding]
    input_schema: dict[str, JsonValue]
    result_version: Selector


class ProviderProfile(BaseModel, frozen=True):
    id: str
    version: str
    display_name: str
    operations: dict[str, ReadOperation]
    objects: dict[str, ObjectProfile]
    writes: dict[str, WriteOperationProfile]
    redacted_paths: frozenset[str] = frozenset()


class ProviderBinding(BaseModel, frozen=True):
    profile_id: str
    profile_version: str
    tools: dict[str, str]
    resources: dict[str, str]
    actor_principals: dict[str, frozenset[str]]
```

Generate `schemas/mcp-provider-profile.schema.json` from `ProviderProfile.model_json_schema()` in a deterministic schema-generation test and check the generated file into the repository.

- [ ] **Step 5: Implement the constrained selector grammar**

Support only root `$`, dot-delimited object keys, and integer list indexes such as `$.items.0.id`. Reject wildcards, recursive descent, function calls, brackets containing expressions, and keys beginning with double underscore. Allow transforms from this fixed registry: `string`, `integer`, `iso_datetime`, `string_list`, `canonical_json`, and `sha256`. Each transform is a pure function with JSON-compatible input/output.

```python
TRANSFORMS: dict[str, Callable[[JsonValue], JsonValue]] = {
    "string": to_string,
    "integer": to_integer,
    "iso_datetime": to_iso_datetime,
    "string_list": to_string_list,
    "canonical_json": to_canonical_json,
    "sha256": to_sha256,
}


def bind_arguments(
    bindings: Mapping[str, ArgumentBinding],
    context: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {}
    for name, binding in bindings.items():
        if binding.source == "constant":
            arguments[name] = binding.value
        elif binding.source == "field":
            if binding.field is None:
                raise ProfileValidationError(f"field source requires field for {name}")
            fields = cast(dict[str, JsonValue], context["fields"])
            arguments[name] = fields[binding.field]
        else:
            arguments[name] = context[binding.source]
    return arguments
```

- [ ] **Step 6: Verify and commit**

Run: `.venv/bin/pytest tests/unit/capture/mcp/test_profiles.py tests/unit/capture/mcp/test_selectors.py -v`
Run: `.venv/bin/ruff check src/intent_engineering/capture/mcp tests/unit/capture/mcp`
Expected: PASS.

```bash
git add pyproject.toml src/intent_engineering/capture/mcp schemas/mcp-provider-profile.schema.json tests/unit/capture/mcp
git commit -m "feat: define typed mcp provider profiles"
```

### Task 2: Build the shared MCP client runtime

**Files:**
- Create: `src/intent_engineering/capture/mcp/session.py`
- Create: `src/intent_engineering/capture/mcp/runtime.py`
- Create: `src/intent_engineering/capture/mcp/errors.py`
- Create: `tests/fakes/mcp_session.py`
- Create: `tests/unit/capture/mcp/test_runtime.py`
- Modify: `tests/unit/capture/mcp/conftest.py`

**Interfaces:**
- Consumes: MCP server configuration and the official SDK.
- Produces: async `McpSession` port, `McpRuntime.open()`, capability inspection, redacted typed errors.

- [ ] **Step 1: Write failing runtime capability tests**

```python
@pytest.mark.anyio
async def test_runtime_rejects_missing_bound_tool(fake_session: FakeMcpSession) -> None:
    fake_session.tools = {"search_messages"}
    runtime = McpRuntime(session_factory=lambda config: fake_session)
    binding = ProviderBinding(
        profile_id="slack",
        profile_version="1",
        tools={"discover_messages": "search_messages", "fetch_message": "get_message"},
        resources={},
        actor_principals={"local-user": frozenset({"U123"})},
    )
    with pytest.raises(McpCapabilityError, match="get_message"):
        await runtime.validate_binding(server_config(), binding)


@pytest.mark.anyio
async def test_runtime_redacts_transport_secrets(fake_session: FakeMcpSession) -> None:
    fake_session.fail_with = RuntimeError("Authorization: Bearer secret-token")
    runtime = McpRuntime(session_factory=lambda config: fake_session)
    with pytest.raises(McpTransportError) as captured:
        await runtime.call(server_config(), "search_messages", {})
    assert "secret-token" not in str(captured.value)
```

- [ ] **Step 2: Run runtime tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -v`
Expected: FAIL because runtime modules are missing.

- [ ] **Step 3: Define the provider-neutral async session port**

```python
class McpSession(Protocol):
    async def list_tools(self) -> frozenset[str]:
        raise NotImplementedError

    async def list_resources(self) -> frozenset[str]:
        raise NotImplementedError

    async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        raise NotImplementedError

    async def read_resource(self, uri: str) -> JsonValue:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
```

Define this configuration and validate that stdio has a command and no URL, while Streamable HTTP has a URL and no command. Environment values are references such as `env:SLACK_TOKEN`, not resolved secrets.

```python
class McpServerConfig(BaseModel, frozen=True):
    id: str
    transport: Literal["stdio", "streamable_http"]
    command: str | None = None
    args: Sequence[str] = ()
    url: AnyHttpUrl | None = None
    environment_refs: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> "McpServerConfig":
        if self.transport == "stdio" and (self.command is None or self.url is not None):
            raise ValueError("stdio requires command and forbids url")
        if self.transport == "streamable_http" and (self.url is None or self.command is not None):
            raise ValueError("streamable_http requires url and forbids command")
        return self


class McpConnectorConfig(BaseModel, frozen=True):
    id: str
    profile_path: Path
    server: McpServerConfig
    binding: ProviderBinding
    scope: dict[str, JsonValue] = Field(default_factory=dict)
```

- [ ] **Step 4: Implement official-SDK adapters and runtime behavior**

Wrap the SDK's stdio and supported HTTP client session factories behind `McpSession`. Resolve environment references only at process launch/request time. `McpRuntime` must open one session per operation group, apply an anyio timeout, validate required tools/resources, translate transport/protocol/schema failures to typed errors, and call `close()` in `finally`.

```python
class McpRuntime:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def call(
        self,
        config: McpServerConfig,
        tool_name: str,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        session = self._session_factory(config)
        try:
            with anyio.fail_after(config.timeout_seconds):
                return await session.call_tool(tool_name, arguments)
        except Exception as error:
            raise translate_mcp_error(error) from error
        finally:
            await session.close()

    async def validate_binding(
        self,
        config: McpServerConfig,
        binding: ProviderBinding,
    ) -> None:
        session = self._session_factory(config)
        try:
            tools = await session.list_tools()
            resources = await session.list_resources()
            binding.assert_capabilities(tools, resources)
        finally:
            await session.close()
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/unit/capture/mcp/test_runtime.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/capture/mcp/session.py src/intent_engineering/capture/mcp/runtime.py src/intent_engineering/capture/mcp/errors.py tests/fakes/mcp_session.py tests/unit/capture/mcp/test_runtime.py
git commit -m "feat: add shared mcp client runtime"
```

### Task 3: Ship Slack, Notion, Jira, and Confluence reference profiles

**Files:**
- Create: `profiles/mcp/slack.yaml`
- Create: `profiles/mcp/notion.yaml`
- Create: `profiles/mcp/jira.yaml`
- Create: `profiles/mcp/confluence.yaml`
- Create: `profiles/mcp/example-bindings/slack.yaml`
- Create: `profiles/mcp/example-bindings/notion.yaml`
- Create: `profiles/mcp/example-bindings/jira.yaml`
- Create: `profiles/mcp/example-bindings/confluence.yaml`
- Create: `tests/contract/mcp/test_reference_profiles.py`
- Create: `tests/fixtures/mcp/`

**Interfaces:**
- Consumes: `ProviderProfile` schema and fake provider payloads.
- Produces: four valid semantic profiles and illustrative local bindings.

- [ ] **Step 1: Write the failing reference-profile matrix**

```python
@pytest.mark.parametrize(
    ("provider", "read_objects", "write_operations"),
    [
        ("slack", {"message", "thread"}, {"post_message", "reply", "update_message"}),
        ("notion", {"page", "block"}, {"update_page", "append_blocks"}),
        ("jira", {"issue", "comment"}, {"update_issue", "add_comment"}),
        ("confluence", {"page", "comment"}, {"update_page", "add_comment"}),
    ],
)
def test_reference_profile_contract(
    provider: str,
    read_objects: set[str],
    write_operations: set[str],
) -> None:
    profile = load_profile(PROFILES / f"{provider}.yaml")
    assert set(profile.objects) == read_objects
    assert set(profile.writes) == write_operations
    validate_fixture_payloads(profile, FIXTURES / provider)
```

- [ ] **Step 2: Run the profile matrix to verify it fails**

Run: `.venv/bin/pytest tests/contract/mcp/test_reference_profiles.py -v`
Expected: FAIL because reference profiles and fixtures are absent.

- [ ] **Step 3: Add read mappings with provenance and ACL fields**

For every object type, provide discover/fetch bindings and selectors for stable external ID, source version, author, observed time, locator, parent/thread context, ACL principals where the source exposes them, and normalized content. Use fixture payloads with fixed provider IDs and timestamps. Ensure content hashes ignore transport envelopes and include semantic content.

The Slack message mapping demonstrates the exact YAML shape used by all profiles:

```yaml
id: slack
version: "1"
display_name: Slack
operations:
  discover_messages:
    semantic_name: discover_messages
    kind: tool
    pagination: cursor
    arguments:
      cursor: {source: cursor}
    item_selector: {path: "$.messages"}
    next_cursor_selector: {path: "$.next_cursor", required: false}
  fetch_message:
    semantic_name: fetch_message
    kind: tool
    pagination: none
    arguments:
      message_id: {source: object_id}
    item_selector: {path: "$"}
objects:
  message:
    discover_operation: discover_messages
    fetch_operation: fetch_message
    external_id: {path: "$.id", transforms: [string]}
    external_version: {path: "$.updated", transforms: [string]}
    author: {path: "$.user", transforms: [string]}
    observed_at: {path: "$.updated", transforms: [iso_datetime]}
    locator: {path: "$.permalink", transforms: [string]}
    parent_ref: {path: "$.thread_ts", transforms: [string], required: false}
    acl: {path: "$.allowed_principals", transforms: [string_list], required: false}
    content:
      channel: {path: "$.channel", transforms: [string]}
      text: {path: "$.text", transforms: [string]}
```

- [ ] **Step 4: Add guarded write mappings**

Every write operation must specify its exact target selector, before-version selector, allowlisted fields, JSON input schema, and result-version selector. Slack updates require message/channel timestamp identity; Notion and Confluence updates require page/block version; Jira updates require issue identity plus updated/version field. Comment/reply creation uses the parent object's version as the optimistic precondition.

```yaml
writes:
  update_message:
    semantic_name: update_message
    target_id: {path: "$.id", transforms: [string]}
    before_version: {path: "$.updated", transforms: [string]}
    allowed_fields: [text]
    arguments:
      channel: {source: field, field: channel}
      message_id: {source: target_id}
      text: {source: field, field: text}
      expected_version: {source: before_version}
    input_schema:
      type: object
      required: [channel, message_id, text]
      additionalProperties: false
      properties:
        channel: {type: string}
        message_id: {type: string}
        text: {type: string}
    result_version: {path: "$.updated", transforms: [string]}
```

- [ ] **Step 5: Add illustrative bindings and verify profiles**

Example bindings use semantic names and clearly state that users must map them to the actual tool names offered by their chosen server. They contain environment references only. Run:

```yaml
id: slack-local
profile_path: profiles/mcp/slack.yaml
server:
  id: slack-server
  transport: stdio
  command: slack-mcp-server
  args: []
  environment_refs:
    SLACK_TOKEN: env:SLACK_TOKEN
binding:
  profile_id: slack
  profile_version: "1"
  tools:
    discover_messages: search_messages
    fetch_message: get_message
    update_message: update_message
  resources: {}
  actor_principals:
    local-user: [U123]
scope:
  workspace_id: workspace-1
```

Run: `.venv/bin/pytest tests/contract/mcp/test_reference_profiles.py -v`
Expected: PASS for all four providers.

- [ ] **Step 6: Commit**

```bash
git add profiles/mcp tests/contract/mcp/test_reference_profiles.py tests/fixtures/mcp
git commit -m "feat: add typed collaboration source profiles"
```

### Task 4: Implement the read-side MCP Connector

**Files:**
- Create: `src/intent_engineering/capture/mcp/connector.py`
- Create: `src/intent_engineering/capture/mcp/authorization.py`
- Create: `tests/contract/capture/test_mcp_connector_contract.py`
- Create: `tests/integration/mcp/test_read_sync.py`
- Create: `tests/integration/mcp/test_acl_filtering.py`

**Interfaces:**
- Consumes: runtime, provider profile, binding, local actor, core Connector contract.
- Produces: `McpConnector` and conservative ACL decisions.

- [ ] **Step 1: Write failing read and ACL tests**

```python
@pytest.mark.anyio
async def test_mcp_connector_normalizes_versioned_evidence(mcp_connector: McpConnector) -> None:
    records = await collect_records(mcp_connector)
    first = records[0]
    assert first.external_object_id == "slack:workspace-1:channel-1:1700000000.000100"
    assert first.external_version == "1700000000.000200"
    assert first.author == "U123"
    assert first.content_hash.startswith("sha256:")


def test_unresolved_restricted_acl_is_denied() -> None:
    decision = authorize(
        local_actor="local-user",
        actor_principals={},
        evidence_acl=frozenset({"team-private"}),
    )
    assert decision is AuthorizationDecision.DENY
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/contract/capture/test_mcp_connector_contract.py tests/integration/mcp/test_read_sync.py tests/integration/mcp/test_acl_filtering.py -v`
Expected: FAIL because the MCP connector is missing.

- [ ] **Step 3: Implement discovery, fetch, pagination, and normalization**

`McpConnector` adapts async MCP calls to the application's async connector path. It validates the binding once per run, invokes the configured discover operation until its cursor is empty, fetches current source versions, applies profile selectors/transforms, builds version-addressed EvidenceRecords, and returns a canonical checkpoint containing provider cursor plus observed object versions.

Use the same idempotency identity as other connectors: connector ID, external object ID, source version, and content hash.

```python
class McpConnector:
    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        await self.runtime.validate_binding(self.server, self.binding)
        operation = self.profile.operations[self.object_profile.discover_operation]
        return await self._discover_all_pages(operation, cursor)

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        operation_name = self.binding.tools[self.object_profile.fetch_operation]
        payload = await self.runtime.call(
            self.server,
            operation_name,
            bind_arguments(
                self.profile.operations[self.object_profile.fetch_operation].arguments,
                {
                    "object_id": object_id,
                    "object_version": version,
                    "fields": {},
                },
            ),
        )
        return RawSourceObject.from_mcp(payload)

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        return normalize_with_profile(self.profile, self.object_profile, raw)
```

- [ ] **Step 4: Enforce conservative local authorization**

`authorize(local_actor, actor_principals, evidence_acl)` returns ALLOW when evidence is public or any mapped provider principal intersects the ACL. It returns DENY for non-empty ACLs with no resolved mapping. Denied evidence may be persisted in a restricted store only when configured, but it must never appear in context, explain output, reports, or MCP resources for the unauthorized actor.

```python
def authorize(
    local_actor: str,
    actor_principals: Mapping[str, frozenset[str]],
    evidence_acl: frozenset[str],
) -> AuthorizationDecision:
    if not evidence_acl:
        return AuthorizationDecision.ALLOW
    principals = actor_principals.get(local_actor)
    if principals is None:
        return AuthorizationDecision.DENY
    return (
        AuthorizationDecision.ALLOW
        if principals.intersection(evidence_acl)
        else AuthorizationDecision.DENY
    )
```

- [ ] **Step 5: Verify failure/checkpoint isolation and commit**

Add a fake-server timeout test proving a partial sync keeps the prior MCP checkpoint. Run:

Run: `.venv/bin/pytest tests/contract/capture/test_mcp_connector_contract.py tests/integration/mcp -v`
Expected: PASS.

```bash
git add src/intent_engineering/capture/mcp/connector.py src/intent_engineering/capture/mcp/authorization.py tests/contract/capture/test_mcp_connector_contract.py tests/integration/mcp
git commit -m "feat: ingest authorized evidence through mcp"
```

### Task 5: Model write plans, approvals, and receipts

**Files:**
- Create: `src/intent_engineering/mutations/models.py`
- Create: `src/intent_engineering/mutations/planner.py`
- Create: `src/intent_engineering/mutations/approval.py`
- Create: `src/intent_engineering/storage/jsonl/approval_store.py`
- Create: `tests/unit/mutations/test_planner.py`
- Create: `tests/unit/mutations/test_approval.py`

**Interfaces:**
- Consumes: reconciliation resolution, provider profile/binding, current remote representation.
- Produces: `WritePlan.canonical_hash`, `build_write_plan()`, `ApprovalRecord`, and `ApprovalStore`.

- [ ] **Step 1: Write failing plan-hash and approval tests**

```python
def test_plan_hash_changes_when_target_or_payload_changes(base_plan: WritePlan) -> None:
    target_changed = base_plan.model_copy(update={"target_id": "ISSUE-2"})
    payload_changed = base_plan.model_copy(update={"arguments": {"summary": "Different"}})
    assert base_plan.canonical_hash != target_changed.canonical_hash
    assert base_plan.canonical_hash != payload_changed.canonical_hash


def test_approval_is_bound_to_plan_hash_and_target_version(base_plan: WritePlan) -> None:
    approval = approve_plan(base_plan, actor="local-user", now=FIXED_NOW, expires_in=timedelta(minutes=15))
    assert approval.plan_hash == base_plan.canonical_hash
    assert approval.target_version == base_plan.before_version
    assert approval.expires_at == FIXED_NOW + timedelta(minutes=15)
```

- [ ] **Step 2: Run mutation tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/mutations/test_planner.py tests/unit/mutations/test_approval.py -v`
Expected: FAIL because mutation modules are missing.

- [ ] **Step 3: Implement immutable mutation records**

```python
class WritePlan(BaseModel, frozen=True):
    id: str
    case_id: str
    connector_id: str
    profile_id: str
    profile_version: str
    operation: str
    target_id: str
    before_version: str
    before: dict[str, JsonValue]
    after: dict[str, JsonValue]
    arguments: dict[str, JsonValue]
    evidence_refs: Sequence[str]
    created_by: str
    created_at: datetime
    expires_at: datetime

    @property
    def canonical_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"id"})
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ApprovalRecord(BaseModel, frozen=True):
    id: str
    plan_id: str
    plan_hash: str
    target_version: str
    actor: str
    approved_at: datetime
    expires_at: datetime


class ExecutionReceipt(BaseModel, frozen=True):
    id: str
    plan_id: str
    approval_id: str
    status: Literal["succeeded", "rejected", "failed"]
    attempted_at: datetime
    completed_at: datetime
    resulting_version: str | None
    evidence_ref: str
    redacted_error: str | None = None


class RemoteObject(BaseModel, frozen=True):
    connector_id: str
    profile_id: str
    profile_version: str
    id: str
    version: str
    content: dict[str, JsonValue]


class WriteResult(BaseModel, frozen=True):
    resulting_version: str
    redacted_result: dict[str, JsonValue]
```

- [ ] **Step 4: Implement planning and interactive approval persistence**

`build_write_plan()` validates allowed fields and input schema, uses current remote content to create exact before/after views, and sets a 15-minute default expiry. `approve_plan()` requires the caller to have already completed an interactive confirmation; it is not exported through MCP. `ApprovalStore` is append-only JSONL and rejects duplicate IDs with differing bytes.

```python
def build_write_plan(
    case: ReconciliationCase,
    operation: WriteOperationProfile,
    current: RemoteObject,
    requested_fields: dict[str, JsonValue],
    actor: str,
    now: datetime,
) -> WritePlan:
    disallowed = set(requested_fields).difference(operation.allowed_fields)
    if disallowed:
        raise DisallowedWriteFields(sorted(disallowed))
    after = current.content | requested_fields
    arguments = bind_arguments(
        operation.arguments,
        {
            "target_id": current.id,
            "before_version": current.version,
            "fields": after,
        },
    )
    jsonschema.validate(arguments, operation.input_schema)
    return WritePlan(
        id=new_plan_id(case.id, operation.semantic_name, now),
        case_id=case.id,
        connector_id=current.connector_id,
        profile_id=current.profile_id,
        profile_version=current.profile_version,
        operation=operation.semantic_name,
        target_id=current.id,
        before_version=current.version,
        before=current.content,
        after=after,
        arguments=arguments,
        evidence_refs=case.all_evidence_refs,
        created_by=actor,
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/unit/mutations -v`
Expected: PASS.

```bash
git add src/intent_engineering/mutations src/intent_engineering/storage/jsonl/approval_store.py tests/unit/mutations
git commit -m "feat: create hash-bound external write plans"
```

### Task 6: Execute approved writes with optimistic revalidation

**Files:**
- Create: `src/intent_engineering/mutations/executor.py`
- Create: `src/intent_engineering/storage/jsonl/receipt_store.py`
- Create: `tests/integration/mcp/test_write_execution.py`
- Create: `tests/integration/mcp/test_write_conflict.py`
- Create: `tests/integration/mcp/test_write_permission_denied.py`

**Interfaces:**
- Consumes: plan, approval, profile/binding, runtime, stores, current remote object.
- Produces: `WriteExecutor.execute() -> ExecutionReceipt` and evidence-backed reconciliation advancement.

Define the adapter port before the executor:

```python
class ExternalMutationGateway(Protocol):
    async def fetch_target(self, plan: WritePlan) -> RemoteObject:
        raise NotImplementedError

    async def execute(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
    ) -> WriteResult:
        raise NotImplementedError
```

- [ ] **Step 1: Write failing happy-path and changed-target tests**

```python
@pytest.mark.anyio
async def test_approved_unchanged_plan_executes(write_harness: WriteHarness) -> None:
    receipt = await write_harness.execute()
    assert receipt.status == "succeeded"
    assert receipt.resulting_version == "v2"
    assert write_harness.session.calls[-1].name == "update_issue"
    assert write_harness.case_store.get("case-1").status is ReconciliationStatus.RESOLVED


@pytest.mark.anyio
async def test_target_change_invalidates_approval(write_harness: WriteHarness) -> None:
    write_harness.remote_version = "v1-changed"
    receipt = await write_harness.execute()
    assert receipt.status == "rejected"
    assert write_harness.session.write_calls == []
    assert write_harness.case_store.get("case-1").status is not ReconciliationStatus.RESOLVED
```

- [ ] **Step 2: Run write tests to verify they fail**

Run: `.venv/bin/pytest tests/integration/mcp/test_write_execution.py tests/integration/mcp/test_write_conflict.py -v`
Expected: FAIL because execution is missing.

- [ ] **Step 3: Implement the exact execution sequence**

`WriteExecutor.execute(plan_id, approval_id, actor, now)` must load immutable records, require matching plan ID/hash/target version/actor, require unexpired plan and approval, refetch and normalize the remote target, compare current version to `before_version`, validate operation and arguments again, call the bound write tool, validate the result version, persist a redacted receipt/evidence record, then resolve the case via ChangeSet. Any precondition failure persists a rejected receipt and performs no write.

```python
async def execute(
    self,
    plan_id: str,
    approval_id: str,
    actor: str,
    now: datetime,
) -> ExecutionReceipt:
    plan = self.plans.get(plan_id)
    approval = self.approvals.get(approval_id)
    validate_approval(plan, approval, actor, now)
    current = await self.gateway.fetch_target(plan)
    if current.version != plan.before_version:
        return self._reject(plan, approval, now, "target_version_changed")
    result = await self.gateway.execute(plan.operation, plan.arguments)
    receipt = self._succeeded(plan, approval, result, now)
    self.receipts.put(receipt)
    self._resolve_case_with_changeset(plan, receipt, actor, now)
    return receipt
```

- [ ] **Step 4: Handle provider failure and permission denial**

Transport, schema, and permission failures persist a failed receipt with a stable redacted error code. They do not retry mutation calls automatically, do not resolve the case, and do not advance a read checkpoint. Ensure fake session call records exclude credential values.

```python
try:
    result = await self.gateway.execute(plan.operation, plan.arguments)
except McpPermissionError:
    return self._fail(plan, approval, now, "permission_denied")
except (McpTransportError, McpSchemaError):
    return self._fail(plan, approval, now, "provider_failure")
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/integration/mcp/test_write_execution.py tests/integration/mcp/test_write_conflict.py tests/integration/mcp/test_write_permission_denied.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/mutations/executor.py src/intent_engineering/storage/jsonl/receipt_store.py tests/integration/mcp/test_write_execution.py tests/integration/mcp/test_write_conflict.py tests/integration/mcp/test_write_permission_denied.py
git commit -m "feat: execute explicitly approved mcp writes"
```

### Task 7: Add connector and write CLI workflows

**Files:**
- Create: `src/intent_engineering/cli/connectors.py`
- Create: `src/intent_engineering/cli/writes.py`
- Modify: `src/intent_engineering/cli/app.py`
- Create: `tests/e2e/test_cli_connectors.py`
- Create: `tests/e2e/test_cli_write_approval.py`

**Interfaces:**
- Consumes: profile loader, runtime, planner, approval store, executor.
- Produces: `connectors list|inspect|test` and `write preview|approve|execute` commands.

- [ ] **Step 1: Write failing connector and approval CLI tests**

```python
def test_connector_test_reports_capabilities(cli: CliHarness) -> None:
    result = cli.run("connectors", "test", "slack-local", "--format", "json")
    assert result.exit_code == 0
    assert result.json()["profile"] == "slack"
    assert result.json()["read_ready"] is True
    assert result.json()["write_ready"] is True


def test_approval_requires_interactive_exact_confirmation(cli: CliHarness) -> None:
    rejected = cli.run("write", "approve", "plan-1", input_text="no\n")
    assert rejected.exit_code == 4
    accepted = cli.run("write", "approve", "plan-1", input_text="approve plan-1\n")
    assert accepted.exit_code == 0
    assert accepted.json()["plan_hash"].startswith("sha256:")
```

- [ ] **Step 2: Run CLI tests to verify they fail**

Run: `.venv/bin/pytest tests/e2e/test_cli_connectors.py tests/e2e/test_cli_write_approval.py -v`
Expected: FAIL because command groups are missing.

- [ ] **Step 3: Implement connector diagnostics**

`list` shows configured connector ID, profile/version, transport, and enabled state. `inspect` renders required semantic operations and local tool/resource bindings without environment values. `test` opens the session, validates capabilities, performs schema-safe non-mutating probe reads, and reports read/write readiness without executing writes.

```python
@connectors_app.command("test")
def test_connector(
    connector_id: str,
    project: Path = typer.Option(Path.cwd(), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    result = anyio.run(load_connector_manager(project).test, connector_id)
    emit(result, output_format)
```

- [ ] **Step 4: Implement preview, approval, and execution commands**

`preview` prints exact target, before/after, arguments, evidence, precondition, expiry, and plan hash. `approve` refuses non-TTY input unless the test harness injects a terminal abstraction, displays the full preview again, and requires exact text `approve <plan-id>`. `execute` never implies approval; it requires an existing valid ApprovalRecord and prints the receipt.

```python
@write_app.command("approve")
def approve_write(plan_id: str, project: Path = typer.Option(Path.cwd(), "--project")) -> None:
    runtime = load_runtime(project)
    plan = runtime.plan_store.get(plan_id)
    typer.echo(render_write_preview(plan))
    confirmation = typer.prompt(f"Type 'approve {plan_id}' to approve")
    if confirmation != f"approve {plan_id}":
        raise typer.Exit(4)
    approval = approve_plan(plan, runtime.config.local_actor, utc_now(), timedelta(minutes=15))
    runtime.approval_store.put(approval)
    emit(approval, OutputFormat.JSON)
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/e2e/test_cli_connectors.py tests/e2e/test_cli_write_approval.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/cli tests/e2e/test_cli_connectors.py tests/e2e/test_cli_write_approval.py
git commit -m "feat: manage mcp connectors and writes from CLI"
```

### Task 8: Expose read-only intent capabilities through an MCP server

**Files:**
- Create: `src/intent_engineering/integrations/mcp_server/server.py`
- Create: `src/intent_engineering/integrations/mcp_server/tools.py`
- Create: `src/intent_engineering/integrations/mcp_server/resources.py`
- Create: `src/intent_engineering/integrations/mcp_server/prompts.py`
- Create: `tests/contract/mcp/test_intent_server_reads.py`
- Create: `tests/e2e/test_mcp_server.py`

**Interfaces:**
- Consumes: context, graph, evidence, drift, case, and validation services.
- Produces: `intent mcp` stdio server with versioned read tools/resources/prompts.

- [ ] **Step 1: Write failing MCP read-contract tests**

```python
@pytest.mark.anyio
async def test_context_tool_returns_versioned_pack(intent_mcp_client: Client) -> None:
    result = await intent_mcp_client.call_tool(
        "intent_context",
        {"task": "add local export", "format": "json"},
    )
    payload = decode_tool_json(result)
    assert payload["schema_version"] == "1"
    assert payload["task"] == "add local export"
    assert payload["relevant_requirements"][0]["id"] == "req-local-export"


@pytest.mark.anyio
async def test_restricted_evidence_resource_is_not_returned(
    unauthorized_mcp_client: Client,
) -> None:
    with pytest.raises(McpError, match="not found"):
        await unauthorized_mcp_client.read_resource("intent://evidence/private-1")
```

- [ ] **Step 2: Run server tests to verify they fail**

Run: `.venv/bin/pytest tests/contract/mcp/test_intent_server_reads.py tests/e2e/test_mcp_server.py -v`
Expected: FAIL because the Intent MCP server is missing.

- [ ] **Step 3: Implement read tools and resources**

Register tools `intent_context`, `intent_explain`, `intent_impact`, `intent_drift`, `intent_status`, `intent_validate`, `intent_reconcile_list`, and `intent_reconcile_show`. Register resources for graph nodes, evidence chains, schemas, case packets, and generated reports. Every payload includes `schema_version: "1"`, stable IDs, evidence refs, and confidence where relevant. Resolve the configured local actor on every request and apply ACL filtering before serialization.

```python
def register_read_tools(server: MCPServer, services: McpServices) -> None:
    @server.tool(name="intent_context")
    async def intent_context(task: str, format: Literal["json", "markdown"] = "json") -> str:
        pack = services.context.for_task(task, actor=services.local_actor)
        return serialize_context(pack, format)

    @server.resource("intent://evidence/{evidence_id}")
    async def evidence_resource(evidence_id: str) -> str:
        record = services.authorized_evidence.get(evidence_id, services.local_actor)
        return record.model_dump_json()
```

- [ ] **Step 4: Add prompt templates and CLI server entry point**

Register `prepare_task` and `review_reconciliation` prompts that request the corresponding tools rather than embedding graph dumps. `intent mcp` launches stdio by default, reports startup diagnostics only to stderr, and keeps stdout protocol-clean.

```python
@server.prompt(name="prepare_task")
def prepare_task(task: str) -> str:
    return f"Call intent_context for this task, then follow its constraints and warnings: {task}"


def run_stdio(project: Path) -> None:
    build_server(load_mcp_services(project)).run(transport="stdio")
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/contract/mcp/test_intent_server_reads.py tests/e2e/test_mcp_server.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/integrations/mcp_server tests/contract/mcp/test_intent_server_reads.py tests/e2e/test_mcp_server.py src/intent_engineering/cli/app.py
git commit -m "feat: serve intent context over mcp"
```

### Task 9: Add proposal and guarded execution MCP tools

**Files:**
- Create: `src/intent_engineering/integrations/mcp_server/mutations.py`
- Modify: `src/intent_engineering/integrations/mcp_server/server.py`
- Create: `tests/contract/mcp/test_intent_server_mutations.py`
- Create: `tests/e2e/test_mcp_write_guard.py`

**Interfaces:**
- Consumes: ChangeSet proposal service, reconciliation planner, WriteExecutor.
- Produces: proposal/preview/execution tools with no approval-creation capability.

- [ ] **Step 1: Write failing mutation-surface tests**

```python
@pytest.mark.anyio
async def test_server_has_no_approval_creation_tool(intent_mcp_client: Client) -> None:
    tools = await intent_mcp_client.list_tools()
    names = {item.name for item in tools.tools}
    assert "intent_write_approve" not in names
    assert "intent_approve" not in names


@pytest.mark.anyio
async def test_execute_rejects_plan_without_local_approval(
    intent_mcp_client: Client,
) -> None:
    result = await intent_mcp_client.call_tool(
        "intent_write_execute",
        {"plan_id": "plan-1", "approval_id": "missing"},
    )
    payload = decode_tool_json(result)
    assert payload["status"] == "rejected"
    assert payload["reason"] == "approval_not_found"
```

- [ ] **Step 2: Run guard tests to verify they fail**

Run: `.venv/bin/pytest tests/contract/mcp/test_intent_server_mutations.py tests/e2e/test_mcp_write_guard.py -v`
Expected: FAIL because mutation tools are absent.

- [ ] **Step 3: Add proposal and preview tools**

Register `intent_changeset_propose`, `intent_reconciliation_propose`, and `intent_write_preview`. These tools validate structured inputs and persist proposals/previews with actor attribution, but they do not apply semantic changes or create ApprovalRecords.

```python
@server.tool(name="intent_write_preview")
async def intent_write_preview(case_id: str, action: str, fields: dict[str, JsonValue]) -> str:
    plan = await services.planner.for_case(case_id, action, fields, services.local_actor)
    services.plan_store.put(plan)
    return plan.model_dump_json()
```

- [ ] **Step 4: Add guarded execution only**

Register `intent_write_execute(plan_id, approval_id)`. It calls the same `WriteExecutor` as the CLI and returns a redacted receipt. Verify the named ApprovalRecord exists, was created by the local interactive approval service, matches the configured actor, and remains valid. Do not register any tool or prompt that calls `approve_plan()`.

```python
@server.tool(name="intent_write_execute")
async def intent_write_execute(plan_id: str, approval_id: str) -> str:
    receipt = await services.write_executor.execute(
        plan_id,
        approval_id,
        services.local_actor,
        utc_now(),
    )
    return receipt.model_dump_json()
```

- [ ] **Step 5: Verify and commit**

Run: `.venv/bin/pytest tests/contract/mcp/test_intent_server_mutations.py tests/e2e/test_mcp_write_guard.py -v`
Expected: PASS.

```bash
git add src/intent_engineering/integrations/mcp_server tests/contract/mcp/test_intent_server_mutations.py tests/e2e/test_mcp_write_guard.py
git commit -m "feat: guard external writes exposed through mcp"
```

### Task 10: Complete MCP documentation and the public-alpha release gate

**Files:**
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Create: `docs/mcp.md`
- Create: `docs/provider-profiles.md`
- Create: `examples/mcp-bindings/`
- Create: `tests/e2e/test_public_alpha.py`
- Create: `tests/e2e/public_alpha_harness.py`

**Interfaces:**
- Consumes: all three completed implementation slices.
- Produces: executable setup documentation and end-to-end proof of the approved public alpha.

- [ ] **Step 1: Write the failing public-alpha smoke test**

```python
def test_public_alpha_smoke(public_alpha: PublicAlphaHarness) -> None:
    assert public_alpha.init().returncode == 0
    assert public_alpha.validate()["valid"] is True
    assert public_alpha.sync_twice().second["changes_applied"] == 0
    assert public_alpha.case_types() >= {"CODE_LAG", "REQUIREMENT_LAG"}
    assert public_alpha.github_evidence_count() > 0
    assert public_alpha.mcp_context("local export")["schema_version"] == "1"
    preview = public_alpha.external_write_preview("case-jira-1")
    assert preview["plan_hash"].startswith("sha256:")
    assert public_alpha.execute_without_approval(preview["id"])["status"] == "rejected"
    approval = public_alpha.interactive_approve(preview["id"])
    assert public_alpha.execute(preview["id"], approval["id"])["status"] == "succeeded"
```

- [ ] **Step 2: Run the smoke test to identify missing integration wiring**

Run: `.venv/bin/pytest tests/e2e/test_public_alpha.py -v`
Expected on first run: FAIL at the first unconnected slice boundary. Connect only existing public services through runtime factories until every assertion passes; do not duplicate domain logic in the harness or CLI.

- [ ] **Step 3: Document MCP server and external adapter setup**

README adds a concise agent quick start. `docs/mcp.md` documents launching the Intent MCP server, tool/resource contracts, local actor selection, and approval separation. `docs/provider-profiles.md` documents the profile schema, constrained selectors/transforms, binding validation, stdio/HTTP server configuration, environment references, ACL behavior, and compatibility limits. Examples include one binding for each reference profile with non-secret environment references.

- [ ] **Step 4: Run the full release gate**

Run: `.venv/bin/ruff check .`
Run: `.venv/bin/mypy src/intent_engineering`
Run: `.venv/bin/pytest --cov=intent_engineering --cov-report=term-missing`
Run: `.venv/bin/intent validate --project .`
Run: `.venv/bin/intent connectors test --all --transport fake`
Run: `.venv/bin/intent mcp --help`
Expected: all commands PASS; tests require no live external credentials; changed-target write test performs no mutation.

- [ ] **Step 5: Audit persisted files for secrets and canonical-state mutation**

Run the E2E suite with sentinel tokens for GitHub and every MCP provider. Search the temporary project tree and captured logs for each sentinel. Render all generated views and compare canonical graph bytes before/after. Expected: no sentinel is present, and canonical graph bytes change only when a validated ChangeSet is applied.

```bash
pytest tests/e2e/test_public_alpha.py -v
! rg -n 'ghp_test_secret|slack_test_secret|notion_test_secret|jira_test_secret|confluence_test_secret' .intent test-artifacts
```

`tests/e2e/public_alpha_harness.py` defines every method used by `PublicAlphaHarness` in Step 1. It composes production runtime factories with the fake GitHub API and fake MCP sessions; only `interactive_approve()` substitutes the terminal abstraction, and it still goes through the production approval service.

- [ ] **Step 6: Commit**

```bash
git add README.md CONTRIBUTING.md docs/mcp.md docs/provider-profiles.md examples/mcp-bindings tests/e2e/test_public_alpha.py src/intent_engineering
git commit -m "docs: complete the intent engineering public alpha"
```

## Public Alpha Completion Check

Run from a clean worktree:

```bash
.venv/bin/ruff check .
.venv/bin/mypy src/intent_engineering
.venv/bin/pytest
.venv/bin/intent validate --project .
.venv/bin/intent --help
.venv/bin/intent mcp --help
git status --short
```

Expected: every command succeeds, the worktree is clean, no live model or cloud account is required, GitHub uses local credentials, external MCP profiles pass fake-server contracts, and no external write succeeds without an unchanged preview plus interactive human approval.
