"""Deep validation over one descriptor-rooted, recovered local state snapshot."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from pydantic import ConfigDict, Field, ValidationError

from intent_engineering.core.graph.applier import apply_changeset_with_case_effects
from intent_engineering.core.models import (
    ChangeSet,
    EvidenceRecord,
    Graph,
    ProjectConfig,
    ReconciliationCase,
    ReconciliationStatus,
    SyncCheckpoint,
)
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.jsonl.case_store import (
    CaseStoreError,
    parse_case_versions,
)
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
    unique = {
        (item.code, item.scope, item.severity.value): item
        for item in diagnostics
    }
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    loaded: dict[str, object] = {}
    for key, value in pairs:
        if key in loaded:
            raise ValueError("duplicate JSON key")
        loaded[key] = value
    return loaded


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
        values.append(
            json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    ValueError("invalid JSON constant")
                ),
            )
        )
    return tuple(values)


def _parse_config(content: bytes) -> ProjectConfig:
    loaded = _load_yaml(content)
    if not isinstance(loaded, dict):
        raise TypeError("configuration must be a mapping")
    return ProjectConfig.model_validate(cast(dict[str, Any], loaded))


def _parse_graph(content: bytes | None) -> Graph:
    if content is None:
        raise ValueError("graph is missing")
    _load_yaml(content)
    return parse_graph(content)


def _parse_evidence(content: bytes | None) -> tuple[EvidenceRecord, ...]:
    return tuple(
        EvidenceRecord.model_validate(value)
        for value in _load_json_lines(content, allow_blank=True)
    )


def _parse_history(content: bytes | None) -> tuple[ChangeSet, ...]:
    return tuple(
        ChangeSet.model_validate(value)
        for value in _load_json_lines(content, allow_blank=True)
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


def _expected_evidence_id(record: EvidenceRecord) -> str:
    material = (
        f"{record.connector_type}\x00{record.external_object_id}\x00"
        f"{record.external_version}\x00{record.content_hash}"
    )
    return f"evidence:sha256:{sha256(material.encode('utf-8')).hexdigest()}"


def _evidence_diagnostics(records: Sequence[EvidenceRecord]) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    by_id: dict[str, EvidenceRecord] = {}
    by_version: dict[tuple[str, str, str], EvidenceRecord] = {}
    positions: dict[str, int] = {}
    for position, record in enumerate(records):
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
        if record.connector_type == "markdown":
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, str) or record.content_hash != (
                f"sha256:{sha256(content.encode('utf-8')).hexdigest()}"
            ):
                diagnostics.append(_diagnostic("evidence.content_hash_mismatch", "evidence"))
        elif record.connector_type == "git":
            encoded = json.dumps(
                payload,
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
        case_id
        for changeset in history
        for case_id in changeset.reconciliation_cases_created
    }
    resolved_by_changeset = {
        (case_id, changeset.id)
        for changeset in history
        for case_id in changeset.reconciliation_cases_resolved
    }
    for case_id, versions in versions_by_id.items():
        if case_id not in created_by_changeset:
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
        reference not in evidence_by_id
        for node in graph.nodes
        for reference in node.evidence_refs
    ):
        return [_diagnostic("graph.evidence_ref_missing", "graph")]
    return []


def _parse_markdown_cursor(cursor: str) -> dict[str, str]:
    if not cursor.startswith(_MARKDOWN_CURSOR_PREFIX):
        raise ValueError("not a manifest cursor")
    loaded = json.loads(
        cursor.removeprefix(_MARKDOWN_CURSOR_PREFIX),
        object_pairs_hook=_reject_duplicate_json_keys,
    )
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


def _checkpoint_diagnostics(
    checkpoints: Mapping[str, SyncCheckpoint],
    evidence: Sequence[EvidenceRecord],
) -> list[ValidationDiagnostic]:
    diagnostics: list[ValidationDiagnostic] = []
    identities = {
        (record.connector_type, record.external_object_id, record.external_version)
        for record in evidence
    }
    for connector_id in sorted(checkpoints):
        checkpoint = checkpoints[connector_id]
        if connector_id not in {"markdown", "git"}:
            diagnostics.append(_diagnostic("checkpoint.connector_unknown", "checkpoints"))
            continue
        cursor = checkpoint.cursor
        if cursor is None:
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

    def __init__(self, root: Path) -> None:
        self._root = root

    def _capture(self) -> _CapturedWorkspace:
        project_directory: SecureDirectory | None = None
        workspace_directory: SecureDirectory | None = None
        files: list[SecureFile] = []
        try:
            try:
                project_directory = SecureDirectory.open(self._root)
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
                    },
                )
                snapshot = transactions.snapshot(
                    {
                        "config": config_file,
                        "evidence": state_files["evidence"],
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
        cases: tuple[ReconciliationCase, ...] | None = None
        history: tuple[ChangeSet, ...] | None = None
        checkpoints: dict[str, SyncCheckpoint] | None = None
        try:
            graph = _parse_graph(captured.content["graph"])
        except (OSError, UnicodeError, TypeError, ValueError, yaml.YAMLError):
            diagnostics.append(_diagnostic("graph.invalid", "graph"))
        try:
            evidence = _parse_evidence(captured.content["evidence"])
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
            diagnostics.extend(_evidence_diagnostics(evidence))
        if graph is not None and evidence is not None:
            evidence_by_id = {record.id: record for record in evidence}
            diagnostics.extend(_graph_diagnostics(graph, evidence_by_id))
            if history is not None:
                diagnostics.extend(_history_diagnostics(history, graph, evidence_by_id, cases or ()))
            if cases is not None and history is not None:
                diagnostics.extend(_case_diagnostics(cases, graph, evidence_by_id, history))
        if checkpoints is not None and evidence is not None:
            diagnostics.extend(_checkpoint_diagnostics(checkpoints, evidence))
        return _report(diagnostics, graph)


def validate_project(root: Path) -> ValidationReport:
    """Shared fail-closed entry point for CLI validation and workspace doctoring."""
    try:
        return WorkspaceValidationService(root).validate()
    except Exception:  # noqa: BLE001 - public diagnostics never expose local failures
        return _report((_diagnostic("validation.internal_failure", "validation"),))
