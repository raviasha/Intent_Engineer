"""Assembly of the local CLI runtime from production adapters and services."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from intent_engineering.capture.base import Connector
from intent_engineering.capture.git.connector import GitConnector
from intent_engineering.capture.github.auth import (
    GitHubCredentials,
    GitHubTokenRunner,
    run_gh_token,
)
from intent_engineering.capture.github.client import GitHubClient
from intent_engineering.capture.github.connector import GitHubConnector
from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.context import ContextProvider
from intent_engineering.core.models import (
    CandidateAssertion,
    DriftObservation,
    EvidenceDelta,
    EvidenceRecord,
    Graph,
    ProjectConfig,
    ReconciliationCase,
)
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.core.policy.project import ProjectNotInitialized, workspace_path
from intent_engineering.extract.deterministic import DeterministicReasoner
from intent_engineering.reconcile import LocalResolutionService
from intent_engineering.reconcile.evidence_detection import detect_evidence_drift
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.case_store import JsonlCaseStore
from intent_engineering.storage.jsonl.evidence_store import JsonlEvidenceStore
from intent_engineering.storage.secure import (
    SecureDirectory,
    UnsafePathError,
    configured_graph_relative,
)
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.storage.yaml.checkpoint_store import YamlCheckpointStore
from intent_engineering.storage.yaml.graph_store import YamlGraphStore
from intent_engineering.sync import SyncOrchestrator
from intent_engineering.sync.models import SyncRunResult

_GITHUB_OWNER = re.compile(r"(?!-)(?!.*--)[A-Za-z0-9-]{1,39}(?<!-)\Z")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

type GitHubClientFactory = Callable[[GitHubCredentials], GitHubClient]


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
    config = ProjectConfig.model_validate(cast(dict[str, Any], loaded))
    try:
        graph_file = workspace_directory.file(configured_graph_relative(config.graph_path))
        graph_file.assert_regular()
    except UnsafePathError as error:
        raise UnsafePathError("configured graph path is unsafe") from error
    history_file = workspace_directory.file("history/changesets.jsonl")
    case_file = workspace_directory.file("reconciliation/cases.jsonl")
    transactions = LocalTransactionCoordinator(
        workspace_directory.file("history/.local-transaction.json"),
        {"graph": graph_file, "history": history_file, "cases": case_file},
    )
    # Raw preimages must be restored before a torn YAML or JSONL file reaches a parser.
    transactions.recover()
    graph_store = YamlGraphStore(
        graph_file,
        history_path=history_file,
        transactions=transactions,
    )
    evidence_store = JsonlEvidenceStore(workspace_directory.file("evidence/evidence.jsonl"))
    case_store = JsonlCaseStore(case_file)
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
        checkpoint_store=checkpoint_store,
        sync=sync,
        resolution=resolution,
        project_directory=project_directory,
        workspace_directory=workspace_directory,
        transactions=transactions,
    )


def resolve_connectors(
    runtime: Runtime,
    sources: str,
    *,
    github_client: GitHubClient | None = None,
    github_repository: str | None = None,
) -> tuple[Connector, ...]:
    """Resolve one stable connector list for a single orchestrator transaction."""
    requested = parse_sources(sources)
    connectors: list[Connector] = []
    for source in requested:
        if source == "markdown":
            connectors.append(MarkdownConnector(runtime.project_directory, runtime.config))
        elif source == "git":
            connectors.append(GitConnector(runtime.root))
        elif source == "github":
            if github_client is None or github_repository is None:
                raise GitHubConfigurationError()
            owner, repository = github_repository.split("/", 1)
            connectors.append(GitHubConnector(github_client, owner=owner, repository=repository))
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
    unknown = tuple(item for item in requested if item not in {"markdown", "git", "github"})
    if unknown:
        raise ValueError("sources must be markdown, git, and/or github")
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
) -> SyncRunResult:
    """Run all selected sources once and deterministically clean up a CLI-owned client."""
    requested = parse_sources(sources)
    if "github" not in requested:
        return await runtime.sync.run(run_id, resolve_connectors(runtime, sources))

    environment: Mapping[str, object] = os.environ if env is None else env
    repository = github_repository_scope(environment)
    credentials = GitHubCredentials.resolve(environment, token_runner)
    client = client_factory(credentials)
    operation_failed = False
    try:
        connectors = resolve_connectors(
            runtime,
            sources,
            github_client=client,
            github_repository=repository,
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
