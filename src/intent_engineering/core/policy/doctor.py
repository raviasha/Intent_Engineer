"""Fail-closed local workspace health inspection."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from intent_engineering.core.models import Graph, ProjectConfig
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.jsonl.history_store import JsonlHistoryStore
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore


def _kind(path: Path, expected: int) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return not stat.S_ISLNK(metadata.st_mode) and stat.S_IFMT(metadata.st_mode) == expected


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
        config_data = yaml.safe_load(config.read_text(encoding="utf-8"))
        graph_data = yaml.safe_load(graph.read_text(encoding="utf-8"))
        ProjectConfig.model_validate(cast(dict[str, Any], config_data))
        Graph.model_validate(cast(dict[str, Any], graph_data))
    except Exception:  # noqa: BLE001 - health diagnostics intentionally redact parser detail
        diagnostics.extend(("config", "graph"))
    checks = (
        ("evidence", lambda: JsonlEvidenceStore(workspace / "evidence" / "evidence.jsonl")),
        ("cases", lambda: JsonlCaseStore(workspace / "reconciliation" / "cases.jsonl")),
        ("history", lambda: JsonlHistoryStore(workspace / "history" / "changesets.jsonl")),
        (
            "checkpoints",
            lambda: YamlCheckpointStore(workspace / "cache" / "checkpoints.yaml").get("doctor"),
        ),
    )
    for name, check in checks:
        try:
            check()
        except Exception:  # noqa: BLE001 - health diagnostics intentionally redact parser detail
            diagnostics.append(name)
    return not diagnostics, tuple(sorted(set(diagnostics)))
