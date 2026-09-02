"""Descriptor-backed integration fixtures for assessment snapshot acquisition."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.cli.runtime import Runtime, load_runtime
from intent_engineering.core.models import (
    Edge,
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    Node,
    NodeType,
    ProjectConfig,
    RelationType,
    SourceMode,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.yaml.graph_store import parse_graph, serialize_graph

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _evidence(evidence_id: str, *, acl: tuple[str, ...], marker: str) -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence_id,
        connector_type="markdown",
        external_object_id=f"docs/{evidence_id}.md",
        external_version="1",
        author="product:priya",
        observed_at=NOW,
        source_locator=f"docs/{evidence_id}.md",
        content_hash=hashlib.sha256(marker.encode()).hexdigest(),
        payload={"content": marker},
        acl=acl,
    )


def _node(node_id: str, node_type: NodeType, evidence_id: str, label: str) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label=label,
        status="active",
        created_by="product:priya",
        created_at=NOW,
        last_modified_by="product:priya",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT,
        intent_fidelity_confidence=0.95,
        confidence_basis="Fixture evidence",
        last_reassessed_at=NOW,
        evidence_refs=(evidence_id,),
    )


def _edge(edge_id: str, from_id: str, to_id: str) -> Edge:
    return Edge(
        id=edge_id,
        from_id=from_id,
        relation=RelationType.REFINES,
        to_id=to_id,
        status="active",
        created_by="product:priya",
        created_at=NOW,
        last_modified_by="product:priya",
        last_modified_at=NOW,
    )


def _evidence_bytes(records: tuple[EvidenceRecord, ...]) -> bytes:
    frames = []
    for sequence, record in enumerate(records, start=1):
        ingestion = EvidenceIngestion(
            connector_id="markdown",
            sequence=sequence,
            predecessor_id=None,
            evidence=record,
        )
        frames.append(
            json.dumps(
                ingestion.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
    return b"".join(frames)


@dataclass
class AssessmentRuntime:
    runtime: Runtime
    paths: dict[str, Path]

    def replace_after_read(self, target: str) -> None:
        """Replace one canonical name after acquisition while retaining semantic versions."""
        original = self.runtime.transactions.snapshot

        def replaced(*args: object, **kwargs: object):
            held = original(*args, **kwargs)  # type: ignore[arg-type]
            path = self.paths[target]
            content = path.read_bytes()
            if target == "graph":
                graph = parse_graph(content)
                replacement = serialize_graph(graph.model_copy(update={"name": "replacement"}))
            elif target == "config":
                loaded = yaml.safe_load(content)
                assert isinstance(loaded, dict)
                loaded["auto_apply_metadata"] = not loaded["auto_apply_metadata"]
                replacement = yaml.safe_dump(loaded, sort_keys=True).encode("utf-8")
            else:
                replacement = content + b"\n"
            temporary = path.with_name(f".{path.name}.replacement")
            temporary.write_bytes(replacement)
            os.replace(temporary, path)
            return held

        self.runtime.transactions.snapshot = replaced  # type: ignore[method-assign]


@pytest.fixture
def assessment_runtime(tmp_path: Path) -> Iterator[AssessmentRuntime]:
    project = tmp_path / "project"
    project.mkdir()
    initialized = initialize_project(project)
    config = ProjectConfig(project_id="project:assessment", local_actor="local:asha")
    initialized.config_path.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    public_intent = _evidence("evidence:public-intent", acl=("local:asha",), marker="PUBLIC-INTENT")
    public_requirement = _evidence(
        "evidence:public-requirement", acl=("local:asha",), marker="PUBLIC-REQUIREMENT"
    )
    hidden = _evidence("evidence:hidden", acl=("local:ben",), marker="PRIVATE-HIDDEN")
    graph = Graph(
        id="graph:assessment",
        version=7,
        nodes=(
            _node("intent:public", NodeType.PRODUCT_INTENT, public_intent.id, "Public intent"),
            _node("req:public", NodeType.REQUIREMENT, public_requirement.id, "Public requirement"),
            _node("req:hidden", NodeType.REQUIREMENT, hidden.id, "PRIVATE-HIDDEN"),
        ),
        edges=(
            _edge("edge:public", "intent:public", "req:public"),
            _edge("edge:hidden-adjacent", "req:public", "req:hidden"),
        ),
    )
    initialized.graph_path.write_bytes(serialize_graph(graph))
    workspace = project / ".intent"
    paths = {
        "acl_policy": workspace / "approvals/policy.yaml",
        "config": workspace / "config.yaml",
        "graph": workspace / "graph.yaml",
        "evidence": workspace / "evidence/evidence.jsonl",
        "cases": workspace / "reconciliation/cases.jsonl",
        "history": workspace / "history/changesets.jsonl",
        "intent_proposals": workspace / "history/intent-proposals.jsonl",
    }
    paths["acl_policy"].write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "contributors": ["local:asha"],
                "approvers": ["local:asha"],
                "executors": ["local:asha"],
                "identities": {"local:asha": ["local:asha", "github:asha"]},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    paths["evidence"].write_bytes(_evidence_bytes((public_intent, hidden, public_requirement)))
    runtime = load_runtime(project)
    try:
        yield AssessmentRuntime(runtime, paths)
    finally:
        runtime.close()
