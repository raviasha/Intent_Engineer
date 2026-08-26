"""Integration contracts for one exactly approved, at-most-once provider write."""

from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from intent_engineering.capture.mcp import McpTransportError
from intent_engineering.core.models import (
    EvidenceRecord,
    Graph,
    JsonValue,
    Node,
    NodeType,
    ReconciliationStatus,
    SourceMode,
)
from intent_engineering.mutations import (
    ApprovalRecord,
    ExecutionReceipt,
    RemoteObject,
    WritePlan,
    WriteResult,
)
from intent_engineering.mutations.committer import WriteCommitError
from intent_engineering.mutations.executor import (
    ExecutionUnavailable,
    ExternalMutationGateway,
    LocalWriteCommitter,
    SuccessfulWriteCommitter,
    WriteExecutor,
)
from intent_engineering.mutations.models import approval_id, receipt_id, write_plan_id
from intent_engineering.storage.jsonl.approval_store import (
    JsonlApprovalStore,
    JsonlWritePlanStore,
)
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.jsonl.receipt_store import JsonlReceiptStore
from intent_engineering.storage.secure import SecureDirectory, SecureFile
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from tests.unit.mutations.test_approval import _approve
from tests.unit.mutations.test_planner import (
    IDENTITY_ALIASES,
    NOW,
    base_plan,
    jira_binding,
    jira_profile,
    remote_object,
    review_case,
)


@dataclass
class FakeGateway(ExternalMutationGateway):
    current: RemoteObject = field(default_factory=remote_object)
    result: WriteResult = field(
        default_factory=lambda: WriteResult(
            resulting_version="2026-08-26T12:02:00Z",
            redacted_result={"status": "updated"},
        )
    )
    failure: BaseException | None = None
    fetch_failure: BaseException | None = None
    fetch_calls: int = 0
    write_calls: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    write_started: asyncio.Event | None = None
    write_release: asyncio.Event | None = None
    update_after_write: bool = True
    _last_plan: WritePlan | None = None

    async def fetch_target(self, plan: WritePlan) -> RemoteObject:
        self.fetch_calls += 1
        self._last_plan = plan
        if self.fetch_failure is not None:
            raise self.fetch_failure
        return self.current

    async def execute(
        self,
        operation: str,
        arguments: dict[str, JsonValue],
    ) -> WriteResult:
        self.write_calls.append((operation, arguments))
        if self.write_started is not None:
            self.write_started.set()
        if self.write_release is not None:
            await self.write_release.wait()
        if self.failure is not None:
            raise self.failure
        if self.update_after_write and self._last_plan is not None:
            self.current = self.current.model_copy(
                update={
                    "version": self.result.resulting_version,
                    "content": dict(self._last_plan.after),
                }
            )
        return self.result


@dataclass
class FakeCommitter(SuccessfulWriteCommitter):
    receipts: JsonlReceiptStore
    committed: list[tuple[ExecutionReceipt, str]] = field(default_factory=list)
    failure: BaseException | None = None

    def commit_success(
        self,
        receipt: ExecutionReceipt,
        plan: WritePlan,
        approval: ApprovalRecord,
        result: WriteResult,
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.receipts.complete(receipt)
        del plan, approval, result
        assert receipt.evidence_ref is not None
        self.committed.append((receipt, receipt.evidence_ref))


@dataclass
class WriteHarness:
    executor: WriteExecutor
    gateway: FakeGateway
    committer: FakeCommitter
    receipts: JsonlReceiptStore
    plan_id: str
    approval_id: str


@dataclass
class LocalWriteHarness:
    executor: WriteExecutor
    gateway: FakeGateway
    plan_id: str
    approval_id: str
    root: SecureDirectory
    files: dict[str, SecureFile]
    graph_store: YamlGraphStore
    case_store: JsonlCaseStore
    evidence_store: JsonlEvidenceStore
    receipts: JsonlReceiptStore


def fake_write_harness(
    tmp_path: Path,
    plan: WritePlan | None = None,
    *,
    profile: Any = None,
    binding: Any = None,
    authorized_contributors: frozenset[str] = frozenset({"local:proposer"}),
    authorized_approvers: frozenset[str] = frozenset({"local:reviewer"}),
    authorized_executors: frozenset[str] = frozenset({"local:reviewer"}),
) -> WriteHarness:
    selected = plan or base_plan()
    approval = _approve(selected)
    plans = JsonlWritePlanStore(tmp_path / "plans.jsonl")
    approvals = JsonlApprovalStore(tmp_path / "approvals.jsonl")
    receipts = JsonlReceiptStore(tmp_path / "receipts.jsonl")
    assert plans.put(selected)
    assert approvals.put(approval)
    gateway = FakeGateway()
    committer = FakeCommitter(receipts)
    executor = WriteExecutor(
        plans=plans,
        approvals=approvals,
        receipts=receipts,
        profile=profile or jira_profile(),
        binding=binding or jira_binding(),
        gateway=gateway,
        success_committer=committer,
        authorized_contributors=authorized_contributors,
        authorized_approvers=authorized_approvers,
        authorized_executors=authorized_executors,
        identity_aliases=IDENTITY_ALIASES,
    )
    return WriteHarness(
        executor,
        gateway,
        committer,
        receipts,
        selected.id,
        approval.id,
    )


@pytest.fixture
def write_harness(tmp_path: Path) -> WriteHarness:
    return fake_write_harness(tmp_path)


def _node(node_id: str) -> Node:
    return Node(
        id=node_id,
        type=NodeType.REQUIREMENT,
        label=node_id,
        status="active",
        created_by="fixture",
        created_at=NOW,
        last_modified_by="fixture",
        last_modified_at=NOW,
        source_mode=SourceMode.EXPLICIT,
        evidence_refs=("evidence:intent",),
    )


def _source_evidence(evidence_id: str, author: str) -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence_id,
        connector_type="fixture",
        external_object_id=evidence_id,
        external_version="v1",
        author=author,
        observed_at=NOW,
        source_locator=f"fixture://{evidence_id}",
        content_hash=f"sha256:{evidence_id}",
        payload={"claim": evidence_id},
        acl=("local:proposer", "local:reviewer"),
    )


def _changed_plan(plan: WritePlan, **updates: object) -> WritePlan:
    material = plan.model_dump(mode="json", exclude={"id"})
    material.update(updates)
    material["id"] = write_plan_id(material)  # type: ignore[arg-type]
    return WritePlan.model_validate_json(json.dumps(material))


def _successful_receipt(
    plan: WritePlan,
    approval: ApprovalRecord,
    *,
    actor: str = "local:reviewer",
) -> ExecutionReceipt:
    completed_at = NOW + timedelta(minutes=2)
    material: dict[str, object] = {
        "schema_version": 1,
        "plan_id": plan.id,
        "plan_hash": plan.canonical_hash,
        "approval_id": approval.id,
        "target_version": plan.before_version,
        "executed_by": actor,
        "status": "succeeded",
        "attempted_at": completed_at.isoformat().replace("+00:00", "Z"),
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "resulting_version": "2026-08-26T12:02:00Z",
        "evidence_ref": "evidence:mcp-write:" + "b" * 64,
        "redacted_error": None,
    }
    return ExecutionReceipt.model_validate_json(
        json.dumps({**material, "id": receipt_id(material)})  # type: ignore[arg-type]
    )


def _changed_approval(approval: ApprovalRecord, **updates: object) -> ApprovalRecord:
    material = approval.model_dump(mode="json", exclude={"id"})
    material.update(updates)
    material["id"] = approval_id(material)  # type: ignore[arg-type]
    return ApprovalRecord.model_validate_json(json.dumps(material))


def local_write_harness(
    tmp_path: Path,
    *,
    fault_hook: Any = None,
) -> LocalWriteHarness:
    plan = base_plan()
    approval = _approve(plan)
    root = SecureDirectory.open(tmp_path, create=True)
    files = {
        "graph": root.file("graph.yaml"),
        "history": root.file("history.jsonl"),
        "cases": root.file("cases.jsonl"),
        "evidence": root.file("evidence.jsonl"),
        "receipts": root.file("receipts.jsonl"),
    }
    transactions = LocalTransactionCoordinator(
        root.file(".write-transaction.json"),
        files,
        fault_hook=fault_hook,
    )
    graph_store = YamlGraphStore(
        files["graph"],
        history_path=files["history"],
        transactions=transactions,
    )
    graph_store.initialize(
        Graph(
            id="write-fixture",
            version=0,
            nodes=tuple(
                _node(node_id)
                for node_id in (
                    "requirement:export-policy",
                    "jira:ENG-7",
                )
            ),
            edges=(),
        )
    )
    case_store = JsonlCaseStore(files["cases"])
    case_store.put(review_case())
    evidence_store = JsonlEvidenceStore(files["evidence"])
    evidence_store.put(_source_evidence("evidence:intent", "jira-account-101"))
    evidence_store.put(_source_evidence("evidence:requirement", "jira-account-202"))
    receipts = JsonlReceiptStore(files["receipts"])
    plans = JsonlWritePlanStore(root.file("plans.jsonl"))
    approvals = JsonlApprovalStore(root.file("approvals.jsonl"))
    assert plans.put(plan)
    assert approvals.put(approval)
    gateway = FakeGateway()
    committer = LocalWriteCommitter(
        transactions,
        evidence_acl=("local:proposer", "local:reviewer"),
        profile=jira_profile(),
        binding=jira_binding(),
        authorized_contributors=frozenset({"local:proposer"}),
        authorized_approvers=frozenset({"local:reviewer"}),
        authorized_executors=frozenset({"local:reviewer"}),
        identity_aliases=IDENTITY_ALIASES,
    )
    executor = WriteExecutor(
        plans=plans,
        approvals=approvals,
        receipts=receipts,
        profile=jira_profile(),
        binding=jira_binding(),
        gateway=gateway,
        success_committer=committer,
        authorized_contributors=frozenset({"local:proposer"}),
        authorized_approvers=frozenset({"local:reviewer"}),
        authorized_executors=frozenset({"local:reviewer"}),
        identity_aliases=IDENTITY_ALIASES,
    )
    return LocalWriteHarness(
        executor,
        gateway,
        plan.id,
        approval.id,
        root,
        files,
        graph_store,
        case_store,
        evidence_store,
        receipts,
    )


@pytest.mark.anyio
async def test_success_commits_receipt_authorship_evidence_case_graph_and_history(
    tmp_path: Path,
) -> None:
    harness = local_write_harness(tmp_path)

    receipt = await harness.executor.execute(
        harness.plan_id,
        harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )

    case = harness.case_store.get("case-write-1")
    assert receipt.status == "succeeded"
    assert harness.receipts.get_for(harness.plan_id, harness.approval_id) == receipt
    assert receipt.evidence_ref is not None
    evidence = harness.evidence_store.get(receipt.evidence_ref)
    assert evidence.author == "local:reviewer"
    assert evidence.external_version == receipt.resulting_version
    assert evidence.model_dump(mode="json")["payload"] == {
        "approval": {
            "actor": "local:reviewer",
            "actor_aliases": [
                "jira-account-404",
                "local:reviewer",
                "slack-user-404",
            ],
            "id": receipt.approval_id,
        },
        "case_id": "case-write-1",
        "executed_by": "local:reviewer",
        "plan": {
            "conflicting_authors": ["jira-account-101", "jira-account-202"],
            "created_by": "local:proposer",
            "created_by_aliases": [
                "git:proposer@example.com",
                "jira-account-303",
                "local:proposer",
            ],
            "evidence_refs": ["evidence:intent", "evidence:requirement"],
            "hash": receipt.plan_hash,
            "id": receipt.plan_id,
        },
        "provider": {
            "binding_hash": base_plan().binding_hash,
            "connector_id": "mcp:jira-local:profile:source:scope:actor",
            "operation": "update_issue",
            "profile_id": "jira",
            "profile_version": "1",
            "write_contract_hash": base_plan().write_contract_hash,
        },
        "resolution_action": "update_requirement",
        "result": {"status": "verified"},
        "target": {
            "after": dict(base_plan().after),
            "id": "ENG-7",
            "object_type": "issue",
            "prior_version": "2026-08-26T11:00:00Z",
            "resulting_version": "2026-08-26T12:02:00Z",
        },
    }
    assert evidence.acl == ("local:proposer", "local:reviewer")
    assert case.status is ReconciliationStatus.RESOLVED
    assert case.resolution is base_plan().resolution_action
    assert case.resolved_by_changeset is not None
    assert harness.graph_store.load().version == 1
    history = harness.graph_store.history("case-write-1")
    assert len(history) == 1
    assert history[0].id == case.resolved_by_changeset
    assert history[0].evidence_refs == (
        "evidence:intent",
        "evidence:requirement",
        receipt.evidence_ref,
    )


@pytest.mark.anyio
async def test_provider_result_payload_is_not_trusted_or_persisted(
    tmp_path: Path,
) -> None:
    harness = local_write_harness(tmp_path)
    harness.gateway.result = WriteResult(
        resulting_version="2026-08-26T12:02:00Z",
        redacted_result={"raw_token": "PRIVATE-PROVIDER-BODY"},
    )

    receipt = await harness.executor.execute(
        harness.plan_id,
        harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )

    assert receipt.evidence_ref is not None
    evidence = harness.evidence_store.get(receipt.evidence_ref)
    assert evidence.payload["result"] == {"status": "verified"}
    assert b"PRIVATE-PROVIDER-BODY" not in (harness.files["evidence"].read_optional() or b"")


@pytest.mark.anyio
async def test_postwrite_target_must_match_the_approved_semantic_result(
    write_harness: WriteHarness,
) -> None:
    write_harness.gateway.update_after_write = False

    with pytest.raises(ExecutionUnavailable):
        await write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    assert len(write_harness.gateway.write_calls) == 1
    assert write_harness.receipts.is_claimed(
        write_harness.plan_id,
        write_harness.approval_id,
    )
    assert (
        write_harness.receipts.get_for(
            write_harness.plan_id,
            write_harness.approval_id,
        )
        is None
    )


@pytest.mark.parametrize(
    "mismatch",
    (
        "approval_plan",
        "executor",
        "conflicting_authors",
        "self_approval",
        "result_payload",
    ),
)
def test_local_committer_rejects_mismatched_authenticated_bundle(
    tmp_path: Path,
    mismatch: str,
) -> None:
    harness = local_write_harness(tmp_path)
    plan = base_plan()
    approval = _approve(plan)
    actor = "local:reviewer"
    if mismatch == "approval_plan":
        approval = _approve(_changed_plan(plan, conflicting_authors=("provider:different",)))
    elif mismatch == "executor":
        actor = "local:different"
    elif mismatch == "self_approval":
        approval = _changed_approval(
            approval,
            actor="local:proposer",
            actor_aliases=[
                "git:proposer@example.com",
                "jira-account-303",
                "local:proposer",
            ],
        )
        actor = "local:proposer"
    elif mismatch == "conflicting_authors":
        plan = _changed_plan(plan, conflicting_authors=("provider:different",))
        approval = _approve(plan)
    receipt = _successful_receipt(plan, approval, actor=actor)
    result = WriteResult(
        resulting_version="2026-08-26T12:02:00Z",
        redacted_result=(
            {"raw_token": "PRIVATE-PROVIDER-BODY"}
            if mismatch == "result_payload"
            else {"status": "verified"}
        ),
    )
    transactions = LocalTransactionCoordinator(
        harness.root.file(".write-transaction.json"),
        harness.files,
    )
    committer = LocalWriteCommitter(
        transactions,
        evidence_acl=("local:proposer", "local:reviewer"),
        profile=jira_profile(),
        binding=jira_binding(),
        authorized_contributors=frozenset({"local:proposer"}),
        authorized_approvers=frozenset({"local:reviewer", "local:proposer"}),
        authorized_executors=frozenset({"local:reviewer", "local:proposer", "local:different"}),
        identity_aliases=IDENTITY_ALIASES,
    )
    assert harness.receipts.claim(
        plan.id,
        approval.id,
        actor,
        NOW + timedelta(minutes=2),
    )

    with pytest.raises(WriteCommitError, match="local write commit failed"):
        committer.commit_success(receipt, plan, approval, result)

    assert harness.graph_store.load().version == 0
    assert harness.graph_store.history("case-write-1") == ()
    assert harness.case_store.get("case-write-1").status is ReconciliationStatus.NEEDS_HUMAN
    assert harness.receipts.get_for(plan.id, approval.id) is None


@pytest.mark.anyio
async def test_executor_rejects_canonical_self_approval_even_if_policy_lists_allow_it(
    tmp_path: Path,
) -> None:
    plan = base_plan()
    approval = _changed_approval(
        _approve(plan),
        actor="local:proposer",
        actor_aliases=[
            "git:proposer@example.com",
            "jira-account-303",
            "local:proposer",
        ],
    )
    plans = JsonlWritePlanStore(tmp_path / "plans.jsonl")
    approvals = JsonlApprovalStore(tmp_path / "approvals.jsonl")
    receipts = JsonlReceiptStore(tmp_path / "receipts.jsonl")
    assert plans.put(plan)
    assert approvals.put(approval)
    gateway = FakeGateway()
    executor = WriteExecutor(
        plans=plans,
        approvals=approvals,
        receipts=receipts,
        profile=jira_profile(),
        binding=jira_binding(),
        gateway=gateway,
        success_committer=FakeCommitter(receipts),
        authorized_contributors=frozenset({"local:proposer"}),
        authorized_approvers=frozenset({"local:proposer"}),
        authorized_executors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
    )

    with pytest.raises(ExecutionUnavailable):
        await executor.execute(
            plan.id,
            approval.id,
            actor="local:proposer",
            now=NOW + timedelta(minutes=2),
        )

    assert not receipts.is_claimed(plan.id, approval.id)
    assert gateway.fetch_calls == 0
    assert gateway.write_calls == []


def _recovered_harness(harness: LocalWriteHarness) -> LocalWriteHarness:
    transactions = LocalTransactionCoordinator(
        harness.root.file(".write-transaction.json"),
        harness.files,
    )
    transactions.recover()
    graph_store = YamlGraphStore(
        harness.files["graph"],
        history_path=harness.files["history"],
        transactions=transactions,
    )
    case_store = JsonlCaseStore(harness.files["cases"])
    evidence_store = JsonlEvidenceStore(harness.files["evidence"])
    receipts = JsonlReceiptStore(harness.files["receipts"])
    committer = LocalWriteCommitter(
        transactions,
        evidence_acl=("local:proposer", "local:reviewer"),
        profile=jira_profile(),
        binding=jira_binding(),
        authorized_contributors=frozenset({"local:proposer"}),
        authorized_approvers=frozenset({"local:reviewer"}),
        authorized_executors=frozenset({"local:reviewer"}),
        identity_aliases=IDENTITY_ALIASES,
    )
    executor = WriteExecutor(
        plans=JsonlWritePlanStore(harness.root.file("plans.jsonl")),
        approvals=JsonlApprovalStore(harness.root.file("approvals.jsonl")),
        receipts=receipts,
        profile=jira_profile(),
        binding=jira_binding(),
        gateway=harness.gateway,
        success_committer=committer,
        authorized_contributors=frozenset({"local:proposer"}),
        authorized_approvers=frozenset({"local:reviewer"}),
        authorized_executors=frozenset({"local:reviewer"}),
        identity_aliases=IDENTITY_ALIASES,
    )
    return LocalWriteHarness(
        executor,
        harness.gateway,
        harness.plan_id,
        harness.approval_id,
        harness.root,
        harness.files,
        graph_store,
        case_store,
        evidence_store,
        receipts,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "stage",
    (
        "journal_prepared",
        "target:graph",
        "target:history",
        "target:evidence",
        "target:cases",
        "target:receipts",
    ),
)
async def test_precommit_crash_recovers_every_success_store_but_retains_claim(
    tmp_path: Path,
    stage: str,
) -> None:
    def crash(current: str) -> None:
        if current == stage:
            raise SystemExit()

    harness = local_write_harness(tmp_path, fault_hook=crash)
    before = {
        name: file.read_optional() for name, file in harness.files.items() if name != "receipts"
    }

    with pytest.raises(SystemExit):
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    recovered = _recovered_harness(harness)
    assert {
        name: file.read_optional() for name, file in recovered.files.items() if name != "receipts"
    } == before
    assert recovered.receipts.is_claimed(recovered.plan_id, recovered.approval_id)
    assert recovered.receipts.list() == ()
    with pytest.raises(ExecutionUnavailable):
        await recovered.executor.execute(
            recovered.plan_id,
            recovered.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=3),
        )
    assert len(recovered.gateway.write_calls) == 1


@pytest.mark.anyio
async def test_committed_journal_crash_keeps_all_success_effects_without_second_write(
    tmp_path: Path,
) -> None:
    def crash(stage: str) -> None:
        if stage == "journal_committed":
            raise SystemExit()

    harness = local_write_harness(tmp_path, fault_hook=crash)
    with pytest.raises(SystemExit):
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    recovered = _recovered_harness(harness)
    receipt = recovered.receipts.get_for(recovered.plan_id, recovered.approval_id)
    assert receipt is not None and receipt.status == "succeeded"
    assert recovered.case_store.get("case-write-1").status is ReconciliationStatus.RESOLVED
    assert recovered.graph_store.load().version == 1
    assert receipt.evidence_ref is not None
    assert recovered.evidence_store.get(receipt.evidence_ref).id == receipt.evidence_ref

    repeated = await recovered.executor.execute(
        recovered.plan_id,
        recovered.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=3),
    )
    assert repeated == receipt
    assert len(recovered.gateway.write_calls) == 1


@pytest.mark.anyio
async def test_ordinary_local_commit_failure_rolls_back_and_requires_manual_recovery(
    tmp_path: Path,
) -> None:
    def fail(stage: str) -> None:
        if stage == "target:evidence":
            raise RuntimeError("PRIVATE-LOCAL-COMMIT-FAILURE")

    harness = local_write_harness(tmp_path, fault_hook=fail)
    before = {
        name: file.read_optional() for name, file in harness.files.items() if name != "receipts"
    }

    with pytest.raises(ExecutionUnavailable) as caught:
        await harness.executor.execute(
            harness.plan_id,
            harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    assert caught.value.args == ("external write execution unavailable",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert {
        name: file.read_optional() for name, file in harness.files.items() if name != "receipts"
    } == before
    assert harness.receipts.is_claimed(harness.plan_id, harness.approval_id)
    assert harness.receipts.list() == ()
    assert len(harness.gateway.write_calls) == 1


@pytest.mark.anyio
async def test_approved_unchanged_plan_executes_once_and_reuses_receipt(
    write_harness: WriteHarness,
) -> None:
    receipt = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )
    repeated = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=3),
    )

    assert receipt.status == "succeeded"
    assert receipt.resulting_version == "2026-08-26T12:02:00Z"
    assert repeated == receipt
    assert write_harness.gateway.write_calls == [("update_issue", dict(base_plan().arguments))]
    assert write_harness.committer.committed[0][0] == receipt
    assert write_harness.committer.committed[0][1] == receipt.evidence_ref


@pytest.mark.anyio
async def test_completed_receipt_remains_retrievable_after_approval_expiry(
    write_harness: WriteHarness,
) -> None:
    receipt = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )

    repeated = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(days=1),
    )

    assert repeated == receipt
    assert len(write_harness.gateway.write_calls) == 1


@pytest.mark.anyio
async def test_incomplete_durable_claim_never_retries_provider_mutation(
    write_harness: WriteHarness,
) -> None:
    assert write_harness.receipts.claim(
        write_harness.plan_id,
        write_harness.approval_id,
        "local:reviewer",
        NOW + timedelta(minutes=1),
    )

    with pytest.raises(ExecutionUnavailable):
        await write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    assert write_harness.gateway.fetch_calls == 0
    assert write_harness.gateway.write_calls == []


@pytest.mark.anyio
async def test_concurrent_execution_has_one_provider_call_and_one_durable_owner(
    write_harness: WriteHarness,
) -> None:
    write_harness.gateway.write_started = asyncio.Event()
    write_harness.gateway.write_release = asyncio.Event()
    first = asyncio.create_task(
        write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )
    )
    await write_harness.gateway.write_started.wait()

    with pytest.raises(ExecutionUnavailable):
        await write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    write_harness.gateway.write_release.set()
    receipt = await first
    assert receipt.status == "succeeded"
    assert len(write_harness.gateway.write_calls) == 1
    assert (
        write_harness.receipts.get_for(
            write_harness.plan_id,
            write_harness.approval_id,
        )
        == receipt
    )


@pytest.mark.anyio
async def test_transport_failure_is_persisted_and_never_automatically_retried(
    write_harness: WriteHarness,
) -> None:
    write_harness.gateway.failure = McpTransportError()
    receipt = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=2),
    )
    repeated = await write_harness.executor.execute(
        write_harness.plan_id,
        write_harness.approval_id,
        actor="local:reviewer",
        now=NOW + timedelta(minutes=3),
    )

    assert receipt.status == "failed"
    assert receipt.redacted_error == "provider_failure"
    assert repeated == receipt
    assert len(write_harness.gateway.write_calls) == 1
    assert write_harness.committer.committed == []


@pytest.mark.anyio
async def test_unexpected_local_committer_failure_is_fixed_and_leaves_only_claim(
    write_harness: WriteHarness,
) -> None:
    sentinel = "PRIVATE-UNEXPECTED-COMMITTER-FAILURE"
    write_harness.committer.failure = RuntimeError(sentinel)

    with pytest.raises(ExecutionUnavailable) as caught:
        await write_harness.executor.execute(
            write_harness.plan_id,
            write_harness.approval_id,
            actor="local:reviewer",
            now=NOW + timedelta(minutes=2),
        )

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("external write execution unavailable",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentinel not in repository_locals
    assert write_harness.receipts.is_claimed(
        write_harness.plan_id,
        write_harness.approval_id,
    )
    assert write_harness.receipts.list() == ()
    assert len(write_harness.gateway.write_calls) == 1
