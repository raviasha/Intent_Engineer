"""Owned, timeout-bounded MCP sessions and lazy official-SDK adapters."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, cast

import anyio

from intent_engineering.capture.mcp.errors import (
    McpCapabilityError,
    McpClosedSessionError,
    McpError,
    McpPermissionError,
    McpProtocolError,
    McpSchemaError,
    McpTimeoutError,
    McpTransportError,
)
from intent_engineering.capture.mcp.profile_models import BindingValidationError, ProviderBinding
from intent_engineering.capture.mcp.session import McpServerConfig, McpSession, detached_json
from intent_engineering.core.models import JsonValue

_MAX_CAPABILITIES = 1024
_MAX_CAPABILITY_PAGES = 128
_MAX_NAME = 256
_BACKGROUND_SESSION_OWNERS: set[asyncio.Task[str]] = set()


def _session_owner_finished(task: asyncio.Task[str]) -> None:
    """Forget a detached owner after consuming its already-redacted result."""
    _BACKGROUND_SESSION_OWNERS.discard(task)
    try:
        task.result()
    except BaseException:  # noqa: BLE001 - owner failures never cross the public boundary.
        return


class _Sdk(Protocol):
    ClientSession: Any
    StdioServerParameters: Any
    AsyncClient: Any
    stdio_client: Any
    streamable_http_client: Any
    PaginatedRequestParams: Any
    CallToolResult: Any
    ReadResourceResult: Any


type SdkLoader = Callable[[], _Sdk]
type SessionFactory = Callable[[McpServerConfig], McpSession | SessionLease]
type EnvironmentLookup = Callable[[str], str | None]

_INVALID_JSON = object()


@dataclass(frozen=True, slots=True)
class SessionLease:
    """Explicitly transfers lifecycle ownership of an injected session to a runtime call."""

    session: McpSession
    owned: bool = False


@dataclass(slots=True)
class _OpenState:
    """Private state for a public open scope after its safe start boundary."""

    manager: Any
    session: McpSession
    deadline: float


def _load_official_sdk() -> _Sdk:
    """Import MCP only at the production boundary, never while loading public contracts."""
    import httpx2
    import mcp_types
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client

    return cast(
        _Sdk,
        SimpleNamespace(
            ClientSession=ClientSession,
            StdioServerParameters=StdioServerParameters,
            stdio_client=stdio_client,
            streamable_http_client=streamable_http_client,
            AsyncClient=httpx2.AsyncClient,
            PaginatedRequestParams=mcp_types.PaginatedRequestParams,
            CallToolResult=mcp_types.CallToolResult,
            ReadResourceResult=mcp_types.ReadResourceResult,
        ),
    )


def _system_environment_lookup(name: str) -> str | None:
    """Read one process environment value without retaining the environment mapping."""
    return os.getenv(name)


def _empty_environment_lookup(_: str) -> str | None:
    return None


def _resolved_references(
    references: Mapping[str, str], lookup: EnvironmentLookup
) -> dict[str, str] | None:
    """Resolve only explicit refs into a short-lived local mapping."""
    resolved: dict[str, str] = {}
    for target, reference in references.items():
        value = lookup(reference[4:])
        if value is None:
            return None
        resolved[target] = value
    return resolved


def _name(value: object) -> str | None:
    if type(value) is not str or not value or len(value) > _MAX_NAME:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def _decoded_json(value: object) -> tuple[bool, JsonValue]:
    """Copy one strict JSON value while preserving a valid JSON null result."""
    try:
        return True, detached_json(value)
    except ValueError:
        return False, None


def _sdk_mapping(value: object, expected_type: object) -> dict[str, object] | object:
    """Only deserialize an exact SDK result type; arbitrary model methods are never invoked."""
    if type(value) is dict:
        return cast(dict[str, object], value)
    if not isinstance(expected_type, type) or not isinstance(value, expected_type):
        return _INVALID_JSON
    try:
        dumped = cast(Any, value).model_dump(mode="json", by_alias=True, exclude_unset=True)
    except Exception:  # noqa: BLE001 - the trusted SDK result may still reject serialization.
        return _INVALID_JSON
    return dumped if type(dumped) is dict else _INVALID_JSON


def _decode_tool_result(value: object, expected_type: object) -> tuple[bool, JsonValue]:
    raw = _sdk_mapping(value, expected_type)
    if raw is _INVALID_JSON:
        return False, None
    assert type(raw) is dict
    if "contents" in raw or "structuredContent" not in raw:
        return False, None
    is_error = raw.get("isError", False)
    if type(is_error) is not bool or is_error:
        return False, None
    return _decoded_json(raw["structuredContent"])


def _decode_resource_result(value: object, expected_type: object) -> tuple[bool, JsonValue]:
    raw = _sdk_mapping(value, expected_type)
    if raw is _INVALID_JSON:
        return False, None
    assert type(raw) is dict
    if "structuredContent" in raw or "isError" in raw or "contents" not in raw:
        return False, None
    contents = raw["contents"]
    if type(contents) is not list or len(contents) != 1 or type(contents[0]) is not dict:
        return False, None
    content = cast(dict[str, object], contents[0])
    if set(content) - {"text", "uri", "mimeType", "_meta"} or type(content.get("text")) is not str:
        return False, None
    text = cast(str, content["text"])
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except Exception:  # noqa: BLE001 - provider text is an untrusted boundary.
        return False, None
    return _decoded_json(parsed)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _enter_stdio_transport(
    stack: AsyncExitStack,
    sdk: _Sdk,
    config: McpServerConfig,
    environment: dict[str, str],
) -> tuple[Any, Any]:
    """Launch stdio, then scrub the detached child environment from retained SDK state."""
    parameters = sdk.StdioServerParameters(
        command=config.command,
        args=list(config.args),
        env=environment,
    )
    error_log = stack.enter_context(
        os.fdopen(
            os.open(os.devnull, os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)),
            "w",
            encoding="utf-8",
        )
    )
    try:
        return cast(
            tuple[Any, Any],
            await stack.enter_async_context(sdk.stdio_client(parameters, errlog=error_log)),
        )
    finally:
        environment.clear()
        parameter_environment = getattr(parameters, "env", None)
        if type(parameter_environment) is dict:
            parameter_environment.clear()
        parameter_values = getattr(parameters, "kwargs", None)
        if type(parameter_values) is dict:
            retained_environment = parameter_values.get("env")
            if type(retained_environment) is dict:
                retained_environment.clear()


class _OfficialMcpSession:
    """One fully owned official SDK session; all raw SDK state remains private."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        sdk_loader: SdkLoader,
        environment_lookup: EnvironmentLookup,
    ) -> None:
        self._config = config
        self._sdk_loader = sdk_loader
        self._environment_lookup: EnvironmentLookup = environment_lookup
        self._client: Any | None = None
        self._pagination_params: Any | None = None
        self._tool_result_type: object | None = None
        self._resource_result_type: object | None = None
        self._operation_deadline: float | None = None
        self._owner_task: asyncio.Task[str] | None = None
        self._close_requested: asyncio.Event | None = None
        self._timeout_seconds = config.timeout_seconds
        self._closed = False

    def _set_operation_deadline(self, deadline: float) -> None:
        self._operation_deadline = deadline

    def _cleanup_deadline(self) -> float:
        deadline = self._operation_deadline
        if deadline is None:
            deadline = _deadline(self._timeout_seconds)
        return deadline

    async def _own_session(
        self,
        config: McpServerConfig,
        sdk: _Sdk,
        lookup: EnvironmentLookup,
        ready: asyncio.Future[str],
        close_requested: asyncio.Event,
    ) -> str:
        """Own SDK context entry and exit in one task so AnyIO cancel scopes stay valid."""
        stack = AsyncExitStack()
        code = "ok"
        try:
            await stack.__aenter__()
            if close_requested.is_set() or self._closed:
                if not ready.done():
                    ready.set_result("closed")
                return code
            references = (
                config.environment_refs if config.transport == "stdio" else config.headers
            )
            try:
                resolved = _resolved_references(references, lookup)
            finally:
                del lookup
            if resolved is None:
                raise RuntimeError()
            if config.transport == "stdio":
                streams = await _enter_stdio_transport(stack, sdk, config, resolved)
            else:
                http_client = sdk.AsyncClient(headers=resolved, follow_redirects=False)
                await stack.enter_async_context(_async_client_context(http_client))
                streams = await stack.enter_async_context(
                    sdk.streamable_http_client(
                        cast(str, config.url),
                        http_client=http_client,
                        terminate_on_close=True,
                    )
                )
            del resolved
            client = await stack.enter_async_context(sdk.ClientSession(*streams))
            await client.initialize()
            if close_requested.is_set() or self._closed:
                if not ready.done():
                    ready.set_result("closed")
            else:
                self._client = client
                self._pagination_params = getattr(sdk, "PaginatedRequestParams", None)
                self._tool_result_type = getattr(sdk, "CallToolResult", None)
                self._resource_result_type = getattr(sdk, "ReadResourceResult", None)
                if not ready.done():
                    ready.set_result("ok")
                await close_requested.wait()
        except BaseException as error:  # noqa: BLE001 - convert before crossing task boundary.
            if not ready.done():
                try:
                    code = _exception_code(error)
                except BaseException:  # noqa: BLE001 - caller cancellation owns its own signal.
                    code = "transport"
                ready.set_result(code)
            del error
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await stack.aclose()
            except BaseException as error:  # noqa: BLE001 - return only a redacted category.
                if code == "ok":
                    try:
                        code = _exception_code(error)
                    except BaseException:  # noqa: BLE001 - cleanup control flow is provider-neutral.
                        code = "transport"
                del error
        return code

    async def start(self) -> None:
        if self._closed or self._owner_task is not None:
            raise McpClosedSessionError()
        sdk = self._sdk_loader()
        lookup = self._environment_lookup
        self._environment_lookup = _empty_environment_lookup
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[str] = loop.create_future()
        close_requested = asyncio.Event()
        owner = asyncio.create_task(
            self._own_session(self._config, sdk, lookup, ready, close_requested)
        )
        self._owner_task = owner
        self._close_requested = close_requested
        try:
            code = await ready
        except BaseException:
            close_requested.set()
            owner.cancel()
            _BACKGROUND_SESSION_OWNERS.add(owner)
            owner.add_done_callback(_session_owner_finished)
            raise
        finally:
            del lookup
        if code != "ok":
            _public_result((code, None))

    async def list_tools(self) -> frozenset[str]:
        return await self._list_capabilities("list_tools", "tools", "name")

    async def list_resources(self) -> frozenset[str]:
        return await self._list_capabilities("list_resources", "resources", "uri")

    async def list_resource_templates(self) -> frozenset[str]:
        return await self._list_capabilities(
            "list_resource_templates",
            "resource_templates",
            "uri_template",
        )

    async def call_tool(self, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        client = self._require_client()
        result = await client.call_tool(name, arguments)
        valid, decoded = _decode_tool_result(result, self._tool_result_type)
        if not valid:
            raise McpSchemaError()
        return decoded

    async def read_resource(self, uri: str) -> JsonValue:
        client = self._require_client()
        result = await client.read_resource(uri)
        valid, decoded = _decode_resource_result(result, self._resource_result_type)
        if not valid:
            raise McpSchemaError()
        return decoded

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        owner, self._owner_task = self._owner_task, None
        close_requested, self._close_requested = self._close_requested, None
        self._client = None
        self._pagination_params = None
        self._tool_result_type = None
        self._resource_result_type = None
        self._config = cast(McpServerConfig, None)
        self._environment_lookup = _empty_environment_lookup
        try:
            if close_requested is not None:
                close_requested.set()
            if owner is None:
                return
            done, _ = await asyncio.wait({owner}, timeout=_remaining(self._cleanup_deadline()))
            if not done:
                _BACKGROUND_SESSION_OWNERS.add(owner)
                owner.add_done_callback(_session_owner_finished)
                raise TimeoutError()
            code = owner.result()
            if code != "ok":
                _public_result((code, None))
        finally:
            self._client = None
            self._pagination_params = None
            self._tool_result_type = None
            self._resource_result_type = None
            self._operation_deadline = None

    def _require_client(self) -> Any:
        if self._closed or self._client is None:
            raise McpClosedSessionError()
        return self._client

    async def _list_capabilities(
        self, method_name: str, collection_name: str, item_attribute: str
    ) -> frozenset[str]:
        client = self._require_client()
        method = getattr(client, method_name, None)
        if not callable(method):
            raise McpProtocolError()
        cursor: str | None = None
        seen_cursors: set[str] = set()
        names: set[str] = set()
        page_count = 0
        while True:
            if page_count >= _MAX_CAPABILITY_PAGES:
                raise McpProtocolError()
            page_count += 1
            if cursor is None:
                result = await method()
            else:
                if self._pagination_params is None:
                    raise McpProtocolError()
                result = await method(params=self._pagination_params(cursor=cursor))
            items = _result_field(result, collection_name)
            page = _capability_names(items, item_attribute)
            if len(names) + len(page) > _MAX_CAPABILITIES or names & page:
                raise McpProtocolError()
            names.update(page)
            next_cursor = _cursor(_result_field(result, "next_cursor", "nextCursor"))
            if next_cursor is None:
                return frozenset(names)
            if next_cursor in seen_cursors:
                raise McpProtocolError()
            seen_cursors.add(next_cursor)
            cursor = next_cursor


@asynccontextmanager
async def _async_client_context(client: Any) -> AsyncIterator[Any]:
    """Support httpx2 clients and deterministic client seams without SDK imports."""
    try:
        if hasattr(client, "__aenter__"):
            async with client:
                yield client
        else:
            yield client
    finally:
        if not hasattr(client, "__aenter__"):
            await client.aclose()


def _capability_names(items: object, attribute: str) -> frozenset[str]:
    if type(items) is not list or len(items) > _MAX_CAPABILITIES:
        raise McpProtocolError()
    names: set[str] = set()
    for item in cast(list[object], items):
        value = getattr(item, attribute, None)
        if value is None and type(item) is dict:
            value = cast(dict[str, object], item).get(attribute)
        name = _name(value)
        if name is None or name in names:
            raise McpProtocolError()
        names.add(name)
    return frozenset(names)


def _result_field(result: object, attribute: str, alias: str | None = None) -> object:
    if type(result) is dict:
        raw = cast(dict[str, object], result)
        return raw.get(alias if alias is not None else attribute, raw.get(attribute))
    return getattr(result, attribute, None)


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    cursor = _name(value)
    if cursor is None:
        raise McpProtocolError()
    return cursor


def create_production_session(
    config: McpServerConfig,
    *,
    sdk_loader: SdkLoader = _load_official_sdk,
    environ: Mapping[str, str] | None = None,
    environment_lookup: EnvironmentLookup | None = None,
) -> _OfficialMcpSession:
    """Construct, but do not connect, one production-owned official SDK adapter."""
    lookup = environment_lookup
    if lookup is None:
        lookup = _system_environment_lookup if environ is None else environ.get
    return _OfficialMcpSession(config, sdk_loader=sdk_loader, environment_lookup=lookup)


def _deadline(timeout_seconds: float) -> float:
    return anyio.current_time() + timeout_seconds


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - anyio.current_time())


class McpRuntime:
    """Open an isolated session for each operation and expose only redacted failures."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory or _production_lease

    @asynccontextmanager
    async def open(self, config: McpServerConfig) -> AsyncIterator[McpSession]:
        """Open one redacted, deadline-bounded session scope with explicit ownership."""
        deadline = _deadline(config.timeout_seconds)
        result = await self._enter_open_result(config, deadline)
        del config
        code, value = result
        if code != "ok":
            _public_result((code, None))
        assert isinstance(value, _OpenState)
        state = value
        try:
            yield state.session
        except BaseException:
            await self._exit_open_ignoring_failure(state)
            del state
            raise
        else:
            finished = await self._exit_open_result(state)
            del state
            _public_result(finished)

    async def inspect_capabilities(
        self, config: McpServerConfig
    ) -> tuple[frozenset[str], frozenset[str]]:
        """Return bounded live names through the same timeout and redaction boundary."""
        result = await self._inspection_result(config)
        del config
        code, value = result
        if code != "ok":
            _public_result((code, None))
        assert type(value) is tuple
        return cast(tuple[frozenset[str], frozenset[str]], value)

    async def call(
        self, config: McpServerConfig, tool_name: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        result = await self._call_result(config, tool_name, arguments)
        del config, tool_name, arguments
        return _public_result(result)

    async def read_resource(self, config: McpServerConfig, uri: str) -> JsonValue:
        result = await self._resource_result(config, uri)
        del config, uri
        return _public_result(result)

    async def validate_binding(self, config: McpServerConfig, binding: ProviderBinding) -> None:
        result = await self._validation_result(config, binding)
        del config, binding
        public = _public_result(result)
        assert public is None

    async def _call_result(
        self, config: McpServerConfig, tool_name: str, arguments: dict[str, JsonValue]
    ) -> tuple[str, object | None]:
        name = _name(tool_name)
        deadline = _deadline(config.timeout_seconds)
        try:
            prepared = detached_json(arguments)
            if name is None or type(prepared) is not dict:
                return ("schema", None)
            with anyio.fail_after(_remaining(deadline)):
                async with self._operation(config, deadline) as session:
                    value = await session.call_tool(name, prepared)
                    if type(value) is dict and (
                        "structuredContent" in cast(dict[str, object], value)
                        or "isError" in cast(dict[str, object], value)
                    ):
                        valid, decoded = _decode_tool_result(value, None)
                    else:
                        valid, decoded = _decoded_json(value)
                    return ("ok", decoded) if valid else ("schema", None)
        except TimeoutError:
            return ("timeout", None)
        except McpError as error:
            code = _mcp_code(error)
            del error
            return (code, None)
        except BaseException as error:  # noqa: BLE001 - cancellation must be preserved.
            code = _exception_code(error)
            del error
            return (code, None)

    async def _resource_result(self, config: McpServerConfig, uri: str) -> tuple[str, object | None]:
        name = _name(uri)
        deadline = _deadline(config.timeout_seconds)
        try:
            if name is None:
                return ("schema", None)
            with anyio.fail_after(_remaining(deadline)):
                async with self._operation(config, deadline) as session:
                    value = await session.read_resource(name)
                    if type(value) is dict and "contents" in cast(dict[str, object], value):
                        valid, decoded = _decode_resource_result(value, None)
                    else:
                        valid, decoded = _decoded_json(value)
                    return ("ok", decoded) if valid else ("schema", None)
        except TimeoutError:
            return ("timeout", None)
        except McpError as error:
            code = _mcp_code(error)
            del error
            return (code, None)
        except BaseException as error:  # noqa: BLE001 - cancellation must be preserved.
            code = _exception_code(error)
            del error
            return (code, None)

    async def _validation_result(
        self, config: McpServerConfig, binding: ProviderBinding
    ) -> tuple[str, object | None]:
        deadline = _deadline(config.timeout_seconds)
        try:
            with anyio.fail_after(_remaining(deadline)):
                async with self._operation(config, deadline) as session:
                    tools = _strict_capability_names(await session.list_tools())
                    resources = _strict_capability_names(await session.list_resources())
                    resource_templates = _strict_capability_names(
                        await session.list_resource_templates()
                    )
                    binding.assert_capabilities(tools, resources, resource_templates)
                    return ("ok", None)
        except BindingValidationError:
            missing = _missing_capability(
                binding,
                locals().get("tools"),
                locals().get("resources"),
                locals().get("resource_templates"),
            )
            return ("capability", missing)
        except TimeoutError:
            return ("timeout", None)
        except McpError as error:
            code = _mcp_code(error)
            del error
            return (code, None)
        except BaseException as error:  # noqa: BLE001 - cancellation must be preserved.
            code = _exception_code(error)
            del error
            return (code, None)

    async def _inspection_result(self, config: McpServerConfig) -> tuple[str, object | None]:
        deadline = _deadline(config.timeout_seconds)
        try:
            with anyio.fail_after(_remaining(deadline)):
                async with self._operation(config, deadline) as session:
                    tools = _strict_capability_names(await session.list_tools())
                    resources = _strict_capability_names(await session.list_resources())
                    resource_templates = _strict_capability_names(
                        await session.list_resource_templates()
                    )
                    return ("ok", (tools, resources | resource_templates))
        except TimeoutError:
            return ("timeout", None)
        except McpError as error:
            code = _mcp_code(error)
            del error
            return (code, None)
        except BaseException as error:  # noqa: BLE001 - cancellation must be preserved.
            code = _exception_code(error)
            del error
            return (code, None)

    @asynccontextmanager
    async def _operation(self, config: McpServerConfig, deadline: float) -> AsyncIterator[McpSession]:
        lease_or_session = self._session_factory(config)
        lease = lease_or_session if isinstance(lease_or_session, SessionLease) else SessionLease(lease_or_session)
        session = lease.session
        started = getattr(session, "start", None)
        active_error = False
        try:
            if isinstance(session, _OfficialMcpSession):
                session._set_operation_deadline(deadline)
            if started is not None:
                await started()
            yield session
        except BaseException:
            active_error = True
            raise
        finally:
            if lease.owned:
                try:
                    if isinstance(session, _OfficialMcpSession):
                        await session.close()
                    else:
                        with anyio.move_on_after(_remaining(deadline), shield=True) as close_scope:
                            await session.close()
                        if close_scope.cancel_called and not active_error:
                            raise McpTimeoutError()
                except BaseException:
                    if not active_error:
                        raise

    async def _enter_open_result(
        self, config: McpServerConfig, deadline: float
    ) -> tuple[str, object | None]:
        manager = self._operation(config, deadline)
        try:
            with anyio.fail_after(_remaining(deadline)):
                session = await manager.__aenter__()
            return "ok", _OpenState(manager, session, deadline)
        except TimeoutError:
            return "timeout", None
        except McpError as error:
            code = _mcp_code(error)
            del error
            return code, None
        except BaseException as error:  # noqa: BLE001 - fixed public error boundary.
            code = _exception_code(error)
            del error
            return code, None

    async def _exit_open_result(self, state: _OpenState) -> tuple[str, object | None]:
        try:
            with anyio.fail_after(_remaining(state.deadline)):
                await state.manager.__aexit__(None, None, None)
            return "ok", None
        except TimeoutError:
            return "timeout", None
        except McpError as error:
            code = _mcp_code(error)
            del error
            return code, None
        except BaseException as error:  # noqa: BLE001 - fixed public error boundary.
            code = _exception_code(error)
            del error
            return code, None

    async def _exit_open_ignoring_failure(self, state: _OpenState) -> None:
        """Close after a caller failure without allowing cleanup to replace that failure."""
        with anyio.move_on_after(_remaining(state.deadline), shield=True):
            try:
                await state.manager.__aexit__(None, None, None)
            except BaseException:  # noqa: BLE001 - caller failure takes precedence.
                return


def _production_lease(config: McpServerConfig) -> SessionLease:
    return SessionLease(create_production_session(config), owned=True)


def _mcp_code(error: McpError) -> str:
    if isinstance(error, McpClosedSessionError):
        return "closed"
    if isinstance(error, McpSchemaError):
        return "schema"
    if isinstance(error, McpPermissionError):
        return "permission"
    if isinstance(error, McpProtocolError):
        return "protocol"
    if isinstance(error, McpTimeoutError):
        return "timeout"
    if isinstance(error, McpCapabilityError):
        return "capability"
    return "transport"


def _exception_code(error: BaseException) -> str:
    if isinstance(error, (KeyboardInterrupt, anyio.get_cancelled_exc_class())):
        raise error
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, PermissionError):
        return "permission"
    error_type = type(error)
    module = error_type.__module__
    name = error_type.__name__
    if module == "mcp.shared.exceptions" and hasattr(error, "code"):
        code = getattr(error, "code", None)
        if code == -32000:
            return "closed"
        if code == -32001:
            return "timeout"
        if code == -32602:
            return "schema"
        return "protocol"
    if module.startswith("anyio") and name in {"BrokenResourceError", "ClosedResourceError"}:
        return "closed"
    if module.startswith("pydantic") and name == "ValidationError":
        return "schema"
    if module.startswith(("httpx2", "httpcore2")):
        if "Timeout" in name:
            return "timeout"
        if name == "HTTPStatusError" and _http_status(error) in {401, 403}:
            return "permission"
        return "transport"
    if module == "mcp.client.streamable_http" and name.endswith("Error"):
        if "Timeout" in name:
            return "timeout"
        if _http_status(error) in {401, 403}:
            return "permission"
        return "transport"
    return "transport"


def _http_status(error: BaseException) -> int | None:
    """Read only the bounded status scalar from one known HTTP failure shape."""
    try:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    except Exception:  # noqa: BLE001 - provider-owned exception properties are untrusted.
        return None
    return status if type(status) is int else None


def _missing_capability(
    binding: ProviderBinding,
    tools: object | None,
    resources: object | None,
    resource_templates: object | None,
) -> str:
    known_tools = tools if isinstance(tools, frozenset) else frozenset()
    known_resources = resources if isinstance(resources, frozenset) else frozenset()
    known_templates = (
        resource_templates if isinstance(resource_templates, frozenset) else frozenset()
    )
    for name in binding.tools.values():
        if name not in known_tools:
            return f"missing bound tool: {name}"
    for name in binding.resources.values():
        known = known_templates if "{" in name or "}" in name else known_resources
        if name not in known:
            return f"missing bound resource: {name}"
    return "MCP capability mismatch"


def _strict_capability_names(values: object) -> frozenset[str]:
    if type(values) is not frozenset or len(cast(frozenset[object], values)) > _MAX_CAPABILITIES:
        raise McpProtocolError()
    names = frozenset(_name(value) for value in cast(frozenset[object], values))
    if None in names or len(names) != len(values):
        raise McpProtocolError()
    return cast(frozenset[str], names)


def _public_result(result: tuple[str, object | None]) -> JsonValue:
    code, value = result
    if code == "ok":
        return cast(JsonValue, value)
    if code == "timeout":
        raise McpTimeoutError() from None
    if code == "schema":
        raise McpSchemaError() from None
    if code == "closed":
        raise McpClosedSessionError() from None
    if code == "permission":
        raise McpPermissionError() from None
    if code == "protocol":
        raise McpProtocolError() from None
    if code == "capability":
        if type(value) is str:
            raise McpCapabilityError(value) from None
        raise McpCapabilityError("MCP capability mismatch") from None
    raise McpTransportError() from None
