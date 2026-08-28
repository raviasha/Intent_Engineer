"""CLI contracts for exact local preview and independent approval."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from intent_engineering.capture.mcp import McpConnector, McpConnectorConfig, McpSchemaError
from intent_engineering.cli.app import app
from intent_engineering.cli.connectors import (
    ConfiguredConnector,
    ConnectorCatalog,
    load_connector_catalog,
)
from intent_engineering.cli.writes import (
    McpMutationGateway,
    MutationPolicy,
    WriteWorkflow,
    load_write_workflow,
)
from intent_engineering.core.models import (
    EvidenceRecord,
    Graph,
    JsonValue,
    Node,
    NodeType,
    ProjectConfig,
    ReconciliationStatus,
    ResolutionAction,
    SourceMode,
)
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.mutations.models import ExecutionReceipt, WritePlan, write_plan_id
from tests.unit.mutations.test_planner import base_plan, review_case

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _FakeTerminal:
    interactive: bool
    answer: str
    events: list[tuple[str, object]] = field(default_factory=list)

    def is_interactive(self) -> bool:
        return self.interactive

    def display_preview(self, plan: object) -> None:
        self.events.append(("preview", plan))

    def read_confirmation(self, _plan_id: str) -> str:
        self.events.append(("confirmation", _plan_id))
        return self.answer


@dataclass
class _FakeWrites:
    approvals: int = 0

    def preview(self, _plan_id: str, **_kwargs: object) -> WritePlan:
        return base_plan()

    def approve(self, plan_id: str, terminal: _FakeTerminal) -> dict[str, object]:
        assert terminal.is_interactive()
        if terminal.read_confirmation(plan_id) != f"approve {plan_id}":
            raise ValueError("rejected")
        self.approvals += 1
        return {"id": "approval:1", "plan_id": plan_id, "plan_hash": "sha256:" + "1" * 64}


def test_approval_requires_interactive_exact_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from intent_engineering.cli import writes

    workflow = _FakeWrites()
    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    monkeypatch.setattr(writes, "load_write_workflow", lambda _project: workflow)
    runner = CliRunner()
    plan_id = "write-plan:sha256:" + "a" * 64

    previewed = runner.invoke(
        app,
        ["write", "preview", plan_id, "--project", str(tmp_path), "--format", "json"],
    )

    monkeypatch.setattr(writes, "terminal", lambda: _FakeTerminal(False, ""))
    noninteractive = runner.invoke(
        app,
        ["write", "approve", plan_id, "--project", str(tmp_path), "--format", "json"],
    )
    monkeypatch.setattr(writes, "terminal", lambda: _FakeTerminal(True, "no"))
    rejected = runner.invoke(
        app,
        ["write", "approve", plan_id, "--project", str(tmp_path), "--format", "json"],
    )
    accepted_terminal = _FakeTerminal(True, f"approve {plan_id}")
    monkeypatch.setattr(writes, "terminal", lambda: accepted_terminal)
    accepted = runner.invoke(
        app,
        ["write", "approve", plan_id, "--project", str(tmp_path), "--format", "json"],
    )

    assert noninteractive.exit_code == rejected.exit_code == 4
    assert workflow.approvals == 1
    assert previewed.exit_code == 0
    assert json.loads(previewed.stdout)["plan_hash"] == base_plan().canonical_hash
    assert accepted.exit_code == 0
    assert json.loads(accepted.stdout)["plan_hash"].startswith("sha256:")
    assert [event for event, _value in accepted_terminal.events] == [
        "preview",
        "confirmation",
    ]
    displayed = accepted_terminal.events[0][1]
    assert isinstance(displayed, dict)
    assert displayed["plan_hash"].startswith("sha256:")
    assert displayed["before"] == base_plan().model_dump(mode="json")["before"]
    assert displayed["after"] == base_plan().model_dump(mode="json")["after"]


def _approval_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    config = ProjectConfig(
        project_id="project",
        local_actor="local:reviewer",
        source_exclusions=(".intent/**", ".git/**"),
    )
    (project / ".intent/config.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    profile_dir = project / "profiles/mcp"
    profile_dir.mkdir(parents=True)
    (profile_dir / "jira.yaml").write_bytes((ROOT / "profiles/mcp/jira.yaml").read_bytes())
    loaded = yaml.safe_load(
        (ROOT / "profiles/mcp/example-bindings/jira.yaml").read_text(encoding="utf-8")
    )
    assert type(loaded) is dict
    loaded["binding"]["actor_principals"] = {  # type: ignore[index]
        "local:alice": ["jira-account-101"],
        "local:proposer": ["jira-account-303"],
        "local:reviewer": ["jira-account-404"],
        "local:security": ["jira-account-505"],
    }
    connector = McpConnectorConfig.model_validate(loaded)
    (project / ".intent/connectors/jira.yaml").write_text(
        yaml.safe_dump(connector.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    policy = {
        "schema_version": 1,
        "contributors": ["local:proposer"],
        "approvers": ["local:reviewer", "local:security"],
        "executors": ["local:reviewer", "local:security"],
        "identities": {
            "local:proposer": [
                "git:proposer@example.com",
                "jira-account-303",
                "local:proposer",
            ],
            "local:reviewer": [
                "jira-account-404",
                "local:reviewer",
                "slack-user-404",
            ],
            "local:security": ["jira-account-505", "local:security"],
        },
    }
    (project / ".intent/approvals/policy.yaml").write_text(
        yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
    )
    return project


def test_mutation_policy_rejects_duplicate_roles_and_aliases() -> None:
    with pytest.raises(ValueError):
        MutationPolicy.model_validate(
            {
                "schema_version": 1,
                "contributors": ["local:one", "local:one"],
                "approvers": ["local:two"],
                "executors": ["local:two"],
                "identities": {
                    "local:one": ["local:one", "provider:one"],
                    "local:two": ["local:two"],
                },
            }
        )
    with pytest.raises(ValueError):
        MutationPolicy.model_validate(
            {
                "schema_version": 1,
                "contributors": ["local:one"],
                "approvers": ["local:two"],
                "executors": ["local:two"],
                "identities": {
                    "local:one": ["local:one", "provider:one", "provider:one"],
                    "local:two": ["local:two"],
                },
            }
        )


class _WriteRuntime:
    def __init__(self) -> None:
        self.issue = json.loads(
            (ROOT / "tests/fixtures/mcp/jira/issue.json").read_text(encoding="utf-8")
        )["raw"]
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []

    async def call(self, _server: object, name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "get_issue":
            return copy.deepcopy(self.issue)
        if name == "update_issue":
            self.issue["updated"] = "2026-08-20T13:00:00Z"
            fields = self.issue["fields"]
            assert type(fields) is dict
            for field in ("summary", "description", "status"):
                fields[field] = arguments[field]
            return {"updated": "2026-08-20T13:00:00Z"}
        raise AssertionError(name)

    async def read_resource(self, _server: object, _uri: str) -> JsonValue:
        raise AssertionError("Jira reference profile uses tools")


def _workflow_with_runtime(project: Path, runtime: _WriteRuntime) -> WriteWorkflow:
    loaded = load_write_workflow(project)
    catalog = ConnectorCatalog(
        loaded.catalog.runtime,
        loaded.catalog.configured,
        mcp_runtime=runtime,  # type: ignore[arg-type]
    )
    return WriteWorkflow(catalog, loaded.plans, loaded.approvals, loaded.policy, loaded.actor)


def _set_actor(project: Path, actor: str) -> None:
    config = ProjectConfig(
        project_id="project",
        local_actor=actor,
        source_exclusions=(".intent/**", ".git/**"),
    )
    (project / ".intent/config.yaml").write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )


def _seed_review_state(workflow: WriteWorkflow) -> None:
    timestamp = datetime(2026, 8, 26, 11, 0, tzinfo=UTC)
    nodes = tuple(
        Node(
            id=node_id,
            type=NodeType.REQUIREMENT,
            label=node_id,
            status="active",
            created_by="fixture",
            created_at=timestamp,
            last_modified_by="fixture",
            last_modified_at=timestamp,
            source_mode=SourceMode.EXPLICIT,
            evidence_refs=("evidence:intent",),
        )
        for node_id in ("requirement:export-policy", "jira:jira-issue-1001")
    )
    workflow.catalog.runtime.graph_store.initialize(
        Graph(
            id="graph:project",
            version=0,
            name="project",
            purpose="fixture",
            nodes=nodes,
            edges=(),
        )
    )
    case = review_case().model_copy(
        update={"affected_refs": ("requirement:export-policy", "jira:jira-issue-1001")}
    )
    workflow.catalog.runtime.case_store.put(case)
    for evidence_id, author in (
        ("evidence:intent", "jira-account-101"),
        ("evidence:requirement", "jira-account-202"),
    ):
        workflow.catalog.runtime.evidence_store.put(
            EvidenceRecord(
                id=evidence_id,
                connector_type="fixture",
                external_object_id=evidence_id,
                external_version="1",
                author=author,
                observed_at=timestamp,
                source_locator="fixture://write-preview",
                content_hash="sha256:" + "1" * 64,
                payload={"kind": "fixture"},
                acl=(),
            )
        )


def test_production_workflow_reloads_full_preview_and_preserves_independent_actor(
    tmp_path: Path,
) -> None:
    project = _approval_project(tmp_path)
    workflow = load_write_workflow(project)
    source_plan = base_plan()
    selected = workflow.catalog.configured[0]
    connector_id = McpConnector(
        workflow.catalog.mcp_runtime,
        config=selected.config,
        profile=selected.profile,
        object_name=source_plan.object_type,
        local_actor=source_plan.created_by,
    ).connector_id
    material = source_plan.model_dump(mode="json", exclude={"id"})
    material["connector_id"] = connector_id
    plan = WritePlan.model_validate_json(json.dumps({"id": write_plan_id(material), **material}))
    assert workflow.plans.put(plan)

    preview = workflow.preview(plan.id)
    approval = workflow.approve(
        plan.id,
        _FakeTerminal(True, f"approve {plan.id}"),
        now=datetime(2026, 8, 26, 12, 1, tzinfo=UTC),
    )

    assert preview == plan
    assert approval.plan_id == plan.id
    assert approval.actor == "local:reviewer"
    assert approval.actor != plan.created_by
    assert "jira-account-404" in approval.actor_aliases


@pytest.mark.anyio
async def test_preview_fetches_exact_remote_state_and_persists_hash_bound_plan(
    tmp_path: Path,
) -> None:
    project = _approval_project(tmp_path)
    _set_actor(project, "local:proposer")
    runtime = _WriteRuntime()
    workflow = _workflow_with_runtime(project, runtime)
    _seed_review_state(workflow)
    case = workflow.catalog.runtime.case_store.get("case-write-1")

    plan = await workflow.create_preview(
        case.id,
        connector_id="jira-local",
        operation="update_issue",
        requested_fields={
            "summary": "Keep exports local by default",
            "description": "Enable centralized export only after independent approval.",
            "status": "Approved",
        },
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    assert workflow.preview(plan.id) == plan
    assert plan.target_id == "jira-issue-1001"
    assert plan.before_version == "2026-08-20T12:00:00Z"
    assert plan.before["original_author_id"] == "jira-account-101"
    assert plan.created_by == "local:proposer"
    assert plan.conflicting_authors == ("jira-account-101", "jira-account-202")
    assert runtime.calls == [("get_issue", {"issue_id": "jira-issue-1001"})]


@pytest.mark.anyio
async def test_gateway_calls_only_bound_write_and_returns_selected_result_version(
    tmp_path: Path,
) -> None:
    project = _approval_project(tmp_path)
    runtime = _WriteRuntime()
    catalog = load_connector_catalog(project)
    selected = catalog.configured[0]
    gateway = McpMutationGateway(
        runtime,  # type: ignore[arg-type]
        selected,
        local_actor="local:reviewer",
        object_type="issue",
        semantic_operation="update_issue",
    )

    result = await gateway.execute(
        "update_issue",
        {
            "issue_id": "jira-issue-1001",
            "expected_version": "2026-08-20T12:00:00Z",
            "summary": "new",
            "description": "new",
            "status": "Approved",
        },
    )

    assert result.resulting_version == "2026-08-20T13:00:00Z"
    assert dict(result.redacted_result) == {"status": "verified"}
    assert runtime.calls == [
        (
            "update_issue",
            {
                "issue_id": "jira-issue-1001",
                "expected_version": "2026-08-20T12:00:00Z",
                "summary": "new",
                "description": "new",
                "status": "Approved",
            },
        )
    ]


@pytest.mark.anyio
async def test_gateway_schema_failure_retains_no_provider_payload_in_repository_traceback(
    tmp_path: Path,
) -> None:
    project = _approval_project(tmp_path)
    runtime = _WriteRuntime()
    secret = "provider-private-payload-7719"
    runtime.issue["fields"] = {"description": secret, "status": "Open"}
    catalog = load_connector_catalog(project)
    gateway = McpMutationGateway(
        runtime,  # type: ignore[arg-type]
        catalog.configured[0],
        local_actor="local:reviewer",
        object_type="issue",
        semantic_operation="update_issue",
    )

    with pytest.raises(McpSchemaError) as caught:
        await gateway.fetch_object("jira-issue-1001")

    repository_locals: list[str] = []
    current = caught.value.__traceback__
    while current is not None:
        if "/src/intent_engineering/" in current.tb_frame.f_code.co_filename:
            repository_locals.append(repr(current.tb_frame.f_locals))
        current = current.tb_next
    assert secret not in "".join(repository_locals)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.anyio
async def test_gateway_rejects_object_identity_that_disagrees_with_write_contract(
    tmp_path: Path,
) -> None:
    project = _approval_project(tmp_path)
    runtime = _WriteRuntime()
    catalog = load_connector_catalog(project)
    selected = catalog.configured[0]
    profile_payload = selected.profile.model_dump(mode="json")
    profile_payload["writes"]["update_issue"]["target_id"] = {  # type: ignore[index]
        "path": "$.key",
        "transforms": ["string"],
        "required": True,
    }
    changed_profile = type(selected.profile).model_validate(profile_payload)
    gateway = McpMutationGateway(
        runtime,  # type: ignore[arg-type]
        ConfiguredConnector(selected.config, changed_profile),
        local_actor="local:reviewer",
        object_type="issue",
        semantic_operation="update_issue",
    )

    with pytest.raises(McpSchemaError):
        await gateway.fetch_object("jira-issue-1001")


@pytest.mark.anyio
async def test_gateway_refetches_the_live_target_without_the_approved_version(
    tmp_path: Path,
) -> None:
    project = _approval_project(tmp_path)
    runtime = _WriteRuntime()
    historical = copy.deepcopy(runtime.issue)
    runtime.issue["updated"] = "2026-08-20T13:00:00Z"
    catalog = load_connector_catalog(project)
    selected = catalog.configured[0]
    profile_payload = selected.profile.model_dump(mode="json")
    fetch_arguments = profile_payload["operations"]["fetch_issue"]["arguments"]  # type: ignore[index]
    fetch_arguments["known_version"] = {"source": "object_version"}  # type: ignore[index]
    changed_profile = type(selected.profile).model_validate(profile_payload)

    async def versioned_call(
        _server: object,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        runtime.calls.append((name, copy.deepcopy(arguments)))
        assert name == "get_issue"
        return copy.deepcopy(historical if arguments["known_version"] else runtime.issue)

    runtime.call = versioned_call  # type: ignore[method-assign]
    plan = base_plan().model_copy(
        update={
            "target_id": "jira-issue-1001",
            "before_version": "2026-08-20T12:00:00Z",
        }
    )
    gateway = McpMutationGateway(
        runtime,  # type: ignore[arg-type]
        ConfiguredConnector(selected.config, changed_profile),
        local_actor="local:reviewer",
        object_type="issue",
        semantic_operation="update_issue",
        connector_id=plan.connector_id,
    )

    current = await gateway.fetch_target(plan)

    assert current.version == "2026-08-20T13:00:00Z"
    assert runtime.calls == [("get_issue", {"issue_id": "jira-issue-1001", "known_version": None})]


@pytest.mark.anyio
async def test_production_execute_requires_separate_approval_and_commits_authorship(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from intent_engineering.cli import writes

    project = _approval_project(tmp_path)
    provider = _WriteRuntime()
    _set_actor(project, "local:proposer")
    proposer = _workflow_with_runtime(project, provider)
    _seed_review_state(proposer)
    plan = await proposer.create_preview(
        "case-write-1",
        connector_id="jira-local",
        operation="update_issue",
        requested_fields={
            "summary": "Keep exports local by default",
            "description": "Enable centralized export only after independent approval.",
            "status": "Approved",
        },
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=datetime(2026, 8, 26, 12, 0, tzinfo=UTC),
    )

    _set_actor(project, "local:reviewer")
    reviewer = _workflow_with_runtime(project, provider)
    approval = reviewer.approve(
        plan.id,
        _FakeTerminal(True, f"approve {plan.id}"),
        now=datetime(2026, 8, 26, 12, 1, tzinfo=UTC),
    )
    approvals_directory = project / ".intent/approvals"
    held_approvals = tmp_path / "held-approvals"
    approvals_directory.rename(held_approvals)
    approvals_directory.mkdir()
    receipt_stores: list[object] = []
    real_receipt_store = writes.JsonlReceiptStore

    def coordinated_receipt_store(path: object, *, transactions: object) -> object:
        assert transactions is reviewer.catalog.runtime.transactions
        store = real_receipt_store(path, transactions=transactions)  # type: ignore[arg-type]
        receipt_stores.append(store)
        return store

    monkeypatch.setattr(writes, "JsonlReceiptStore", coordinated_receipt_store)
    receipt = await reviewer.execute(
        plan.id,
        approval.id,
        now=datetime(2026, 8, 26, 12, 2, tzinfo=UTC),
    )

    assert isinstance(receipt, ExecutionReceipt)
    assert receipt.status == "succeeded"
    assert receipt.executed_by == "local:reviewer"
    assert (
        reviewer.catalog.runtime.case_store.get(plan.case_id).status
        is ReconciliationStatus.RESOLVED
    )
    assert reviewer.catalog.runtime.graph_store.load().version == 1
    assert receipt.evidence_ref is not None
    evidence = reviewer.catalog.runtime.evidence_store.get(receipt.evidence_ref)
    assert evidence.author == "local:reviewer"
    assert evidence.payload["plan"]["created_by"] == "local:proposer"  # type: ignore[index]
    assert [name for name, _arguments in provider.calls].count("update_issue") == 1
    assert len(receipt_stores) == 1
    assert (held_approvals / "receipts.jsonl").stat().st_size > 0
    assert not (approvals_directory / "receipts.jsonl").exists()
