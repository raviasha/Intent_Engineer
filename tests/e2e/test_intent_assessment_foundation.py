"""Executable release journey for the explainable assessment foundation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import structlog
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import load_assessment_runtime, load_runtime
from intent_engineering.core.models import EvidenceRecord, Graph, JsonValue
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.storage.yaml.graph_store import parse_graph, serialize_graph
from tests.helpers.cli import init_git_repo, run_intent

ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = ROOT / "tests/fixtures/assessment/rubric-v1.yaml"


@dataclass(frozen=True)
class _AssessmentView:
    semantic_digest: str
    graph_version: int


class AssessmentReleaseHarness:
    """Drive CLI, MCP, and CI adapters over one unchanged canonical state."""

    def __init__(self, root: Path) -> None:
        payload = yaml.safe_load(_FIXTURE.read_text(encoding="utf-8"))
        head_graph = Graph.model_validate(payload["graph"])
        self.project = init_git_repo(root)
        self._seed(self.project, head_graph, payload["evidence"])
        base_root = root / "comparison"
        base_root.mkdir()
        self.base_project = init_git_repo(base_root)
        self._seed(
            self.base_project,
            head_graph.model_copy(update={"version": head_graph.version - 1}),
            payload["evidence"],
        )
        self.before_bytes = self.canonical_bytes()
        self._cli_report: bytes | None = None

    @staticmethod
    def _seed(project: Path, graph: Graph, evidence: object) -> None:
        initialized = run_intent(project, "init")
        assert initialized.returncode == 0, initialized.stderr
        runtime = load_runtime(project)
        try:
            (project / ".intent/graph.yaml").write_bytes(serialize_graph(graph))
            assert isinstance(evidence, list)
            for item in evidence:
                runtime.evidence_store.put(EvidenceRecord.model_validate(item))
        finally:
            runtime.close()

    def canonical_bytes(self) -> dict[str, bytes]:
        """Return every durable canonical byte, excluding process-local locks."""
        return {
            path.relative_to(self.project).as_posix(): path.read_bytes()
            for path in sorted((self.project / ".intent").rglob("*"))
            if path.is_file() and not path.name.endswith(".lock")
        }

    def graph(self) -> Graph:
        return parse_graph((self.project / ".intent/graph.yaml").read_bytes())

    def cli_assess(self) -> _AssessmentView:
        result = run_intent(self.project, "assess", "--project", ".", "--format", "json")
        assert result.returncode == 0, result.stderr
        self._cli_report = result.stdout.encode("utf-8")
        payload = cast(dict[str, JsonValue], json.loads(result.stdout))
        assessment = cast(dict[str, JsonValue], payload["assessment"])
        return _AssessmentView(
            semantic_digest=cast(str, payload["semantic_digest"]),
            graph_version=cast(int, assessment["graph_version"]),
        )

    def mcp_assess(self) -> _AssessmentView:
        async def read() -> dict[str, JsonValue]:
            runtime = load_assessment_runtime(self.project)
            try:
                result = await build_server(McpReadServices(runtime)).call_tool(
                    "intent_assessment_summary", {}
                )
                return cast(dict[str, JsonValue], result.structured_content)
            finally:
                runtime.close()

        payload = asyncio.run(read())
        assessment = cast(dict[str, JsonValue], payload["assessment"])
        return _AssessmentView(
            semantic_digest=cast(str, payload["semantic_digest"]),
            graph_version=cast(int, assessment["graph_version"]),
        )

    def ci_assess(self) -> _AssessmentView:
        base = run_intent(self.base_project, "assess", "--project", ".", "--format", "json")
        assert base.returncode == 0, base.stderr
        head = run_intent(self.project, "assess", "--project", ".", "--format", "json")
        assert head.returncode == 0, head.stderr
        (self.project / "assessment-base.json").write_text(base.stdout, encoding="utf-8")
        (self.project / "assessment-head.json").write_text(head.stdout, encoding="utf-8")
        result = run_intent(
            self.project,
            "assessment-gate",
            "--project",
            ".",
            "--base-report",
            "assessment-base.json",
            "--head-report",
            "assessment-head.json",
            "--format",
            "json",
        )
        assert result.returncode == 0, result.stderr
        payload = cast(dict[str, JsonValue], json.loads(result.stdout))
        gate = cast(dict[str, JsonValue], payload["gate"])
        head_payload = cast(dict[str, JsonValue], json.loads(head.stdout))
        assessment = cast(dict[str, JsonValue], head_payload["assessment"])
        return _AssessmentView(
            semantic_digest=cast(str, gate["head_assessment_digest"]),
            graph_version=cast(int, assessment["graph_version"]),
        )


@pytest.fixture(autouse=True)
def _reset_cli_logging(request: pytest.FixtureRequest) -> None:
    request.addfinalizer(structlog.reset_defaults)


@pytest.fixture
def assessment_harness(tmp_path: Path) -> Iterator[AssessmentReleaseHarness]:
    yield AssessmentReleaseHarness(tmp_path)


def test_one_snapshot_has_identical_cli_mcp_and_ci_assessment(
    assessment_harness: AssessmentReleaseHarness,
) -> None:
    """Catches adapter-specific scoring or any assessment write to canonical state."""
    cli = assessment_harness.cli_assess()
    mcp = assessment_harness.mcp_assess()
    gate = assessment_harness.ci_assess()
    assert assessment_harness._cli_report is not None
    cli_payload = cast(
        dict[str, JsonValue], json.loads(assessment_harness._cli_report.decode("utf-8"))
    )
    base_payload = cast(
        dict[str, JsonValue],
        json.loads(
            (assessment_harness.project / "assessment-base.json").read_text(encoding="utf-8")
        ),
    )
    head_payload = cast(
        dict[str, JsonValue],
        json.loads(
            (assessment_harness.project / "assessment-head.json").read_text(encoding="utf-8")
        ),
    )

    assert cli.semantic_digest == mcp.semantic_digest == gate.semantic_digest
    assert base_payload["semantic_digest"] != head_payload["semantic_digest"]
    assert head_payload["semantic_digest"] == gate.semantic_digest
    assert (
        cast(dict[str, JsonValue], head_payload["assessment"])["generated_at"]
        != cast(dict[str, JsonValue], cli_payload["assessment"])["generated_at"]
    )
    assert cli.graph_version == mcp.graph_version == assessment_harness.graph().version
    assert assessment_harness.canonical_bytes() == assessment_harness.before_bytes
    assert (ROOT / "docs/assessment.md").is_file()
