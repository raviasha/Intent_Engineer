"""Assembly of the local CLI runtime from production adapters and services."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

import anyio
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from intent_engineering.capture.base import Connector
from intent_engineering.capture.git.connector import GitConnector, run_git
from intent_engineering.capture.github.auth import (
    GitHubCredentials,
    GitHubTokenRunner,
    run_gh_token,
)
from intent_engineering.capture.github.client import GitHubClient
from intent_engineering.capture.github.connector import GitHubConnector
from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.context import ContextProvider
from intent_engineering.control_plane.webauthn_store import (
    WebAuthnChallengeStore,
    WebAuthnCredentialStore,
)
from intent_engineering.core.models import (
    CandidateAssertion,
    DriftObservation,
    EvidenceDelta,
    EvidenceRecord,
    Graph,
    ProjectConfig,
    ReconciliationCase,
    is_nonterminal_case_status,
)
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.core.policy.project import ProjectNotInitialized, workspace_path
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.intent_workflow.assurance import AssuranceService
from intent_engineering.intent_workflow.check import (
    MAX_TEST_RESULT_BYTES,
    SharedStateRestorer,
    SharedStateRestoreResult,
    SharedStateRestoreStatus,
    TestResultArtifact,
)
from intent_engineering.intent_workflow.models import (
    ClarificationEvent,
    IntentProposal,
    ProposalDecisionRecord,
)
from intent_engineering.intent_workflow.proposal_store import (
    IntentProposalStore,
    parse_intent_ledger,
)
from intent_engineering.intent_workflow.readiness import (
    EnsureRequest,
    EnsureResult,
    ReadinessService,
)
from intent_engineering.reconcile import LocalResolutionService
from intent_engineering.reconcile.evidence_detection import detect_evidence_drift
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore, parse_case_versions
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import (
    SecureDirectory,
    UnsafePathError,
    configured_graph_relative,
)
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore, parse_graph
from intent_engineering.sync import SyncOrchestrator
from intent_engineering.sync.models import ConnectorRunResult, SyncRunResult, SyncRunStatus
from intent_engineering.validation import ValidationReport, validate_project_directory

if TYPE_CHECKING:
    from intent_engineering.assessment import AssessmentSnapshot

_GITHUB_OWNER = re.compile(r"(?!-)(?!.*--)[A-Za-z0-9-]{1,39}(?<!-)\Z")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

type GitHubClientFactory = Callable[[GitHubCredentials], GitHubClient]
type PrincipalResolver = Callable[[Runtime], frozenset[str]]
type McpConnectorResolver = Callable[[Runtime], Sequence[Connector]]


class GitHubConfigurationError(ValueError):
    """A fixed public failure for missing or malformed local GitHub scope."""

    def __init__(self) -> None:
        super().__init__("GitHub repository scope is unavailable")


def github_repository_scope(env: Mapping[str, object]) -> str:
    """Return one canonical repository scope from the standard non-secret variable."""
    invalid = False
    try:
        value = env.get("GITHUB_REPOSITORY")
    except Exception:  # noqa: BLE001 - discard hostile mapping failures at the boundary
        invalid = True
        value = None
    if invalid or type(value) is not str or value != value.strip():
        raise GitHubConfigurationError()
    owner, separator, repository = value.partition("/")
    if (
        not separator
        or "/" in repository
        or _GITHUB_OWNER.fullmatch(owner) is None
        or _GITHUB_REPOSITORY.fullmatch(repository) is None
    ):
        raise GitHubConfigurationError()
    return f"{owner.lower()}/{repository.lower()}"


def _front_matter(content: str) -> Mapping[str, Any] | None:
    """Read strict YAML metadata owned by the local fixture convention."""
    if not content.startswith("---\n"):
        return None
    closing = content.find("\n---\n", 4)
    if closing < 0:
        return None
    try:
        loaded = yaml.safe_load(content[4:closing])
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, Mapping):
        return None
    metadata = loaded.get("intent_engineering")
    return cast(Mapping[str, Any], metadata) if isinstance(metadata, Mapping) else None


def _detect_cases(delta: EvidenceDelta, graph: Graph, actor: str) -> Sequence[DriftObservation]:
    """Resolve combined-run fixture declarations against evidence and final graph state."""
    return detect_evidence_drift(delta.added, graph, actor, delta.ingestions)


class _FrontMatterReasoner(DeterministicReasoner):
    """Expose approved Markdown fixture metadata at the existing reasoner boundary."""

    def extract_assertions(self, delta: EvidenceDelta) -> Sequence[CandidateAssertion]:
        normalized = tuple(self._validated(record) for record in delta.added)
        return super().extract_assertions(delta.model_copy(update={"added": normalized}))

    def _validated(self, record: EvidenceRecord) -> EvidenceRecord:
        """Remove typed-invalid fixture assertions before the shared reasoner sees them."""
        normalized = self._normalized(record)
        assertion = normalized.payload.get("intent_assertion")
        if assertion is None:
            return normalized
        try:
            CandidateAssertion.model_validate(assertion)
        except (ValidationError, ValueError):
            payload = dict(normalized.payload)
            payload.pop("intent_assertion", None)
            return normalized.model_copy(update={"payload": payload})
        return normalized

    def _normalized(self, record: EvidenceRecord) -> EvidenceRecord:
        content = record.payload.get("content")
        metadata = _front_matter(content) if isinstance(content, str) else None
        payload = dict(record.payload)
        if metadata is not None:
            for key in ("intent_assertion", "detection_input"):
                if key not in payload and key in metadata:
                    payload[key] = metadata[key]
        if "intent_assertion" in payload:
            assertion = payload["intent_assertion"]
            if not isinstance(assertion, Mapping):
                payload.pop("intent_assertion", None)
                return record.model_copy(update={"payload": payload})
            copied = dict(assertion)
            references = copied.get("evidence_refs")
            valid_refs = (
                isinstance(references, Sequence)
                and not isinstance(references, str)
                and len(references) == 1
                and isinstance(references[0], str)
                and references[0] in {"$self", record.id}
            )
            if not valid_refs or not refs_allowed((record.id,), (record,), self._actor):
                payload.pop("intent_assertion", None)
                return record.model_copy(update={"payload": payload})
            copied["evidence_refs"] = [record.id]
            payload["intent_assertion"] = copied
        return record.model_copy(update={"payload": payload})


@dataclass(frozen=True)
class Runtime:
    """Configured local services and their canonical production stores."""

    root: Path
    workspace: Path
    config: ProjectConfig
    graph_store: YamlGraphStore
    evidence_store: JsonlEvidenceStore
    case_store: JsonlCaseStore
    intent_proposals: IntentProposalStore
    webauthn_credentials: WebAuthnCredentialStore
    webauthn_challenges: WebAuthnChallengeStore
    checkpoint_store: YamlCheckpointStore
    sync: SyncOrchestrator
    resolution: LocalResolutionService
    project_directory: SecureDirectory
    workspace_directory: SecureDirectory
    transactions: LocalTransactionCoordinator

    def evidence(self) -> tuple[EvidenceRecord, ...]:
        """Load persisted evidence in append order for read-only CLI projections."""
        return tuple(self.evidence_store.list())

    def cases(self) -> tuple[ReconciliationCase, ...]:
        """Return the current durable reconciliation-case versions."""
        return tuple(self.case_store.list())

    def context(self) -> ContextProvider:
        """Build a fresh, conservative context provider from durable state."""
        return ContextProvider(self.graph_store.load(), self.cases(), self.config, self.evidence())

    def assessment_snapshot(self, actor: str) -> AssessmentSnapshot:
        """Build one descriptor-held ACL projection for deterministic assessment."""
        from intent_engineering.assessment.snapshot import build_assessment_snapshot

        return build_assessment_snapshot(self, actor)

    def close(self) -> None:
        """Release every descriptor owned by this assembled runtime after it quiesces."""
        self.sync.close()
        self.intent_proposals.close()
        self.webauthn_credentials.close()
        self.webauthn_challenges.close()
        self.checkpoint_store.close()
        self.case_store.close()
        self.evidence_store.close()
        self.graph_store.close()
        self.transactions.close()
        self.workspace_directory.close()
        self.project_directory.close()


@dataclass(frozen=True)
class _ReadinessSnapshot:
    """One raw, no-write read of every readiness input."""

    config: bytes
    graph: bytes
    cases: bytes | None
    intent_proposals: bytes | None
    journal: bytes | None


@dataclass(frozen=True)
class _ReadinessGraphStore:
    """Detached graph reader backed only by one validated readiness snapshot."""

    graph: Graph

    def load(self) -> Graph:
        return self.graph


@dataclass(frozen=True)
class _ReadinessProposalStore:
    """Detached proposal reader backed only by one validated readiness snapshot."""

    proposals: tuple[IntentProposal, ...]
    decisions: Mapping[str, ProposalDecisionRecord]
    events: tuple[ClarificationEvent, ...]

    def list(self) -> tuple[IntentProposal, ...]:
        return self.proposals

    def decision_for(self, proposal_id: str) -> ProposalDecisionRecord | None:
        return self.decisions.get(proposal_id)

    def clarification_events(self) -> tuple[ClarificationEvent, ...]:
        return self.events


@dataclass(frozen=True)
class ReadinessRuntime:
    """Detached coherent snapshot used by the read-only readiness gate."""

    graph_store: _ReadinessGraphStore
    intent_proposals: _ReadinessProposalStore
    case_items: tuple[ReconciliationCase, ...]

    def cases(self) -> tuple[ReconciliationCase, ...]:
        return self.case_items

    def close(self) -> None:
        """Match the ordinary runtime lifecycle; detached snapshots own no descriptors."""


def _read_readiness_file(
    workspace_directory: SecureDirectory,
    relative_path: str,
    *,
    optional: bool = False,
) -> bytes | None:
    secure_file = workspace_directory.file(relative_path)
    try:
        return secure_file.read_optional() if optional else secure_file.read_bytes()
    finally:
        secure_file.close()


def _capture_readiness_snapshot(
    workspace_directory: SecureDirectory,
) -> _ReadinessSnapshot:
    """Read all readiness inputs once without recovery, locking, or local writes."""
    config = _read_readiness_file(workspace_directory, "config.yaml")
    if config is None:  # pragma: no cover - required file invariant
        raise ValueError("project configuration is unavailable")
    loaded = yaml.safe_load(config.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("project configuration is invalid")
    project_config = ProjectConfig.model_validate_json(
        json.dumps(cast(dict[str, Any], loaded), ensure_ascii=False, separators=(",", ":"))
    )
    graph_relative = configured_graph_relative(project_config.graph_path).as_posix()
    graph = _read_readiness_file(workspace_directory, graph_relative)
    if graph is None:  # pragma: no cover - required file invariant
        raise ValueError("graph is unavailable")
    return _ReadinessSnapshot(
        config=config,
        graph=graph,
        cases=_read_readiness_file(
            workspace_directory, "reconciliation/cases.jsonl", optional=True
        ),
        intent_proposals=_read_readiness_file(
            workspace_directory,
            "history/intent-proposals.jsonl",
            optional=True,
        ),
        journal=_read_readiness_file(
            workspace_directory,
            "history/.local-transaction.json",
            optional=True,
        ),
    )


def load_readiness_runtime(root: Path) -> ReadinessRuntime:
    """Return a stable no-write snapshot or fail closed when local state changes."""
    project_directory: SecureDirectory | None = None
    workspace_directory: SecureDirectory | None = None
    snapshot: _ReadinessSnapshot | None = None
    try:
        root = Path(os.path.abspath(root))
        try:
            project_directory = SecureDirectory.open(root)
            workspace_directory = project_directory.subdirectory(".intent")
        except UnsafePathError as error:
            raise ProjectNotInitialized("local project is not initialized") from error
        for _ in range(2):
            first = _capture_readiness_snapshot(workspace_directory)
            second = _capture_readiness_snapshot(workspace_directory)
            if first.journal is not None or second.journal is not None:
                raise ValueError("unrecovered local transaction")
            if first == second:
                snapshot = second
                break
        if snapshot is None:
            raise ValueError("unstable readiness snapshot")
        graph = parse_graph(snapshot.graph)
        latest_cases = {case.id: case for case in parse_case_versions(snapshot.cases)}
        ledger = parse_intent_ledger(snapshot.intent_proposals or b"")
        if ledger is None:
            raise ValueError("intent proposal ledger is invalid")
        return ReadinessRuntime(
            graph_store=_ReadinessGraphStore(graph),
            intent_proposals=_ReadinessProposalStore(
                proposals=tuple(ledger.proposals.values()),
                decisions=MappingProxyType(ledger.decisions),
                events=ledger.clarification_events,
            ),
            case_items=tuple(sorted(latest_cases.values(), key=lambda case: case.id)),
        )
    finally:
        if workspace_directory is not None:
            workspace_directory.close()
        if project_directory is not None:
            project_directory.close()


def load_runtime(root: Path) -> Runtime:
    """Locate one initialized workspace and assemble only reviewed local adapters."""
    root = Path(os.path.abspath(root))
    try:
        project_directory = SecureDirectory.open(root)
        workspace_directory = project_directory.subdirectory(".intent")
        config_file = workspace_directory.file("config.yaml")
        loaded = yaml.safe_load(config_file.read_bytes().decode("utf-8"))
    except UnsafePathError as error:
        raise ProjectNotInitialized("local project is not initialized") from error
    workspace = workspace_path(root)
    if not isinstance(loaded, dict):
        raise TypeError("project configuration is invalid")
    config = ProjectConfig.model_validate_json(
        json.dumps(cast(dict[str, Any], loaded), ensure_ascii=False, separators=(",", ":"))
    )
    try:
        graph_file = workspace_directory.file(configured_graph_relative(config.graph_path))
        graph_file.assert_regular()
    except UnsafePathError as error:
        raise UnsafePathError("configured graph path is unsafe") from error
    history_file = workspace_directory.file("history/changesets.jsonl")
    case_file = workspace_directory.file("reconciliation/cases.jsonl")
    evidence_file = workspace_directory.file("evidence/evidence.jsonl")
    receipts_file = workspace_directory.file("approvals/receipts.jsonl")
    approvals_file = workspace_directory.file("approvals/approvals.jsonl")
    intent_proposals_file = workspace_directory.file("history/intent-proposals.jsonl")
    webauthn_credentials_file = workspace_directory.file("approvals/webauthn-credentials.jsonl")
    webauthn_challenges_file = workspace_directory.file("approvals/webauthn-challenges.jsonl")
    transactions = LocalTransactionCoordinator(
        workspace_directory.file("history/.local-transaction.json"),
        {
            "graph": graph_file,
            "history": history_file,
            "cases": case_file,
            "evidence": evidence_file,
            "receipts": receipts_file,
            "approvals": approvals_file,
            "intent_proposals": intent_proposals_file,
            "webauthn_credentials": webauthn_credentials_file,
            "webauthn_challenges": webauthn_challenges_file,
        },
        legacy_target_sets=(
            frozenset({"graph", "history", "cases"}),
            frozenset({"graph", "history", "cases", "evidence", "receipts"}),
            frozenset({"graph", "history", "cases", "evidence", "receipts", "intent_proposals"}),
            frozenset(
                {
                    "graph",
                    "history",
                    "cases",
                    "evidence",
                    "receipts",
                    "intent_proposals",
                    "webauthn_credentials",
                    "webauthn_challenges",
                }
            ),
        ),
    )
    # Raw preimages must be restored before a torn YAML or JSONL file reaches a parser.
    transactions.recover()
    graph_store = YamlGraphStore(
        graph_file,
        history_path=history_file,
        transactions=transactions,
    )
    evidence_store = JsonlEvidenceStore(evidence_file, transactions=transactions)
    case_store = JsonlCaseStore(case_file)
    intent_proposals_target = transactions.target_file("intent_proposals")
    try:
        intent_proposals = IntentProposalStore(
            intent_proposals_target,
            transactions=transactions,
        )
    finally:
        intent_proposals_target.close()
    credentials_target = transactions.target_file("webauthn_credentials")
    challenges_target = transactions.target_file("webauthn_challenges")
    try:
        webauthn_credentials = WebAuthnCredentialStore(
            credentials_target, transactions=transactions
        )
        webauthn_challenges = WebAuthnChallengeStore(challenges_target, transactions=transactions)
    finally:
        credentials_target.close()
        challenges_target.close()
    checkpoint_store = YamlCheckpointStore(workspace_directory.file("cache/checkpoints.yaml"))
    changeset_executor = LocalChangeSetExecutor(graph_store, case_store, transactions)
    sync = SyncOrchestrator(
        graph_store=graph_store,
        evidence_store=evidence_store,
        checkpoint_store=checkpoint_store,
        case_store=case_store,
        reasoner=_FrontMatterReasoner(actor=config.local_actor),
        case_detector=lambda delta, graph: _detect_cases(delta, graph, config.local_actor),
        changeset_executor=changeset_executor,
        assurance_service=AssuranceService(actor=config.local_actor),
        transactions=transactions,
        snapshot_files={"config": config_file},
    )
    resolution = LocalResolutionService(
        graph_store,
        evidence_store,
        case_store,
        config.local_actor,
        transactions=transactions,
    )
    resolution.recover()
    return Runtime(
        root=root,
        workspace=workspace,
        config=config,
        graph_store=graph_store,
        evidence_store=evidence_store,
        case_store=case_store,
        intent_proposals=intent_proposals,
        webauthn_credentials=webauthn_credentials,
        webauthn_challenges=webauthn_challenges,
        checkpoint_store=checkpoint_store,
        sync=sync,
        resolution=resolution,
        project_directory=project_directory,
        workspace_directory=workspace_directory,
        transactions=transactions,
    )


class CheckRuntimeAdapter:
    """Lazy production adapter for the consolidated non-authoritative check service."""

    def __init__(
        self,
        root: Path,
        *,
        principal_resolver: PrincipalResolver | None = None,
        mcp_connector_resolver: McpConnectorResolver | None = None,
        shared_state_restorer: SharedStateRestorer | None = None,
    ) -> None:
        self._root = Path(os.path.abspath(root))
        self._runtime: Runtime | None = None
        self._principal_resolver = principal_resolver
        self._mcp_connector_resolver = mcp_connector_resolver
        self._shared_state_restorer = shared_state_restorer

    def _opened(self) -> Runtime:
        if self._runtime is None:
            self._runtime = load_runtime(self._root)
        return self._runtime

    @property
    def repository_id(self) -> str:
        return self._opened().config.project_id

    @property
    def principals(self) -> frozenset[str]:
        runtime = self._opened()
        if self._principal_resolver is None:
            return frozenset({runtime.config.local_actor})
        resolved = self._principal_resolver(runtime)
        if type(resolved) is not frozenset or any(
            type(value) is not str or not value for value in resolved
        ):
            raise ValueError("check principals are unavailable")
        return resolved

    def restore(self, *, require_shared: bool) -> SharedStateRestoreResult:
        if require_shared:
            if self._shared_state_restorer is None:
                return SharedStateRestoreResult(status=SharedStateRestoreStatus.UNAVAILABLE)
            result = self._shared_state_restorer.verify_and_restore_approved_baseline(self._root)
            if type(result) is not SharedStateRestoreResult:
                return SharedStateRestoreResult(status=SharedStateRestoreStatus.INVALID)
            if result.status is not SharedStateRestoreStatus.VERIFIED:
                return result
        self._opened()
        return SharedStateRestoreResult(
            status=(
                SharedStateRestoreStatus.VERIFIED
                if require_shared
                else SharedStateRestoreStatus.NOT_REQUIRED
            )
        )

    def ensure(self) -> EnsureResult:
        readiness = load_readiness_runtime(self._root)
        try:
            return ReadinessService(readiness).ensure(EnsureRequest())
        finally:
            readiness.close()

    def read_test_results(self, path: Path) -> bytes:
        return (
            self._opened()
            .project_directory.read_relative(
                path,
                nonblocking=True,
                max_bytes=MAX_TEST_RESULT_BYTES,
            )
            .content
        )

    def current_revision(self) -> str:
        try:
            revision = run_git(
                self._root,
                ["rev-parse", "--verify", "--quiet", "HEAD"],
            ).strip()
        except (OSError, subprocess.SubprocessError, UnicodeError) as error:
            raise ValueError("repository revision is unavailable") from error
        if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision) is None:
            raise ValueError("repository revision is unavailable")
        return revision

    async def capture(
        self,
        sources: tuple[str, ...],
        test_result: TestResultArtifact | None,
    ) -> SyncRunResult:
        runtime = self._opened()
        mcp_connectors: Sequence[Connector] = ()
        if "mcp" in sources:
            if self._mcp_connector_resolver is None:
                raise ValueError("MCP connector selection is unavailable")
            mcp_connectors = self._mcp_connector_resolver(runtime)
        captured = await run_selected_sync(
            runtime,
            ",".join(sources),
            new_run_id(),
            mcp_connectors=mcp_connectors,
        )
        if test_result is None:
            return captured
        test_result_new = runtime.evidence_store.associate(
            "test-results",
            test_result.evidence(),
        )
        connector_results = {
            **captured.connectors,
            "test-results": ConnectorRunResult.succeeded(
                evidence_added=int(test_result_new),
                changes_applied=0,
                cases_created=0,
                checkpoint_advanced=False,
            ),
        }
        return SyncRunResult.from_connector_results(
            captured.run_id,
            connector_results,
            captured.duration_ms,
        )

    def validate(self) -> ValidationReport:
        return validate_project_directory(self._opened().project_directory)

    async def assure(self) -> SyncRunResult:
        runtime = self._opened()
        observations = AssuranceService(actor=runtime.config.local_actor).detect(
            graph=runtime.graph_store.load(),
            records=runtime.evidence(),
            ingestions=runtime.evidence_store.ingestions(),
            existing_cases=runtime.cases(),
        )
        status = SyncRunStatus.SUCCESS if not observations else SyncRunStatus.FAILED
        return SyncRunResult(
            run_id=new_run_id(),
            status=status,
            connectors={},
            evidence_added=0,
            changes_applied=0,
            cases_created=0,
            duration_ms=0,
        )

    def authorized_cases(self) -> tuple[ReconciliationCase, ...]:
        runtime = self._opened()
        records = runtime.evidence()
        principals = self.principals
        return tuple(
            case
            for case in runtime.cases()
            if is_nonterminal_case_status(case.status)
            and refs_allowed(case.all_evidence_refs, records, principals)
        )

    @staticmethod
    def render_drift(cases: tuple[ReconciliationCase, ...]) -> str:
        from intent_engineering.render import render_drift_report

        return render_drift_report(cases)

    def close(self) -> None:
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None


def resolve_connectors(
    runtime: Runtime,
    sources: str,
    *,
    github_client: GitHubClient | None = None,
    github_repository: str | None = None,
    mcp_connectors: Sequence[Connector] = (),
) -> tuple[Connector, ...]:
    """Resolve one stable connector list for a single orchestrator transaction."""
    requested = parse_sources(sources)
    connectors: list[Connector] = []
    for source in requested:
        if source == "markdown":
            connectors.append(MarkdownConnector(runtime.project_directory, runtime.config))
        elif source == "git":
            connectors.append(GitConnector(runtime.root, repository_id=runtime.config.project_id))
        elif source == "github":
            if github_client is None or github_repository is None:
                raise GitHubConfigurationError()
            owner, repository = github_repository.split("/", 1)
            connectors.append(GitHubConnector(github_client, owner=owner, repository=repository))
        elif source == "mcp":
            if not mcp_connectors:
                raise ValueError("MCP connector selection is unavailable")
            connectors.extend(mcp_connectors)
        else:  # pragma: no cover - parse_sources establishes this boundary
            raise AssertionError(source)
    return tuple(connectors)


def parse_sources(sources: str) -> tuple[str, ...]:
    """Validate a connector selection before the command crosses into AnyIO."""
    requested = tuple(item.strip() for item in sources.split(","))
    if not requested or any(not item for item in requested):
        raise ValueError("sources must name one or more connectors")
    if len(requested) != len(set(requested)):
        raise ValueError("sources must not contain duplicates")
    unknown = tuple(item for item in requested if item not in {"markdown", "git", "github", "mcp"})
    if unknown:
        raise ValueError("sources must be markdown, git, github, and/or mcp")
    return requested


def validate_github_environment(sources: str, env: Mapping[str, object]) -> None:
    """Validate GitHub scope before a CLI command opens canonical project state."""
    if "github" in parse_sources(sources):
        github_repository_scope(env)


def _default_github_client(credentials: GitHubCredentials) -> GitHubClient:
    return GitHubClient(credentials)


async def run_selected_sync(
    runtime: Runtime,
    sources: str,
    run_id: str,
    *,
    env: Mapping[str, object] | None = None,
    token_runner: GitHubTokenRunner = run_gh_token,
    client_factory: GitHubClientFactory = _default_github_client,
    mcp_connectors: Sequence[Connector] = (),
) -> SyncRunResult:
    """Run all selected sources once and deterministically clean up a CLI-owned client."""
    requested = parse_sources(sources)
    if "github" not in requested:
        return await runtime.sync.run(
            run_id,
            resolve_connectors(runtime, sources, mcp_connectors=mcp_connectors),
        )

    environment: Mapping[str, object] = os.environ if env is None else env
    repository = github_repository_scope(environment)
    credentials = GitHubCredentials.resolve(environment, token_runner)
    environment = {}
    env = None
    client = client_factory(credentials)
    operation_failed = False
    try:
        connectors = resolve_connectors(
            runtime,
            sources,
            github_client=client,
            github_repository=repository,
            mcp_connectors=mcp_connectors,
        )
        return await runtime.sync.run(run_id, connectors)
    except BaseException:
        operation_failed = True
        raise
    finally:
        close_failed = False
        try:
            with anyio.CancelScope(shield=True):
                await client.aclose()
        except anyio.get_cancelled_exc_class():
            if not operation_failed:
                raise
        except BaseException:  # noqa: BLE001 - discard untrusted close failures
            close_failed = True
        if close_failed and not operation_failed:
            raise RuntimeError("GitHub client cleanup failed") from None


def new_run_id() -> str:
    """Return a unique opaque run identifier without embedding project paths."""
    from uuid import uuid4

    return f"run:{uuid4().hex}"
