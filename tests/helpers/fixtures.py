"""Materialize deterministic, local-only repositories for reconciliation fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import load_runtime, resolve_connectors
from intent_engineering.core.models import Graph, ReconciliationCase
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.sync.models import SyncRunResult

FIXTURES = Path(__file__).parents[1] / "fixtures"
_TIMESTAMP = datetime(2026, 8, 25, tzinfo=UTC)
_GIT_DATE = "2026-08-25T00:00:00+00:00"


@dataclass(frozen=True)
class FixtureRun:
    """One fixture run plus the durable graph and reconciliation projection it produced."""

    sync: SyncRunResult
    cases: tuple[ReconciliationCase, ...]
    graph: Graph


def _side(label: str, claim: str, author: str = "fixture") -> dict[str, Any]:
    return {
        "label": label,
        "claim": claim,
        "evidence_refs": ["$self"],
        "observed_at": _GIT_DATE,
        "authors": [author],
        "confidence": 0.9,
    }


def _detection_input(kind: str, subject: str) -> dict[str, Any]:
    """Build explicit facts that exercise one reviewed detector branch."""
    requirement = _side("requirement", "documented behavior")
    implementation = _side("implementation", "current code")
    test = _side("test", "current verification")
    decision = _side("decision", "approved change")
    payload: dict[str, Any] = {"subject_ref": subject, "affected_refs": [subject]}
    if kind == "aligned":
        payload.update(
            compatibility="aligns",
            requirement_version=1,
            implementation_version=1,
            test_version=1,
            requirement=requirement,
            implementation=implementation,
            test=test,
        )
    elif kind == "code_lag":
        payload.update(
            compatibility="aligns",
            requirement_version=2,
            implementation_version=1,
            test_version=1,
            requirement=requirement,
            implementation=implementation,
            test=test,
        )
    elif kind == "requirement_lag":
        payload.update(
            compatibility="aligns",
            requirement_version=1,
            decision_version=2,
            implementation_version=2,
            test_version=2,
            requirement=requirement,
            decision=decision,
            implementation=implementation,
            test=test,
        )
    elif kind == "test_lag":
        payload.update(
            compatibility="aligns",
            requirement_version=2,
            implementation_version=2,
            test_version=1,
            requirement=requirement,
            implementation=implementation,
            test=test,
        )
    elif kind == "undocumented_code":
        payload.update(
            compatibility="aligns",
            material_code_change=True,
            has_mapped_semantics=False,
            implementation=implementation,
        )
    elif kind == "ambiguous_divergence":
        payload.update(
            compatibility="unknown", requirement=requirement, implementation=implementation
        )
    elif kind == "cross_author_conflict":
        payload.update(
            compatibility="contradicts",
            requirement=_side("requirement", "first position", "author-a"),
            decision=_side("decision", "second position", "author-b"),
        )
    else:
        raise ValueError(f"unknown fixture kind: {kind}")
    return payload


def _assertion(subject: str, label: str) -> dict[str, Any]:
    return {
        "id": f"assertion:{subject}",
        "subject_id": subject,
        "change_kind": "confirm",
        "node_type": "REQUIREMENT",
        "label": label,
        "source_mode": "explicit",
        "evidence_refs": ["$self"],
        "confidence": 0.9,
    }


def _write_markdown(path: Path, metadata: Mapping[str, Any], title: str) -> None:
    front_matter = yaml.safe_dump({"intent_engineering": dict(metadata)}, sort_keys=True)
    path.write_text(f"---\n{front_matter}---\n# {title}\n", encoding="utf-8")
    timestamp = _TIMESTAMP.timestamp()
    os.utime(path, (timestamp, timestamp))


def _read_fixture(path: Path) -> Mapping[str, str]:
    loaded = yaml.safe_load((path / "fixture.yaml").read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping) or not all(
        isinstance(loaded.get(key), str) for key in ("kind", "subject")
    ):
        raise ValueError(f"fixture descriptor is invalid: {path.name}")
    return cast(Mapping[str, str], loaded)


def _commit_fixture_repository(project: Path) -> None:
    environment = {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"}
    environment.update({"GIT_AUTHOR_DATE": _GIT_DATE, "GIT_COMMITTER_DATE": _GIT_DATE})
    for command in (
        ("git", "init", "--quiet"),
        ("git", "config", "user.name", "Intent Fixture"),
        ("git", "config", "user.email", "fixture@example.test"),
        ("git", "add", "--", "*.md"),
        ("git", "commit", "--quiet", "-m", "fixture"),
    ):
        subprocess.run(command, cwd=project, env=environment, check=True, capture_output=True)


def materialize_fixture_repository(path: Path, destination: Path) -> Path:
    """Copy a descriptor into a tiny deterministic Git repository without tracking ``.git``."""
    descriptor = _read_fixture(path)
    project = destination / path.name
    shutil.copytree(path, project)
    initialize_project(project)
    (project / ".intent" / "evidence" / "evidence.jsonl").write_text("", encoding="utf-8")
    (project / ".intent" / "reconciliation" / "cases.jsonl").write_text("", encoding="utf-8")
    kind, subject = descriptor["kind"], descriptor["subject"]
    if kind == "cross_author_conflict":
        _write_markdown(
            project / "first.md",
            {"intent_assertion": _assertion("requirement:first", "First")},
            "First",
        )
        _write_markdown(
            project / "second.md",
            {"intent_assertion": _assertion("decision:second", "Second")},
            "Second",
        )
        metadata: dict[str, Any] = {"detection_input": _detection_input(kind, subject)}
    else:
        metadata = {
            "intent_assertion": _assertion(f"node:{path.name}", f"{path.name} assertion"),
            "detection_input": _detection_input(kind, subject),
        }
    _write_markdown(project / "fixture.md", metadata, path.name)
    _commit_fixture_repository(project)
    return project


def _run(
    path: Path, *, second: bool
) -> tuple[SyncRunResult, SyncRunResult | None, tuple[ReconciliationCase, ...], Graph]:
    with tempfile.TemporaryDirectory(prefix="intent-fixture-") as temporary:
        project = materialize_fixture_repository(path, Path(temporary))
        runtime = load_runtime(project)
        connectors = resolve_connectors(runtime, "markdown,git")
        first = anyio.run(runtime.sync.run, "fixture-run", connectors)
        second_result = anyio.run(runtime.sync.run, "fixture-run-2", connectors) if second else None
        return first, second_result, runtime.cases(), runtime.graph_store.load()


def run_fixture(path: Path) -> FixtureRun:
    """Execute a fresh materialized fixture through the production local runtime."""
    sync, _, cases, graph = _run(path, second=False)
    return FixtureRun(sync=sync, cases=cases, graph=graph)


def run_fixture_twice(
    path: Path,
) -> tuple[SyncRunResult, SyncRunResult, tuple[ReconciliationCase, ...]]:
    """Run the same materialized repository twice to prove checkpoint idempotency."""
    first, second, cases, _ = _run(path, second=True)
    assert second is not None
    return first, second, cases
