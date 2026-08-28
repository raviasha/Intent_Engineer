"""Real-orchestrator integration for partial MCP capture and durable replay."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.mcp import McpConnectorConfig, load_profile
from intent_engineering.capture.mcp.connector import McpCheckpoint, McpConnector
from intent_engineering.core.models import (
    CandidateAssertion,
    ChangeSet,
    EvidenceDelta,
    EvidenceIngestion,
    Graph,
    JsonValue,
)
from intent_engineering.extract.base import SemanticReasoner
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from intent_engineering.sync.models import SyncRunStatus
from intent_engineering.sync.orchestrator import SyncOrchestrator
from intent_engineering.validation.service import (
    _checkpoint_diagnostics,
    _evidence_diagnostics,
    _mcp_association_diagnostics,
)

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ROOT / "profiles" / "mcp"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class RecordingNoopReasoner(SemanticReasoner):
    def __init__(self) -> None:
        self.deltas: list[EvidenceDelta] = []

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        self.deltas.append(delta)
        return ()

    def map_to_graph(self, assertions: Sequence[CandidateAssertion], graph: Graph) -> ChangeSet:
        del assertions
        return ChangeSet(
            id="changeset:mcp-noop",
            actor="fixture",
            timestamp=NOW,
            baseline_graph_version=graph.version,
            evidence_refs=(),
            nodes_added=(),
            nodes_updated=(),
            nodes_superseded=(),
            edges_added=(),
            edges_updated=(),
            edges_superseded=(),
            confidence_changes=(),
            implementation_status_changes=(),
            reconciliation_cases_created=(),
            reconciliation_cases_resolved=(),
            validation_status="validated",
        )

    def propose_reconciliation(self, case: object) -> None:
        del case


class MutableRuntime:
    def __init__(self, raw: dict[str, JsonValue]) -> None:
        self.raw: dict[str, JsonValue] | None = raw
        self.fail_second_page = False

    async def validate_binding(self, _server: object, _binding: object) -> None:
        return None

    async def call(
        self, _server: object, tool_name: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        if tool_name == "search_messages":
            if arguments["cursor"] == "page-2":
                if self.fail_second_page:
                    raise TimeoutError("PRIVATE-MCP-TIMEOUT")
                return {"messages": [], "next_cursor": None}
            items = [] if self.raw is None else [{"id": self.raw["id"]}]
            return {"messages": items, "next_cursor": "page-2"}
        if tool_name == "get_message" and self.raw is not None:
            return copy.deepcopy(self.raw)
        raise RuntimeError("PRIVATE-MCP-FETCH")

    async def read_resource(self, _server: object, _uri: str) -> JsonValue:
        raise AssertionError("unexpected resource")


def _config() -> McpConnectorConfig:
    loaded = yaml.safe_load(
        (PROFILES / "example-bindings" / "slack.yaml").read_text(encoding="utf-8")
    )
    assert type(loaded) is dict
    return McpConnectorConfig.model_validate(loaded)


def _message() -> dict[str, JsonValue]:
    loaded = json.loads(
        (ROOT / "tests" / "fixtures" / "mcp" / "slack" / "message.json").read_text()
    )
    return cast(dict[str, JsonValue], loaded["raw"])


class McpSyncHarness:
    def __init__(self, root: Path) -> None:
        directory = SecureDirectory.open(root, create=True)
        self.checkpoint_path = root / "checkpoints.yaml"
        self.runtime = MutableRuntime(_message())
        self.reasoner = RecordingNoopReasoner()
        case_store = JsonlCaseStore(directory.file("cases.jsonl"))
        transactions = LocalTransactionCoordinator(
            directory.file(".local-transaction.json"),
            {
                "graph": directory.file("graph.yaml"),
                "history": directory.file("history.jsonl"),
                "cases": case_store._file,
            },
        )
        graph_store = YamlGraphStore(
            directory.file("graph.yaml"),
            history_path=directory.file("history.jsonl"),
            transactions=transactions,
        )
        graph_store.initialize(Graph(id="mcp-fixture", version=0, nodes=(), edges=()))
        self.evidence_store = JsonlEvidenceStore(directory.file("evidence.jsonl"))
        self.checkpoint_store = YamlCheckpointStore(directory.file("checkpoints.yaml"))
        executor = LocalChangeSetExecutor(graph_store, case_store, transactions)
        self.orchestrator = SyncOrchestrator(
            graph_store=graph_store,
            evidence_store=self.evidence_store,
            checkpoint_store=self.checkpoint_store,
            case_store=case_store,
            reasoner=self.reasoner,
            changeset_executor=executor,
            clock=lambda: NOW,
        )

    def connector(self) -> McpConnector:
        return McpConnector(
            self.runtime,
            config=_config(),
            profile=load_profile(PROFILES / "slack.yaml"),
            object_name="message",
            local_actor="local-asha",
        )


@pytest.mark.anyio
async def test_later_page_failure_persists_prior_evidence_but_keeps_checkpoint_bytes(
    tmp_path: Path,
) -> None:
    harness = McpSyncHarness(tmp_path)
    baseline = await harness.orchestrator.run("baseline", (harness.connector(),))
    assert baseline.status is SyncRunStatus.SUCCESS
    prior_bytes = harness.checkpoint_path.read_bytes()

    assert harness.runtime.raw is not None
    harness.runtime.raw["updated"] = "1700000000.000300"
    harness.runtime.raw["updated_at"] = "2026-08-20T10:20:30Z"
    harness.runtime.raw["text"] = "A conflicting author proposes central export."
    harness.runtime.raw["last_modified_by"] = {"id": "U456"}
    harness.runtime.raw["allowed_principals"] = ["U123", "U456", "slack-group:ENG"]
    harness.runtime.fail_second_page = True

    failed = await harness.orchestrator.run("partial", (harness.connector(),))

    assert failed.status is SyncRunStatus.FAILED
    assert failed.evidence_added == 1
    assert harness.checkpoint_path.read_bytes() == prior_bytes
    ledger = harness.evidence_store.ledger(
        harness.connector().connector_id,
        connector_type="mcp",
    )
    assert [item.evidence.author for item in ledger] == ["U123", "U456"]
    assert ledger[1].predecessor_id == ledger[0].evidence.id
    assert [item.evidence.payload["content"]["text"] for item in ledger] == [
        "The export must remain local by default.",
        "A conflicting author proposes central export.",
    ]
    assert failed.connectors[harness.connector().connector_id].redacted_error == "connector failed"


@pytest.mark.anyio
async def test_new_connector_retry_replays_durable_deleted_version_and_then_is_noop(
    tmp_path: Path,
) -> None:
    harness = McpSyncHarness(tmp_path)
    harness.runtime.fail_second_page = True
    partial = await harness.orchestrator.run("partial", (harness.connector(),))
    assert partial.status is SyncRunStatus.FAILED
    pending_id = harness.reasoner.deltas[0].added[0].id
    assert harness.checkpoint_path.exists() is False

    harness.runtime.fail_second_page = False
    harness.runtime.raw = None
    recovered_connector = harness.connector()
    recovered = await harness.orchestrator.run("recovered", (recovered_connector,))
    checkpoint = harness.checkpoint_store.get(recovered_connector.connector_id)

    assert recovered.status is SyncRunStatus.SUCCESS
    assert checkpoint is not None and checkpoint.cursor is not None
    assert pending_id in tuple(record.id for record in harness.reasoner.deltas[1].added)
    decoded = McpCheckpoint.decode(checkpoint.cursor, connector=recovered_connector)
    assert decoded.observed_versions == {
        "slack:workspace-1:C111:1700000000.000100": "1700000000.000200"
    }
    ledger = harness.evidence_store.ledger(
        recovered_connector.connector_id,
        connector_type="mcp",
    )
    assert (
        _checkpoint_diagnostics(
            {recovered_connector.connector_id: checkpoint},
            tuple(item.evidence for item in ledger),
            tuple(ledger),
            (),
        )
        == []
    )
    assert _mcp_association_diagnostics(tuple(ledger)) == []
    tampered_source_hash = decoded.source_hash[:-1] + (
        "0" if decoded.source_hash[-1] != "0" else "1"
    )
    tampered_checkpoint = checkpoint.model_copy(
        update={
            "cursor": decoded.model_copy(
                update={"source_hash": tampered_source_hash}
            ).encode()
        }
    )
    assert [
        item.code
        for item in _checkpoint_diagnostics(
            {recovered_connector.connector_id: tampered_checkpoint},
            tuple(item.evidence for item in ledger),
            tuple(ledger),
            (),
        )
    ] == ["checkpoint.cursor_invalid"]
    record = ledger[0].evidence
    dumped = record.model_dump(mode="json")
    assert type(dumped["payload"]) is dict
    dumped["payload"]["content"]["text"] = "tampered after capture"
    tampered = record.model_validate(dumped)
    assert [item.code for item in _evidence_diagnostics((tampered,))] == [
        "evidence.content_hash_mismatch"
    ]

    checkpoint_bytes = harness.checkpoint_path.read_bytes()
    noop = await harness.orchestrator.run("noop", (harness.connector(),))
    assert noop.status is SyncRunStatus.SUCCESS
    assert noop.connectors[recovered_connector.connector_id].checkpoint_advanced is False
    assert harness.checkpoint_path.read_bytes() == checkpoint_bytes


def test_uncheckpointed_mcp_associations_are_deeply_validated() -> None:
    raw = _message()
    # Build one real normalized record through the connector contract, then tamper only its
    # immutable association envelope to exercise validation before any checkpoint exists.
    profile_id = "slack"
    profile_identity = hashlib.sha256(
        json.dumps(
            {
                "object_type": "message",
                "profile_id": profile_id,
                "profile_version": "1",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    evidence = EvidenceIngestion.model_validate(
        {
            "storage_schema_version": 1,
            "connector_id": (
                f"mcp:slack-local:{profile_identity}:{'a' * 64}:"
                f"{'b' * 64}:{'c' * 64}"
            ),
            "sequence": 1,
            "predecessor_id": None,
            "evidence": {
                "id": "evidence:sha256:" + "0" * 64,
                "connector_type": "mcp",
                "external_object_id": f"{profile_id}:{raw['id']}",
                "external_version": raw["updated"],
                "author": "U123",
                "observed_at": raw["updated_at"],
                "source_locator": raw["permalink"],
                "content_hash": "sha256:" + "1" * 64,
                "payload": {
                    "kind": "mcp_object",
                    "profile_id": profile_id,
                    "profile_version": "1",
                    "object_type": "wrong-object-type",
                    "scope_hash": "sha256:" + "b" * 64,
                    "source_hash": "sha256:" + "a" * 64,
                    "parent_context": None,
                    "content": {"text": "tampered"},
                },
                "acl": ["U123"],
            },
        }
    )

    assert [item.code for item in _mcp_association_diagnostics((evidence,))] == [
        "evidence.mcp_association_invalid"
    ]


@pytest.mark.anyio
async def test_uncheckpointed_mcp_association_authenticates_profile_to_source_identity() -> None:
    runtime = MutableRuntime(_message())
    connector = McpConnector(
        runtime,
        config=_config(),
        profile=load_profile(PROFILES / "slack.yaml"),
        object_name="message",
        local_actor="local-asha",
    )
    discovered = await connector.discover(None)
    raw = await connector.fetch(
        discovered[0].external_object_id,
        discovered[0].external_version,
    )
    record = connector.normalize(raw)
    dumped = record.model_dump(mode="json")
    assert type(dumped["payload"]) is dict
    dumped["external_object_id"] = dumped["external_object_id"].replace(
        "slack:", "other-profile:", 1
    )
    dumped["payload"]["profile_id"] = "other-profile"
    dumped["payload"]["profile_version"] = "999"
    dumped["payload"]["parent_context"] = None
    ingestion = EvidenceIngestion(
        connector_id=connector.connector_id,
        sequence=1,
        predecessor_id=None,
        evidence=record.model_validate(dumped),
    )

    assert [item.code for item in _mcp_association_diagnostics((ingestion,))] == [
        "evidence.mcp_association_invalid"
    ]
