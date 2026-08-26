"""Local MCP connector discovery, inspection, testing, and sync assembly."""

# ruff: noqa: B008

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

import anyio
import typer

from intent_engineering.capture.base import Connector
from intent_engineering.capture.mcp import (
    McpConnector,
    McpConnectorConfig,
    McpRuntime,
    ProviderProfile,
)
from intent_engineering.capture.mcp.profile_loader import (
    load_connector_config_bytes,
    load_profile_bytes,
)
from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import Runtime, load_runtime
from intent_engineering.storage.secure import UnsafePathError

connectors_app = typer.Typer(help="Inspect and validate local MCP connector bindings.")


class ConnectorConfigurationError(ValueError):
    """Fixed public failure for unavailable or invalid local connector configuration."""


@dataclass(frozen=True)
class ConfiguredConnector:
    """One strictly loaded local binding plus its semantic profile."""

    config: McpConnectorConfig
    profile: ProviderProfile


class ConnectorCatalog:
    """Descriptor-rooted catalog of the project's enabled MCP connector bindings."""

    def __init__(
        self,
        runtime: Runtime,
        configured: tuple[ConfiguredConnector, ...],
        *,
        mcp_runtime: McpRuntime | None = None,
    ) -> None:
        self.runtime = runtime
        self.configured = configured
        self.mcp_runtime = mcp_runtime or McpRuntime()

    def _selected(self, connector_id: str) -> ConfiguredConnector:
        matches = tuple(item for item in self.configured if item.config.id == connector_id)
        if len(matches) != 1:
            raise ConnectorConfigurationError("MCP connector is unavailable")
        return matches[0]

    def summaries(self) -> tuple[dict[str, object], ...]:
        """Return stable, credential-free connector summaries."""
        return tuple(
            {
                "id": item.config.id,
                "profile_id": item.profile.id,
                "profile_version": item.profile.version,
                "transport": item.config.server.transport,
                "enabled": True,
            }
            for item in self.configured
        )

    def inspect(self, connector_id: str) -> dict[str, object]:
        """Render semantic and local capability names without resolved values."""
        item = self._selected(connector_id)
        return {
            "id": item.config.id,
            "profile_id": item.profile.id,
            "profile_version": item.profile.version,
            "transport": item.config.server.transport,
            "object_types": sorted(item.profile.objects),
            "read_operations": sorted(item.profile.operations),
            "write_operations": sorted(item.profile.writes),
            "tools": dict(sorted(item.config.binding.tools.items())),
            "resources": dict(sorted(item.config.binding.resources.items())),
            "environment_names": sorted(item.config.server.environment_refs),
            "header_names": sorted(item.config.server.headers),
        }

    def read_connectors(self, connector_id: str | None = None) -> tuple[Connector, ...]:
        """Construct one read connector per configured profile object type."""
        selected = self.configured if connector_id is None else (self._selected(connector_id),)
        if not selected:
            raise ConnectorConfigurationError("MCP connector is unavailable")
        return tuple(
            cast(
                Connector,
                McpConnector(
                    self.mcp_runtime,
                    config=item.config,
                    profile=item.profile,
                    object_name=object_name,
                    local_actor=self.runtime.config.local_actor,
                ),
            )
            for item in selected
            for object_name in sorted(item.profile.objects)
        )

    async def test(self, connector_id: str) -> dict[str, object]:
        """Validate capabilities and declared reads without invoking any write."""
        item = self._selected(connector_id)
        await self.mcp_runtime.validate_binding(item.config.server, item.config.binding)
        tools, resources = await self.mcp_runtime.inspect_capabilities(item.config.server)
        probes = self.read_connectors(connector_id)
        try:
            for connector in probes:
                await connector.discover(None)
        finally:
            for connector in probes:
                abort = getattr(connector, "abort_sync", None)
                if abort is not None:
                    abort()
        return {
            "profile": item.profile.id,
            "read_ready": all(
                name in tools or name in resources
                for name in (
                    *item.config.binding.tools.values(),
                    *item.config.binding.resources.values(),
                )
                if name
            ),
            "write_ready": all(
                item.config.binding.tools[name] in tools for name in item.profile.writes
            ),
        }


def _configured_result(runtime: Runtime) -> tuple[ConfiguredConnector, ...] | None:
    """Parse configuration internally so document bytes never reach a public traceback."""
    try:
        directory = runtime.workspace_directory.subdirectory("connectors")
        try:
            files = directory.walk_regular_files(".yaml", reject_symlinks=True)
        finally:
            directory.close()
        configured: list[ConfiguredConnector] = []
        seen: set[str] = set()
        for relative, source in files:
            if len(relative.parts) != 1 or not isinstance(relative, PurePosixPath):
                raise ConnectorConfigurationError("invalid MCP connector configuration")
            config = load_connector_config_bytes(source.content)
            if config.id in seen:
                raise ConnectorConfigurationError("invalid MCP connector configuration")
            seen.add(config.id)
            profile_source = runtime.project_directory.read_relative(
                config.profile_path,
                nonblocking=True,
            )
            profile = load_profile_bytes(profile_source.content)
            config.binding.validate_against(profile)
            configured.append(ConfiguredConnector(config, profile))
        return tuple(sorted(configured, key=lambda item: item.config.id))
    except (OSError, TypeError, UnsafePathError, ValueError):
        return None


def _load_configured(runtime: Runtime) -> tuple[ConfiguredConnector, ...]:
    configured = _configured_result(runtime)
    del runtime
    if configured is None:
        raise ConnectorConfigurationError("invalid MCP connector configuration") from None
    return configured


def load_connector_catalog(project: Path) -> ConnectorCatalog:
    """Load one production catalog from the initialized project."""
    return connector_catalog(load_runtime(project))


def connector_catalog(runtime: Runtime) -> ConnectorCatalog:
    """Load connector configuration against the already selected runtime snapshot."""
    return ConnectorCatalog(runtime, _load_configured(runtime))


def configured_actor_principals(runtime: Runtime) -> frozenset[str]:
    """Return only provider principals authenticated for the runtime's local actor."""
    configured = _configured_result(runtime)
    if configured is None:
        return frozenset()
    actor = runtime.config.local_actor
    return frozenset(
        principal
        for item in configured
        for principal in item.config.binding.actor_principals.get(actor, frozenset())
    )


def _catalog_error() -> None:
    typer.echo("intent error: MCP connector operation failed", err=True)
    raise typer.Exit(1)


def _catalog_result(
    project: Path, operation: str, connector_id: str | None
) -> tuple[bool, object, BaseException | None]:
    """Keep configuration/provider errors and their locals behind a non-raising boundary."""
    try:
        catalog = load_connector_catalog(project)
        if operation == "list":
            return True, {"connectors": catalog.summaries()}, None
        if connector_id is None:
            return False, None, None
        if operation == "inspect":
            return True, catalog.inspect(connector_id), None

        async def run() -> dict[str, object]:
            return await catalog.test(connector_id)

        return True, anyio.run(run), None
    except Exception:  # noqa: BLE001 - caller receives only an inert failure bit
        return False, None, None
    except BaseException as error:  # noqa: BLE001 - preserve detached cancellation/interrupt
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        return False, None, error


@connectors_app.command("list")
def list_connectors(
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """List configured local MCP connectors without resolving credentials."""
    ok, payload, abort = _catalog_result(project, "list", None)
    del project
    if abort is not None:
        raise abort
    if not ok:
        del payload
        _catalog_error()
    emit(payload, output_format)


@connectors_app.command("inspect")
def inspect_connector(
    connector_id: str,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Inspect semantic and mapped capability names without environment values."""
    ok, payload, abort = _catalog_result(project, "inspect", connector_id)
    del project, connector_id
    if abort is not None:
        raise abort
    if not ok:
        del payload
        _catalog_error()
    emit(payload, output_format)


@connectors_app.command("test")
def test_connector(
    connector_id: str,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Perform capability checks and non-mutating declared read probes."""
    ok, payload, abort = _catalog_result(project, "test", connector_id)
    del project, connector_id
    if abort is not None:
        raise abort
    if not ok:
        del payload
        _catalog_error()
    emit(payload, output_format)


__all__ = [
    "ConfiguredConnector",
    "ConnectorCatalog",
    "ConnectorConfigurationError",
    "connector_catalog",
    "connectors_app",
    "load_connector_catalog",
]
