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


def _validate_jsonl(
    path: Path, model: type[EvidenceRecord | ReconciliationCase | ChangeSet]
) -> None:
    content = _read_regular(path)
    for line in content.splitlines():
        if line:
            model.model_validate_json(line)


def _validate_checkpoints(path: Path) -> None:
    loaded = yaml.safe_load(_read_regular(path).decode("utf-8"))
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
    workspace = root.resolve() / ".intent"
    diagnostics: list[str] = []
    if not _kind(workspace, stat.S_IFDIR):
        return False, ("workspace",)
    config = workspace / "config.yaml"
    graph = workspace / "graph.yaml"
    for name, path in (("config", config), ("graph", graph)):
        if not _kind(path, stat.S_IFREG):
            diagnostics.append(name)
    for name in ("evidence", "reconciliation", "history", "approvals", "cache"):
        if not _kind(workspace / name, stat.S_IFDIR):
            diagnostics.append(name)
    if diagnostics:
        return False, tuple(diagnostics)
    try:
        config_data = yaml.safe_load(_read_regular(config).decode("utf-8"))
        ProjectConfig.model_validate(cast(dict[str, Any], config_data))
    except Exception:  # noqa: BLE001 - health diagnostics intentionally redact parser detail
        diagnostics.append("config")
    try:
        graph_data = yaml.safe_load(_read_regular(graph).decode("utf-8"))
        Graph.model_validate(cast(dict[str, Any], graph_data))
    except Exception:  # noqa: BLE001 - health diagnostics intentionally redact parser detail
        diagnostics.append("graph")
    state_paths = (
        ("evidence", workspace / "evidence" / "evidence.jsonl"),
        ("cases", workspace / "reconciliation" / "cases.jsonl"),
        ("history", workspace / "history" / "changesets.jsonl"),
        ("checkpoints", workspace / "cache" / "checkpoints.yaml"),
    )
    present_state = {name for name, path in state_paths if path.exists() or path.is_symlink()}
    invalid_state = {
        name
        for name, path in state_paths
        if name in present_state and not _kind(path, stat.S_IFREG)
    }
    diagnostics.extend(sorted(invalid_state))
    checks: tuple[tuple[str, Callable[[], None]], ...] = (
        (
            "evidence",
            lambda: _validate_jsonl(workspace / "evidence" / "evidence.jsonl", EvidenceRecord),
        ),
        (
            "cases",
            lambda: _validate_jsonl(
                workspace / "reconciliation" / "cases.jsonl", ReconciliationCase
            ),
        ),
        ("history", lambda: _validate_jsonl(workspace / "history" / "changesets.jsonl", ChangeSet)),
        (
            "checkpoints",
            lambda: _validate_checkpoints(workspace / "cache" / "checkpoints.yaml"),
        ),
    )
    for name, check in checks:
        if name not in present_state or name in invalid_state:
            continue
        try:
            check()
        except Exception:  # noqa: BLE001 - health diagnostics intentionally redact parser detail
            diagnostics.append(name)
    return not diagnostics, tuple(sorted(set(diagnostics)))
