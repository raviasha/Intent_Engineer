"""Materialize deterministic, local-only repositories for reconciliation fixtures."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import anyio
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import load_runtime, resolve_connectors
from intent_engineering.core.models import EvidenceRecord, Graph, ReconciliationCase
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.sync.models import SyncRunResult

FIXTURES = Path(__file__).parents[1] / "fixtures"
_FIXTURE_DATES = {
    "aligned": {
        "requirement": "2026-08-21T00:00:00+00:00",
        "implementation": "2026-08-22T00:00:00+00:00",
        "test": "2026-08-23T00:00:00+00:00",
        "decision": "2026-08-24T00:00:00+00:00",
    },
    "code_lag": {
        "requirement": "2026-08-25T00:00:00+00:00",
        "implementation": "2026-08-22T00:00:00+00:00",
        "test": "2026-08-23T00:00:00+00:00",
        "decision": "2026-08-24T00:00:00+00:00",
    },
    "requirement_lag": {
        "requirement": "2026-08-20T00:00:00+00:00",
        "decision": "2026-08-23T00:00:00+00:00",
        "implementation": "2026-08-24T00:00:00+00:00",
        "test": "2026-08-25T00:00:00+00:00",
    },
    "test_lag": {
        "requirement": "2026-08-21T00:00:00+00:00",
        "implementation": "2026-08-24T00:00:00+00:00",
        "test": "2026-08-23T00:00:00+00:00",
        "decision": "2026-08-22T00:00:00+00:00",
    },
}
_DEFAULT_DATES = _FIXTURE_DATES["aligned"]


@dataclass(frozen=True)
class FixtureRun:
    """One fixture run plus the durable graph and reconciliation projection it produced."""

    sync: SyncRunResult
    cases: tuple[ReconciliationCase, ...]
    graph: Graph
    evidence: tuple[EvidenceRecord, ...]


def _side(label: str, claim: str, evidence_ref: str) -> dict[str, Any]:
    return {
        "label": label,
        "claim": claim,
        "evidence_refs": [evidence_ref],
        "confidence": 0.9,
    }


def _detection_input(kind: str, subject: str) -> dict[str, Any]:
    """Build explicit facts that exercise one reviewed detector branch."""
    requirement = _side("requirement", "documented behavior", "$self")
    implementation = _side(
        "implementation",
        "current code",
        "git-path:src/implementation.py",
    )
    test = _side("test", "current verification", "git-path:tests/test_implementation.py")
    decision = _side("decision", "approved change", "git-path:docs/decision.txt")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "subject_ref": subject,
        "affected_refs": [subject],
    }
    if kind in {"aligned", "code_lag"}:
        payload.update(
            compatibility="aligns",
            requirement=requirement,
            implementation=implementation,
            test=test,
        )
    elif kind == "requirement_lag":
        payload.update(
            compatibility="aligns",
            requirement=requirement,
            decision=decision,
            implementation=implementation,
            test=test,
        )
    elif kind == "test_lag":
        payload.update(
            compatibility="aligns",
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
            requirement=_side("requirement", "first position", "$self"),
            decision=_side(
                "decision",
                "second position",
                "git-path:docs/decision.txt",
            ),
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


def _write_markdown(
    path: Path,
    metadata: Mapping[str, Any],
    title: str,
    observed_at: datetime,
) -> None:
    front_matter = yaml.safe_dump({"intent_engineering": dict(metadata)}, sort_keys=True)
    path.write_text(f"---\n{front_matter}---\n# {title}\n", encoding="utf-8")
    timestamp = observed_at.timestamp()
    os.utime(path, (timestamp, timestamp))


def _read_fixture(path: Path) -> Mapping[str, str]:
    loaded = yaml.safe_load((path / "fixture.yaml").read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping) or not all(
        isinstance(loaded.get(key), str) for key in ("kind", "subject")
    ):
        raise ValueError(f"fixture descriptor is invalid: {path.name}")
    return cast(Mapping[str, str], loaded)


def _git(project: Path, *arguments: str, environment: Mapping[str, str] | None = None) -> None:
    base_environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
    }
    if environment is not None:
        base_environment.update(environment)
    subprocess.run(
        ("git", *arguments),
        cwd=project,
        env=base_environment,
        check=True,
        capture_output=True,
    )


def _commit_path(
    project: Path,
    path: str,
    message: str,
    *,
    author_name: str,
    author_email: str,
    date: str,
) -> None:
    _git(project, "add", "--", path)
    identity = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    _git(project, "commit", "--quiet", "-m", message, environment=identity)


def _commit_fixture_repository(project: Path, kind: str) -> None:
    """Create independent code, test, decision, and Markdown evidence revisions."""
    dates = _FIXTURE_DATES.get(kind, _DEFAULT_DATES)
    _git(project, "init", "--quiet")
    (project / "src").mkdir()
    (project / "src" / "implementation.py").write_text(
        "def export() -> str:\n    return 'legacy'\n",
        encoding="utf-8",
    )
    _commit_path(
        project,
        "src/implementation.py",
        "implement export",
        author_name="Implementation Author",
        author_email="implementation@example.test",
        date=dates["implementation"],
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_implementation.py").write_text(
        "def test_export_contract() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _commit_path(
        project,
        "tests/test_implementation.py",
        "verify export",
        author_name="Test Author",
        author_email="tests@example.test",
        date=dates["test"],
    )
    (project / "docs").mkdir()
    (project / "docs" / "decision.txt").write_text(
        "Approved export decision.\n",
        encoding="utf-8",
    )
    _commit_path(
        project,
        "docs/decision.txt",
        "record decision",
        author_name="Decision Author",
        author_email="decision@example.test",
        date=dates["decision"],
    )
    _commit_path(
        project,
        "fixture.md",
        "revise requirement",
        author_name="Product Author",
        author_email="product@example.test",
        date=dates["requirement"],
    )


def materialize_fixture_repository(path: Path, destination: Path) -> Path:
    """Copy a descriptor into a tiny deterministic Git repository without tracking ``.git``."""
    descriptor = _read_fixture(path)
    project = destination / path.name
    shutil.copytree(path, project)
    initialize_project(project)
    (project / ".intent" / "evidence" / "evidence.jsonl").write_text("", encoding="utf-8")
    (project / ".intent" / "reconciliation" / "cases.jsonl").write_text("", encoding="utf-8")
    kind, subject = descriptor["kind"], descriptor["subject"]
    metadata: dict[str, Any] = {
        "intent_assertion": _assertion(subject, f"{path.name} assertion"),
        "detection_input": _detection_input(kind, subject),
    }
    dates = _FIXTURE_DATES.get(kind, _DEFAULT_DATES)
    _write_markdown(
        project / "fixture.md",
        metadata,
        path.name,
        datetime.fromisoformat(dates["requirement"]),
    )
    _commit_fixture_repository(project, kind)
    return project


def _run(
    path: Path,
    *,
    second: bool,
    sources: str = "markdown,git",
) -> tuple[
    SyncRunResult,
    SyncRunResult | None,
    tuple[ReconciliationCase, ...],
    Graph,
    tuple[EvidenceRecord, ...],
]:
    with tempfile.TemporaryDirectory(prefix="intent-fixture-") as temporary:
        project = materialize_fixture_repository(path, Path(temporary).resolve())
        runtime = load_runtime(project)
        connectors = resolve_connectors(runtime, sources)
        first = anyio.run(runtime.sync.run, "fixture-run", connectors)
        second_result = anyio.run(runtime.sync.run, "fixture-run-2", connectors) if second else None
        return first, second_result, runtime.cases(), runtime.graph_store.load(), runtime.evidence()


def run_fixture(path: Path) -> FixtureRun:
    """Execute a fresh materialized fixture through the production local runtime."""
    sync, _, cases, graph, evidence = _run(path, second=False)
    return FixtureRun(sync=sync, cases=cases, graph=graph, evidence=evidence)


def run_fixture_with_sources(path: Path, sources: str) -> FixtureRun:
    """Execute one fixture with an explicit production connector selection."""
    sync, _, cases, graph, evidence = _run(path, second=False, sources=sources)
    return FixtureRun(sync=sync, cases=cases, graph=graph, evidence=evidence)


def run_fixture_twice(
    path: Path,
) -> tuple[SyncRunResult, SyncRunResult, tuple[ReconciliationCase, ...]]:
    """Run the same materialized repository twice to prove checkpoint idempotency."""
    first, second, cases, _, _ = _run(path, second=True)
    assert second is not None
    return first, second, cases
