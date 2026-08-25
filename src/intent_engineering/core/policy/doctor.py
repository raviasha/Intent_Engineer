"""Fail-closed local workspace health inspection."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.models import (
    EvidenceRecord,
    Graph,
    ProjectConfig,
    ReconciliationCase,
    SyncCheckpoint,
)
from intent_engineering.core.models.changeset import ChangeSet


def _kind(path: Path, expected: int) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return not stat.S_ISLNK(metadata.st_mode) and stat.S_IFMT(metadata.st_mode) == expected


def _read_regular(path: Path) -> bytes:
    """Read one canonical file through a no-follow descriptor, never a store lock."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def _open_directory(parent_fd: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise TypeError("not a directory")
    return descriptor


def _read_regular_at(parent_fd: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise TypeError("not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            return source.read()
    finally:
        os.close(descriptor)


def _validate_jsonl_content(
    content: bytes, model: type[EvidenceRecord | ReconciliationCase | ChangeSet]
) -> None:
    for line in content.splitlines():
        if line:
            model.model_validate_json(line)


def _validate_checkpoints_content(content: bytes) -> None:
    loaded = yaml.safe_load(content.decode("utf-8"))
    if not isinstance(loaded, dict) or set(loaded) != {"checkpoints"}:
        raise ValueError("invalid checkpoints")
    records = loaded["checkpoints"]
    if not isinstance(records, dict):
        raise TypeError("invalid checkpoints")
    for connector_id, record in records.items():
        checkpoint = SyncCheckpoint.model_validate(record)
        if checkpoint.connector_id != connector_id:
            raise ValueError("invalid checkpoints")


def inspect_workspace(root: Path) -> tuple[bool, tuple[str, ...]]:
    """Return deterministic non-sensitive diagnostics for local canonical state."""
    diagnostics: list[str] = []
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        workspace_fd = _open_directory(root_fd, ".intent")
    except OSError:
        return False, ("workspace",)
    try:
        directory_fds: dict[str, int] = {}
        for name in ("evidence", "reconciliation", "history", "approvals", "cache"):
            try:
                directory_fds[name] = _open_directory(workspace_fd, name)
            except OSError:
                diagnostics.append(name)
        for name, model in (("config", ProjectConfig), ("graph", Graph)):
            try:
                data = yaml.safe_load(_read_regular_at(workspace_fd, f"{name}.yaml").decode("utf-8"))
                model.model_validate(cast(dict[str, Any], data))
            except Exception:  # noqa: BLE001 - health diagnostics intentionally redact parser detail
                diagnostics.append(name)
        checks: tuple[tuple[str, str, Callable[[bytes], None]], ...] = (
            ("evidence", "evidence.jsonl", lambda content: _validate_jsonl_content(content, EvidenceRecord)),
            ("reconciliation", "cases.jsonl", lambda content: _validate_jsonl_content(content, ReconciliationCase)),
            ("history", "changesets.jsonl", lambda content: _validate_jsonl_content(content, ChangeSet)),
            ("cache", "checkpoints.yaml", _validate_checkpoints_content),
        )
        for directory, filename, check in checks:
            descriptor = directory_fds.get(directory)
            if descriptor is None:
                continue
            try:
                check(_read_regular_at(descriptor, filename))
            except FileNotFoundError:
                continue
            except Exception:  # noqa: BLE001 - health diagnostics intentionally redact parser detail
                diagnostics.append("cases" if directory == "reconciliation" else directory)
    finally:
        for descriptor in locals().get("directory_fds", {}).values():
            os.close(descriptor)
        os.close(workspace_fd)
        os.close(root_fd)
    return not diagnostics, tuple(sorted(set(diagnostics)))
