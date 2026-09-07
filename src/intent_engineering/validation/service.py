"""Deep validation over one descriptor-rooted, recovered local state snapshot."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, ValidationError

from intent_engineering.capture.github.connector import GitHubCheckpoint
from intent_engineering.capture.mcp.connector import McpCheckpoint, _mcp_profile_identity
from intent_engineering.core.graph.applier import apply_changeset_with_case_effects
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceIngestion,
    EvidenceRecord,
    Graph,
    ProjectConfig,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
    SyncCheckpoint,
    is_exact_consumed_prefix,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.jsonl.case_store import (
    CaseStoreError,
    parse_case_versions,
)
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.jsonl.strict import loads_strict_json
from intent_engineering.storage.secure import (
    SecureDirectory,
    SecureFile,
    UnsafePathError,
    configured_graph_relative,
)
from intent_engineering.storage.transaction import (
    LocalTransactionCoordinator,
    TransactionRecoveryError,
)
from intent_engineering.storage.yaml.graph_store import parse_graph

_CODE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_CURSOR = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MARKDOWN_CURSOR_PREFIX = "markdown:v1:"
_GITHUB_CONNECTOR_PREFIX = "github:"
_GITHUB_SHA = re.compile(r"^[0-9a-f]{40}$")
_MCP_CONNECTOR_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_MCP_WRITE_EVIDENCE = re.compile(r"^evidence:mcp-write:[0-9a-f]{64}$")


class DiagnosticSeverity(StrEnum):
    """Whether one diagnostic invalidates state or records safe recovery work."""

    ERROR = "error"
    NOTICE = "notice"


class ValidationDiagnostic(StrictModel):
    """One stable, versioned, non-sensitive validation finding."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    code: str = Field(pattern=_CODE.pattern)
    scope: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR


class ValidationReport(StrictModel):
    """The deterministic result of validating one consistent local snapshot."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    valid: bool
    graph_id: str | None = None
    graph_version: int | None = None
    diagnostics: tuple[ValidationDiagnostic, ...]


@dataclass(frozen=True)
class _CapturedWorkspace:
    config: ProjectConfig
    content: Mapping[str, bytes | None]
    recovered: bool


class _CaptureFailure(ValueError):
    def __init__(self, code: str, scope: str) -> None:
        self.code = code
        self.scope = scope
        super().__init__(code)


def _diagnostic(
    code: str,
    scope: str,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
) -> ValidationDiagnostic:
    return ValidationDiagnostic(code=code, scope=scope, severity=severity)


def _report(
    diagnostics: Sequence[ValidationDiagnostic],
    graph: Graph | None = None,
) -> ValidationReport:
    unique = {(item.code, item.scope, item.severity.value): item for item in diagnostics}
    ordered = tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (item[2] != DiagnosticSeverity.ERROR.value, item[0], item[1]),
        )
    )
    return ValidationReport(
        valid=not any(item.severity is DiagnosticSeverity.ERROR for item in ordered),
        graph_id=graph.id if graph is not None else None,
        graph_version=graph.version if graph is not None else None,
        diagnostics=ordered,
    )


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise ValueError("unhashable YAML key") from error
        if duplicate:
            raise ValueError("duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(content: bytes) -> object:
    return yaml.load(content.decode("utf-8"), Loader=_UniqueKeyLoader)


def _load_json_lines(
    content: bytes | None,
    *,
    allow_blank: bool,
) -> tuple[object, ...]:
    if content is None:
        return ()
    lines = content.decode("utf-8").splitlines()
    values: list[object] = []
    for line in lines:
        if not line.strip():
            if allow_blank:
                continue
            raise ValueError("blank JSONL record")
        values.append(loads_strict_json(line))
    return tuple(values)


def _parse_config(content: bytes) -> ProjectConfig:
    loaded = _load_yaml(content)
    if not isinstance(loaded, dict):
        raise TypeError("configuration must be a mapping")
    return ProjectConfig.model_validate_json(json.dumps(cast(dict[str, Any], loaded)))


def _parse_graph(content: bytes | None) -> Graph:
    if content is None:
        raise ValueError("graph is missing")
    _load_yaml(content)
    return parse_graph(content)


def _parse_evidence(
    content: bytes | None,
) -> tuple[tuple[EvidenceRecord, ...], tuple[EvidenceIngestion, ...], tuple[str, ...]]:
    return parse_evidence_lines(content)


def _parse_history(content: bytes | None) -> tuple[ChangeSet, ...]:
    return tuple(
        ChangeSet.model_validate(value) for value in _load_json_lines(content, allow_blank=True)
    )


def _parse_cases(content: bytes | None) -> tuple[ReconciliationCase, ...]:
    # Preflight strict JSON first; the store parser then supplies the sole legacy migration.
    _load_json_lines(content, allow_blank=False)
    return parse_case_versions(content)


def _parse_checkpoints(content: bytes | None) -> dict[str, SyncCheckpoint]:
    if content is None:
        return {}
    loaded = _load_yaml(content)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict) or set(loaded) != {"checkpoints"}:
        raise ValueError("invalid checkpoints")
    records = loaded["checkpoints"]
    if not isinstance(records, dict):
        raise TypeError("invalid checkpoints")
    checkpoints: dict[str, SyncCheckpoint] = {}
    for connector_id, payload in records.items():
        if not isinstance(connector_id, str) or not isinstance(payload, dict):
            raise TypeError("invalid checkpoint")
        checkpoint = SyncCheckpoint.model_validate(payload)
        if checkpoint.connector_id != connector_id:
            raise ValueError("checkpoint connector mismatch")
        checkpoints[connector_id] = checkpoint
    return checkpoints


def _conversation_identity(record: EvidenceRecord) -> tuple[str, str, str] | None:
    payload = record.model_dump(mode="json")["payload"]
    if not isinstance(payload, dict) or set(payload) != {"content", "role"}:
        return None
    role = payload["role"]
    if role not in {"human", "agent"}:
        return None
    encoded_content = json.dumps(
        payload["content"],
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    content_hash = f"sha256:{sha256(encoded_content).hexdigest()}"
    version_material = {
        "acl": sorted(record.acl),
        "author": record.author,
        "captured_at": record.observed_at.isoformat().replace("+00:00", "Z"),
        "content_hash": content_hash,
        "conversation_ref": record.external_object_id,
        "role": role,
    }
    encoded_version = json.dumps(
        version_material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    version = f"sha256:{sha256(encoded_version).hexdigest()}"
    return content_hash, version, f"evidence:conversation:{version.removeprefix('sha256:')}"


def _expected_evidence_id(record: EvidenceRecord) -> str:
    if record.connector_type == "conversation":
        identity = _conversation_identity(record)
        if identity is not None:
            return identity[2]
    if record.connector_type == "mcp-write" and _MCP_WRITE_EVIDENCE.fullmatch(record.id):
        return record.id
    material = (
        f"{record.connector_type}\x00{record.external_object_id}\x00"
        f"{record.external_version}\x00{record.content_hash}"
    )
    return f"evidence:sha256:{sha256(material.encode('utf-8')).hexdigest()}"


def _evidence_diagnostics(
    records: Sequence[EvidenceRecord],
    legacy_ids: Sequence[str] = (),
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    by_id: dict[str, EvidenceRecord] = {}
    by_version: dict[tuple[str, str, str], EvidenceRecord] = {}
    positions: dict[str, int] = {}
    for position, record in enumerate(records):
        if record.id in legacy_ids and record.connector_type not in {"markdown", "git"}:
            diagnostics.append(_diagnostic("evidence.legacy_association_ambiguous", "evidence"))
        previous_id = by_id.get(record.id)
        if previous_id is not None:
            diagnostics.append(
                _diagnostic(
                    "evidence.id_duplicate" if previous_id == record else "evidence.id_conflict",
                    "evidence",
                )
            )
        else:
            by_id[record.id] = record
            positions[record.id] = position
        version_key = (
            record.connector_type,
            record.external_object_id,
            record.external_version,
        )
        previous_version = by_version.get(version_key)
        if previous_version is not None and previous_version != record:
            diagnostics.append(_diagnostic("evidence.version_conflict", "evidence"))
        else:
            by_version[version_key] = record
        if record.id != _expected_evidence_id(record):
            diagnostics.append(_diagnostic("evidence.id_mismatch", "evidence"))
        if _SHA256.fullmatch(record.content_hash) is None:
            diagnostics.append(_diagnostic("evidence.content_hash_invalid", "evidence"))
        payload = record.model_dump(mode="json")["payload"]
        if record.connector_type == "conversation":
            identity = _conversation_identity(record)
            if identity is None or record.content_hash != identity[0]:
                diagnostics.append(_diagnostic("evidence.content_hash_mismatch", "evidence"))
            if identity is None or record.external_version != identity[1]:
                diagnostics.append(_diagnostic("evidence.version_mismatch", "evidence"))
        elif record.connector_type == "markdown":
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, str) or record.content_hash != (
                f"sha256:{sha256(content.encode('utf-8')).hexdigest()}"
            ):
                diagnostics.append(_diagnostic("evidence.content_hash_mismatch", "evidence"))
        elif record.connector_type in {"git", "github", "mcp-write"}:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if record.content_hash != f"sha256:{sha256(encoded).hexdigest()}":
                diagnostics.append(_diagnostic("evidence.content_hash_mismatch", "evidence"))
        elif record.connector_type == "mcp":
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, dict):
                diagnostics.append(_diagnostic("evidence.content_hash_mismatch", "evidence"))
            else:
                encoded = json.dumps(
                    content,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
                if record.content_hash != f"sha256:{sha256(encoded).hexdigest()}":
                    diagnostics.append(_diagnostic("evidence.content_hash_mismatch", "evidence"))

    for position, record in enumerate(records):
        if record.parent_ref is None:
            continue
        parent = by_id.get(record.parent_ref)
        if parent is None:
            diagnostics.append(_diagnostic("evidence.parent_ref_missing", "evidence"))
            continue
        if (
            positions[parent.id] >= position
            or parent.connector_type != record.connector_type
            or parent.external_object_id != record.external_object_id
            or parent.external_version == record.external_version
        ):
            diagnostics.append(_diagnostic("evidence.parent_chain_invalid", "evidence"))
    return diagnostics


def _case_diagnostics(
    cases: Sequence[ReconciliationCase],
    graph: Graph,
    evidence_by_id: Mapping[str, EvidenceRecord],
    history: Sequence[ChangeSet],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    graph_ids = {node.id for node in graph.nodes}
    versions_by_id: dict[str, list[ReconciliationCase]] = defaultdict(list)
    for case in cases:
        versions_by_id[case.id].append(case)
        if case.subject_ref not in graph_ids:
            diagnostics.append(_diagnostic("case.subject_ref_missing", "cases"))
        if any(reference not in graph_ids for reference in case.affected_refs):
            diagnostics.append(_diagnostic("case.affected_ref_missing", "cases"))
        for side in case.evidence_sides:
            resolved = tuple(evidence_by_id.get(reference) for reference in side.evidence_refs)
            if any(record is None for record in resolved):
                diagnostics.append(_diagnostic("case.evidence_ref_missing", "cases"))
                continue
            records = cast(tuple[EvidenceRecord, ...], resolved)
            expected_authors = tuple(sorted({record.author for record in records if record.author}))
            expected_observed_at = max(record.observed_at for record in records)
            if side.authors != expected_authors or side.observed_at != expected_observed_at:
                diagnostics.append(_diagnostic("case.provenance_mismatch", "cases"))
        if case.case_type is ReconciliationCaseType.CONFLICTING_SOURCES:
            seen_refs: set[str] = set()
            for side in case.evidence_sides:
                current_refs = set(side.evidence_refs)
                if seen_refs & current_refs:
                    diagnostics.append(_diagnostic("case.evidence_overlap", "cases"))
                seen_refs.update(current_refs)
        previous_at = case.created_at
        for event in case.history:
            if event.at < previous_at:
                diagnostics.append(_diagnostic("case.history_time_invalid", "cases"))
                break
            previous_at = event.at

    created_by_changeset = {
        case_id for changeset in history for case_id in changeset.reconciliation_cases_created
    }
    resolved_by_changeset = {
        (case_id, changeset.id)
        for changeset in history
        for case_id in changeset.reconciliation_cases_resolved
    }
    for case_id, versions in versions_by_id.items():
        # Task preflight creates an evidence-backed review case without changing
        # canonical graph semantics, so it intentionally has no graph history row.
        workflow_review_case = versions[0].detector_id in {
            "intent_workflow.preflight.v1",
            "intent_workflow.proposal_governance.v1",
        }
        if case_id not in created_by_changeset and not workflow_review_case:
            diagnostics.append(_diagnostic("history.case_creation_missing", "history"))
        latest = versions[-1]
        if latest.status is ReconciliationStatus.RESOLVED and (
            latest.resolved_by_changeset is None
            or (case_id, latest.resolved_by_changeset) not in resolved_by_changeset
        ):
            diagnostics.append(_diagnostic("history.case_resolution_missing", "history"))
        serialized_versions = [item.model_dump_json() for item in versions]
        if len(serialized_versions) != len(set(serialized_versions)):
            diagnostics.append(_diagnostic("case.version_duplicate", "cases"))
    known_case_ids = set(versions_by_id)
    for changeset in history:
        for case_id in changeset.reconciliation_cases_created:
            if case_id not in known_case_ids:
                diagnostics.append(_diagnostic("history.case_creation_effect_missing", "history"))
        for case_id in changeset.reconciliation_cases_resolved:
            resolved_versions = versions_by_id.get(case_id)
            if (
                not resolved_versions
                or resolved_versions[-1].status is not ReconciliationStatus.RESOLVED
                or resolved_versions[-1].resolved_by_changeset != changeset.id
            ):
                diagnostics.append(_diagnostic("history.case_resolution_effect_missing", "history"))
    return diagnostics


def _changeset_evidence_refs(
    changeset: ChangeSet,
    cases_by_id: Mapping[str, ReconciliationCase],
) -> tuple[set[str], set[str]]:
    direct = set(changeset.evidence_refs)
    nested: set[str] = set()
    for node in changeset.nodes_added:
        nested.update(node.evidence_refs)
    for update in changeset.nodes_updated:
        nested.update(update.replacement.evidence_refs)
    for confidence_change in changeset.confidence_changes:
        nested.update(confidence_change.evidence_refs)
    for status_change in changeset.implementation_status_changes:
        nested.update(status_change.evidence_refs)
    for case_id in (
        *changeset.reconciliation_cases_created,
        *changeset.reconciliation_cases_resolved,
    ):
        case = cases_by_id.get(case_id)
        if case is not None:
            nested.update(case.all_evidence_refs)
    return direct, nested


def _history_diagnostics(
    history: Sequence[ChangeSet],
    graph: Graph,
    evidence_by_id: Mapping[str, EvidenceRecord],
    cases: Sequence[ReconciliationCase],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    ids: set[str] = set()
    cases_by_id = {case.id: case for case in cases}
    replay = graph.model_copy(update={"version": 0, "nodes": (), "edges": ()})
    replay_failed = False
    for expected_baseline, changeset in enumerate(history):
        if changeset.id in ids:
            diagnostics.append(_diagnostic("history.id_duplicate", "history"))
        ids.add(changeset.id)
        if changeset.baseline_graph_version != expected_baseline:
            diagnostics.append(_diagnostic("history.baseline_mismatch", "history"))
        if not changeset.is_semantic:
            diagnostics.append(_diagnostic("history.empty_changeset", "history"))
        direct, nested = _changeset_evidence_refs(changeset, cases_by_id)
        if any(reference not in evidence_by_id for reference in direct | nested):
            diagnostics.append(_diagnostic("history.evidence_ref_missing", "history"))
        if not nested.issubset(direct):
            diagnostics.append(_diagnostic("history.evidence_scope_mismatch", "history"))
        if not replay_failed:
            try:
                replay = apply_changeset_with_case_effects(replay, changeset)
            except (TypeError, ValueError):
                replay_failed = True
                diagnostics.append(_diagnostic("history.replay_invalid", "history"))
    if graph.version != len(history):
        diagnostics.append(_diagnostic("history.graph_version_mismatch", "history"))
    if not replay_failed and replay != graph:
        diagnostics.append(_diagnostic("history.graph_state_mismatch", "history"))
    return diagnostics


def _graph_diagnostics(
    graph: Graph,
    evidence_by_id: Mapping[str, EvidenceRecord],
) -> list[ValidationDiagnostic]:
    if any(
        reference not in evidence_by_id for node in graph.nodes for reference in node.evidence_refs
    ):
        return [_diagnostic("graph.evidence_ref_missing", "graph")]
    return []


def _parse_markdown_cursor(cursor: str) -> dict[str, str]:
    if not cursor.startswith(_MARKDOWN_CURSOR_PREFIX):
        raise ValueError("not a manifest cursor")
    loaded = loads_strict_json(cursor.removeprefix(_MARKDOWN_CURSOR_PREFIX))
    if not isinstance(loaded, dict) or set(loaded) != {"files"}:
        raise ValueError("invalid manifest")
    files = loaded["files"]
    if not isinstance(files, list):
        raise TypeError("invalid manifest")
    result: dict[str, str] = {}
    previous: str | None = None
    canonical: list[list[str]] = []
    for item in files:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            raise ValueError("invalid manifest")
        path, version = cast(tuple[str, str], tuple(item))
        if (
            not path
            or path.startswith("/")
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or _SHA256.fullmatch(version) is None
            or (previous is not None and path <= previous)
        ):
            raise ValueError("invalid manifest")
        result[path] = version
        canonical.append([path, version])
        previous = path
    expected = _MARKDOWN_CURSOR_PREFIX + json.dumps(
        {"files": canonical}, separators=(",", ":"), sort_keys=True
    )
    if cursor != expected:
        raise ValueError("noncanonical manifest")
    return result


def _github_scoped_record_kind(record: EvidenceRecord, repository: str) -> str | None:
    """Return one deeply valid GitHub kind scoped to the checkpoint repository."""
    if record.connector_type != "github":
        return None
    prefix = f"github:{repository}:"
    if not record.external_object_id.startswith(prefix):
        return None
    suffix = record.external_object_id.removeprefix(prefix)
    kind, separator, provider_identity = suffix.partition(":")
    if not separator:
        return None
    payload = record.model_dump(mode="json")["payload"]
    if payload.get("repository") != repository or payload.get("kind") != kind:
        return None
    if kind == "commit":
        if (
            _GITHUB_SHA.fullmatch(provider_identity) is None
            or record.external_version != provider_identity
            or payload.get("sha") != provider_identity
        ):
            return None
        return kind
    if kind not in {"issue", "pull_request", "issue_comment", "review_comment"}:
        return None
    if (
        not provider_identity.isascii()
        or not provider_identity.isdecimal()
        or provider_identity.startswith("0")
    ):
        return None
    identity_field = "number" if kind in {"issue", "pull_request"} else "provider_id"
    if payload.get(identity_field) != int(provider_identity):
        return None
    canonical_observed_at = record.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if (
        record.external_version != canonical_observed_at
        or payload.get("updated_at") != canonical_observed_at
    ):
        return None
    return kind


def _github_association_diagnostics(
    ingestions: Sequence[EvidenceIngestion],
) -> list[ValidationDiagnostic]:
    """Validate every GitHub ingestion association, including partial uncheckpointed ledgers."""
    diagnostics: list[ValidationDiagnostic] = []
    for ingestion in ingestions:
        connector_id = ingestion.connector_id
        record = ingestion.evidence
        if connector_id.startswith(_GITHUB_CONNECTOR_PREFIX):
            repository = connector_id.removeprefix(_GITHUB_CONNECTOR_PREFIX)
            try:
                GitHubCheckpoint(repository=repository)
            except (TypeError, ValidationError, ValueError):
                diagnostics.append(_diagnostic("evidence.github_association_invalid", "evidence"))
                continue
            if _github_scoped_record_kind(record, repository) is None:
                diagnostics.append(_diagnostic("evidence.github_association_invalid", "evidence"))
        elif record.connector_type == "github":
            diagnostics.append(_diagnostic("evidence.github_association_invalid", "evidence"))
    return diagnostics


def _mcp_scoped_record(record: EvidenceRecord, connector_id: str) -> bool:
    """Return whether one MCP record is deeply scoped to its ingestion identity."""
    if record.connector_type != "mcp" or not connector_id.startswith("mcp:"):
        return False
    try:
        connector_prefix, profile_identity, source_identity, scope_identity, actor_identity = (
            connector_id.rsplit(":", 4)
        )
    except ValueError:
        return False
    if (
        not connector_prefix.startswith("mcp:")
        or not connector_prefix.removeprefix("mcp:")
        or _MCP_CONNECTOR_IDENTITY.fullmatch(profile_identity) is None
        or _MCP_CONNECTOR_IDENTITY.fullmatch(source_identity) is None
        or _MCP_CONNECTOR_IDENTITY.fullmatch(scope_identity) is None
        or _MCP_CONNECTOR_IDENTITY.fullmatch(actor_identity) is None
    ):
        return False
    payload = record.model_dump(mode="json")["payload"]
    if not isinstance(payload, dict) or set(payload) != {
        "kind",
        "profile_id",
        "profile_version",
        "object_type",
        "scope_hash",
        "source_hash",
        "parent_context",
        "content",
    }:
        return False
    profile_id = payload.get("profile_id")
    profile_version = payload.get("profile_version")
    object_type = payload.get("object_type")
    scope_hash = payload.get("scope_hash")
    source_hash = payload.get("source_hash")
    parent_context = payload.get("parent_context")
    content = payload.get("content")
    if (
        payload.get("kind") != "mcp_object"
        or not isinstance(profile_id, str)
        or not profile_id
        or not isinstance(profile_version, str)
        or not profile_version
        or not isinstance(object_type, str)
        or not object_type
        or profile_identity != _mcp_profile_identity(profile_id, profile_version, object_type)
        or scope_hash != f"sha256:{scope_identity}"
        or source_hash != f"sha256:{source_identity}"
        or not record.external_object_id.startswith(f"{profile_id}:")
        or not record.external_object_id.removeprefix(f"{profile_id}:")
        or not isinstance(record.author, str)
        or not record.author.strip()
        or not isinstance(content, dict)
        or not content
    ):
        return False
    return parent_context is None or (
        isinstance(parent_context, str) and parent_context.startswith(f"{profile_id}:")
    )


def _mcp_association_diagnostics(
    ingestions: Sequence[EvidenceIngestion],
) -> list[ValidationDiagnostic]:
    """Validate every MCP association, including partial uncheckpointed ledgers."""
    diagnostics: list[ValidationDiagnostic] = []
    for ingestion in ingestions:
        connector_id = ingestion.connector_id
        record = ingestion.evidence
        if connector_id.startswith("mcp:"):
            if not _mcp_scoped_record(record, connector_id):
                diagnostics.append(_diagnostic("evidence.mcp_association_invalid", "evidence"))
        elif record.connector_type == "mcp":
            diagnostics.append(_diagnostic("evidence.mcp_association_invalid", "evidence"))
    return diagnostics


def _checkpoint_diagnostics(
    checkpoints: Mapping[str, SyncCheckpoint],
    evidence: Sequence[EvidenceRecord],
    ingestions: Sequence[EvidenceIngestion],
    legacy_ids: Sequence[str],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    identities = {
        (record.connector_type, record.external_object_id, record.external_version)
        for record in evidence
    }
    evidence_ids = {record.id for record in evidence}
    associations = {(item.connector_id, item.evidence.id) for item in ingestions}
    associations.update(
        (record.connector_type, record.id)
        for record in evidence
        if record.id in legacy_ids and record.connector_type in {"markdown", "git"}
    )
    ledger_ids_by_connector: dict[str, list[str]] = defaultdict(list)
    for ingestion in ingestions:
        ledger_ids_by_connector[ingestion.connector_id].append(ingestion.evidence.id)
    for record in evidence:
        if (
            record.id in legacy_ids
            and record.connector_type in {"markdown", "git"}
            and record.id not in ledger_ids_by_connector[record.connector_type]
        ):
            ledger_ids_by_connector[record.connector_type].append(record.id)
    for connector_id in sorted(checkpoints):
        checkpoint = checkpoints[connector_id]
        github_repository: str | None = None
        is_mcp_connector = False
        if connector_id.startswith(_GITHUB_CONNECTOR_PREFIX):
            github_repository = connector_id.removeprefix(_GITHUB_CONNECTOR_PREFIX)
            try:
                GitHubCheckpoint(repository=github_repository)
            except (TypeError, ValidationError, ValueError):
                diagnostics.append(_diagnostic("checkpoint.connector_unknown", "checkpoints"))
                continue
        elif connector_id.startswith("mcp:"):
            is_mcp_connector = True
        elif connector_id not in {"markdown", "git"}:
            diagnostics.append(_diagnostic("checkpoint.connector_unknown", "checkpoints"))
            continue
        for evidence_id in checkpoint.consumed_evidence_ids:
            if evidence_id not in evidence_ids:
                diagnostics.append(
                    _diagnostic("checkpoint.consumed_evidence_missing", "checkpoints")
                )
            elif (connector_id, evidence_id) not in associations:
                diagnostics.append(
                    _diagnostic("checkpoint.consumed_evidence_foreign", "checkpoints")
                )
        if not is_exact_consumed_prefix(
            checkpoint.consumed_evidence_ids,
            ledger_ids_by_connector.get(connector_id, ()),
        ):
            diagnostics.append(
                _diagnostic("checkpoint.consumed_evidence_prefix_invalid", "checkpoints")
            )
        cursor = checkpoint.cursor
        if cursor is None:
            if is_mcp_connector or (
                github_repository is not None and checkpoint.consumed_evidence_ids
            ):
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
            continue
        if github_repository is not None:
            try:
                github_cursor = GitHubCheckpoint.decode(
                    cursor,
                    expected_repository=github_repository,
                )
            except (TypeError, ValueError):
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
                continue
            associated_consumed_records = tuple(
                record
                for record in evidence
                if record.id in checkpoint.consumed_evidence_ids
                and (connector_id, record.id) in associations
            )
            kinds_by_evidence_id = {
                record.id: _github_scoped_record_kind(record, github_repository)
                for record in associated_consumed_records
            }
            invalid_association = any(
                kinds_by_evidence_id[record.id] is None for record in associated_consumed_records
            )
            if invalid_association:
                diagnostics.append(
                    _diagnostic("checkpoint.consumed_evidence_foreign", "checkpoints")
                )
            consumed_records = tuple(
                record
                for record in associated_consumed_records
                if kinds_by_evidence_id[record.id] is not None
            )
            mutable_records = tuple(
                record for record in consumed_records if kinds_by_evidence_id[record.id] != "commit"
            )
            if mutable_records:
                expected_updated_at = max(record.observed_at for record in mutable_records)
                if github_cursor.newest_updated_at != expected_updated_at:
                    diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
            elif github_cursor.newest_updated_at is not None:
                diagnostics.append(_diagnostic("checkpoint.evidence_missing", "checkpoints"))
            commit_records = tuple(
                record for record in consumed_records if kinds_by_evidence_id[record.id] == "commit"
            )
            if commit_records:
                if github_cursor.newest_commit_sha is None:
                    diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
                elif not any(
                    record.external_object_id
                    == f"github:{github_repository}:commit:{github_cursor.newest_commit_sha}"
                    and record.external_version == github_cursor.newest_commit_sha
                    for record in commit_records
                ):
                    diagnostics.append(_diagnostic("checkpoint.evidence_missing", "checkpoints"))
            elif github_cursor.newest_commit_sha is not None:
                diagnostics.append(_diagnostic("checkpoint.evidence_missing", "checkpoints"))
            if not consumed_records and (
                github_cursor.newest_updated_at is not None
                or github_cursor.newest_commit_sha is not None
            ):
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
            continue
        if is_mcp_connector:
            try:
                mcp_cursor = McpCheckpoint.decode_unscoped(cursor)
            except (TypeError, ValidationError, ValueError):
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
                continue
            if mcp_cursor.connector_id != connector_id:
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
                continue
            try:
                (
                    _connector_prefix,
                    profile_identity,
                    source_identity,
                    scope_identity,
                    _actor_identity,
                ) = connector_id.rsplit(":", 4)
            except ValueError:
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
                continue
            if (
                profile_identity
                != _mcp_profile_identity(
                    mcp_cursor.profile_id,
                    mcp_cursor.profile_version,
                    mcp_cursor.object_type,
                )
                or source_identity != mcp_cursor.source_hash.removeprefix("sha256:")
                or scope_identity != mcp_cursor.scope_hash.removeprefix("sha256:")
            ):
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
                continue
            records_by_id = {record.id: record for record in evidence}
            consumed_records = tuple(
                records_by_id[evidence_id]
                for evidence_id in checkpoint.consumed_evidence_ids
                if evidence_id in records_by_id and (connector_id, evidence_id) in associations
            )
            expected_versions: dict[str, str] = {}
            invalid_record = False
            for record in consumed_records:
                payload = record.payload
                if not _mcp_scoped_record(record, connector_id) or (
                    payload.get("profile_id") != mcp_cursor.profile_id
                    or payload.get("profile_version") != mcp_cursor.profile_version
                    or payload.get("object_type") != mcp_cursor.object_type
                ):
                    invalid_record = True
                    continue
                expected_versions[record.external_object_id] = record.external_version
            if invalid_record:
                diagnostics.append(
                    _diagnostic("checkpoint.consumed_evidence_foreign", "checkpoints")
                )
            if dict(mcp_cursor.observed_versions) != expected_versions:
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
            continue
        if connector_id == "git":
            if _GIT_CURSOR.fullmatch(cursor) is None:
                diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
            elif ("git", f"commit:{cursor}", cursor) not in identities:
                diagnostics.append(_diagnostic("checkpoint.evidence_missing", "checkpoints"))
            continue
        if _SHA256.fullmatch(cursor) is not None:
            diagnostics.append(
                _diagnostic(
                    "checkpoint.cursor_legacy",
                    "checkpoints",
                    DiagnosticSeverity.NOTICE,
                )
            )
            continue
        try:
            manifest = _parse_markdown_cursor(cursor)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            diagnostics.append(_diagnostic("checkpoint.cursor_invalid", "checkpoints"))
            continue
        if any(
            ("markdown", f"path:{path}", version) not in identities
            for path, version in manifest.items()
        ):
            diagnostics.append(_diagnostic("checkpoint.evidence_missing", "checkpoints"))
    return diagnostics


class WorkspaceValidationService:
    """Capture and deeply validate one local workspace without exposing local data."""

    def __init__(
        self,
        root: Path,
        *,
        project_directory: SecureDirectory | None = None,
    ) -> None:
        self._root = root
        self._project_directory = project_directory

    def _capture(self) -> _CapturedWorkspace:
        project_directory: SecureDirectory | None = None
        workspace_directory: SecureDirectory | None = None
        files: list[SecureFile] = []
        try:
            try:
                project_directory = (
                    SecureDirectory.open(self._root)
                    if self._project_directory is None
                    else self._project_directory.duplicate()
                )
                workspace_directory = project_directory.subdirectory(".intent")
            except (OSError, UnsafePathError) as error:
                raise _CaptureFailure("workspace.not_initialized", "workspace") from error

            for name in ("evidence", "reconciliation", "history", "approvals", "cache"):
                try:
                    directory = workspace_directory.subdirectory(name)
                    directory.close()
                except (OSError, UnsafePathError) as error:
                    raise _CaptureFailure("workspace.structure_invalid", "workspace") from error

            config_file = workspace_directory.file("config.yaml")
            files.append(config_file)
            try:
                with same_path_lock(config_file):
                    initial_config_content = config_file.read_bytes()
                config = _parse_config(initial_config_content)
            except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError) as error:
                raise _CaptureFailure("config.invalid", "config") from error

            try:
                graph_file = workspace_directory.file(configured_graph_relative(config.graph_path))
                graph_file.assert_regular()
            except (OSError, UnsafePathError) as error:
                raise _CaptureFailure("graph.path_invalid", "graph") from error
            files.append(graph_file)
            state_files = {
                "history": workspace_directory.file("history/changesets.jsonl"),
                "cases": workspace_directory.file("reconciliation/cases.jsonl"),
                "evidence": workspace_directory.file("evidence/evidence.jsonl"),
                "receipts": workspace_directory.file("approvals/receipts.jsonl"),
                "checkpoints": workspace_directory.file("cache/checkpoints.yaml"),
            }
            files.extend(state_files.values())
            for scope, state_file in state_files.items():
                try:
                    if state_file.exists():
                        state_file.assert_regular()
                except (OSError, UnsafePathError) as error:
                    raise _CaptureFailure(f"{scope}.unsafe", scope) from error

            journal = workspace_directory.file("history/.local-transaction.json")
            files.append(journal)
            try:
                transactions = LocalTransactionCoordinator(
                    journal,
                    {
                        "graph": graph_file,
                        "history": state_files["history"],
                        "cases": state_files["cases"],
                        "evidence": state_files["evidence"],
                        "receipts": state_files["receipts"],
                    },
                    legacy_target_sets=(frozenset({"graph", "history", "cases"}),),
                )
                snapshot = transactions.snapshot(
                    {
                        "config": config_file,
                        "checkpoints": state_files["checkpoints"],
                    }
                )
            except TransactionRecoveryError as error:
                raise _CaptureFailure("transaction.corrupt", "transaction") from error
            except (OSError, UnsafePathError, ValueError) as error:
                raise _CaptureFailure("workspace.snapshot_invalid", "workspace") from error
            if snapshot.content["config"] != initial_config_content:
                raise _CaptureFailure("workspace.snapshot_changed", "workspace")
            return _CapturedWorkspace(config, snapshot.content, snapshot.recovered)
        finally:
            for secure_file in files:
                secure_file.close()
            if workspace_directory is not None:
                workspace_directory.close()
            if project_directory is not None:
                project_directory.close()

    def validate(self) -> ValidationReport:
        """Return deterministic diagnostics while redacting all parser and path details."""
        try:
            captured = self._capture()
        except _CaptureFailure as error:
            return _report((_diagnostic(error.code, error.scope),))
        return self._validate_snapshot(captured)

    @staticmethod
    def _validate_snapshot(captured: _CapturedWorkspace) -> ValidationReport:
        diagnostics: list[ValidationDiagnostic] = []
        if captured.recovered:
            diagnostics.append(
                _diagnostic(
                    "transaction.recovered",
                    "transaction",
                    DiagnosticSeverity.NOTICE,
                )
            )

        graph: Graph | None = None
        evidence: tuple[EvidenceRecord, ...] | None = None
        ingestions: tuple[EvidenceIngestion, ...] | None = None
        legacy_ids: tuple[str, ...] = ()
        cases: tuple[ReconciliationCase, ...] | None = None
        history: tuple[ChangeSet, ...] | None = None
        checkpoints: dict[str, SyncCheckpoint] | None = None
        try:
            graph = _parse_graph(captured.content["graph"])
        except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
            diagnostics.append(_diagnostic("graph.invalid", "graph"))
        try:
            evidence, ingestions, legacy_ids = _parse_evidence(captured.content["evidence"])
        except (UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
            diagnostics.append(_diagnostic("evidence.invalid", "evidence"))
        try:
            cases = _parse_cases(captured.content["cases"])
        except (
            UnicodeError,
            json.JSONDecodeError,
            ValidationError,
            CaseStoreError,
            TypeError,
            ValueError,
        ):
            diagnostics.append(_diagnostic("cases.invalid", "cases"))
        try:
            history = _parse_history(captured.content["history"])
        except (UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
            diagnostics.append(_diagnostic("history.invalid", "history"))
        try:
            checkpoints = _parse_checkpoints(captured.content["checkpoints"])
        except (UnicodeError, ValidationError, TypeError, ValueError, yaml.YAMLError):
            diagnostics.append(_diagnostic("checkpoints.invalid", "checkpoints"))

        if evidence is not None:
            diagnostics.extend(_evidence_diagnostics(evidence, legacy_ids))
            if ingestions is not None:
                diagnostics.extend(_github_association_diagnostics(ingestions))
                diagnostics.extend(_mcp_association_diagnostics(ingestions))
        if graph is not None and evidence is not None:
            evidence_by_id = {record.id: record for record in evidence}
            diagnostics.extend(_graph_diagnostics(graph, evidence_by_id))
            if history is not None:
                diagnostics.extend(
                    _history_diagnostics(history, graph, evidence_by_id, cases or ())
                )
            if cases is not None and history is not None:
                diagnostics.extend(_case_diagnostics(cases, graph, evidence_by_id, history))
        if checkpoints is not None and evidence is not None and ingestions is not None:
            diagnostics.extend(
                _checkpoint_diagnostics(checkpoints, evidence, ingestions, legacy_ids)
            )
        return _report(diagnostics, graph)


def validate_canonical_snapshot(content: Mapping[str, bytes | None]) -> ValidationReport:
    """Validate one bounded immutable canonical snapshot without any filesystem reads."""
    try:
        snapshot = dict(content)
        if set(snapshot) != {
            "config",
            "graph",
            "history",
            "cases",
            "evidence",
            "receipts",
            "checkpoints",
        }:
            raise ValueError("invalid canonical snapshot")
        if any(value is not None and type(value) is not bytes for value in snapshot.values()):
            raise ValueError("invalid canonical snapshot")
        sizes = [len(value) for value in snapshot.values() if value is not None]
        if any(size > 8 * 1024 * 1024 for size in sizes) or sum(sizes) > 16 * 1024 * 1024:
            raise ValueError("oversized canonical snapshot")
    except (TypeError, ValueError):
        return _report((_diagnostic("workspace.snapshot_invalid", "workspace"),))
    try:
        if snapshot["config"] is None:
            raise ValueError("missing config")
        config = _parse_config(snapshot["config"])
    except (UnicodeError, TypeError, ValueError, yaml.YAMLError):
        return _report((_diagnostic("config.invalid", "config"),))
    return WorkspaceValidationService._validate_snapshot(
        _CapturedWorkspace(config, MappingProxyType(snapshot), False)
    )


def validate_project(root: Path) -> ValidationReport:
    """Shared fail-closed entry point for CLI validation and workspace doctoring."""
    try:
        return WorkspaceValidationService(root).validate()
    except Exception:  # noqa: BLE001 - public diagnostics never expose local failures
        return _report((_diagnostic("validation.internal_failure", "validation"),))


def validate_project_directory(directory: SecureDirectory) -> ValidationReport:
    """Validate the exact descriptor-held project selected by a long-lived caller."""
    try:
        return WorkspaceValidationService(
            directory.path,
            project_directory=directory,
        ).validate()
    except Exception:  # noqa: BLE001 - public diagnostics never expose local failures
        return _report((_diagnostic("validation.internal_failure", "validation"),))
