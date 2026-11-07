"""Enrichment session state participates in canonical runtime recovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from intent_engineering.cli.runtime import load_assessment_runtime, load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator


def test_runtime_owns_enrichment_store_and_assessment_holds_its_target(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)

    runtime = load_runtime(project)
    try:
        assert "enrichment_sessions" in runtime.transactions.target_names
        assert runtime.enrichment_sessions.bytes() == b""
        assert runtime.enrichment_sessions.path.name == "enrichment-sessions.jsonl"
    finally:
        runtime.close()

    assessment = load_assessment_runtime(project)
    try:
        assert "enrichment_sessions" in assessment.transactions.target_names
    finally:
        assessment.close()


def test_torn_enrichment_and_evidence_append_rolls_back_before_store_parse(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    workspace = SecureDirectory.open(project / ".intent")
    targets = {
        "graph": workspace.file("graph.yaml"),
        "history": workspace.file("history/changesets.jsonl"),
        "cases": workspace.file("reconciliation/cases.jsonl"),
        "evidence": workspace.file("evidence/evidence.jsonl"),
        "receipts": workspace.file("approvals/receipts.jsonl"),
        "approvals": workspace.file("approvals/approvals.jsonl"),
        "intent_proposals": workspace.file("history/intent-proposals.jsonl"),
        "webauthn_credentials": workspace.file("approvals/webauthn-credentials.jsonl"),
        "webauthn_challenges": workspace.file("approvals/webauthn-challenges.jsonl"),
        "enrichment_sessions": workspace.file("history/enrichment-sessions.jsonl"),
    }

    def crash(stage: str) -> None:
        if stage == "target:enrichment_sessions":
            raise SystemExit("injected crash")

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"), targets, fault_hook=crash
    )
    with pytest.raises(SystemExit), coordinator.transaction() as transaction:
        transaction.write("evidence", b'{"answer":"PRIVATE ANSWER"}\n')
        transaction.write("enrichment_sessions", b'{"torn":')
    coordinator.close()
    for target in targets.values():
        target.close()
    workspace.close()

    runtime = load_runtime(project)
    try:
        assert runtime.evidence_store.list() == ()
        assert runtime.enrichment_sessions.bytes() == b""
    finally:
        runtime.close()
