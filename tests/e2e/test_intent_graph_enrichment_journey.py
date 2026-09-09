"""Executable release journey for assessment and governed graph enrichment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]
from mcp.types import CallToolResult

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.control_plane import ControlPlaneService
from intent_engineering.core.models import ProjectConfig
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.enrichment_models import EnrichmentSession
from intent_engineering.storage.yaml.graph_store import serialize_graph
from tests.integration.intent_workflow.test_enrichment import (
    _clarification_proposal,
    _evidence,
    _evidence_bytes,
    _graph,
)

_ORIGIN = "http://localhost:43127"
pytestmark = pytest.mark.anyio


def _initialize(project: Path) -> Path:
    initialized = initialize_project(project)
    config = ProjectConfig(project_id="project:enrichment", local_actor="local:asha")
    initialized.config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    evidence = _evidence()
    initialized.graph_path.write_bytes(serialize_graph(_graph(evidence.id)))
    (initialized.workspace / "evidence/evidence.jsonl").write_bytes(_evidence_bytes(evidence))
    (initialized.workspace / "approvals/policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local:asha"],
                "approvers": ["local:asha"],
                "executors": ["local:asha"],
                "identities": {"local:asha": ["local:asha"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return initialized.graph_path


def _durable_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".intent").rglob("*"))
        if path.is_file() and not path.name.endswith(".lock")
    }


async def test_assess_improve_restart_and_propose_journey_keeps_graph_governed(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    graph_path = _initialize(project)
    graph_before = graph_path.read_bytes()
    answer = "Workspace owners"

    runtime = load_runtime(project)
    control: ControlPlaneService | None = None
    session_id = ""
    evidence_ref = ""
    try:
        control = ControlPlaneService(runtime, origin=_ORIGIN)
        assessment = control.assessment()
        assessment_report = cast(dict[str, object], assessment["assessment"])
        page = cast(dict[str, object], assessment["page"])
        scorecards = {
            cast(str, row["node_id"]): row
            for row in cast(list[dict[str, object]], assessment_report["nodes"])
        }
        table_rows = cast(list[dict[str, object]], page["rows"])
        assert table_rows
        assert all(scorecards[cast(str, row["node_id"])] == row for row in table_rows)

        started = control.enrichment_start(5, None)
        started_session = cast(dict[str, object], started["session"])
        question = cast(dict[str, object], started["question"])
        session_id = cast(str, started_session["id"])
        answered = control.enrichment_answer(
            session_id,
            cast(str, question["gap_id"]),
            answer,
        )
        answered_session = cast(dict[str, object], answered["session"])
        evidence_ref = cast(list[str], answered_session["answer_evidence_refs"])[0]
        assert runtime.evidence_store.get(evidence_ref).payload == {
            "role": "human",
            "content": answer,
        }

        detached = EnrichmentSession.model_validate_json(
            json.dumps(answered_session, separators=(",", ":"), sort_keys=True),
            strict=True,
        )
        _coordinator, submission = _clarification_proposal(runtime, detached)
        proposal = control.enrichment_propose(session_id, submission)
        proposal_record = cast(dict[str, object], proposal["proposal"])
        assert (
            proposal_record["id"]
            == runtime.intent_proposals.get(cast(str, proposal_record["id"])).id
        )
        assert graph_path.read_bytes() == graph_before

        paused = control.enrichment_pause(session_id)
        assert cast(dict[str, object], paused["session"])["status"] == "paused"
    finally:
        if control is not None:
            control.close()
        runtime.close()

    restarted = load_runtime(project)
    restarted_control: ControlPlaneService | None = None
    try:
        restarted_control = ControlPlaneService(restarted, origin=_ORIGIN)
        resumed = restarted_control.enrichment_resume(session_id)
        resumed_session = cast(dict[str, object], resumed["session"])
        assert evidence_ref in cast(list[str], resumed_session["answer_evidence_refs"])

        before_mcp = _durable_bytes(project)
        server = build_server(McpReadServices(restarted))
        status = cast(
            dict[str, object],
            cast(
                CallToolResult,
                await server.call_tool("intent_enrichment_status", {"session_id": session_id}),
            ).structured_content,
        )
        next_question = cast(
            dict[str, object],
            cast(
                CallToolResult,
                await server.call_tool(
                    "intent_enrichment_next_question",
                    {"session_id": session_id},
                ),
            ).structured_content,
        )
        assert status["session"] == resumed_session
        projected_question = cast(dict[str, object], next_question["question"])
        assert projected_question["gap_id"] == resumed_session["current_gap_id"]
        assert answer not in json.dumps(status, sort_keys=True)
        assert answer not in json.dumps(next_question, sort_keys=True)
        assert _durable_bytes(project) == before_mcp
    finally:
        if restarted_control is not None:
            restarted_control.close()
        restarted.close()

    assert graph_path.read_bytes() == graph_before
    assert answer.encode() in (project / ".intent/evidence/evidence.jsonl").read_bytes()
    assert (
        answer.encode() not in (project / ".intent/history/enrichment-sessions.jsonl").read_bytes()
    )
