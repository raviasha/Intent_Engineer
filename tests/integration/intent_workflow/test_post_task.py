"""Post-task completion remains bound to live intent authorization and evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from intent_engineering.core.models import (
    EvidenceRecord,
    Graph,
    ImplementationClaim,
    ImplementationStatus,
    Node,
    NodeType,
    RelationType,
    SourceMode,
)
from intent_engineering.integrations.agent_host import HostTaskResult, IntentAgentHostAdapter
from intent_engineering.intent_workflow.authorization import (
    AuthorizationIssuer,
    AuthorizationVerification,
)
from intent_engineering.intent_workflow.models import (
    PreflightResult,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.post_task import (
    PostTaskResult,
    PostTaskService,
    PostTaskSubmission,
)
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import SecureDirectory, SecureFile
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.graph_store import YamlGraphStore

NOW = datetime(2026, 8, 26, 12, 1, tzinfo=UTC)
BASE = "a" * 40
FINAL = "b" * 40
REQUEST = "Implement the approved CSV export"
REQUEST_DIGEST = f"sha256:{hashlib.sha256(REQUEST.encode()).hexdigest()}"


def _node(
    node_id: str,
    node_type: NodeType,
    label: str,
    *,
    evidence_refs: tuple[str, ...] = (),
    source_mode: SourceMode | None = None,
) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label=label,
        status="active",
        created_by="local:asha",
        created_at=NOW,
        last_modified_by="local:asha",
        last_modified_at=NOW,
        source_mode=source_mode,
        evidence_refs=evidence_refs,
    )


def _evidence(
    evidence_id: str,
    connector_type: str,
    *,
    author: str = "local:asha",
    acl: tuple[str, ...] = (),
    repository_id: str = "demo",
) -> EvidenceRecord:
    if connector_type == "git":
        payload = {
            "sha": FINAL,
            "author": author,
            "timestamp": NOW.isoformat(),
            "parents": [BASE],
            "subject": "Implement approved CSV export",
            "body": "",
            "changed_paths": ["src/export.py", "tests/test_export.py"],
            "repository_id": repository_id,
        }
        external_object_id = f"commit:{FINAL}"
        version = FINAL
        locator = f"git:commit:{FINAL}"
    elif connector_type == "test_result":
        payload = {
            "commit_sha": FINAL,
            "outcome": "passed",
            "test_refs": ["test:export"],
        }
        external_object_id = f"test-run:{FINAL}"
        version = "run-1"
        locator = f"test:run:{FINAL}"
    else:
        payload = {"content": "CSV export is required"}
        external_object_id = "path:prd.md"
        version = "v1"
        locator = "prd.md"
    return EvidenceRecord(
        id=evidence_id,
        connector_type=connector_type,
        external_object_id=external_object_id,
        external_version=version,
        author=author,
        observed_at=NOW,
        source_locator=locator,
        content_hash=f"sha256:{evidence_id}",
        payload=payload,
        acl=acl,
    )


@dataclass
class PostTaskHarness:
    service: PostTaskService
    issuer: AuthorizationIssuer
    graph_store: YamlGraphStore
    evidence_store: JsonlEvidenceStore
    transactions: LocalTransactionCoordinator
    files: dict[str, SecureFile]
    envelope: TaskEnvelope
    preflight: PreflightResult

    def state(self) -> tuple[bytes | None, ...]:
        return tuple(
            self.files[name].read_optional()
            for name in ("graph", "history", "cases", "evidence")
        )

    def semantic_state(self) -> tuple[bytes | None, ...]:
        return tuple(
            self.files[name].read_optional()
            for name in ("graph", "history", "cases", "evidence")
        )

    def issue(self) -> str:
        return self.issuer.issue(
            self.envelope,
            self.preflight,
            graph_content=self.files["graph"].read_bytes(),
            now=NOW,
        )
    def submission(self, **updates: object) -> PostTaskSubmission:
        values: dict[str, object] = {
            "repository_id": "demo",
            "task_id": self.envelope.id,
            "actor": "local:asha",
            "request_digest": REQUEST_DIGEST,
            "graph_version": 3,
            "base_revision": BASE,
            "final_revision": FINAL,
            "changed_paths": ("src/export.py", "tests/test_export.py"),
            "requirement_ids": ("requirement:csv-export",),
            "code_refs": ("file:export",),
            "test_refs": ("test:export",),
            "git_evidence_refs": ("evidence:git",),
            "test_evidence_refs": ("evidence:test-run",),
            "completed_at": NOW,
        }
        values.update(updates)
        return PostTaskSubmission.model_validate(values)


def _repository_traceback_locals(error: BaseException) -> str:
    frames: list[str] = []
    current = error.__traceback__
    while current is not None:
        if "/src/intent_engineering/" in current.tb_frame.f_code.co_filename:
            frames.append(repr(current.tb_frame.f_locals))
        current = current.tb_next
    return "\n".join(frames)


def _harness(
    root: Path,
    *,
    requirement_acl: tuple[str, ...] = (),
    git_author: str = "local:asha",
    git_repository_id: str = "demo",
    test_acl: tuple[str, ...] = ("local:asha",),
) -> PostTaskHarness:
    directory = SecureDirectory.open(root, create=True)
    files = {
        name: directory.file(filename)
        for name, filename in {
            "graph": "graph.yaml",
            "history": "history.jsonl",
            "cases": "cases.jsonl",
            "evidence": "evidence.jsonl",
            "config": "config.yaml",
            "policy": "policy.yaml",
            "repository": "repository",
            "binding": "binding.yaml",
            "transaction": ".transaction.json",
        }.items()
    }
    authority_content = {
        "config": b"project_id: demo\n",
        "policy": b"identity_aliases: enabled\n",
        "repository": b"demo\n",
        "binding": b"local:asha: Asha <asha@example.test>\n",
    }
    for name, content in authority_content.items():
        files[name].atomic_write(content)
    transactions = LocalTransactionCoordinator(
        files["transaction"],
        {name: files[name] for name in ("graph", "history", "cases", "evidence")},
    )
    graph_store = YamlGraphStore(
        files["graph"], history_path=files["history"], transactions=transactions
    )
    graph_store.initialize(
        Graph(
            id="demo-graph",
            version=3,
            nodes=(
                _node(
                    "requirement:csv-export",
                    NodeType.REQUIREMENT,
                    "Reports support CSV export",
                    evidence_refs=("evidence:prd",),
                    source_mode=SourceMode.EXPLICIT,
                ),
                _node("file:export", NodeType.FILE, "src/export.py"),
                _node("test:export", NodeType.TEST, "tests/test_export.py"),
            ),
            edges=(),
        )
    )
    case_store = JsonlCaseStore(files["cases"])
    evidence_store = JsonlEvidenceStore(files["evidence"], transactions=transactions)
    evidence_store.associate(
        "markdown", _evidence("evidence:prd", "markdown", acl=requirement_acl)
    )
    evidence_store.associate(
        "git",
        _evidence(
            "evidence:git",
            "git",
            author=git_author,
            repository_id=git_repository_id,
        ),
    )
    evidence_store.associate(
        "test-results",
        _evidence(
            "evidence:test-run",
            "test_result",
            author="ci:test-runner",
            acl=test_acl,
        ),
    )
    executor = LocalChangeSetExecutor(graph_store, case_store, transactions)
    issuer = AuthorizationIssuer()
    envelope = TaskEnvelope(
        repository_id="demo",
        actor="local:asha",
        conversation_ref="codex:thread-1:turn-1",
        request=REQUEST,
        request_evidence_ref="evidence:conversation:" + "1" * 64,
        graph_version=3,
        created_at=NOW,
        requested_scope=("src/export.py", "tests/test_export.py"),
    )
    preflight = PreflightResult(
        task_id=envelope.id,
        graph_version=3,
        classification=TaskClassification.ALIGNED,
        authorized=True,
        basis="The approved requirement permits CSV export",
        relevant_node_ids=("requirement:csv-export",),
        evidence_refs=("evidence:prd",),
        permitted_scope=envelope.requested_scope,
    )

    def resolve_authority(
        actor: str,
        snapshot: Mapping[str, bytes | None],
    ) -> tuple[str, frozenset[str]]:
        if any(snapshot.get(name) != content for name, content in authority_content.items()):
            return "invalid", frozenset()
        return "demo", frozenset({actor, "Asha <asha@example.test>"})

    return PostTaskHarness(
        service=PostTaskService(
            issuer=issuer,
            changeset_executor=executor,
            transactions=transactions,
            graph_file=files["graph"],
            evidence_file=files["evidence"],
            authority_files={
                name: files[name]
                for name in ("config", "policy", "repository", "binding")
            },
            authority_resolver=resolve_authority,
            clock=lambda: NOW,
        ),
        issuer=issuer,
        graph_store=graph_store,
        evidence_store=evidence_store,
        transactions=transactions,
        files=files,
        envelope=envelope,
        preflight=preflight,
    )


@pytest.fixture
def post_task_harness(tmp_path: Path) -> PostTaskHarness:
    return _harness(tmp_path)


def test_post_task_links_code_and_tests_to_authorized_requirement_atomically(
    post_task_harness: PostTaskHarness,
) -> None:
    evidence_before = post_task_harness.files["evidence"].read_bytes()
    result = post_task_harness.service.evaluate(
        post_task_harness.submission(), token=post_task_harness.issue()
    )

    assert result.status == "recorded"
    assert result.claim is not None
    assert result.claim.requirement_refs == ("requirement:csv-export",)
    assert result.claim.code_evidence == ("evidence:git",)
    assert result.claim.test_evidence == ("evidence:test-run",)
    assert result.claim.verified_commit == FINAL
    graph = post_task_harness.graph_store.load()
    assert graph.version == 4
    assert {
        (edge.from_id, edge.relation, edge.to_id)
        for edge in graph.edges
    } == {
        ("requirement:csv-export", RelationType.IMPLEMENTED_BY, "file:export"),
        ("requirement:csv-export", RelationType.VERIFIED_BY, "test:export"),
    }
    implementation = next(node for node in graph.nodes if node.id == "file:export")
    assert implementation.implementation_status is ImplementationStatus.IMPLEMENTED_BASELINE
    assert implementation.evidence_refs == ("evidence:git", "evidence:test-run")
    history = post_task_harness.graph_store.history("file:export")
    assert len(history) == 1
    assert history[0].id == result.changeset_id
    assert post_task_harness.files["evidence"].read_bytes() == evidence_before


def test_scope_expansion_withholds_completion_and_is_byte_noop(
    post_task_harness: PostTaskHarness,
) -> None:
    before = post_task_harness.state()
    token = post_task_harness.issue()
    result = post_task_harness.service.evaluate(
        post_task_harness.submission(
            changed_paths=("src/export.py", "src/unrelated.py", "tests/test_export.py")
        ),
        token=token,
    )

    assert result.status == "preflight_required"
    assert result.reason == "scope_changed"
    assert post_task_harness.state() == before
    denied = post_task_harness.issuer.verify(
        token,
        actor="local:asha",
        repository_id="demo",
        task_id=post_task_harness.envelope.id,
        graph_version=3,
        graph_content=post_task_harness.files["graph"].read_bytes(),
        requested_paths=("src/export.py",),
        now=NOW,
    )
    assert denied.authorized is False


@pytest.mark.parametrize(
    ("updates", "expected_reason"),
    [
        ({"repository_id": "other"}, "authorization_rejected"),
        ({"actor": "local:mallory"}, "authorization_rejected"),
        ({"task_id": "task:sha256:" + "f" * 64}, "authorization_rejected"),
        ({"request_digest": "sha256:" + "f" * 64}, "authorization_rejected"),
        ({"graph_version": 2}, "authorization_rejected"),
        ({"completed_at": NOW + timedelta(seconds=1)}, "evidence_rejected"),
        ({"base_revision": "c" * 40}, "evidence_rejected"),
        ({"final_revision": "d" * 40}, "evidence_rejected"),
        ({"requirement_ids": ("requirement:missing",)}, "authorization_rejected"),
        ({"code_refs": ("requirement:csv-export",)}, "reference_rejected"),
        ({"test_refs": ("file:export",)}, "reference_rejected"),
        ({"git_evidence_refs": ("evidence:missing",)}, "evidence_rejected"),
        ({"test_evidence_refs": ()}, "review_required"),
        ({"test_evidence_refs": ("evidence:git",)}, "evidence_rejected"),
    ],
)
def test_post_task_rejection_matrix_is_byte_noop(
    post_task_harness: PostTaskHarness,
    updates: dict[str, object],
    expected_reason: str,
) -> None:
    before = post_task_harness.state()
    result = post_task_harness.service.evaluate(
        post_task_harness.submission(**updates), token=post_task_harness.issue()
    )
    assert result.status in {"preflight_required", "review_required", "rejected"}
    assert result.reason == expected_reason
    assert post_task_harness.state() == before


def test_hidden_requirement_and_wrong_git_author_are_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    for name, harness in (
        ("hidden", _harness(tmp_path / "hidden", requirement_acl=("local:ben",))),
        ("author", _harness(tmp_path / "author", git_author="local:mallory")),
    ):
        before = harness.state()
        result = harness.service.evaluate(harness.submission(), token=harness.issue())
        assert result.status == "rejected", name
        assert harness.state() == before, name


def test_review_fix_changed_test_file_without_executed_test_evidence_never_verifies(
    post_task_harness: PostTaskHarness,
) -> None:
    before = post_task_harness.semantic_state()

    result = post_task_harness.service.evaluate(
        post_task_harness.submission(test_evidence_refs=()),
        token=post_task_harness.issue(),
    )

    assert (result.status, result.reason, result.claim) == (
        "review_required",
        "review_required",
        None,
    )
    assert post_task_harness.semantic_state() == before


def test_review_fix_authoritative_git_alias_is_preserved_as_edge_author(
    tmp_path: Path,
) -> None:
    alias = "Asha <asha@example.test>"
    harness = _harness(tmp_path, git_author=alias)

    result = harness.service.evaluate(harness.submission(), token=harness.issue())

    assert result.status == "recorded"
    assert {edge.created_by for edge in harness.graph_store.load().edges} == {alias}


def test_review_fix_wrong_repository_git_evidence_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, git_repository_id="other")
    before = harness.semantic_state()

    result = harness.service.evaluate(harness.submission(), token=harness.issue())

    assert (result.status, result.reason) == ("rejected", "evidence_rejected")
    assert harness.semantic_state() == before


def test_review_fix_hidden_and_stale_test_results_are_never_execution_proof(
    tmp_path: Path,
) -> None:
    hidden = _harness(tmp_path / "hidden", test_acl=("local:ben",))
    hidden_before = hidden.semantic_state()
    hidden_result = hidden.service.evaluate(hidden.submission(), token=hidden.issue())
    assert (hidden_result.status, hidden_result.reason) == (
        "rejected",
        "evidence_rejected",
    )
    assert hidden.semantic_state() == hidden_before

    stale = _harness(tmp_path / "stale")
    stale.evidence_store.associate(
        "test-results",
        _evidence(
            "evidence:test-run-newer",
            "test_result",
            author="ci:test-runner",
            acl=("local:asha",),
        ).model_copy(
            update={
                "external_version": "run-2",
                "observed_at": NOW + timedelta(seconds=1),
                "content_hash": "sha256:evidence:test-run-newer",
            }
        ),
    )
    stale_before = stale.semantic_state()
    stale_result = stale.service.evaluate(stale.submission(), token=stale.issue())
    assert (stale_result.status, stale_result.reason) == (
        "rejected",
        "evidence_rejected",
    )
    assert stale.semantic_state() == stale_before


@pytest.mark.parametrize(
    "target",
    ("config", "policy", "repository", "binding", "graph", "evidence"),
)
def test_review_fix_authority_revocation_or_same_semantics_drift_is_commit_bound(
    tmp_path: Path,
    target: str,
) -> None:
    harness = _harness(tmp_path)
    before = harness.semantic_state()

    def revoke_after_resolution(
        actor: str,
        snapshot: Mapping[str, bytes | None],
    ) -> tuple[str, frozenset[str]]:
        assert actor == "local:asha"
        assert snapshot[target] is not None
        harness.files[target].append(b"\n")
        return "demo", frozenset({actor, "Asha <asha@example.test>"})

    harness.service._authority_resolver = revoke_after_resolution  # type: ignore[attr-defined]
    result = harness.service.evaluate(harness.submission(), token=harness.issue())

    assert (result.status, result.reason) == ("rejected", "evidence_rejected")
    assert harness.graph_store.load().version == 3
    assert harness.files["history"].read_optional() == before[1]
    assert harness.files["cases"].read_optional() == before[2]
    if target not in {"graph", "evidence"}:
        assert harness.semantic_state() == before


def test_stale_and_duplicate_git_evidence_are_byte_noop(tmp_path: Path) -> None:
    stale = _harness(tmp_path / "stale")
    stale.evidence_store.associate(
        "git", _evidence("evidence:git-newer", "git", author="local:asha")
    )
    stale_before = stale.state()
    stale_result = stale.service.evaluate(stale.submission(), token=stale.issue())
    assert (stale_result.status, stale_result.reason) == (
        "rejected",
        "evidence_rejected",
    )
    assert stale.state() == stale_before

    duplicate = _harness(tmp_path / "duplicate")
    content = duplicate.files["evidence"].read_bytes()
    duplicate.files["evidence"].append(content)
    duplicate_before = duplicate.state()
    duplicate_result = duplicate.service.evaluate(
        duplicate.submission(), token=duplicate.issue()
    )
    assert (duplicate_result.status, duplicate_result.reason) == (
        "rejected",
        "evidence_rejected",
    )
    assert duplicate.state() == duplicate_before


def test_expired_revoked_missing_and_substituted_capabilities_are_byte_noop(
    tmp_path: Path,
) -> None:
    for name in ("expired", "revoked", "missing", "substituted"):
        harness = _harness(tmp_path / name)
        before = harness.state()
        token = harness.issue()
        if name == "expired":
            harness.service._clock = lambda: NOW.replace(minute=6)  # type: ignore[attr-defined]
        elif name == "revoked":
            harness.issuer.revoke(token)
        elif name == "missing":
            token = ""
        else:
            token = "A" * 43

        result = harness.service.evaluate(harness.submission(), token=token)

        assert (result.status, result.reason) == (
            "rejected",
            "authorization_rejected",
        ), name
        assert harness.state() == before, name


def test_concurrent_completion_consumes_exactly_once_and_cannot_be_replayed(
    post_task_harness: PostTaskHarness,
) -> None:
    token = post_task_harness.issue()
    submission = post_task_harness.submission()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _index: post_task_harness.service.evaluate(submission, token=token),
                range(2),
            )
        )

    assert sorted(result.status for result in results) == ["recorded", "rejected"]
    assert post_task_harness.graph_store.load().version == 4
    assert len(post_task_harness.graph_store.history("file:export")) == 1
    before_replay = post_task_harness.state()
    replay = post_task_harness.service.evaluate(submission, token=token)
    assert (replay.status, replay.reason) == ("rejected", "authorization_rejected")
    assert post_task_harness.state() == before_replay


class _PostTaskCancellation(BaseException):
    pass


@pytest.mark.parametrize("failure_type", ["exception", "cancellation"])
def test_transaction_failure_rolls_back_exact_state_and_consumes_capability(
    post_task_harness: PostTaskHarness,
    failure_type: str,
) -> None:
    before = post_task_harness.state()
    token = post_task_harness.issue()
    signal = _PostTaskCancellation()

    def fail(stage: str) -> None:
        if stage != "target:graph":
            return
        if failure_type == "exception":
            raise RuntimeError("PRIVATE-POST-TASK-FAILURE")
        raise signal

    post_task_harness.transactions._fault_hook = fail  # type: ignore[attr-defined]
    if failure_type == "exception":
        result = post_task_harness.service.evaluate(
            post_task_harness.submission(), token=token
        )
        assert (result.status, result.reason) == ("rejected", "evidence_rejected")
    else:
        with pytest.raises(_PostTaskCancellation) as caught:
            post_task_harness.service.evaluate(post_task_harness.submission(), token=token)
        assert caught.value is signal
        traceback_locals = _repository_traceback_locals(caught.value)
        for private in (token, "requirement:csv-export", "tests/test_export.py", FINAL):
            assert private not in traceback_locals
    assert post_task_harness.state() == before
    assert not post_task_harness.issuer.verify(
        token,
        actor="local:asha",
        repository_id="demo",
        task_id=post_task_harness.envelope.id,
        graph_version=3,
        graph_content=post_task_harness.files["graph"].read_bytes(),
        requested_paths=("src/export.py",),
        now=NOW,
    ).authorized


def test_post_task_records_are_strict_frozen_detached_canonical_and_token_free(
    post_task_harness: PostTaskHarness,
) -> None:
    submission = post_task_harness.submission()
    roundtrip = PostTaskSubmission.model_validate_json(submission.model_dump_json())
    assert roundtrip == submission
    assert "token" not in PostTaskSubmission.model_fields
    assert "token" not in PostTaskResult.model_fields
    with pytest.raises(ValidationError):
        PostTaskSubmission.model_validate({**submission.model_dump(), "unknown": True})
    with pytest.raises((TypeError, ValidationError)):
        PostTaskSubmission.model_validate(
            {**submission.model_dump(), "changed_paths": ["src/export.py"]}
        )
    payload = submission.model_dump(mode="json")
    payload["completed_at"] = "2026-08-26T12:01:00+00:00"
    with pytest.raises(ValueError, match="canonical UTC"):
        PostTaskSubmission.model_validate_json(json.dumps(payload))
    with pytest.raises(ValidationError):
        submission.actor = "local:ben"  # type: ignore[misc]

    class StringSubclass(str):
        pass

    recorded = PostTaskResult(
        status="recorded",
        reason="recorded",
        task_id=submission.task_id,
        graph_version=4,
        claim=ImplementationClaim(
            id="claim:strict",
            status=ImplementationStatus.IMPLEMENTED_BASELINE,
            requirement_refs=submission.requirement_ids,
            current_behavior="Implement approved CSV export",
            code_evidence=submission.git_evidence_refs,
            test_evidence=submission.test_evidence_refs,
            verified_commit=submission.final_revision,
            verified_at=NOW,
        ),
        changeset_id="changeset:strict",
    )
    recorded_values = {name: getattr(recorded, name) for name in PostTaskResult.model_fields}
    with pytest.raises((TypeError, ValidationError)):
        PostTaskResult.model_validate(
            {**recorded_values, "changeset_id": StringSubclass("changeset:strict")}
        )
    with pytest.raises(ValidationError):
        PostTaskResult.model_validate({**recorded_values, "changeset_id": ""})


class _HostWorkflow:
    def __init__(
        self,
        *,
        cancellation: BaseException | None = None,
        completion_status: str = "recorded",
    ) -> None:
        self.calls: list[tuple[str, object]] = []
        self.cancellation = cancellation
        self.completion_status = completion_status

    async def before_task(
        self, request: str, actor: str
    ) -> tuple[TaskEnvelope, PreflightResult, str]:
        envelope = TaskEnvelope(
            repository_id="demo",
            actor=actor,
            conversation_ref="host:thread-1:turn-1",
            request=request,
            request_evidence_ref="evidence:conversation:" + "3" * 64,
            graph_version=3,
            created_at=NOW,
            requested_scope=("src/export.py", "tests/test_export.py"),
        )
        result = PreflightResult(
            task_id=envelope.id,
            graph_version=3,
            classification=TaskClassification.ALIGNED,
            authorized=True,
            basis="approved",
            relevant_node_ids=("requirement:csv-export",),
            evidence_refs=("evidence:prd",),
            permitted_scope=envelope.requested_scope,
        )
        return envelope, result, "private-host-capability"

    async def authorization_verify(self, **_kwargs: object) -> AuthorizationVerification:
        raise AssertionError("completion does not use the public verify path")

    async def post_task_evaluate(
        self, submission: PostTaskSubmission, *, token: str
    ) -> PostTaskResult:
        self.calls.append(("post_task", (submission, token)))
        if self.cancellation is not None:
            raise self.cancellation
        if self.completion_status != "recorded":
            return PostTaskResult(
                status=self.completion_status,
                reason=(
                    "scope_changed"
                    if self.completion_status == "preflight_required"
                    else "review_required"
                    if self.completion_status == "review_required"
                    else "evidence_rejected"
                ),
                task_id=submission.task_id,
                graph_version=submission.graph_version,
            )
        return PostTaskResult(
            status="recorded",
            reason="recorded",
            task_id=submission.task_id,
            graph_version=4,
            claim=ImplementationClaim(
                id="claim:host",
                status=ImplementationStatus.IMPLEMENTED_BASELINE,
                requirement_refs=submission.requirement_ids,
                current_behavior="Implement approved CSV export",
                code_evidence=submission.git_evidence_refs,
                test_evidence=submission.test_evidence_refs,
                verified_commit=submission.final_revision,
                verified_at=NOW,
            ),
            changeset_id="changeset:host",
        )

    def authorization_revoke(self, token: str) -> None:
        self.calls.append(("revoke", token))


@pytest.mark.anyio
async def test_enabled_host_delegates_exact_post_task_once_and_revokes_private_state() -> None:
    workflow = _HostWorkflow()
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)
    task = await adapter.before_task(REQUEST, "local:asha")
    result = HostTaskResult(
        task_id=task.id,
        status="completed",
        response_evidence_ref="evidence:conversation:" + "4" * 64,
        changed_paths=("src/export.py", "tests/test_export.py"),
        base_revision=BASE,
        commit_sha=FINAL,
        requirement_ids=("requirement:csv-export",),
        code_refs=("file:export",),
        test_refs=("test:export",),
        git_evidence_refs=("evidence:git",),
        test_evidence_refs=("evidence:test-run",),
        completed_at=NOW,
    )

    completion = await adapter.after_task(task, result)

    assert completion is not None
    assert completion.status == "recorded"
    assert [name for name, _value in workflow.calls] == ["post_task", "revoke"]
    submission, capability = workflow.calls[0][1]  # type: ignore[misc]
    assert type(submission) is PostTaskSubmission
    assert submission.task_id == task.id
    assert submission.request_digest == task.request_digest
    assert capability == "private-host-capability"
    assert task.id not in adapter._tasks  # type: ignore[attr-defined]
    assert task.id not in adapter._tokens  # type: ignore[attr-defined]


class _HostCancellation(BaseException):
    pass


@pytest.mark.anyio
async def test_cancelled_host_completion_preserves_identity_and_revokes_capability() -> None:
    signal = _HostCancellation()
    workflow = _HostWorkflow(cancellation=signal)
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)
    task = await adapter.before_task(REQUEST, "local:asha")
    result = HostTaskResult(
        task_id=task.id,
        status="completed",
        response_evidence_ref="evidence:conversation:" + "4" * 64,
        changed_paths=("src/export.py", "tests/test_export.py"),
        base_revision=BASE,
        commit_sha=FINAL,
        requirement_ids=("requirement:csv-export",),
        code_refs=("file:export",),
        test_refs=("test:export",),
        git_evidence_refs=("evidence:git",),
        test_evidence_refs=("evidence:test-run",),
        completed_at=NOW,
    )

    with pytest.raises(_HostCancellation) as caught:
        await adapter.after_task(task, result)

    assert caught.value is signal
    assert workflow.calls[-1] == ("revoke", "private-host-capability")
    assert task.id not in adapter._tasks  # type: ignore[attr-defined]
    assert task.id not in adapter._tokens  # type: ignore[attr-defined]


class _MismatchedHostWorkflow(_HostWorkflow):
    async def post_task_evaluate(
        self, submission: PostTaskSubmission, *, token: str
    ) -> PostTaskResult:
        valid = await super().post_task_evaluate(submission, token=token)
        return valid.model_copy(update={"task_id": "task:sha256:" + "f" * 64})


@pytest.mark.anyio
async def test_malformed_host_completion_revokes_private_state() -> None:
    workflow = _MismatchedHostWorkflow()
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)
    task = await adapter.before_task(REQUEST, "local:asha")
    result = HostTaskResult(
        task_id=task.id,
        status="completed",
        response_evidence_ref="evidence:conversation:" + "4" * 64,
        changed_paths=("src/export.py", "tests/test_export.py"),
        base_revision=BASE,
        commit_sha=FINAL,
        requirement_ids=("requirement:csv-export",),
        code_refs=("file:export",),
        test_refs=("test:export",),
        git_evidence_refs=("evidence:git",),
        test_evidence_refs=("evidence:test-run",),
        completed_at=NOW,
    )

    with pytest.raises(ValueError, match="invalid host post-task result"):
        await adapter.after_task(task, result)

    assert workflow.calls[-1] == ("revoke", "private-host-capability")
    assert task.id not in adapter._tasks  # type: ignore[attr-defined]
    assert task.id not in adapter._tokens  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_disabled_host_after_task_is_a_transparent_noop() -> None:
    workflow = _HostWorkflow()
    adapter = IntentAgentHostAdapter(
        workflow=workflow,
        enabled=False,
        repository_id="demo",
        conversation_ref="host:thread-1:turn-1",
        graph_version=3,
        clock=lambda: NOW,
    )
    task = await adapter.before_task(REQUEST, "local:asha")
    result = HostTaskResult(
        task_id=task.id,
        status="failed",
        response_evidence_ref="evidence:conversation:" + "4" * 64,
        completed_at=NOW,
    )

    assert await adapter.after_task(task, result) is None
    assert workflow.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    ("preflight_required", "review_required", "rejected"),
)
async def test_review_fix_host_surfaces_exact_bounded_withheld_completion(
    status: str,
) -> None:
    workflow = _HostWorkflow(completion_status=status)
    adapter = IntentAgentHostAdapter(workflow=workflow, enabled=True)
    task = await adapter.before_task(REQUEST, "local:asha")
    host_result = HostTaskResult(
        task_id=task.id,
        status="completed",
        response_evidence_ref="evidence:conversation:" + "4" * 64,
        changed_paths=("src/export.py", "tests/test_export.py"),
        base_revision=BASE,
        commit_sha=FINAL,
        requirement_ids=("requirement:csv-export",),
        code_refs=("file:export",),
        test_refs=("test:export",),
        git_evidence_refs=("evidence:git",),
        test_evidence_refs=("evidence:test-run",),
        completed_at=NOW,
    )

    completion = await adapter.after_task(task, host_result)

    assert completion is not None
    assert (completion.status, completion.reason, completion.claim) == (
        status,
        "scope_changed"
        if status == "preflight_required"
        else "review_required"
        if status == "review_required"
        else "evidence_rejected",
        None,
    )
    assert workflow.calls[-1] == ("revoke", "private-host-capability")
    assert task.id not in adapter._tasks  # type: ignore[attr-defined]
