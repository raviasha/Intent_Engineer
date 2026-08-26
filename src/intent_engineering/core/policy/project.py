"""No-follow creation and validation rules for the local ``.intent`` workspace."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.models import Graph, ProjectConfig

_REQUIRED_DIRECTORIES = ("evidence", "reconciliation", "history", "approvals", "cache")
_DIRECTORIES = (*_REQUIRED_DIRECTORIES, "connectors")
_STATE_FILES = (
    ("evidence", "evidence.jsonl"),
    ("reconciliation", "cases.jsonl"),
    ("history", "changesets.jsonl"),
    ("cache", "checkpoints.yaml"),
    ("approvals", "plans.jsonl"),
    ("approvals", "approvals.jsonl"),
    ("approvals", "receipts.jsonl"),
    ("approvals", "policy.yaml"),
)
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


class ProjectNotInitialized(RuntimeError):
    """Raised when a command needs a local workspace that does not exist."""


class ProjectAlreadyInitialized(RuntimeError):
    """Raised when initialization would overwrite stateful local workspace data."""


@dataclass(frozen=True)
class InitializedProject:
    """The canonical paths produced by one successful initialization."""

    root: Path
    workspace: Path
    config_path: Path
    graph_path: Path


def workspace_path(root: Path) -> Path:
    """Return the lexical workspace path below the selected, resolved project root."""
    return Path(os.path.abspath(root)) / ".intent"


def _yaml_bytes(value: dict[str, Any]) -> bytes:
    return cast(str, yaml.safe_dump(value, allow_unicode=True, sort_keys=True)).encode("utf-8")


def _initial_graph(project_id: str) -> Graph:
    return Graph(
        id=f"graph:{project_id}",
        version=0,
        name=project_id,
        purpose="Local evidence-backed intent graph",
        nodes=(),
        edges=(),
    )


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise ProjectAlreadyInitialized("local workspace has an unsafe path") from error


def _read_regular(parent_fd: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProjectAlreadyInitialized("local workspace has an unsafe path")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _replace_regular(parent_fd: int, name: str, content: bytes) -> None:
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o644,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _is_complete_valid(workspace_fd: int) -> bool:
    descriptors: tuple[int, ...] = ()
    try:
        config_data = yaml.safe_load(_read_regular(workspace_fd, "config.yaml").decode("utf-8"))
        graph_data = yaml.safe_load(_read_regular(workspace_fd, "graph.yaml").decode("utf-8"))
        if not isinstance(config_data, dict) or not isinstance(graph_data, dict):
            return False
        ProjectConfig.model_validate(cast(dict[str, Any], config_data))
        Graph.model_validate(cast(dict[str, Any], graph_data))
        descriptors = tuple(
            os.open(name, _DIRECTORY_FLAGS, dir_fd=workspace_fd) for name in _REQUIRED_DIRECTORIES
        )
        return True
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return False
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _state_is_empty(workspace_fd: int) -> bool:
    for directory_name, filename in _STATE_FILES:
        try:
            directory_fd = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=workspace_fd)
        except OSError:
            continue
        try:
            try:
                if _read_regular(directory_fd, filename):
                    return False
            except FileNotFoundError:
                continue
        finally:
            os.close(directory_fd)
    try:
        connector_fd = os.open("connectors", _DIRECTORY_FLAGS, dir_fd=workspace_fd)
    except OSError:
        return True
    try:
        return not os.listdir(connector_fd)
    finally:
        os.close(connector_fd)


def initialize_project(root: Path, *, force: bool = False) -> InitializedProject:
    """Create or safely repair local state without traversing workspace links."""
    root = Path(os.path.abspath(root))
    workspace = workspace_path(root)
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        try:
            workspace_fd = os.open(".intent", _DIRECTORY_FLAGS, dir_fd=root_fd)
            existed = True
        except FileNotFoundError:
            os.mkdir(".intent", 0o755, dir_fd=root_fd)
            workspace_fd = os.open(".intent", _DIRECTORY_FLAGS, dir_fd=root_fd)
            existed = False
        except OSError as error:
            raise ProjectAlreadyInitialized("local workspace has an unsafe path") from error
        try:
            if existed and _is_complete_valid(workspace_fd):
                connector_fd = _open_or_create_directory(workspace_fd, "connectors")
                os.close(connector_fd)
                return InitializedProject(
                    root, workspace, workspace / "config.yaml", workspace / "graph.yaml"
                )
            if existed and not force:
                raise ProjectAlreadyInitialized("local workspace is incomplete or conflicting")
            if existed and force and not _state_is_empty(workspace_fd):
                raise ProjectAlreadyInitialized("local workspace contains durable state")
            directory_fds = tuple(
                _open_or_create_directory(workspace_fd, name) for name in _DIRECTORIES
            )
            try:
                config = ProjectConfig(
                    project_id=root.name or "project",
                    local_actor="local",
                    source_exclusions=(".intent/**", ".git/**"),
                )
                graph = _initial_graph(config.project_id)
                _replace_regular(
                    workspace_fd, "config.yaml", _yaml_bytes(config.model_dump(mode="json"))
                )
                _replace_regular(
                    workspace_fd,
                    "graph.yaml",
                    _yaml_bytes(graph.model_dump(mode="json", by_alias=True)),
                )
            finally:
                for descriptor in directory_fds:
                    os.close(descriptor)
        finally:
            os.close(workspace_fd)
    finally:
        os.close(root_fd)
    return InitializedProject(root, workspace, workspace / "config.yaml", workspace / "graph.yaml")
