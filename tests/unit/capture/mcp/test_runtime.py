"""Behavioral contracts for the provider-neutral MCP runtime."""

from __future__ import annotations

import asyncio
import math
import sys
import time
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import MappingProxyType, SimpleNamespace
from typing import Any, ClassVar, cast

import anyio
import httpx2
import pytest
from pydantic import ValidationError

from intent_engineering.capture.mcp.errors import (
    McpCapabilityError,
    McpClosedSessionError,
    McpPermissionError,
    McpProtocolError,
    McpSchemaError,
    McpTimeoutError,
    McpTransportError,
)
from intent_engineering.capture.mcp.profile_models import ProviderBinding
from intent_engineering.capture.mcp.runtime import (
    McpRuntime,
    SessionLease,
    create_production_session,
)
from intent_engineering.capture.mcp.session import McpConnectorConfig, McpServerConfig
from intent_engineering.core.models import JsonValue
from tests.fakes.mcp_session import FakeMcpSession


def stdio_config(**changes: object) -> McpServerConfig:
    payload: dict[str, object] = {
        "id": "local-messages",
        "transport": "stdio",
        "command": "message-mcp",
        "args": ["--readonly"],
        "environment_refs": {"TOKEN": "env:MCP_TEST_TOKEN"},
    }
    payload.update(changes)
    return McpServerConfig(**payload)


def http_config(**changes: object) -> McpServerConfig:
    payload: dict[str, object] = {
        "id": "remote-messages",
        "transport": "streamable_http",
        "url": "https://mcp.example.test/v1",
        "headers": {"Authorization": "env:MCP_TEST_TOKEN"},
    }
    payload.update(changes)
    return McpServerConfig(**payload)


def binding() -> ProviderBinding:
    return ProviderBinding(
        profile_id="messages",
        profile_version="1",
        tools={"discover": "search_messages", "fetch": "get_message"},
        resources={"record": "resource://message"},
        actor_principals={},
    )


def test_public_configs_are_strict_frozen_and_detached() -> None:
    """Catches config mutation, ignored fields, and caller mapping aliasing."""
    refs = {"TOKEN": "env:MCP_TEST_TOKEN"}
    config = stdio_config(environment_refs=refs)
    refs["TOKEN"] = "env:OTHER"

    assert isinstance(config.environment_refs, MappingProxyType)
    assert config.environment_refs["TOKEN"] == "env:MCP_TEST_TOKEN"
    with pytest.raises(TypeError):
        config.environment_refs["X"] = "env:X"  # type: ignore[index]
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate({**config.model_dump(), "extra": True})


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad\nidentifier"},
        {"args": "--wrong"},
        {"command": "sh -c unsafe"},
        {"args": ["-c", "unsafe"]},
        {"url": "https://mcp.example.test"},
        {"environment_refs": {"TOKEN": "secret-token"}},
        {"timeout_seconds": 0},
    ],
)
def test_stdio_config_rejects_ambiguous_or_unsafe_values(changes: dict[str, object]) -> None:
    """Catches accepting shell forms, URL ambiguity, raw secrets, or invalid limits."""
    with pytest.raises(ValidationError):
        stdio_config(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "ftp://mcp.example.test"},
        {"url": "https://token@example.test/mcp"},
        {"url": "https://mcp.example.test/mcp#fragment"},
        {"command": "program"},
        {"headers": {"Authorization": "secret-token"}},
        {"headers": {"Authorization": "env:A", "authorization": "env:B"}},
        {"timeout_seconds": math.inf},
    ],
)
def test_http_config_rejects_credential_urls_and_unsafe_headers(changes: dict[str, object]) -> None:
    """Catches remote credentials escaping into config or redirect-capable URLs."""
    with pytest.raises(ValidationError):
        http_config(**changes)


@pytest.mark.anyio
async def test_runtime_validates_complete_binding_and_closes_owned_session() -> None:
    """Catches accepting a missing live capability or leaking a runtime-owned session."""
    fake = FakeMcpSession()
    fake.tools = frozenset({"search_messages"})
    runtime = McpRuntime(session_factory=lambda _: SessionLease(fake, owned=True))

    with pytest.raises(McpCapabilityError) as caught:
        await runtime.validate_binding(stdio_config(), binding())

    assert caught.value.args == ("missing bound tool: get_message",)
    assert fake.closed == 1


@pytest.mark.anyio
async def test_runtime_leaves_borrowed_injected_session_open() -> None:
    """Catches closing an injected fake without an explicit ownership transfer."""
    fake = FakeMcpSession()
    runtime = McpRuntime(session_factory=lambda _: fake)

    assert await runtime.call(stdio_config(), "search_messages", {"q": "fixed"}) == {
        "items": [{"id": "one"}]
    }
    assert fake.closed == 0


@pytest.mark.anyio
async def test_runtime_detaches_exact_json_arguments_and_results() -> None:
    """Catches runtime aliasing caller data or returning mutable provider data."""
    fake = FakeMcpSession()
    runtime = McpRuntime(session_factory=lambda _: fake)
    arguments: dict[str, JsonValue] = {"query": ["one"]}

    result = await runtime.call(stdio_config(), "search_messages", arguments)
    arguments["query"].append("two")  # type: ignore[union-attr]
    assert fake.calls == [("search_messages", {"query": ["one"]})]
    assert result == {"items": [{"id": "one"}]}
    assert result is not fake.tool_result


@pytest.mark.anyio
@pytest.mark.parametrize("value", [{"x": float("nan")}, {"x": ("not", "json")}, object()])
async def test_runtime_rejects_non_exact_sdk_results(value: object) -> None:
    """Catches accepting non-finite values, tuples, and arbitrary SDK objects."""
    fake = FakeMcpSession()
    fake.tool_result = value
    runtime = McpRuntime(session_factory=lambda _: fake)

    with pytest.raises(McpSchemaError) as caught:
        await runtime.call(stdio_config(), "search_messages", {})

    assert caught.value.args == ("invalid MCP JSON result",)


@pytest.mark.anyio
async def test_runtime_redacts_secret_from_error_traceback_and_logs() -> None:
    """Catches a transport failure retaining provider text in the public traceback."""
    secret = "secret-token-never-render"
    fake = FakeMcpSession()
    fake.failure = RuntimeError(f"Authorization: Bearer {secret}")
    runtime = McpRuntime(session_factory=lambda _: fake)

    with pytest.raises(McpTransportError) as caught:
        await runtime.call(stdio_config(), "search_messages", {"token": secret})

    traceback_lines = traceback.TracebackException.from_exception(caught.value, capture_locals=True).stack
    rendered = "\n".join(
        str(frame.locals)
        for frame in traceback_lines
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("MCP transport failure",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in rendered


@pytest.mark.anyio
async def test_runtime_timeout_and_cancellation_close_owned_session() -> None:
    """Catches a stalled operation bypassing timeout or cancellation-safe cleanup."""
    fake = FakeMcpSession()
    fake.block = anyio.Event()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(fake, owned=True))

    with pytest.raises(McpTimeoutError):
        await runtime.call(stdio_config(timeout_seconds=0.01), "search_messages", {})

    assert fake.closed == 1


@pytest.mark.anyio
async def test_runtime_concurrent_calls_do_not_share_or_close_each_other_sessions() -> None:
    """Catches one operation stealing lifecycle ownership from another operation."""
    created: list[FakeMcpSession] = []

    def factory(_: McpServerConfig) -> SessionLease:
        session = FakeMcpSession()
        created.append(session)
        return SessionLease(session, owned=True)

    runtime = McpRuntime(session_factory=factory)
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(runtime.call, stdio_config(), "search_messages", {"q": "one"})
        task_group.start_soon(runtime.call, stdio_config(), "search_messages", {"q": "two"})

    assert len(created) == 2
    assert [session.closed for session in created] == [1, 1]
    assert [session.calls[0][1] for session in created] == [{"q": "one"}, {"q": "two"}]


class _CapturedSdk:
    def __init__(self) -> None:
        self.stdio_parameters: object | None = None
        self.launched_environment: dict[str, str] | None = None
        self.http_client: object | None = None
        self.http_url: str | None = None
        self.initialized = 0

    class StdioServerParameters:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class AsyncClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1

    @asynccontextmanager
    async def stdio_client(
        self, parameters: object, *, errlog: object | None = None
    ) -> AsyncIterator[tuple[str, str]]:
        self.stdio_parameters = parameters
        values = getattr(parameters, "kwargs", None)
        if type(values) is dict and type(values.get("env")) is dict:
            self.launched_environment = dict(values["env"])
        yield ("read", "write")

    @asynccontextmanager
    async def streamable_http_client(
        self, url: str, *, http_client: object, terminate_on_close: bool
    ) -> AsyncIterator[tuple[str, str]]:
        self.http_url = url
        self.http_client = http_client
        assert terminate_on_close is True
        yield ("read", "write")

    class ClientSession:
        def __init__(self, read: str, write: str) -> None:
            self.read = read
            self.write = write

        async def __aenter__(self) -> _CapturedSdk.ClientSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def list_tools(self) -> object:
            return SimpleNamespace(tools=[])

        async def list_resources(self) -> object:
            return SimpleNamespace(resources=[])

        async def list_resource_templates(self) -> object:
            return SimpleNamespace(resource_templates=[])

        async def call_tool(self, _: str, __: dict[str, JsonValue]) -> object:
            return {"structuredContent": {"ok": True}}

        async def read_resource(self, _: str) -> object:
            return {"contents": [{"text": "{\"ok\":true}"}]}


@pytest.mark.anyio
async def test_runtime_validates_bound_resource_template_through_official_v2_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TemplateSdk(_CapturedSdk):
        class ClientSession(_CapturedSdk.ClientSession):
            async def list_tools(self) -> object:
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(name="search_messages"),
                        SimpleNamespace(name="get_message"),
                    ]
                )

            async def list_resources(self) -> object:
                return SimpleNamespace(resources=[])

            async def list_resource_templates(self) -> object:
                return SimpleNamespace(
                    resource_templates=[
                        SimpleNamespace(uri_template="resource://message/{message_id}")
                    ]
                )

    template_binding = binding().model_copy(
        update={"resources": {"record": "resource://message/{message_id}"}}
    )
    runtime = McpRuntime(
        session_factory=lambda config: SessionLease(
            create_production_session(config, sdk_loader=TemplateSdk),
            owned=True,
        )
    )

    monkeypatch.setenv("MCP_TEST_TOKEN", "token")
    await runtime.validate_binding(stdio_config(), template_binding)


@pytest.mark.anyio
async def test_production_factory_uses_official_adapters_without_shell_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches bypassing SDK transports, shell commands, inherited secrets, or HTTP redirects."""
    sdk = _CapturedSdk()
    monkeypatch.setenv("MCP_TEST_TOKEN", "actual-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-secret")

    stdio = create_production_session(stdio_config(), sdk_loader=lambda: sdk)
    await stdio.start()
    parameters = sdk.stdio_parameters
    assert parameters is not None
    assert parameters.kwargs == {
        "command": "message-mcp",
        "args": ["--readonly"],
        "env": {},
    }
    assert sdk.launched_environment == {"TOKEN": "actual-secret"}
    await stdio.close()

    http = create_production_session(http_config(), sdk_loader=lambda: sdk)
    await http.start()
    assert sdk.http_url == "https://mcp.example.test/v1"
    assert sdk.http_client.kwargs == {
        "headers": {"Authorization": "actual-secret"},
        "follow_redirects": False,
    }
    await http.close()


def test_connector_config_rejects_non_json_scope_and_detaches_it() -> None:
    """Catches connector config retaining mutable input or accepting arbitrary scope values."""
    scope: dict[str, JsonValue] = {"projects": ["one"]}
    connector = McpConnectorConfig(
        id="messages-connector",
        profile_path="profiles/messages.yaml",
        server=stdio_config(),
        binding=binding(),
        scope=scope,
    )
    scope["projects"].append("two")  # type: ignore[union-attr]

    assert connector.model_dump(mode="json")["scope"] == {"projects": ["one"]}
    with pytest.raises(ValidationError):
        McpConnectorConfig(
            id="messages-connector",
            profile_path="profiles/messages.yaml",
            server=stdio_config(),
            binding=binding(),
            scope={"bad": cast(Any, object())},
        )


@pytest.mark.anyio
async def test_public_open_and_capability_inspection_own_only_transferred_session() -> None:
    """Catches a missing public session context or inspection bypassing its lifecycle."""
    fake = FakeMcpSession()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(fake, owned=True))

    async with runtime.open(stdio_config()) as opened:
        assert opened is fake
    assert fake.closed == 1

    tools, resources = await runtime.inspect_capabilities(stdio_config())
    assert tools == frozenset({"search_messages", "get_message"})
    assert resources == frozenset({"resource://message"})
    assert fake.closed == 2


@pytest.mark.anyio
async def test_runtime_reads_strict_json_resource_text_and_rejects_duplicate_or_nonfinite_json() -> None:
    """Catches resource decoding accepting duplicate keys or non-finite encoded provider data."""
    fake = FakeMcpSession()
    runtime = McpRuntime(session_factory=lambda _: fake)
    fake.resource_result = {"contents": [{"text": "{\"id\":\"one\"}"}]}
    assert await runtime.read_resource(stdio_config(), "resource://message") == {"id": "one"}

    for encoded in ('{"id":"one","id":"two"}', '{"score":NaN}'):
        fake.resource_result = {"contents": [{"text": encoded}]}
        with pytest.raises(McpSchemaError):
            await runtime.read_resource(stdio_config(), "resource://message")


@pytest.mark.anyio
async def test_runtime_preserves_cancellation_interrupt_and_original_error_while_closing_once() -> None:
    """Catches translating cancellation/interrupt or allowing cleanup to replace the operation failure."""
    class ClosingFailure(FakeMcpSession):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError("cleanup-provider-secret")

    failure = ClosingFailure()
    failure.failure = RuntimeError("original-provider-secret")
    runtime = McpRuntime(session_factory=lambda _: SessionLease(failure, owned=True))
    with pytest.raises(McpTransportError):
        await runtime.call(stdio_config(), "search_messages", {})
    assert failure.closed == 1

    interrupt = FakeMcpSession()
    interrupt.failure = KeyboardInterrupt()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(interrupt, owned=True))
    with pytest.raises(KeyboardInterrupt):
        await runtime.call(stdio_config(), "search_messages", {})
    assert interrupt.closed == 1

    blocked = FakeMcpSession()
    blocked.block = anyio.Event()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(blocked, owned=True))
    cancelled: list[BaseException] = []

    async def cancelled_call() -> None:
        try:
            await runtime.call(stdio_config(timeout_seconds=1), "search_messages", {})
        except BaseException as error:  # noqa: BLE001 - asserts cancellation propagation.
            cancelled.append(error)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(cancelled_call)
        await anyio.lowlevel.checkpoint()
        tasks.cancel_scope.cancel()
    assert len(cancelled) == 1
    assert isinstance(cancelled[0], anyio.get_cancelled_exc_class())
    assert blocked.closed == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (McpPermissionError(), McpPermissionError),
        (McpProtocolError(), McpProtocolError),
        (McpClosedSessionError(), McpClosedSessionError),
    ],
)
async def test_runtime_translates_fixed_typed_failures(failure: BaseException, expected: type[BaseException]) -> None:
    """Catches raw/incorrect typed failure translation at the public runtime boundary."""
    fake = FakeMcpSession()
    fake.failure = failure
    runtime = McpRuntime(session_factory=lambda _: fake)

    with pytest.raises(expected) as caught:
        await runtime.call(stdio_config(), "search_messages", {})

    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.anyio
async def test_runtime_rejects_control_or_overlong_live_capabilities_and_call_names() -> None:
    """Catches a malicious live capability list reaching binding validation or provider call."""
    fake = FakeMcpSession()
    fake.tools = frozenset({"search_messages\n"})
    runtime = McpRuntime(session_factory=lambda _: fake)
    with pytest.raises(McpProtocolError):
        await runtime.inspect_capabilities(stdio_config())

    with pytest.raises(McpSchemaError):
        await runtime.call(stdio_config(), "x" * 257, {})


@pytest.mark.anyio
async def test_production_resolves_environment_only_at_start_and_forgets_it_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches eager secret resolution or retaining a resolved value beyond the owned session."""
    sdk = _CapturedSdk()
    monkeypatch.delenv("MCP_TEST_TOKEN", raising=False)
    session = create_production_session(stdio_config(), sdk_loader=lambda: sdk)
    monkeypatch.setenv("MCP_TEST_TOKEN", "resolved-only-at-start")
    await session.start()
    await session.close()

    assert "resolved-only-at-start" not in repr(vars(session))


def test_config_rejects_credential_query_nonexact_mappings_and_roundtrips_public_json() -> None:
    """Catches secrets in URL query, mapping-subclass coercion, or non-detached config serialization."""
    with pytest.raises(ValidationError):
        http_config(url="https://mcp.example.test/v1?token=secret")
    with pytest.raises(ValidationError):
        stdio_config(environment_refs=cast(Any, MappingProxyType({"TOKEN": "env:MCP_TEST_TOKEN"})))
    with pytest.raises(ValidationError):
        McpConnectorConfig(
            id="messages-connector",
            profile_path="profiles/messages.yaml",
            server=stdio_config(),
            binding=binding(),
            scope=cast(Any, {"items": ("not", "json")} ),
        )

    original = http_config()
    assert McpServerConfig.model_validate_json(original.model_dump_json()) == original


@pytest.mark.anyio
async def test_one_deadline_covers_start_and_owned_close() -> None:
    """Catches separate timeout budgets that let start or cleanup extend an operation deadline."""
    class SlowStart(FakeMcpSession):
        async def start(self) -> None:
            await anyio.Event().wait()

    starting = SlowStart()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(starting, owned=True))
    with pytest.raises(McpTimeoutError):
        await runtime.call(stdio_config(timeout_seconds=0.01), "search_messages", {})
    assert starting.closed == 1

    class SlowClose(FakeMcpSession):
        async def close(self) -> None:
            self.closed += 1
            await anyio.Event().wait()

    closing = SlowClose()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(closing, owned=True))
    started = time.monotonic()
    with pytest.raises(McpTimeoutError):
        await runtime.call(stdio_config(timeout_seconds=0.01), "search_messages", {})
    assert time.monotonic() - started < 0.018
    assert closing.closed == 1


@pytest.mark.anyio
async def test_production_start_failure_redacts_resolved_secret_from_public_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a lazy adapter error chaining through resolved headers or environment values."""
    secret = "adapter-secret-never-render"

    class FailingSdk(_CapturedSdk):
        class ClientSession(_CapturedSdk.ClientSession):
            async def initialize(self) -> None:
                raise RuntimeError(f"provider said {secret}")

    monkeypatch.setenv("MCP_TEST_TOKEN", secret)
    runtime = McpRuntime(
        session_factory=lambda config: SessionLease(
            create_production_session(config, sdk_loader=FailingSdk), owned=True
        )
    )
    with pytest.raises(McpTransportError) as caught:
        await runtime.call(stdio_config(), "search_messages", {})

    frames = traceback.TracebackException.from_exception(caught.value, capture_locals=True).stack
    assert secret not in "\n".join(
        str(frame.locals) for frame in frames if "/src/intent_engineering/" in frame.filename
    )


@pytest.mark.anyio
async def test_public_open_redacts_start_and_close_failures_but_preserves_caller_failure() -> None:
    """Catches raw lifecycle errors escaping the public context-manager boundary."""
    secret = "open-lifecycle-secret"

    class StartFailure(FakeMcpSession):
        async def start(self) -> None:
            raise RuntimeError(secret)

    runtime = McpRuntime(session_factory=lambda _: SessionLease(StartFailure(), owned=True))
    with pytest.raises(McpTransportError) as started:
        async with runtime.open(stdio_config(timeout_seconds=0.01)):
            pytest.fail("body must not run")
    assert started.value.args == ("MCP transport failure",)
    assert started.value.__cause__ is None and started.value.__context__ is None
    rendered = traceback.TracebackException.from_exception(started.value, capture_locals=True)
    assert secret not in "\n".join(
        str(frame.locals) for frame in rendered.stack if "/src/intent_engineering/" in frame.filename
    )

    class CloseFailure(FakeMcpSession):
        async def close(self) -> None:
            await super().close()
            raise RuntimeError(secret)

    closing = CloseFailure()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(closing, owned=True))
    with pytest.raises(McpTransportError) as closed:
        async with runtime.open(stdio_config(timeout_seconds=0.01)):
            pass
    assert closed.value.args == ("MCP transport failure",)
    assert closing.closed == 1

    preserving = CloseFailure()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(preserving, owned=True))
    with pytest.raises(KeyboardInterrupt):
        async with runtime.open(stdio_config(timeout_seconds=0.01)):
            raise KeyboardInterrupt()
    assert preserving.closed == 1


@pytest.mark.anyio
async def test_public_open_uses_one_deadline_for_start_and_owned_close() -> None:
    """Catches an explicit open scope bypassing the runtime's lifecycle deadline."""
    class SlowStart(FakeMcpSession):
        async def start(self) -> None:
            await anyio.Event().wait()

    starting = SlowStart()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(starting, owned=True))
    with pytest.raises(McpTimeoutError):
        async with runtime.open(stdio_config(timeout_seconds=0.01)):
            pytest.fail("unreachable")
    assert starting.closed == 1

    class SlowClose(FakeMcpSession):
        async def close(self) -> None:
            self.closed += 1
            await anyio.Event().wait()

    closing = SlowClose()
    runtime = McpRuntime(session_factory=lambda _: SessionLease(closing, owned=True))
    with pytest.raises(McpTimeoutError):
        async with runtime.open(stdio_config(timeout_seconds=0.01)):
            pass
    assert closing.closed == 1


@pytest.mark.anyio
async def test_stdio_adapter_uses_discarding_errlog_without_retaining_stderr_secret(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the SDK default stderr sink receiving child-process/provider diagnostics."""
    secret = "stdio-stderr-secret"

    class ErrlogSdk(_CapturedSdk):
        received_errlog: object | None = None

        @asynccontextmanager
        async def stdio_client(self, parameters: object, *, errlog: object) -> AsyncIterator[tuple[str, str]]:
            self.stdio_parameters = parameters
            type(self).received_errlog = errlog
            errlog.write(secret)
            yield ("read", "write")

    monkeypatch.setenv("MCP_TEST_TOKEN", "token")
    session = create_production_session(stdio_config(), sdk_loader=ErrlogSdk)
    await session.start()
    await session.close()

    assert ErrlogSdk.received_errlog is not None
    assert secret not in capsys.readouterr().err
    assert secret not in repr(vars(session))


@pytest.mark.anyio
async def test_official_adapter_paginates_capabilities_and_rejects_bad_cursor_or_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches validating only the first v2 capability page or accepting cyclic cursors."""
    class PagingSdk(_CapturedSdk):
        tool_params: ClassVar[list[object | None]] = []
        resource_params: ClassVar[list[object | None]] = []

        class PaginatedRequestParams:
            def __init__(self, *, cursor: str) -> None:
                self.cursor = cursor

        class ClientSession(_CapturedSdk.ClientSession):
            async def list_tools(self, *, params: object | None = None) -> object:
                PagingSdk.tool_params.append(params)
                cursor = getattr(params, "cursor", None)
                if cursor is None:
                    return SimpleNamespace(tools=[SimpleNamespace(name="first")], next_cursor="tool-next")
                return SimpleNamespace(tools=[SimpleNamespace(name="second")], next_cursor=None)

            async def list_resources(self, *, params: object | None = None) -> object:
                PagingSdk.resource_params.append(params)
                cursor = getattr(params, "cursor", None)
                if cursor is None:
                    return SimpleNamespace(
                        resources=[SimpleNamespace(uri="resource://first")], next_cursor="resource-next"
                    )
                return SimpleNamespace(resources=[SimpleNamespace(uri="resource://second")], next_cursor=None)

    monkeypatch.setenv("MCP_TEST_TOKEN", "token")
    session = create_production_session(stdio_config(), sdk_loader=PagingSdk)
    await session.start()
    assert await session.list_tools() == frozenset({"first", "second"})
    assert await session.list_resources() == frozenset({"resource://first", "resource://second"})
    assert [getattr(value, "cursor", None) for value in PagingSdk.tool_params] == [None, "tool-next"]
    await session.close()

    class CyclicSdk(PagingSdk):
        class ClientSession(PagingSdk.ClientSession):
            async def list_tools(self, *, params: object | None = None) -> object:
                cursor = getattr(params, "cursor", None)
                name = "one" if cursor is None else "two"
                return SimpleNamespace(tools=[SimpleNamespace(name=name)], next_cursor="again")

    cyclic = create_production_session(stdio_config(), sdk_loader=CyclicSdk)
    await cyclic.start()
    with pytest.raises(McpProtocolError):
        await cyclic.list_tools()
    await cyclic.close()

    class InvalidCursorSdk(PagingSdk):
        class ClientSession(PagingSdk.ClientSession):
            async def list_resources(self, *, params: object | None = None) -> object:
                return SimpleNamespace(resources=[SimpleNamespace(uri="resource://one")], next_cursor="bad\n")

    invalid = create_production_session(stdio_config(), sdk_loader=InvalidCursorSdk)
    await invalid.start()
    with pytest.raises(McpProtocolError):
        await invalid.list_resources()
    await invalid.close()


@pytest.mark.anyio
async def test_official_decoder_rejects_error_and_ambiguous_payloads_without_hostile_model_dump(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches accepting protocol error content, ambiguous wrappers, or arbitrary model methods."""
    class DecoderSdk(_CapturedSdk):
        class ClientSession(_CapturedSdk.ClientSession):
            async def call_tool(self, _: str, __: dict[str, JsonValue]) -> object:
                return {"structuredContent": None, "isError": False}

            async def read_resource(self, _: str) -> object:
                return {"contents": [{"text": "null"}]}

    monkeypatch.setenv("MCP_TEST_TOKEN", "token")
    session = create_production_session(stdio_config(), sdk_loader=DecoderSdk)
    await session.start()
    assert await session.call_tool("search_messages", {}) is None
    assert await session.read_resource("resource://message") is None
    await session.close()

    class Hostile:
        invoked = False

        def model_dump(self, **_: object) -> object:
            type(self).invoked = True
            return {"structuredContent": {"bad": True}}

    class HostileSdk(DecoderSdk):
        class ClientSession(DecoderSdk.ClientSession):
            async def call_tool(self, _: str, __: dict[str, JsonValue]) -> object:
                return Hostile()

            async def read_resource(self, _: str) -> object:
                return {"contents": [{"text": "{}"}], "structuredContent": {}}

    hostile = create_production_session(stdio_config(), sdk_loader=HostileSdk)
    await hostile.start()
    with pytest.raises(McpSchemaError):
        await hostile.call_tool("search_messages", {})
    with pytest.raises(McpSchemaError):
        await hostile.read_resource("resource://message")
    assert Hostile.invoked is False
    await hostile.close()

    class ErrorSdk(DecoderSdk):
        class ClientSession(DecoderSdk.ClientSession):
            async def call_tool(self, _: str, __: dict[str, JsonValue]) -> object:
                return {"structuredContent": {"ignored": True}, "isError": True}

            async def read_resource(self, _: str) -> object:
                return {"contents": [{"text": "{\"score\":NaN}"}]}

    errors = create_production_session(stdio_config(), sdk_loader=ErrorSdk)
    await errors.start()
    with pytest.raises(McpSchemaError):
        await errors.call_tool("search_messages", {})
    with pytest.raises(McpSchemaError):
        await errors.read_resource("resource://message")
    await errors.close()


@pytest.mark.anyio
async def test_runtime_classifies_faithful_v2_failures_without_provider_data() -> None:
    """Catches treating installed-v2 closed/protocol/schema/permission failures as generic transport."""
    secret = "v2-provider-data-secret"

    class V2McpError(Exception):
        __module__ = "mcp.shared.exceptions"

        def __init__(self, code: int, message: str, data: object) -> None:
            self.code = code
            self.message = message
            self.data = data
            super().__init__(message)

    cases: list[tuple[BaseException, type[BaseException]]] = [
        (PermissionError(secret), McpPermissionError),
        (V2McpError(-32000, secret, {"token": secret}), McpClosedSessionError),
        (V2McpError(-32602, secret, {"token": secret}), McpSchemaError),
        (V2McpError(-32600, secret, {"token": secret}), McpProtocolError),
    ]
    for failure, expected in cases:
        fake = FakeMcpSession()
        fake.failure = failure
        runtime = McpRuntime(session_factory=lambda _, session=fake: session)
        with pytest.raises(expected) as caught:
            await runtime.call(stdio_config(), "search_messages", {})
        assert secret not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("/bin/sh", []),
        ("/usr/bin/bash", []),
        ("program", ["-lc", "unsafe"]),
        ("program", ["-cl", "unsafe"]),
        ("program", ["--command", "unsafe"]),
    ],
)
def test_config_rejects_shell_basenames_and_combined_execution_switches(
    command: str, args: list[str]
) -> None:
    """Catches disguised shell execution despite argv transport construction."""
    with pytest.raises(ValidationError):
        stdio_config(command=command, args=args)


@pytest.mark.anyio
async def test_production_session_never_retains_full_environment_or_resolver_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches retaining ambient environment contents before, during, or after deferred resolution."""
    referenced = "referenced-environment-secret"
    unrelated = "unrelated-environment-secret"
    monkeypatch.setenv("MCP_TEST_TOKEN", referenced)
    monkeypatch.setenv("UNRELATED_MCP_TEST_SENTINEL", unrelated)
    session = create_production_session(stdio_config(), sdk_loader=_CapturedSdk)
    assert referenced not in repr(vars(session))
    assert unrelated not in repr(vars(session))
    await session.start()
    assert referenced not in repr(vars(session))
    assert unrelated not in repr(vars(session))
    await session.close()
    assert referenced not in repr(vars(session))
    assert unrelated not in repr(vars(session))


@pytest.mark.anyio
async def test_owner_task_drops_bound_environment_resolver_and_stdio_values() -> None:
    """Catches a long-lived SDK owner retaining unrelated or resolved environment secrets."""
    referenced = "owner-referenced-environment-secret"
    unrelated = "owner-unrelated-environment-secret"
    environment = {
        "MCP_TEST_TOKEN": referenced,
        "UNRELATED_MCP_TEST_SENTINEL": unrelated,
    }
    sdk = _CapturedSdk()
    session = create_production_session(
        stdio_config(), sdk_loader=lambda: sdk, environ=environment
    )

    await session.start()
    owner = vars(session).get("_owner_task")
    assert isinstance(owner, asyncio.Task)
    assert all("lookup" not in frame.f_locals for frame in owner.get_stack())
    parameters = sdk.stdio_parameters
    assert parameters is not None
    assert parameters.kwargs["env"] == {}
    await session.close()


@pytest.mark.anyio
async def test_production_session_rejects_repeated_and_post_failure_start() -> None:
    """Catches replacing the one tracked owner after a successful or failed start."""
    sdk = _CapturedSdk()
    session = create_production_session(
        stdio_config(), sdk_loader=lambda: sdk, environ={"MCP_TEST_TOKEN": "token"}
    )
    await session.start()
    first_owner = vars(session).get("_owner_task")
    with pytest.raises(McpClosedSessionError):
        await session.start()
    assert vars(session).get("_owner_task") is first_owner
    await session.close()

    class FailingOnceSdk(_CapturedSdk):
        attempts = 0

        class ClientSession(_CapturedSdk.ClientSession):
            async def initialize(self) -> None:
                FailingOnceSdk.attempts += 1
                if FailingOnceSdk.attempts == 1:
                    raise RuntimeError("fixed-start-failure")

    failed = create_production_session(
        stdio_config(), sdk_loader=FailingOnceSdk, environ={"MCP_TEST_TOKEN": "token"}
    )
    with pytest.raises(McpTransportError):
        await failed.start()
    failed_owner = vars(failed).get("_owner_task")
    with pytest.raises(McpClosedSessionError):
        await failed.start()
    assert vars(failed).get("_owner_task") is failed_owner
    with pytest.raises(McpTransportError):
        await failed.close()


@pytest.mark.anyio
async def test_production_session_allows_only_one_concurrent_start_owner() -> None:
    """Catches concurrent starts orphaning the first SDK owner and its transport."""
    class CountingSdk(_CapturedSdk):
        owner_tasks: ClassVar[set[asyncio.Task[object]]] = set()
        opened = 0
        closed = 0

        @asynccontextmanager
        async def stdio_client(
            self, parameters: object, *, errlog: object
        ) -> AsyncIterator[tuple[str, str]]:
            del errlog
            self.stdio_parameters = parameters
            owner = asyncio.current_task()
            assert owner is not None
            CountingSdk.owner_tasks.add(cast(asyncio.Task[object], owner))
            CountingSdk.opened += 1
            try:
                yield ("read", "write")
            finally:
                CountingSdk.closed += 1

    session = create_production_session(
        stdio_config(), sdk_loader=CountingSdk, environ={"MCP_TEST_TOKEN": "token"}
    )
    outcomes: list[str] = []

    async def start_once() -> None:
        try:
            await session.start()
        except McpClosedSessionError:
            outcomes.append("closed")
        else:
            outcomes.append("started")

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(start_once)
            tasks.start_soon(start_once)
        assert sorted(outcomes) == ["closed", "started"]
    finally:
        await session.close()
        for owner in CountingSdk.owner_tasks:
            if not owner.done():
                owner.cancel()
        await asyncio.gather(*CountingSdk.owner_tasks, return_exceptions=True)

    assert CountingSdk.opened == 1
    assert CountingSdk.closed == 1


@pytest.mark.anyio
async def test_concurrent_close_prevents_delayed_start_from_publishing_a_client() -> None:
    """Catches initialization reporting success or retaining a client after close wins the race."""
    initializing = anyio.Event()
    finish_initializing = anyio.Event()

    class DelayedSdk(_CapturedSdk):
        closed = 0

        @asynccontextmanager
        async def stdio_client(
            self, parameters: object, *, errlog: object
        ) -> AsyncIterator[tuple[str, str]]:
            del errlog
            self.stdio_parameters = parameters
            try:
                yield ("read", "write")
            finally:
                DelayedSdk.closed += 1

        class ClientSession(_CapturedSdk.ClientSession):
            async def initialize(self) -> None:
                initializing.set()
                await finish_initializing.wait()

    session = create_production_session(
        stdio_config(), sdk_loader=DelayedSdk, environ={"MCP_TEST_TOKEN": "token"}
    )
    outcomes: list[str] = []

    async def start_session() -> None:
        try:
            await session.start()
        except McpClosedSessionError:
            outcomes.append("start-closed")
        else:
            outcomes.append("start-succeeded")

    async def close_session() -> None:
        await session.close()
        outcomes.append("close-succeeded")

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(start_session)
        await initializing.wait()
        tasks.start_soon(close_session)
        while not vars(session).get("_closed"):
            await anyio.lowlevel.checkpoint()
        finish_initializing.set()

    assert sorted(outcomes) == ["close-succeeded", "start-closed"]
    assert vars(session).get("_client") is None
    assert DelayedSdk.closed == 1


@pytest.mark.anyio
async def test_stdio_error_sink_supports_a_real_subprocess_fileno_without_emitting_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches passing the SDK a write-only object that cannot be used as subprocess stderr."""
    secret = "real-subprocess-stderr-secret"

    class FilenoSdk(_CapturedSdk):
        @asynccontextmanager
        async def stdio_client(
            self, parameters: object, *, errlog: object
        ) -> AsyncIterator[tuple[str, str]]:
            self.stdio_parameters = parameters
            process = await anyio.open_process(
                [sys.executable, "-c", f"import sys; sys.stderr.write({secret!r})"],
                stderr=errlog,
            )
            try:
                assert await process.wait() == 0
            finally:
                await process.aclose()
            yield ("read", "write")

    monkeypatch.setenv("MCP_TEST_TOKEN", "token")
    session = create_production_session(stdio_config(), sdk_loader=FilenoSdk)
    await session.start()
    await session.close()

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.anyio
@pytest.mark.parametrize("failure_kind", ["mcp-timeout", "http-timeout", "http-401", "http-403"])
async def test_runtime_classifies_v2_timeout_and_http_auth_failures(failure_kind: str) -> None:
    """Catches reporting SDK timeouts as protocol errors or HTTP auth failures as transport errors."""
    secret = "classification-provider-secret"

    class V2McpError(Exception):
        __module__ = "mcp.shared.exceptions"

        def __init__(self) -> None:
            self.code = -32001
            self.data = {"token": secret}
            super().__init__(secret)

    if failure_kind == "mcp-timeout":
        failure: BaseException = V2McpError()
        expected = McpTimeoutError
    elif failure_kind == "http-timeout":
        failure = httpx2.ReadTimeout(secret)
        expected = McpTimeoutError
    else:
        request = httpx2.Request("GET", "https://mcp.example.test/v1")
        response = httpx2.Response(int(failure_kind.removeprefix("http-")), request=request)
        failure = httpx2.HTTPStatusError(secret, request=request, response=response)
        expected = McpPermissionError

    fake = FakeMcpSession()
    fake.failure = failure
    runtime = McpRuntime(session_factory=lambda _: fake)
    with pytest.raises(expected) as caught:
        await runtime.call(stdio_config(), "search_messages", {})

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.anyio
async def test_production_session_cleanup_obeys_the_single_runtime_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches nested shielded SDK cleanup running beyond start/close operation deadlines."""
    cleanup_delay = 0.08

    class SlowCleanupSdk(_CapturedSdk):
        fail_start = False
        completed_cleanups = 0

        @asynccontextmanager
        async def stdio_client(
            self, parameters: object, *, errlog: object
        ) -> AsyncIterator[tuple[str, str]]:
            self.stdio_parameters = parameters
            async with anyio.create_task_group():
                try:
                    yield ("read", "write")
                finally:
                    with anyio.CancelScope(shield=True):
                        await anyio.sleep(cleanup_delay)
                    SlowCleanupSdk.completed_cleanups += 1

        class ClientSession(_CapturedSdk.ClientSession):
            async def initialize(self) -> None:
                if SlowCleanupSdk.fail_start:
                    raise RuntimeError("fixed-start-failure")

    monkeypatch.setenv("MCP_TEST_TOKEN", "token")

    for fail_start, expected in (
        (True, McpTransportError),
        (False, McpTimeoutError),
    ):
        SlowCleanupSdk.fail_start = fail_start
        runtime = McpRuntime(
            session_factory=lambda config: SessionLease(
                create_production_session(config, sdk_loader=SlowCleanupSdk), owned=True
            )
        )
        started = time.monotonic()
        with pytest.raises(expected):
            await runtime.call(
                stdio_config(timeout_seconds=0.01), "search_messages", {}
            )
        assert time.monotonic() - started < cleanup_delay / 2

    await anyio.sleep(cleanup_delay + 0.01)
    assert SlowCleanupSdk.completed_cleanups == 2


@pytest.mark.anyio
async def test_capability_pagination_bounds_empty_unique_cursor_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an empty provider page stream growing its cursor set without a bound."""
    page_limit = 128

    class EndlessPagingSdk(_CapturedSdk):
        calls = 0

        class PaginatedRequestParams:
            def __init__(self, *, cursor: str) -> None:
                self.cursor = cursor

        class ClientSession(_CapturedSdk.ClientSession):
            async def list_tools(self, *, params: object | None = None) -> object:
                del params
                EndlessPagingSdk.calls += 1
                if EndlessPagingSdk.calls > page_limit:
                    raise AssertionError("pagination exceeded its public page bound")
                return SimpleNamespace(
                    tools=[], next_cursor=f"cursor-{EndlessPagingSdk.calls}"
                )

    monkeypatch.setenv("MCP_TEST_TOKEN", "token")
    session = create_production_session(stdio_config(), sdk_loader=EndlessPagingSdk)
    await session.start()
    with pytest.raises(McpProtocolError):
        await session.list_tools()
    assert EndlessPagingSdk.calls == page_limit
    await session.close()
