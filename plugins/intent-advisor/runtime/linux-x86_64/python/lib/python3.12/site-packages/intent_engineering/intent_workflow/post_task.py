"""Evidence-backed completion processing for an exact authorized agent task."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Annotated, Literal, cast

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from intent_engineering.core.models import (
    ChangeSet,
    Edge,
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    ImplementationClaim,
    ImplementationStatus,
    ImplementationStatusChange,
    Node,
    NodeType,
    RelationType,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.intent_workflow.authorization import (
    AuthorizationIssuer,
    AuthorizationVerification,
)
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import (
    LocalTransactionCoordinator,
    LocalTransactionSnapshot,
)
from intent_engineering.storage.yaml.graph_store import parse_graph

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TASK_ID = r"^task:sha256:[0-9a-f]{64}$"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_REVISION = r"^[0-9a-f]{40,64}$"
_WINDOWS_DEVICE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])$", re.IGNORECASE
)
_MAX_ID_BYTES = 2 * 1024
_MAX_PATH_BYTES = 2 * 1024
_MAX_ITEMS = 256

type PostTaskStatus = Literal["recorded", "preflight_required", "review_required", "rejected"]
type PostTaskReason = Literal[
    "recorded",
    "scope_changed",
    "authorization_rejected",
    "evidence_rejected",
    "reference_rejected",
    "review_required",
]


def _utc(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if type(value) is not datetime or offset is None or offset.total_seconds() != 0:
        raise ValueError("post-task timestamp must use UTC")
    return value.astimezone(UTC)


def _timestamp_input(value: object, info: ValidationInfo) -> object:
    if info.mode != "json":
        return value
    if type(value) is not str:
        raise ValueError("invalid post-task timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid post-task timestamp") from None
    canonical = parsed.isoformat()
    if canonical.endswith("+00:00"):
        canonical = canonical[:-6] + "Z"
    if value != canonical:
        raise ValueError("post-task timestamp must use canonical UTC Z form")
    return parsed


def _identity(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_ID_BYTES
    ):
        raise ValueError("invalid post-task identity")
    return value


def _has_reserved_windows_segment(path: PureWindowsPath) -> bool:
    for part in path.parts:
        normalized = part.rstrip(" .")
        stem = normalized.split(".", 1)[0].rstrip(" ")
        if PureWindowsPath(part).is_reserved() or _WINDOWS_DEVICE.fullmatch(stem):
            return True
    return False


def _path(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or "\\" in value
        or len(value.encode("utf-8")) > _MAX_PATH_BYTES
    ):
        raise ValueError("invalid post-task path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.drive
        or windows.is_absolute()
        or _has_reserved_windows_segment(windows)
        or ":" in value
        or posix.as_posix() != value
        or value == "."
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise ValueError("invalid post-task path")
    return value


def _canonical_tuple(
    values: tuple[str, ...], *, paths: bool = False
) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > _MAX_ITEMS:
        raise ValueError("invalid post-task collection")
    checked = tuple((_path(value) if paths else _identity(value)) for value in values)
    if len(checked) != len(set(checked)) or tuple(sorted(checked)) != checked:
        raise ValueError("post-task collection must be sorted and unique")
    return checked


def _require_exact_input(value: object) -> None:
    if value is None or type(value) in {str, bool, int, float, bytes, datetime}:
        return
    if isinstance(value, (str, bool, int, float, bytes, datetime)):
        raise TypeError("post-task input requires exact built-in values")
    if type(value) is tuple:
        for item in cast(tuple[object, ...], value):
            _require_exact_input(item)
        return
    if isinstance(value, tuple):
        raise TypeError("post-task input requires exact built-in values")
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            _require_exact_input(key)
            _require_exact_input(item)
        return
    if isinstance(value, (dict, list)):
        raise TypeError("post-task input requires exact built-in values")


class _PostTaskModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    @model_validator(mode="before")
    @classmethod
    def reject_python_subclasses(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "python":
            _require_exact_input(value)
        return value


class PostTaskSubmission(_PostTaskModel):
    """Untrusted token-free claims about the result of one authorized task."""

    schema_version: Literal[1] = 1
    repository_id: str
    task_id: Annotated[str, Field(pattern=_TASK_ID)]
    actor: str
    request_digest: Annotated[str, Field(pattern=_DIGEST)]
    graph_version: Annotated[int, Field(ge=0)]
    base_revision: Annotated[str, Field(pattern=_REVISION)]
    final_revision: Annotated[str, Field(pattern=_REVISION)]
    changed_paths: Annotated[tuple[str, ...], Field(min_length=1, max_length=_MAX_ITEMS)]
    requirement_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=_MAX_ITEMS)]
    code_refs: Annotated[tuple[str, ...], Field(min_length=1, max_length=_MAX_ITEMS)]
    test_refs: Annotated[tuple[str, ...], Field(max_length=_MAX_ITEMS)] = ()
    git_evidence_refs: Annotated[tuple[str, ...], Field(min_length=1, max_length=_MAX_ITEMS)]
    test_evidence_refs: Annotated[tuple[str, ...], Field(max_length=_MAX_ITEMS)] = ()
    completed_at: datetime

    @model_validator(mode="before")
    @classmethod
    def reject_python_subclasses(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "python":
            _require_exact_input(value)
        return value

    @field_validator("repository_id", "actor")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _identity(value)

    @field_validator(
        "changed_paths",
        "requirement_ids",
        "code_refs",
        "test_refs",
        "git_evidence_refs",
        "test_evidence_refs",
        mode="before",
    )
    @classmethod
    def require_exact_collections(cls, value: object, info: ValidationInfo) -> object:
        if info.mode == "json" and type(value) is list:
            return tuple(value)
        if info.mode == "python" and type(value) is not tuple:
            raise ValueError("invalid post-task collection")
        return value

    @field_validator(
        "changed_paths",
        "requirement_ids",
        "code_refs",
        "test_refs",
        "git_evidence_refs",
        "test_evidence_refs",
    )
    @classmethod
    def validate_collections(cls, values: tuple[str, ...], info: ValidationInfo) -> tuple[str, ...]:
        return _canonical_tuple(values, paths=info.field_name == "changed_paths")

    @field_validator("completed_at", mode="before")
    @classmethod
    def parse_json_timestamp(cls, value: object, info: ValidationInfo) -> object:
        return _timestamp_input(value, info)

    @field_validator("completed_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_revisions(self) -> PostTaskSubmission:
        if self.base_revision == self.final_revision:
            raise ValueError("post-task revisions must describe a change")
        return self


class PostTaskResult(_PostTaskModel):
    """Detached bounded outcome; it never contains authorization material."""

    schema_version: Literal[1] = 1
    status: PostTaskStatus
    reason: PostTaskReason
    task_id: Annotated[str, Field(pattern=_TASK_ID)]
    graph_version: Annotated[int, Field(ge=0)]
    claim: ImplementationClaim | None = None
    changeset_id: str | None = None

    @field_validator("changeset_id")
    @classmethod
    def validate_changeset_id(cls, value: str | None) -> str | None:
        return None if value is None else _identity(value)

    @field_validator("claim", mode="before")
    @classmethod
    def detach_claim(cls, value: object, info: ValidationInfo) -> ImplementationClaim | None:
        if value is None:
            return None
        if info.mode == "json" and type(value) is dict:
            return ImplementationClaim.model_validate(value)
        if type(value) is not ImplementationClaim:
            raise ValueError("invalid post-task claim")
        return ImplementationClaim.model_validate_json(value.model_dump_json())

    @model_validator(mode="after")
    def validate_shape(self) -> PostTaskResult:
        if self.status == "recorded":
            if self.reason != "recorded" or self.claim is None:
                raise ValueError("invalid recorded post-task result")
        elif self.claim is not None or self.changeset_id is not None or self.reason == "recorded":
            raise ValueError("invalid rejected post-task result")
        return self


class _PostTaskFailure(ValueError):
    def __init__(self, status: PostTaskStatus, reason: PostTaskReason) -> None:
        self.status = status
        self.reason = reason
        super().__init__("post-task evaluation rejected")


def _current(record: EvidenceRecord, ingestions: Sequence[EvidenceIngestion]) -> bool:
    associations = tuple(item for item in ingestions if item.evidence.id == record.id)
    if len(associations) != 1:
        return False
    association = associations[0]
    return not any(
        item.connector_id == association.connector_id
        and item.evidence.connector_type == record.connector_type
        and item.evidence.external_object_id == record.external_object_id
        and item.sequence > association.sequence
        for item in ingestions
    )


def _node_type(node: Node) -> NodeType | None:
    return node.type if isinstance(node.type, NodeType) else None


class PostTaskService:
    """Consume one capability and atomically link real Git/code/test evidence."""

    def __init__(
        self,
        *,
        issuer: AuthorizationIssuer,
        changeset_executor: LocalChangeSetExecutor,
        transactions: LocalTransactionCoordinator,
        graph_file: SecureFile,
        evidence_file: SecureFile,
        authority_files: Mapping[str, SecureFile],
        authority_resolver: Callable[
            [str, Mapping[str, bytes | None]], tuple[str, Collection[str]]
        ],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if set(authority_files) != {"config", "policy", "repository", "binding"}:
            raise ValueError("post-task authority files are incomplete")
        if not transactions.target_matches("graph", graph_file) or not transactions.target_matches(
            "evidence", evidence_file
        ):
            raise ValueError("post-task transaction targets do not match")
        self._issuer = issuer
        self._executor = changeset_executor
        self._transactions = transactions
        self._authority_files = {
            name: file.duplicate() for name, file in sorted(authority_files.items())
        }
        self._authority_resolver = authority_resolver
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(UTC)) if clock is None else clock
        )

    @staticmethod
    def _failure(
        submission: PostTaskSubmission,
        status: PostTaskStatus,
        reason: PostTaskReason,
    ) -> PostTaskResult:
        return PostTaskResult(
            status=status,
            reason=reason,
            task_id=submission.task_id,
            graph_version=submission.graph_version,
        )

    def evaluate(self, submission: PostTaskSubmission, *, token: str) -> PostTaskResult:
        """Reauthenticate, resolve exact evidence, and consume the capability once."""
        detached: PostTaskSubmission | None = None
        graph_content = b""
        evidence_content: bytes | None = None
        commit_action: Callable[[AuthorizationVerification], PostTaskResult] | None = None
        signal: BaseException | None = None
        try:
            if type(submission) is not PostTaskSubmission:
                raise ValueError("invalid post-task submission")
            detached = PostTaskSubmission.model_validate_json(submission.model_dump_json())
            checked_at = _utc(self._clock())
            if detached.completed_at > checked_at:
                raise _PostTaskFailure("rejected", "evidence_rejected")
            snapshot = self._transactions.snapshot(self._authority_files)
            snapshot_graph_content = snapshot.content.get("graph")
            evidence_content = snapshot.content.get("evidence")
            if snapshot_graph_content is None or evidence_content is None:
                raise _PostTaskFailure("rejected", "evidence_rejected")
            graph_content = snapshot_graph_content
            graph = parse_graph(graph_content)
            initial = self._issuer.verify(
                token,
                actor=detached.actor,
                repository_id=detached.repository_id,
                task_id=detached.task_id,
                graph_version=detached.graph_version,
                graph_content=graph_content,
                requested_paths=detached.changed_paths,
                now=checked_at,
                request_digest=detached.request_digest,
            )
            if not initial.authorized:
                reason: PostTaskReason = (
                    "scope_changed" if initial.reason == "scope_mismatch" else "authorization_rejected"
                )
                status: PostTaskStatus = (
                    "preflight_required" if initial.reason == "scope_mismatch" else "rejected"
                )
                return self._failure(detached, status, reason)
            authorized_submission: PostTaskSubmission = detached
            authorized_evidence: bytes = evidence_content
            authorized_snapshot = snapshot.content
            commit_action = lambda verification: self._commit(
                    authorized_submission,
                    verification,
                    graph,
                    graph_content,
                    authorized_evidence,
                    authorized_snapshot,
                )

            consumed = self._issuer.consume(
                token,
                actor=detached.actor,
                repository_id=detached.repository_id,
                task_id=detached.task_id,
                graph_version=detached.graph_version,
                graph_content=graph_content,
                requested_paths=detached.changed_paths,
                now=_utc(self._clock()),
                action=commit_action,
                request_digest=detached.request_digest,
            )
            if consumed is None:
                return self._failure(detached, "rejected", "authorization_rejected")
            return consumed
        except _PostTaskFailure as failure:
            if detached is None:
                raise ValueError("invalid post-task submission") from None
            return self._failure(detached, failure.status, failure.reason)
        except Exception:  # noqa: BLE001 - expose one bounded rejection shape
            if detached is None:
                raise ValueError("invalid post-task submission") from None
            return self._failure(detached, "rejected", "evidence_rejected")
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            if type(token) is str:
                self._issuer.revoke(token)
            submission = cast(PostTaskSubmission, None)
            detached = None
            token = ""
            graph_content = b""
            evidence_content = None
            commit_action = None
            if "graph" in locals():
                graph = cast(Graph, None)
                initial = cast(AuthorizationVerification, None)
            if "authorized_submission" in locals():
                authorized_submission = cast(PostTaskSubmission, None)
                authorized_evidence = b""
                authorized_snapshot = MappingProxyType({})
            if "snapshot" in locals():
                snapshot = cast(LocalTransactionSnapshot, None)
                snapshot_graph_content = None
            if "checked_at" in locals():
                checked_at = cast(datetime, None)
            self = cast(PostTaskService, None)  # noqa: PLW0642 - scrub traceback state
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        raise RuntimeError("post-task evaluation unavailable") from None

    def _commit(
        self,
        submission: PostTaskSubmission,
        verification: AuthorizationVerification,
        graph: Graph,
        graph_content: bytes,
        evidence_content: bytes,
        snapshot_content: Mapping[str, bytes | None],
    ) -> PostTaskResult:
        if (
            graph.version != submission.graph_version
            or not verification.authorized
            or not set(submission.requirement_ids).issubset(verification.relevant_node_ids)
        ):
            raise _PostTaskFailure("rejected", "authorization_rejected")
        repository_id, resolved_principals = self._authority_resolver(
            submission.actor,
            MappingProxyType(dict(snapshot_content)),
        )
        principals = frozenset(resolved_principals)
        if (
            type(repository_id) is not str
            or repository_id != submission.repository_id
            or not principals
            or submission.actor not in principals
            or any(
            type(item) is not str or not item for item in principals
            )
        ):
            raise _PostTaskFailure("rejected", "authorization_rejected")
        records, ingestions, legacy_ids = parse_evidence_lines(evidence_content)
        if legacy_ids:
            raise _PostTaskFailure("rejected", "evidence_rejected")
        by_id = {record.id: record for record in records}
        try:
            git_records = tuple(by_id[reference] for reference in submission.git_evidence_refs)
        except KeyError:
            raise _PostTaskFailure("rejected", "evidence_rejected") from None
        if not refs_allowed(submission.git_evidence_refs, git_records, principals):
            raise _PostTaskFailure("rejected", "evidence_rejected")
        covered: set[str] = set()
        subjects: set[str] = set()
        for record in git_records:
            payload = record.payload
            paths = cast(object, payload.get("changed_paths"))
            parents = cast(object, payload.get("parents"))
            subject = cast(object, payload.get("subject"))
            if (
                record.connector_type != "git"
                or record.author not in principals
                or record.external_object_id != f"commit:{submission.final_revision}"
                or record.external_version != submission.final_revision
                or record.source_locator != f"git:commit:{submission.final_revision}"
                or payload.get("sha") != submission.final_revision
                or payload.get("author") != record.author
                or payload.get("repository_id") != repository_id
                or type(paths) is not tuple
                or type(parents) is not tuple
                or submission.base_revision not in parents
                or type(subject) is not str
                or not subject.strip()
                or record.observed_at > submission.completed_at
                or not _current(record, ingestions)
            ):
                raise _PostTaskFailure("rejected", "evidence_rejected")
            try:
                covered.update(_canonical_tuple(cast(tuple[str, ...], paths), paths=True))
            except ValueError:
                raise _PostTaskFailure("rejected", "evidence_rejected") from None
            subjects.add(subject.strip())
        if covered != set(submission.changed_paths) or len(subjects) != 1:
            raise _PostTaskFailure("rejected", "evidence_rejected")

        nodes = {node.id: node for node in graph.nodes}
        try:
            requirements = tuple(nodes[item] for item in submission.requirement_ids)
            code_nodes = tuple(nodes[item] for item in submission.code_refs)
            test_nodes = tuple(nodes[item] for item in submission.test_refs)
        except KeyError:
            raise _PostTaskFailure("rejected", "reference_rejected") from None
        code_types = {
            NodeType.MODULE,
            NodeType.FILE,
            NodeType.SYMBOL,
            NodeType.ENDPOINT,
            NodeType.SCHEMA,
            NodeType.BUILD_ARTIFACT,
            NodeType.DEPLOYMENT,
        }
        if (
            any(_node_type(node) is not NodeType.REQUIREMENT or node.status != "active" for node in requirements)
            or any(_node_type(node) not in code_types for node in code_nodes)
            or any(_node_type(node) is not NodeType.TEST for node in test_nodes)
            or not {node.label for node in (*code_nodes, *test_nodes)}.issubset(
                submission.changed_paths
            )
            or {node.label for node in (*code_nodes, *test_nodes)}
            != set(submission.changed_paths)
        ):
            raise _PostTaskFailure("rejected", "reference_rejected")
        requirement_evidence: list[EvidenceRecord] = []
        for requirement in requirements:
            try:
                resolved = tuple(by_id[ref] for ref in requirement.evidence_refs)
            except KeyError:
                raise _PostTaskFailure("rejected", "reference_rejected") from None
            if (
                not requirement.evidence_refs
                or not refs_allowed(requirement.evidence_refs, resolved, principals)
                or any(not _current(record, ingestions) for record in resolved)
            ):
                raise _PostTaskFailure("rejected", "reference_rejected")
            requirement_evidence.extend(resolved)

        if not submission.test_refs or not submission.test_evidence_refs:
            raise _PostTaskFailure("review_required", "review_required")
        try:
            test_records = tuple(by_id[reference] for reference in submission.test_evidence_refs)
        except KeyError:
            raise _PostTaskFailure("rejected", "evidence_rejected") from None
        if not refs_allowed(submission.test_evidence_refs, test_records, principals):
            raise _PostTaskFailure("rejected", "evidence_rejected")
        covered_tests: set[str] = set()
        for record in test_records:
            payload = record.payload
            test_refs = cast(object, payload.get("test_refs"))
            if (
                record.connector_type != "test_result"
                or record.external_object_id != f"test-run:{submission.final_revision}"
                or record.source_locator != f"test:run:{submission.final_revision}"
                or payload.get("commit_sha") != submission.final_revision
                or payload.get("outcome") != "passed"
                or type(test_refs) is not tuple
                or record.observed_at > submission.completed_at
                or not _current(record, ingestions)
            ):
                raise _PostTaskFailure("rejected", "evidence_rejected")
            try:
                covered_tests.update(_canonical_tuple(cast(tuple[str, ...], test_refs)))
            except ValueError:
                raise _PostTaskFailure("rejected", "evidence_rejected") from None
        if covered_tests != set(submission.test_refs):
            raise _PostTaskFailure("rejected", "evidence_rejected")

        code_evidence_refs = tuple(sorted(submission.git_evidence_refs))
        test_evidence_refs = tuple(sorted(submission.test_evidence_refs))
        evidence_refs = tuple(sorted((*code_evidence_refs, *test_evidence_refs)))
        verified_at = max(record.observed_at for record in (*git_records, *test_records))
        claim_material = "\x00".join(
            (
                submission.task_id,
                submission.final_revision,
                *submission.requirement_ids,
                *submission.code_refs,
                *submission.test_refs,
                *evidence_refs,
            )
        )
        claim = ImplementationClaim(
            id=f"implementation-claim:sha256:{hashlib.sha256(claim_material.encode()).hexdigest()}",
            status=ImplementationStatus.IMPLEMENTED_BASELINE,
            requirement_refs=submission.requirement_ids,
            current_behavior=next(iter(subjects)),
            code_evidence=code_evidence_refs,
            test_evidence=test_evidence_refs,
            verified_commit=submission.final_revision,
            verified_at=verified_at,
            test_evidence_required=bool(submission.test_refs),
        )
        existing_links = {
            (edge.from_id, edge.relation, edge.to_id) for edge in graph.edges if edge.status == "active"
        }
        evidence_author = git_records[0].author
        edges: list[Edge] = []
        for requirement in requirements:
            for target, relation in (
                *((node, RelationType.IMPLEMENTED_BY) for node in code_nodes),
                *((node, RelationType.VERIFIED_BY) for node in test_nodes),
            ):
                key = (requirement.id, relation, target.id)
                if key in existing_links:
                    continue
                material = "\x00".join((requirement.id, relation.value, target.id, *evidence_refs))
                edges.append(
                    Edge.model_validate(
                        {
                            "id": f"edge:post-task:sha256:{hashlib.sha256(material.encode()).hexdigest()}",
                            "from": requirement.id,
                            "relation": relation,
                            "to": target.id,
                            "status": "active",
                            "created_by": cast(str, evidence_author),
                            "created_at": verified_at,
                            "last_modified_by": cast(str, evidence_author),
                            "last_modified_at": verified_at,
                        }
                    )
                )
        status_changes: list[ImplementationStatusChange] = []
        for node in code_nodes:
            prior = node.implementation_status or ImplementationStatus.UNKNOWN
            if prior is ImplementationStatus.IMPLEMENTED_BASELINE:
                if not set(evidence_refs).issubset(node.evidence_refs):
                    raise _PostTaskFailure("review_required", "review_required")
                continue
            status_changes.append(
                ImplementationStatusChange(
                    claim_id=node.id,
                    prior=prior,
                    new=ImplementationStatus.IMPLEMENTED_BASELINE,
                    evidence_refs=evidence_refs,
                )
            )
        mutations = bool(edges or status_changes)
        changeset_id: str | None = None
        next_version = graph.version
        if mutations:
            changeset_material = "\x00".join(
                (
                    str(graph.version),
                    submission.task_id,
                    submission.final_revision,
                    *(edge.id for edge in sorted(edges, key=lambda item: item.id)),
                    *(item.claim_id for item in sorted(status_changes, key=lambda item: item.claim_id)),
                    *evidence_refs,
                )
            )
            changeset_id = (
                "changeset:post-task:sha256:"
                + hashlib.sha256(changeset_material.encode()).hexdigest()
            )
            changeset = ChangeSet(
                id=changeset_id,
                actor=submission.actor,
                timestamp=verified_at,
                baseline_graph_version=graph.version,
                evidence_refs=evidence_refs,
                nodes_added=(),
                nodes_updated=(),
                nodes_superseded=(),
                edges_added=tuple(sorted(edges, key=lambda item: item.id)),
                edges_updated=(),
                edges_superseded=(),
                confidence_changes=(),
                implementation_status_changes=tuple(
                    sorted(status_changes, key=lambda item: item.claim_id)
                ),
                reconciliation_cases_created=(),
                reconciliation_cases_resolved=(),
                validation_status="validated",
            )
            committed = self._executor.apply(
                changeset,
                graph_preimage=graph_content,
                evidence_preimage=evidence_content,
                rollback_base_exceptions=True,
                read_only_extras=self._authority_files,
                extra_preimages={
                    name: snapshot_content.get(name) for name in self._authority_files
                },
            )
            next_version = committed.version
        return PostTaskResult(
            status="recorded",
            reason="recorded",
            task_id=submission.task_id,
            graph_version=next_version,
            claim=claim,
            changeset_id=changeset_id,
        )


__all__ = [
    "PostTaskReason",
    "PostTaskResult",
    "PostTaskService",
    "PostTaskStatus",
    "PostTaskSubmission",
]
