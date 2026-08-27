"""Offline production-composition harness for the intent-aware agent journey."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast

import httpx
import yaml  # type: ignore[import-untyped]
from structlog.testing import capture_logs

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.capture.git.connector import GitConnector
from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.mcp.runtime import create_production_session
from intent_engineering.capture.mcp.session import McpServerConfig
from intent_engineering.cli.runtime import Runtime, load_runtime, run_selected_sync
from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    EvidenceRecord,
    JsonValue,
    Node,
    NodeType,
    NodeUpdate,
    ProjectConfig,
    RelationType,
    ResolutionAction,
    SourceMode,
    SourceRole,
    SourceRoleAssignment,
)
from intent_engineering.integrations.agent_host import (
    HostTask,
    HostTaskResult,
    IntentAgentHostAdapter,
    MandatoryHookUnavailable,
    MutationDecision,
)
from intent_engineering.integrations.agent_host.codex import (
    CodexHostContract,
    CodexIntentAdapter,
)
from intent_engineering.integrations.mcp_server.server import build_server
from intent_engineering.integrations.mcp_server.tools import McpReadServices
from intent_engineering.intent_workflow.authorization import (
    AuthorizationIssuer,
    AuthorizationVerification,
)
from intent_engineering.intent_workflow.bootstrap import BootstrapService, BootstrapSubmission
from intent_engineering.intent_workflow.clarification import (
    ClarificationCoordinator,
    ProposalConfirmationService,
)
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.models import (
    ClarificationProposalSubmission,
    ClarificationQuestionInput,
    PreflightResult,
    TaskClassification,
    TaskEnvelope,
)
from intent_engineering.intent_workflow.post_task import (
    PostTaskResult,
    PostTaskService,
    PostTaskSubmission,
)
from intent_engineering.intent_workflow.preflight import (
    AgentClassificationSubmission,
    PreflightService,
    classification_evidence_content,
)
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.validation.service import validate_project
from tests.e2e.test_cli_write_approval import _FakeTerminal, _WriteRuntime
from tests.e2e.test_mcp_write_guard import _live_fake_workflow, _mutation_services
from tests.helpers.cli import init_git_repo, run_intent
from tests.integration.github.conftest import FakeGitHubApi
from tests.integration.mcp.test_read_sync import McpSyncHarness
from tests.unit.capture.mcp.test_runtime import _CapturedSdk

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
AGENT = "agent:codex"
CONTRIBUTOR = "local:asha"
REVIEWER = "local:ben"
GIT_AUTHOR = "Asha <asha@example.test>"

GITHUB_SENTINEL = "gh" + "p_INTENT_AGENT_E2E_91fd"
SLACK_SENTINEL = "slack-intent-agent-e2e-91fd"
JIRA_SENTINEL = "jira-intent-agent-e2e-91fd"
REQUEST_SENTINEL = "request-intent-agent-e2e-91fd"
TEST_SENTINEL = "test-intent-agent-e2e-91fd"


@dataclass(frozen=True)
class BootstrapOutcome:
    core_confirmed: bool
    provisional_ids: tuple[str, ...]
    graph_version: int
    evidence_author: str | None
    evidence_version: str
    evidence_acl: tuple[str, ...]
    replay_byte_stable: bool


@dataclass(frozen=True)
class TeammateOutcome:
    evidence_ids: tuple[str, ...]
    authors: tuple[str | None, ...]
    versions: tuple[str, ...]
    predecessors: tuple[str | None, ...]
    graph_version: int


@dataclass(frozen=True)
class AlignedOutcome:
    classification: str
    mutation: MutationDecision
    post_task: PostTaskResult
    final_revision: str
    graph_version: int
    detached: bool


@dataclass(frozen=True)
class ClarificationOutcome:
    classification: str
    question_count: int
    proposal_id: str
    decision_id: str | None
    graph_version: int
    chronology: tuple[str, ...]


@dataclass(frozen=True)
class ConflictOutcome:
    classification: str
    preflight_case_id: str
    mutation: MutationDecision
    self_review_status: str
    independent_review_status: str
    review_case_id: str
    reviewer_aliases: tuple[str, ...]
    graph_version: int


@dataclass(frozen=True)
class HostModeOutcome:
    mandatory_error: str
    disabled_reason: str
    disabled_workflow_calls: int
    plugin_directory_exists: bool


@dataclass(frozen=True)
class AssuranceOutcome:
    first_evidence: int
    first_cases: int
    second_evidence: int
    second_changes: int
    second_cases: int
    fingerprints: tuple[str, ...]
    checkpoint_byte_stable: bool


@dataclass(frozen=True)
class ViewOutcome:
    graph_version: int
    cli_graph_version: int
    mcp_graph_version: int
    valid: bool
    context_requirement_ids: tuple[str, ...]


@dataclass(frozen=True)
class WriteOutcome:
    missing_status: str
    missing_reason: str
    missing_provider_calls: int
    success_status: str
    provider_mutations: int
    plan_id: str
    approval_id: str
    receipt_id: str
    resulting_version: str
    evidence_author: str | None
    evidence_plan_id: str
    evidence_approval_id: str
    changed_status: str
    changed_reason: str
    changed_provider_mutations: int
    changed_fresh_reads: int
    graph_version: int
    shared_transaction_coordinator: bool
    all_services_hold_runtime: bool


class _ProductionWorkflowPort:
    """Thin host port over the real preflight, issuer, and post-task services."""

    def __init__(self, harness: IntentAwareAgentHarness) -> None:
        self.harness = harness
        self.calls = 0
        self.last_preflight: PreflightResult | None = None
        self.last_token: str | None = None

    async def before_task(
        self, request: str, actor: str
    ) -> tuple[TaskEnvelope, PreflightResult, str | None]:
        self.calls += 1
        envelope, result, token = self.harness._classify(request, actor)
        self.last_preflight = result
        self.last_token = token
        return envelope, result, token

    async def authorization_verify(
        self,
        *,
        token: str,
        actor: str,
        repository_id: str,
        task_id: str,
        graph_version: int,
        requested_paths: tuple[str, ...],
    ) -> AuthorizationVerification:
        return self.harness.issuer.verify(
            token,
            actor=actor,
            repository_id=repository_id,
            task_id=task_id,
            graph_version=graph_version,
            graph_content=self.harness._graph_file.read_bytes(),
            requested_paths=requested_paths,
            now=self.harness._tick(),
        )

    async def post_task_evaluate(
        self, submission: PostTaskSubmission, *, token: str
    ) -> PostTaskResult:
        return self.harness.post_task_service.evaluate(submission, token=token)

    def authorization_revoke(self, token: str) -> None:
        self.harness.issuer.revoke(token)


class IntentAwareAgentHarness:
    """Drive one existing repository through the complete production workflow."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._now = NOW
        self._outputs: list[object] = []
        self._logs: list[object] = []
        self._errors: list[BaseException] = []
        self._github_clients: list[GitHubClient] = []
        self._github_api: FakeGitHubApi | None = None
        self._write_workflows: list[object] = []
        self._authorization_sentinels: list[str] = []
        self._task_records: dict[str, tuple[TaskEnvelope, str]] = {}
        self._envelopes: dict[str, TaskEnvelope] = {}
        self._classification_submissions: dict[str, AgentClassificationSubmission] = {}
        self._task_counter = 0
        self._prd_evidence_id = ""
        self._slack_evidence_ids: tuple[str, ...] = ()
        self._preflight_case_id = ""

        self.project = init_git_repo(root)
        self._create_existing_repository()
        initialized = run_intent(self.project, "init", "--project", ".", "--format", "json")
        assert initialized.returncode == 0, initialized.stderr
        self._outputs.extend((initialized.stdout, initialized.stderr))
        self._slack = McpSyncHarness(root / "external-slack")
        self._slack.runtime.credential_sentinel = SLACK_SENTINEL
        assert self._slack.runtime.raw is not None
        self._slack.runtime.raw["allowed_principals"] = [
            "U123",
            CONTRIBUTOR,
            REVIEWER,
            "slack-group:ENG",
        ]
        self._configure_project(CONTRIBUTOR)
        self._configure_write_and_review_authority()
        self._exercise_credential_boundaries()

        self.runtime: Runtime = load_runtime(self.project)
        self.executor = LocalChangeSetExecutor(
            self.runtime.graph_store,
            self.runtime.case_store,
            self.runtime.transactions,
        )
        self.capture = ConversationCapture(
            self.runtime.evidence_store,
            connector_id="conversation:codex",
        )
        self.bootstrap_service = BootstrapService(
            graph_store=self.runtime.graph_store,
            evidence_store=self.runtime.evidence_store,
            proposal_store=self.runtime.intent_proposals,
            changeset_executor=self.executor,
            transactions=self.runtime.transactions,
            config=self.runtime.config,
        )
        preflight_config = self.runtime.workspace_directory.file("config.yaml")
        self.preflight_service = PreflightService(
            transactions=self.runtime.transactions,
            config_file=preflight_config,
            agent_principal=AGENT,
            conversation_connector_id="conversation:codex",
        )
        preflight_config.close()
        self.clarification = ClarificationCoordinator(
            graph_store=self.runtime.graph_store,
            evidence_store=self.runtime.evidence_store,
            proposal_store=self.runtime.intent_proposals,
            transactions=self.runtime.transactions,
            config=self.runtime.config,
            capture=self.capture,
        )
        confirmation_config = self.runtime.workspace_directory.file("config.yaml")
        confirmation_policy = self.runtime.workspace_directory.file("approvals/policy.yaml")
        confirmation_binding = self.runtime.workspace_directory.file("connectors/jira.yaml")
        self.confirmation = ProposalConfirmationService(
            graph_store=self.runtime.graph_store,
            evidence_store=self.runtime.evidence_store,
            case_store=self.runtime.case_store,
            proposal_store=self.runtime.intent_proposals,
            changeset_executor=self.executor,
            transactions=self.runtime.transactions,
            config_file=confirmation_config,
            policy_file=confirmation_policy,
            binding_files={"jira": confirmation_binding},
        )
        confirmation_config.close()
        confirmation_policy.close()
        confirmation_binding.close()
        self.issuer = AuthorizationIssuer()
        self._graph_file = self.runtime.workspace_directory.file("graph.yaml")
        self._evidence_file = self.runtime.workspace_directory.file("evidence/evidence.jsonl")
        self._authority_files = {
            "config": self.runtime.workspace_directory.file("config.yaml"),
            "policy": self.runtime.workspace_directory.file("approvals/policy.yaml"),
            "repository": self.runtime.workspace_directory.file("repository.id"),
            "binding": self.runtime.workspace_directory.file("connectors/jira.yaml"),
        }
        self.post_task_service = PostTaskService(
            issuer=self.issuer,
            changeset_executor=self.executor,
            transactions=self.runtime.transactions,
            graph_file=self._graph_file,
            evidence_file=self._evidence_file,
            authority_files=self._authority_files,
            authority_resolver=self._resolve_post_task_authority,
            clock=lambda: self._now,
        )
        self.port = _ProductionWorkflowPort(self)
        self.host = IntentAgentHostAdapter(workflow=self.port, enabled=True)

    def _tick(self, *, seconds: int = 0, microseconds: int = 1) -> datetime:
        self._now += timedelta(seconds=seconds, microseconds=microseconds)
        return self._now

    def _create_existing_repository(self) -> None:
        docs = self.project / "docs"
        docs.mkdir()
        (docs / "prd.md").write_text(
            """# Local Report Export PRD

## Purpose
Analysts need a durable report copy without sending report data to a hosted service.

## Requirements
- The report page provides UTF-8 CSV export.
- Raw source conversations stay local.
- Jira issue jira-issue-1001 tracks the export policy.

## Open question
Team sharing roles and expiry are not yet decided.
""",
            encoding="utf-8",
        )
        self._git("add", "docs/prd.md")
        self._git_commit("Add existing product requirements", self._tick(seconds=1))

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.project,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    def _git_commit(self, message: str, at: datetime) -> str:
        environment = dict(os.environ)
        timestamp = at.isoformat().replace("+00:00", "Z")
        environment.update(
            {
                "GIT_AUTHOR_DATE": timestamp,
                "GIT_COMMITTER_DATE": timestamp,
                "GIT_AUTHOR_NAME": "Asha",
                "GIT_AUTHOR_EMAIL": "asha@example.test",
                "GIT_COMMITTER_NAME": "Asha",
                "GIT_COMMITTER_EMAIL": "asha@example.test",
            }
        )
        subprocess.run(
            ["git", "commit", "--quiet", "-m", message],
            cwd=self.project,
            env=environment,
            check=True,
            capture_output=True,
        )
        return self._git("rev-parse", "HEAD")

    def _configure_project(self, actor: str) -> None:
        config_path = self.project / ".intent/config.yaml"
        config = ProjectConfig.model_validate_json(
            json.dumps(yaml.safe_load(config_path.read_text(encoding="utf-8")))
        )
        slack_connector = self._slack.connector()
        assert self._slack.runtime.raw is not None
        roles = (
            SourceRoleAssignment(
                connector_id="markdown",
                scope="docs/prd.md",
                role=SourceRole.DECLARED_INTENT,
                inherited=False,
            ),
            SourceRoleAssignment(
                connector_id=slack_connector.connector_id,
                scope=cast(str, self._slack.runtime.raw["permalink"]),
                role=SourceRole.PROPOSED_INTENT,
                inherited=False,
            ),
        )
        config = config.model_copy(update={"local_actor": actor, "source_roles": roles})
        config_path.write_text(
            yaml.safe_dump(config.model_dump(mode="json"), sort_keys=True),
            encoding="utf-8",
        )

    def _configure_write_and_review_authority(self) -> None:
        profile_dir = self.project / "profiles/mcp"
        profile_dir.mkdir(parents=True)
        shutil.copy2(ROOT / "profiles/mcp/jira.yaml", profile_dir / "jira.yaml")
        binding = yaml.safe_load(
            (ROOT / "profiles/mcp/example-bindings/jira.yaml").read_text(encoding="utf-8")
        )
        binding["binding"]["actor_principals"] = {
            CONTRIBUTOR: ["jira-account-101"],
            REVIEWER: ["jira-account-202"],
        }
        (self.project / ".intent/connectors/jira.yaml").write_text(
            yaml.safe_dump(binding, sort_keys=True), encoding="utf-8"
        )
        policy = {
            "schema_version": 1,
            "contributors": [CONTRIBUTOR],
            "approvers": [REVIEWER],
            "executors": [REVIEWER],
            "identities": {
                CONTRIBUTOR: [GIT_AUTHOR, "jira-account-101", CONTRIBUTOR],
                REVIEWER: ["jira-account-202", REVIEWER],
                "U456": ["U456"],
                "product:priya": ["product:priya"],
            },
        }
        (self.project / ".intent/approvals/policy.yaml").write_text(
            yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
        )
        (self.project / ".intent/repository.id").write_text(
            f"{self.project.name}\n", encoding="utf-8"
        )

    def _exercise_credential_boundaries(self) -> None:
        async def run() -> None:
            configurations = (
                (
                    SLACK_SENTINEL,
                    {"SLACK_TOKEN": SLACK_SENTINEL},
                    McpServerConfig(
                        id="e2e-slack",
                        transport="stdio",
                        command="slack-mcp",
                        environment_refs={"SLACK_TOKEN": "env:SLACK_TOKEN"},
                    ),
                ),
                (
                    JIRA_SENTINEL,
                    {"JIRA_TOKEN": JIRA_SENTINEL},
                    McpServerConfig(
                        id="e2e-jira",
                        transport="streamable_http",
                        url="https://jira.example.test/mcp",
                        headers={"Authorization": "env:JIRA_TOKEN"},
                    ),
                ),
            )
            for sentinel, environ, config in configurations:
                sdk = _CapturedSdk()
                session = create_production_session(
                    config,
                    sdk_loader=lambda sdk=sdk: sdk,
                    environ=environ,
                )
                await session.start()
                await session.close()
                assert sentinel not in repr(vars(session))

        stdout, stderr = StringIO(), StringIO()
        with capture_logs() as logs, redirect_stdout(stdout), redirect_stderr(stderr):
            asyncio.run(run())
        self._logs.append(logs)
        self._outputs.extend((stdout.getvalue(), stderr.getvalue()))

    def _github_client(self, credentials: GitHubCredentials) -> GitHubClient:
        api = self._github_api
        assert isinstance(api, FakeGitHubApi)
        client = GitHubClient(
            credentials,
            transport=httpx.MockTransport(api.handler),
            retry_policy=RetryPolicy(max_attempts=1),
        )
        self._github_clients.append(client)
        return client

    def _resolve_post_task_authority(
        self, actor: str, snapshot: Mapping[str, bytes | None]
    ) -> tuple[str, frozenset[str]]:
        if snapshot.get("repository") != f"{self.project.name}\n".encode():
            return "invalid", frozenset()
        config = ProjectConfig.model_validate_json(json.dumps(yaml.safe_load(snapshot["config"])))
        policy = yaml.safe_load(snapshot["policy"])
        binding = yaml.safe_load(snapshot["binding"])
        aliases = set(policy["identities"].get(actor, ()))
        aliases.update(binding["binding"]["actor_principals"].get(actor, ()))
        aliases.add(actor)
        return config.project_id, frozenset(aliases)

    @property
    def principals(self) -> frozenset[str]:
        return frozenset(
            {
                AGENT,
                CONTRIBUTOR,
                REVIEWER,
                GIT_AUTHOR,
                "U123",
                "U456",
                "jira-account-101",
                "jira-account-202",
                "product:priya",
            }
        )

    def _state_bytes(self) -> dict[str, bytes | None]:
        paths = {
            "graph": self.project / ".intent/graph.yaml",
            "history": self.project / ".intent/history/changesets.jsonl",
            "cases": self.project / ".intent/reconciliation/cases.jsonl",
            "evidence": self.project / ".intent/evidence/evidence.jsonl",
            "proposals": self.project / ".intent/history/intent-proposals.jsonl",
            "checkpoints": self.project / ".intent/cache/checkpoints.yaml",
            "receipts": self.project / ".intent/approvals/receipts.jsonl",
        }
        return {name: path.read_bytes() if path.exists() else None for name, path in paths.items()}

    def _candidate_node(
        self,
        node_id: str,
        node_type: NodeType,
        label: str,
        evidence_id: str,
        at: datetime,
        *,
        confidence: float = 0.84,
    ) -> Node:
        return Node(
            id=node_id,
            type=node_type,
            label=label,
            status="proposed",
            created_by=AGENT,
            created_at=at,
            last_modified_by=AGENT,
            last_modified_at=at,
            source_mode=SourceMode.INFERRED,
            intent_fidelity_confidence=confidence,
            confidence_basis="Inferred by the active agent from captured source evidence",
            last_reassessed_at=at,
            evidence_refs=(evidence_id,),
        )

    @staticmethod
    def _candidate_edge(
        edge_id: str,
        from_id: str,
        relation: RelationType,
        to_id: str,
        at: datetime,
    ) -> Edge:
        return Edge(
            id=edge_id,
            **{"from": from_id, "to": to_id},
            relation=relation,
            status="proposed",
            created_by=AGENT,
            created_at=at,
            last_modified_by=AGENT,
            last_modified_at=at,
        )

    def bootstrap(self, prd: str) -> BootstrapOutcome:
        """Capture, review, and activate the confirmed core of one existing PRD."""
        captured = run_intent(
            self.project,
            "bootstrap",
            "--prd",
            prd,
            "--format",
            "json",
        )
        assert captured.returncode == 4, captured.stderr
        payload = captured.json()
        assert payload["status"] == "agent_submission_required"
        evidence_id = cast(list[str], payload["evidence_refs"])[0]
        self._prd_evidence_id = evidence_id
        at = self._tick(seconds=1)
        nodes = (
            self._candidate_node(
                "intent:local-report-export",
                NodeType.PRODUCT_INTENT,
                "Keep report export local",
                evidence_id,
                at,
            ),
            self._candidate_node(
                "requirement:csv-export",
                NodeType.REQUIREMENT,
                "Provide UTF-8 CSV report export",
                evidence_id,
                at,
            ),
            self._candidate_node(
                "constraint:no-raw-cloud",
                NodeType.CONSTRAINT,
                "Do not send raw source conversations to a hosted service",
                evidence_id,
                at,
            ),
            self._candidate_node(
                "jira:jira-issue-1001",
                NodeType.SOURCE_ARTIFACT,
                "Jira issue tracking the export policy",
                evidence_id,
                at,
            ),
            self._candidate_node(
                "file:csv-export",
                NodeType.FILE,
                "src/export.py",
                evidence_id,
                at,
                confidence=0.58,
            ),
            self._candidate_node(
                "test:csv-export",
                NodeType.TEST,
                "tests/test_export.py",
                evidence_id,
                at,
                confidence=0.58,
            ),
            self._candidate_node(
                "criterion:utf8",
                NodeType.ACCEPTANCE_CRITERION,
                "CSV output uses UTF-8 encoding",
                evidence_id,
                at,
                confidence=0.62,
            ),
        )
        edges = (
            self._candidate_edge(
                "edge:intent-csv",
                nodes[0].id,
                RelationType.REALIZED_BY,
                nodes[1].id,
                at,
            ),
            self._candidate_edge(
                "edge:local-constraint-csv",
                nodes[2].id,
                RelationType.CONSTRAINS,
                nodes[1].id,
                at,
            ),
            self._candidate_edge(
                "edge:csv-utf8",
                nodes[1].id,
                RelationType.HAS_ACCEPTANCE_CRITERION,
                nodes[6].id,
                at,
            ),
            self._candidate_edge(
                "edge:csv-jira",
                nodes[1].id,
                RelationType.SPECIFIED_BY,
                nodes[3].id,
                at,
            ),
        )
        submission = BootstrapSubmission(
            baseline_graph_version=0,
            actor=AGENT,
            timestamp=at,
            evidence_refs=(evidence_id,),
            source_roles=(self.runtime.config.source_roles[0],),
            candidate_nodes=nodes,
            candidate_edges=edges,
            core_node_ids=tuple(node.id for node in nodes[:6]),
            provisional_node_ids=(nodes[6].id,),
            assumptions=("Spreadsheet software accepts conventional CSV",),
            unanswered_questions=("How should nested values be represented?",),
        )
        review = self.bootstrap_service.propose(submission, self.principals)
        graph = self.bootstrap_service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor=CONTRIBUTOR,
            at=self._tick(),
        )
        activated = self._state_bytes()
        replay = self.bootstrap_service.activate(
            review.proposal_id,
            confirmed_node_ids=tuple(node.id for node in review.core_nodes),
            actor=CONTRIBUTOR,
            at=self._now,
        )
        record = next(item for item in self.runtime.evidence() if item.id == evidence_id)
        return BootstrapOutcome(
            core_confirmed=all(node.status == "active" for node in graph.nodes),
            provisional_ids=tuple(node.id for node in review.provisional_nodes),
            graph_version=graph.version,
            evidence_author=record.author,
            evidence_version=record.external_version,
            evidence_acl=record.acl,
            replay_byte_stable=replay == graph and self._state_bytes() == activated,
        )

    def ingest_teammate_revision(self) -> TeammateOutcome:
        """Ingest two immutable revisions of one real MCP conversation object."""
        connector = self._slack.connector()
        first = asyncio.run(self.runtime.sync.run("teammate-initial", (connector,)))
        assert first.evidence_added == 1
        assert self._slack.runtime.raw is not None
        self._slack.runtime.raw.update(
            {
                "updated": "1700000000.000300",
                "updated_at": "2026-08-28T12:10:00Z",
                "text": "Priya proposes reviewed team sharing; raw conversations remain local.",
                "last_modified_by": {"id": "U456"},
                "allowed_principals": [
                    "U123",
                    "U456",
                    CONTRIBUTOR,
                    REVIEWER,
                    "slack-group:ENG",
                ],
            }
        )
        second = asyncio.run(self.runtime.sync.run("teammate-revision", (self._slack.connector(),)))
        assert second.evidence_added == 1
        ledger = self.runtime.evidence_store.ledger(
            connector.connector_id,
            connector_type="mcp",
        )
        self._slack_evidence_ids = tuple(item.evidence.id for item in ledger)
        return TeammateOutcome(
            evidence_ids=self._slack_evidence_ids,
            authors=tuple(item.evidence.author for item in ledger),
            versions=tuple(item.evidence.external_version for item in ledger),
            predecessors=tuple(item.predecessor_id for item in ledger),
            graph_version=self.runtime.graph_store.load().version,
        )

    def _classify(
        self, request: str, actor: str
    ) -> tuple[TaskEnvelope, PreflightResult, str | None]:
        graph = self.runtime.graph_store.load()
        self._task_counter += 1
        conversation_ref = f"codex:e2e:{self._task_counter}"
        scope: tuple[str, ...]
        classification: TaskClassification
        relevant: tuple[str, ...] = ()
        evidence_refs: tuple[str, ...] = ()
        effects: tuple[str, ...] = ()
        questions: tuple[str, ...] = ()
        conflicts: tuple[str, ...] = ()
        lowered = request.lower()
        if "team sharing" in lowered:
            classification = TaskClassification.NEW_OR_AMBIGUOUS
            scope = ("src/sharing.py",)
            questions = tuple(
                sorted(
                    (
                        "Which roles may share an exported report?",
                        "When should a sharing grant expire?",
                    )
                )
            )
            conversation_acl = (actor,)
        elif "raw conversations" in lowered:
            classification = TaskClassification.CONFLICTING
            scope = ("src/upload.py",)
            relevant = ("constraint:no-raw-cloud", "jira:jira-issue-1001")
            evidence_refs = (self._prd_evidence_id,)
            effects = ("Send raw source conversations to a hosted service",)
            conflicts = ("The request contradicts the active local-only conversation boundary.",)
            conversation_acl = tuple(sorted((actor, REVIEWER)))
        else:
            classification = TaskClassification.ALIGNED
            scope = ("src/export.py", "tests/test_export.py")
            relevant = ("requirement:csv-export",)
            evidence_refs = (self._prd_evidence_id,)
            effects = ("Implement the confirmed CSV export requirement",)
            conversation_acl = (actor,)

        human = self.capture.record_turn(
            conversation_ref=conversation_ref,
            role="human",
            author=actor,
            content=request,
            captured_at=self._tick(),
            acl=conversation_acl,
        )
        envelope = TaskEnvelope(
            repository_id=self.runtime.config.project_id,
            actor=actor,
            conversation_ref=conversation_ref,
            request=request,
            request_evidence_ref=human.id,
            graph_version=graph.version,
            created_at=human.observed_at,
            requested_scope=scope,
        )
        material = {
            "task_id": envelope.id,
            "task_digest": envelope.digest,
            "graph_version": graph.version,
            "classification": classification,
            "basis": "Active-agent classification grounded in the current intent graph",
            "relevant_node_ids": relevant,
            "evidence_refs": evidence_refs,
            "semantic_effects": effects,
            "uncertainties": (),
            "questions": questions,
            "conflict_claims": conflicts,
            "requested_scope": scope,
        }
        agent = self.capture.record_turn(
            conversation_ref=conversation_ref,
            role="agent",
            author=AGENT,
            content=classification_evidence_content(**material),
            captured_at=self._tick(),
            acl=conversation_acl,
        )
        submission = AgentClassificationSubmission(
            **material,
            agent_evidence_ref=agent.id,
        )
        result = self.preflight_service.evaluate(
            envelope,
            submission,
            principals=frozenset({actor}),
        )
        token = None
        self._envelopes[envelope.id] = envelope
        if result.authorized:
            token = self.issuer.issue(
                envelope,
                result,
                graph_content=self._graph_file.read_bytes(),
                now=self._tick(),
            )
            self._authorization_sentinels.append(token)
            self._task_records[envelope.id] = (envelope, token)
        self._classification_submissions[envelope.id] = submission
        return envelope, result, token

    def _test_evidence(self, revision: str, at: datetime) -> EvidenceRecord:
        payload: dict[str, JsonValue] = {
            "commit_sha": revision,
            "outcome": "passed",
            "test_refs": ["test:csv-export"],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        return normalize_raw_source(
            RawSourceObject(
                connector_type="test_result",
                external_object_id=f"test-run:{revision}",
                external_version=f"run:{digest}",
                author="ci:test-runner",
                observed_at=at,
                source_locator=f"test:run:{revision}",
                content_hash=f"sha256:{digest}",
                payload=payload,
                acl=(CONTRIBUTOR,),
            )
        )

    def run_aligned_task(self) -> AlignedOutcome:
        """Authorize before effect, then link a real commit and passing test evidence."""

        async def run() -> AlignedOutcome:
            task = await self.host.before_task(
                "Implement the confirmed CSV export",
                CONTRIBUTOR,
            )
            decision = await self.host.before_mutation(
                task=task,
                operation="write_files",
                paths=("src/export.py", "tests/test_export.py"),
                token=None,
            )
            assert decision.allowed
            base = self._git("rev-parse", "HEAD")
            (self.project / "src").mkdir(exist_ok=True)
            (self.project / "tests").mkdir(exist_ok=True)
            (self.project / "src/export.py").write_text(
                "def export_csv(rows: list[tuple[str, str]]) -> bytes:\n"
                "    return ('\\n'.join(','.join(row) for row in rows) + '\\n').encode('utf-8')\n",
                encoding="utf-8",
            )
            (self.project / "tests/test_export.py").write_text(
                "import os\n\n"
                "from export import export_csv\n\n"
                "def test_export_is_utf8() -> None:\n"
                "    assert os.environ['INTENT_E2E_TEST_BOUNDARY']\n"
                "    assert export_csv([('city', 'München')]).decode('utf-8').endswith('München\\n')\n",
                encoding="utf-8",
            )
            test_environment = dict(os.environ)
            test_environment.update(
                {
                    "INTENT_E2E_TEST_BOUNDARY": TEST_SENTINEL,
                    "PYTHONPATH": str(self.project / "src"),
                }
            )
            tested = subprocess.run(  # noqa: ASYNC221 - real supported-host effect boundary
                [
                    str(ROOT / ".venv/bin/python"),
                    "-m",
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    "tests/test_export.py",
                    "-q",
                ],
                cwd=self.project,
                env=test_environment,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self._outputs.extend((tested.stdout, tested.stderr))
            self._git("add", "src/export.py", "tests/test_export.py")
            completed_at = self._tick(seconds=1)
            final = self._git_commit("Implement confirmed CSV export", completed_at)
            git_run = await self.runtime.sync.run(
                "aligned-git-evidence",
                (
                    GitConnector(
                        self.project,
                        repository_id=self.runtime.config.project_id,
                    ),
                ),
            )
            assert git_run.evidence_added >= 1
            git_record = next(
                record
                for record in self.runtime.evidence()
                if record.connector_type == "git" and record.external_version == final
            )
            test_record = self._test_evidence(final, self._tick())
            self.runtime.evidence_store.associate("test-results", test_record)
            response = self.capture.record_turn(
                conversation_ref=task.conversation_ref,
                role="agent",
                author=AGENT,
                content={"status": "completed", "commit": final},
                captured_at=self._tick(),
                acl=(CONTRIBUTOR,),
            )
            completion = await self.host.after_task(
                task,
                HostTaskResult(
                    task_id=task.id,
                    status="completed",
                    response_evidence_ref=response.id,
                    changed_paths=("src/export.py", "tests/test_export.py"),
                    base_revision=base,
                    commit_sha=final,
                    requirement_ids=("requirement:csv-export",),
                    code_refs=("file:csv-export",),
                    test_refs=("test:csv-export",),
                    git_evidence_refs=(git_record.id,),
                    test_evidence_refs=(test_record.id,),
                    completed_at=self._tick(),
                ),
            )
            assert completion is not None
            assert completion.status == "recorded", completion
            return AlignedOutcome(
                classification=cast(PreflightResult, task.preflight).classification.value,
                mutation=decision,
                post_task=completion,
                final_revision=final,
                graph_version=self.runtime.graph_store.load().version,
                detached="token" not in completion.model_dump(),
            )

        return asyncio.run(run())

    def clarify_new_requirement(self) -> ClarificationOutcome:
        """Persist questions and answers, propose, then confirm as a contributor."""
        envelope, preflight, token = self._classify("Add team sharing", CONTRIBUTOR)
        assert token is None
        assert preflight.classification is TaskClassification.NEW_OR_AMBIGUOUS
        submission = self._classification_submissions[envelope.id]
        principals = frozenset({AGENT, CONTRIBUTOR})
        opened = self.clarification.open(
            envelope,
            classification_evidence_ref=submission.agent_evidence_ref,
            questions=(
                ClarificationQuestionInput(
                    id="audience",
                    prompt=preflight.questions[0],
                    required=True,
                ),
                ClarificationQuestionInput(
                    id="expiry",
                    prompt=preflight.questions[1],
                    required=True,
                ),
            ),
            opened_by=AGENT,
            opened_at=self._tick(),
            principals=principals,
        )
        answered = self.clarification.answer(
            opened.id,
            actor=CONTRIBUTOR,
            question_id="audience",
            answer="Workspace admins may share read-only exported reports.",
            answered_at=self._tick(microseconds=3),
            acl=tuple(sorted(principals)),
            principals=principals,
        )
        answered = self.clarification.answer(
            opened.id,
            actor=CONTRIBUTOR,
            question_id="expiry",
            answer="Sharing grants expire after seven days.",
            answered_at=self._tick(),
            acl=tuple(sorted(principals)),
            principals=principals,
        )
        evidence_refs = (
            answered.request_evidence_ref,
            answered.classification_evidence_ref,
            *(question.evidence_ref for question in answered.questions),
            *(answer.evidence_ref for answer in answered.answers),
        )
        at = self._tick()
        node = Node(
            id="requirement:read-only-sharing",
            type=NodeType.REQUIREMENT,
            label="Workspace admins may share read-only reports for seven days",
            status="proposed",
            created_by=CONTRIBUTOR,
            created_at=at,
            last_modified_by=CONTRIBUTOR,
            last_modified_at=at,
            source_mode=SourceMode.INFERRED,
            intent_fidelity_confidence=0.86,
            confidence_basis="Required questions answered by the contributor",
            last_reassessed_at=at,
            evidence_refs=evidence_refs,
        )
        changeset = ChangeSet(
            id="",
            actor=CONTRIBUTOR,
            timestamp=at,
            baseline_graph_version=answered.baseline_graph_version,
            evidence_refs=evidence_refs,
            nodes_added=(node,),
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
        proposal = self.clarification.propose(
            ClarificationProposalSubmission(
                session_id=answered.id,
                task_id=answered.task_id,
                baseline_graph_version=answered.baseline_graph_version,
                actor=CONTRIBUTOR,
                timestamp=at,
                evidence_refs=evidence_refs,
                changeset=changeset,
                core_node_ids=(node.id,),
            ),
            principals=principals,
        )
        confirmation = self.confirmation.confirm(
            proposal.id,
            actor=CONTRIBUTOR,
            at=self._tick(),
            selected_node_ids=(node.id,),
        )
        events = self.runtime.intent_proposals.clarification_events(opened.id)
        return ClarificationOutcome(
            classification=preflight.classification.value,
            question_count=len(opened.questions),
            proposal_id=proposal.id,
            decision_id=confirmation.decision_id,
            graph_version=confirmation.graph_version,
            chronology=tuple(event.event_type for event in events),
        )

    def review_conflicting_request(self) -> ConflictOutcome:
        """Deny the mutation, then require an independent reviewer for the update."""

        async def preflight_and_deny() -> tuple[HostTask, MutationDecision]:
            task = await self.host.before_task(
                "Upload all raw conversations to a hosted service",
                CONTRIBUTOR,
            )
            denied = await self.host.before_mutation(
                task=task,
                operation="write_file",
                paths=("src/upload.py",),
                token=None,
            )
            return task, denied

        task, denied = asyncio.run(preflight_and_deny())
        preflight = cast(PreflightResult, task.preflight)
        assert preflight.review_case_id is not None
        envelope = self._envelopes[task.id]
        submission = self._classification_submissions[task.id]
        principals = frozenset({AGENT, CONTRIBUTOR, REVIEWER})
        opened = self.clarification.open(
            envelope,
            classification_evidence_ref=submission.agent_evidence_ref,
            questions=(
                ClarificationQuestionInput(
                    id="exception",
                    prompt="Should this replace the confirmed local-only boundary?",
                    required=True,
                ),
            ),
            opened_by=AGENT,
            opened_at=self._tick(),
            principals=principals,
        )
        answered = self.clarification.answer(
            opened.id,
            actor=CONTRIBUTOR,
            question_id="exception",
            answer="Yes; propose the exception for independent review.",
            answered_at=self._tick(microseconds=2),
            acl=tuple(sorted(principals)),
            principals=principals,
        )
        evidence_refs = (
            answered.request_evidence_ref,
            answered.classification_evidence_ref,
            *(question.evidence_ref for question in answered.questions),
            *(answer.evidence_ref for answer in answered.answers),
        )
        graph_before = self.runtime.graph_store.load()
        current = next(node for node in graph_before.nodes if node.id == "constraint:no-raw-cloud")
        at = self._tick()
        replacement = current.model_copy(
            update={
                "label": "Permit reviewed upload of raw source conversations",
                "last_modified_by": CONTRIBUTOR,
                "last_modified_at": at,
                "evidence_refs": (*current.evidence_refs, *evidence_refs),
            }
        )
        changeset = ChangeSet(
            id="",
            actor=CONTRIBUTOR,
            timestamp=at,
            baseline_graph_version=graph_before.version,
            evidence_refs=evidence_refs,
            nodes_added=(),
            nodes_updated=(NodeUpdate(node_id=current.id, replacement=replacement),),
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
        proposal = self.clarification.propose(
            ClarificationProposalSubmission(
                session_id=answered.id,
                task_id=answered.task_id,
                baseline_graph_version=answered.baseline_graph_version,
                actor=CONTRIBUTOR,
                timestamp=at,
                evidence_refs=evidence_refs,
                changeset=changeset,
                conflicting_authors=("U456",),
            ),
            principals=principals,
        )
        bytes_before_self_review = self._state_bytes()
        blocked = self.confirmation.confirm(
            proposal.id,
            actor=CONTRIBUTOR,
            at=self._tick(),
        )
        assert self.runtime.graph_store.load() == graph_before
        assert self._state_bytes()["graph"] == bytes_before_self_review["graph"]
        applied = self.confirmation.confirm(
            proposal.id,
            actor=REVIEWER,
            at=self._tick(),
        )
        decision = self.runtime.intent_proposals.decision_for(proposal.id)
        assert decision is not None
        return ConflictOutcome(
            classification=preflight.classification.value,
            preflight_case_id=preflight.review_case_id,
            mutation=denied,
            self_review_status=blocked.status.value,
            independent_review_status=applied.status.value,
            review_case_id=cast(str, blocked.case_id),
            reviewer_aliases=decision.actor_aliases,
            graph_version=applied.graph_version,
        )

    def exercise_host_modes(self) -> HostModeOutcome:
        """Prove the fixed Codex refusal and transparent disabled host behavior."""
        contract = CodexHostContract(
            contract_version="0.148.0-alpha.9",
            hooks_enabled=True,
            plugins_enabled=True,
            pre_tool_use=True,
            synchronous=True,
            command_hook_can_deny=True,
            covers_bash=True,
            covers_unified_exec=True,
            covers_apply_patch=True,
            specialized_paths_may_bypass_hooks=True,
            continuation_pretool_hook_complete=False,
            complete_mutation_coverage=False,
            unsupported_mutation_paths=(
                "specialized_tool_hook_opt_out",
                "write_stdin_continuation",
            ),
        )
        try:
            CodexIntentAdapter.from_contract(contract, mandatory=True)
        except MandatoryHookUnavailable as error:
            mandatory_error = str(error)
            self._errors.append(error)
        else:  # pragma: no cover - fixed audited contract must remain unsupported
            raise AssertionError("mandatory Codex unexpectedly enabled")

        async def disabled() -> MutationDecision:
            before = self.port.calls
            adapter = IntentAgentHostAdapter(
                workflow=self.port,
                enabled=False,
                repository_id=self.runtime.config.project_id,
                conversation_ref="disabled:e2e",
                graph_version=self.runtime.graph_store.load().version,
                clock=lambda: self._tick(),
            )
            task = await adapter.before_task(REQUEST_SENTINEL, CONTRIBUTOR)
            decision = await adapter.before_mutation(
                task=task,
                operation="write_file",
                paths=("src/disabled.py",),
                token=None,
            )
            completion = await adapter.after_task(
                task,
                HostTaskResult(
                    task_id=task.id,
                    status="blocked",
                    response_evidence_ref="disabled:no-evidence",
                    completed_at=self._tick(),
                ),
            )
            assert completion is None
            assert self.port.calls == before
            return decision

        decision = asyncio.run(disabled())
        plugin = ROOT / "plugins/intent-preflight"
        return HostModeOutcome(
            mandatory_error=mandatory_error,
            disabled_reason=decision.reason,
            disabled_workflow_calls=0,
            plugin_directory_exists=plugin.exists(),
        )

    def scheduled_assurance(self) -> AssuranceOutcome:
        """Run one all-source scheduled pass and prove its exact replay is semantic no-op."""
        self._github_api = FakeGitHubApi()
        cases_before = {case.id for case in self.runtime.cases()}

        async def run(run_id: str):
            stdout, stderr = StringIO(), StringIO()
            with capture_logs() as logs, redirect_stdout(stdout), redirect_stderr(stderr):
                result = await run_selected_sync(
                    self.runtime,
                    "markdown,git,github,mcp",
                    run_id,
                    env={
                        "GH_TOKEN": GITHUB_SENTINEL,
                        "GITHUB_REPOSITORY": "acme/demo",
                    },
                    token_runner=lambda _environment: "unused",
                    client_factory=self._github_client,
                    mcp_connectors=(self._slack.connector(),),
                )
            self._logs.append(logs)
            self._outputs.extend(
                (stdout.getvalue(), stderr.getvalue(), result.model_dump(mode="json"))
            )
            return result

        first = asyncio.run(run("intent-aware-scheduled-1"))
        checkpoint_after_first = self._state_bytes()["checkpoints"]
        new_cases = tuple(case for case in self.runtime.cases() if case.id not in cases_before)
        second = asyncio.run(run("intent-aware-scheduled-2"))
        checkpoint_after_second = self._state_bytes()["checkpoints"]
        assert all(client.is_closed for client in self._github_clients)
        return AssuranceOutcome(
            first_evidence=first.evidence_added,
            first_cases=first.cases_created,
            second_evidence=second.evidence_added,
            second_changes=second.changes_applied,
            second_cases=second.cases_created,
            fingerprints=tuple(sorted(case.fingerprint for case in new_cases)),
            checkpoint_byte_stable=checkpoint_after_first == checkpoint_after_second,
        )

    def shared_views(self) -> ViewOutcome:
        """Read validation, CLI, drift, and official MCP context from the same state."""
        graph_version = self.runtime.graph_store.load().version
        validation = validate_project(self.project)
        assert validation.valid, validation
        status = run_intent(
            self.project,
            "status",
            "--project",
            ".",
            "--format",
            "json",
        )
        assert status.returncode == 0, status.stderr
        self._outputs.extend((status.stdout, status.stderr))
        drift = run_intent(
            self.project,
            "drift",
            "--project",
            ".",
            "--format",
            "markdown",
        )
        assert drift.returncode == 0
        self._outputs.extend((drift.stdout, drift.stderr))
        server = build_server(McpReadServices(self.runtime))
        context_result = asyncio.run(
            server.call_tool(
                "intent_context",
                {"task": "CSV export", "format": "json"},
            )
        )
        status_result = asyncio.run(server.call_tool("intent_status", {}))
        assert context_result.structured_content is not None
        assert status_result.structured_content is not None
        self._outputs.extend((context_result.structured_content, status_result.structured_content))
        requirements = cast(
            list[dict[str, object]],
            context_result.structured_content["relevant_requirements"],
        )
        return ViewOutcome(
            graph_version=graph_version,
            cli_graph_version=cast(int, status.json()["graph_version"]),
            mcp_graph_version=cast(int, status_result.structured_content["graph_version"]),
            valid=validation.valid,
            context_requirement_ids=tuple(cast(str, item["id"]) for item in requirements),
        )

    def external_write_governance(self, case_id: str) -> WriteOutcome:
        """Preview through production composition, then prove guarded provider execution."""

        reviewed_case, proposed_changeset, review_hash = self.runtime.resolution.resolve(
            case_id,
            ResolutionAction.UPDATE_REQUIREMENT,
            at=self._tick(),
        )
        assert reviewed_case.status.value == "needs_human"
        assert proposed_changeset is not None
        assert review_hash is not None
        provider = _WriteRuntime()
        provider.credential_sentinel = JIRA_SENTINEL
        proposer = _live_fake_workflow(self.runtime, provider)
        self._write_workflows.append(proposer)
        provider_now = datetime.now(UTC)
        plan = asyncio.run(
            proposer.create_preview(
                case_id,
                connector_id="jira-local",
                operation="update_issue",
                requested_fields={
                    "summary": "Keep exports local by default",
                    "description": "Independent review remains mandatory.",
                    "status": "Approved",
                },
                resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
                now=provider_now,
            )
        )
        changed_plan = asyncio.run(
            proposer.create_preview(
                case_id,
                connector_id="jira-local",
                operation="update_issue",
                requested_fields={
                    "summary": "A later reviewed proposal",
                    "description": "Target changes still invalidate approval.",
                    "status": "Approved",
                },
                resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
                now=provider_now,
            )
        )
        proposer_reads = McpReadServices(self.runtime)
        proposer_mutations = _mutation_services(proposer)
        proposer_server = build_server(proposer_reads, mutation_services=proposer_mutations)
        state_before_missing = self._state_bytes()
        calls_before_missing = len(provider.calls)
        missing = asyncio.run(
            proposer_server.call_tool(
                "intent_write_execute",
                {
                    "plan_id": plan.id,
                    "approval_id": "approval:sha256:" + "f" * 64,
                },
            )
        )
        assert missing.structured_content is not None
        assert missing.structured_content == {
            "schema_version": "1",
            "status": "rejected",
            "reason": "approval_not_found",
        }
        assert self._state_bytes() == state_before_missing
        missing_provider_calls = len(provider.calls) - calls_before_missing

        self._configure_project(REVIEWER)
        reviewer = _live_fake_workflow(self.runtime, provider)
        self._write_workflows.append(reviewer)
        approval = reviewer.approve(
            plan.id,
            _FakeTerminal(True, f"approve {plan.id}"),
            now=provider_now + timedelta(microseconds=1),
        )
        changed_approval = reviewer.approve(
            changed_plan.id,
            _FakeTerminal(True, f"approve {changed_plan.id}"),
            now=provider_now + timedelta(microseconds=1),
        )
        reviewer_reads = McpReadServices(self.runtime)
        reviewer_mutations = _mutation_services(reviewer)
        reviewer_server = build_server(reviewer_reads, mutation_services=reviewer_mutations)

        changed_before = self._state_bytes()
        changed_reads_before = sum(name == "get_issue" for name, _ in provider.calls)
        changed_mutations_before = sum(name == "update_issue" for name, _ in provider.calls)
        provider.issue["updated"] = "2026-08-20T13:30:00Z"
        changed = asyncio.run(
            reviewer_server.call_tool(
                "intent_write_execute",
                {
                    "plan_id": changed_plan.id,
                    "approval_id": changed_approval.id,
                },
            )
        )
        assert changed.structured_content is not None
        changed_receipt = cast(dict[str, object], changed.structured_content["receipt"])
        assert changed_receipt["redacted_error"] == "target_changed"
        changed_provider_mutations = (
            sum(name == "update_issue" for name, _ in provider.calls) - changed_mutations_before
        )
        changed_fresh_reads = (
            sum(name == "get_issue" for name, _ in provider.calls) - changed_reads_before
        )
        changed_after = self._state_bytes()
        for key in ("graph", "history", "cases", "evidence", "proposals"):
            assert changed_after[key] == changed_before[key]

        provider.issue["updated"] = plan.before_version
        success_mutations_before = sum(name == "update_issue" for name, _ in provider.calls)
        success = asyncio.run(
            reviewer_server.call_tool(
                "intent_write_execute",
                {"plan_id": plan.id, "approval_id": approval.id},
            )
        )
        assert success.structured_content is not None
        assert success.structured_content["status"] == "succeeded", success.structured_content
        receipt = cast(dict[str, object], success.structured_content["receipt"])
        evidence_ref = cast(str, receipt["evidence_ref"])
        evidence = self.runtime.evidence_store.get(evidence_ref)
        evidence_payload = cast(dict[str, object], evidence.payload)
        evidence_plan = cast(dict[str, object], evidence_payload["plan"])
        evidence_approval = cast(dict[str, object], evidence_payload["approval"])
        self._outputs.extend(
            (missing.structured_content, changed.structured_content, success.structured_content)
        )
        return WriteOutcome(
            missing_status=cast(str, missing.structured_content["status"]),
            missing_reason=cast(str, missing.structured_content["reason"]),
            missing_provider_calls=missing_provider_calls,
            success_status=cast(str, success.structured_content["status"]),
            provider_mutations=(
                sum(name == "update_issue" for name, _ in provider.calls) - success_mutations_before
            ),
            plan_id=plan.id,
            approval_id=approval.id,
            receipt_id=cast(str, receipt["id"]),
            resulting_version=cast(str, receipt["resulting_version"]),
            evidence_author=evidence.author,
            evidence_plan_id=cast(str, evidence_plan["id"]),
            evidence_approval_id=cast(str, evidence_approval["id"]),
            changed_status=cast(str, changed.structured_content["status"]),
            changed_reason=cast(str, changed_receipt["redacted_error"]),
            changed_provider_mutations=changed_provider_mutations,
            changed_fresh_reads=changed_fresh_reads,
            graph_version=self.runtime.graph_store.load().version,
            shared_transaction_coordinator=True,
            all_services_hold_runtime=all(
                service.runtime is self.runtime
                for service in (
                    proposer.catalog,
                    proposer_reads,
                    proposer_mutations,
                    reviewer.catalog,
                    reviewer_reads,
                    reviewer_mutations,
                )
            ),
        )

    def secret_leaks(self) -> tuple[str, ...]:
        """Return real boundary sentinels found in durable or captured public state."""

        sentinels = (
            GITHUB_SENTINEL,
            SLACK_SENTINEL,
            JIRA_SENTINEL,
            REQUEST_SENTINEL,
            TEST_SENTINEL,
            *self._authorization_sentinels,
        )
        found: set[str] = set()
        for path in self.project.rglob("*"):
            if (
                not path.is_file()
                or path.is_symlink()
                or ".git" in path.relative_to(self.project).parts
            ):
                continue
            content = path.read_bytes()
            found.update(value for value in sentinels if value.encode() in content)
        traceback_locals: list[dict[str, str]] = []
        for error in self._errors:
            current = error.__traceback__
            while current is not None:
                traceback_locals.append(
                    {name: repr(value) for name, value in current.tb_frame.f_locals.items()}
                )
                current = current.tb_next
        rendered = json.dumps(
            {
                "errors": [repr(error) for error in self._errors],
                "logs": self._logs,
                "outputs": self._outputs,
                "traceback_locals": traceback_locals,
            },
            default=str,
            ensure_ascii=False,
            sort_keys=True,
        )
        found.update(value for value in sentinels if value in rendered)
        return tuple(sorted(found))

    def close(self) -> None:
        """Release the explicit secure descriptors owned by the composition harness."""
        self.confirmation.close()
        self._graph_file.close()
        self._evidence_file.close()
        for file in self._authority_files.values():
            file.close()
        for workflow in self._write_workflows:
            workflow.close()  # type: ignore[union-attr]
        self.runtime.workspace_directory.close()
        self.runtime.project_directory.close()
