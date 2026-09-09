"""ACL-filtered, transaction-consistent read services and MCP tool registration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal, Never, cast

from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS, ToolAnnotations
from pydantic import BaseModel, WithJsonSchema

from intent_engineering.assessment.models import AssessmentHealth, AssessmentReport
from intent_engineering.assessment.service import GraphAssessmentService
from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.connectors import configured_actor_principals
from intent_engineering.cli.runtime import Runtime
from intent_engineering.cli.writes import policy_actor_aliases
from intent_engineering.context import ContextProvider
from intent_engineering.core.models import (
    EvidenceRecord,
    Graph,
    Node,
    ProjectConfig,
    ReconciliationCase,
    ReconciliationStatus,
    is_nonterminal_case_status,
)
from intent_engineering.core.models.schemas import schema_bytes
from intent_engineering.core.policy import evidence_allowed, refs_allowed
from intent_engineering.render import render_drift_report
from intent_engineering.storage.jsonl.case_store import parse_case_versions
from intent_engineering.storage.jsonl.evidence_store import parse_evidence_lines
from intent_engineering.storage.yaml.graph_store import parse_graph
from intent_engineering.validation import validate_project_directory

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_SCHEMA_VERSION = "1"
_MAX_ASSESSMENT_GAPS = 100
_MAX_ASSESSMENT_RESPONSE_BYTES = 32 * 1024 * 1024
_ASSESSMENT_TOOL_NAMES = frozenset(
    {
        "intent_assessment_summary",
        "intent_assessment_scorecard",
        "intent_assessment_gaps",
    }
)
type _IdentifierInput = Annotated[
    object,
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": 512}),
]
type _TaskInput = Annotated[
    object,
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": 4096}),
]
type _FormatInput = Annotated[
    object,
    WithJsonSchema({"type": "string", "enum": ["json", "markdown"]}),
]
type _StatusInput = Annotated[
    object,
    WithJsonSchema(
        {
            "type": "string",
            "enum": [status.value for status in ReconciliationStatus],
        }
    ),
]
type _AssessmentHealthInput = Annotated[
    object,
    WithJsonSchema({"type": "string", "enum": ["green", "orange", "red"]}),
]
type _AssessmentLimitInput = Annotated[
    object,
    WithJsonSchema({"type": "integer", "minimum": 1, "maximum": _MAX_ASSESSMENT_GAPS}),
]


def _valid_identifier(value: str) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and value == value.strip()


def _input_string(
    value: object,
    *,
    maximum: int,
    identifier: bool = False,
    choices: frozenset[str] | None = None,
) -> str | None:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        return None
    if identifier and value != value.strip():
        return None
    if choices is not None and value not in choices:
        return None
    return value


def _invalid_input() -> Never:
    raise MCPError(INVALID_PARAMS, "invalid intent tool arguments") from None


def _scrub_signal(error: BaseException) -> BaseException:
    """Remove retained private material while preserving cancellation object identity."""
    error.args = ()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    error.__dict__.clear()
    return error


def _bounded_assessment_response(payload: dict[str, object]) -> dict[str, object] | None:
    """Return one canonically encodable response within the shared public byte bound."""
    encoded = b""
    bounded: dict[str, object] | None = None
    signal: BaseException | None = None
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) <= _MAX_ASSESSMENT_RESPONSE_BYTES:
            bounded = payload
    except Exception:  # noqa: BLE001 - one fixed response-encoding failure boundary
        bounded = None
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        signal = _scrub_signal(caught)
    finally:
        payload = {}
        encoded = b""
    if signal is not None:
        caught_signal = signal
        signal = None
        bounded = None
        raise caught_signal.with_traceback(None)
    return bounded


@dataclass(frozen=True)
class _ReadSnapshot:
    config: ProjectConfig
    graph: Graph
    evidence: tuple[EvidenceRecord, ...]
    cases: tuple[ReconciliationCase, ...]
    principals: frozenset[str]


def _model(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    return value


def _json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _context_markdown(payload: dict[str, object]) -> str:
    """Return inert Markdown without interpreting the caller's task as markup."""
    return f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n```\n"


class McpReadServices:
    """Build fresh, immutable authorized projections from one held local runtime."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._assessment_service = GraphAssessmentService()

    def _assessment_report(self) -> AssessmentReport | None:
        """Assess one fresh no-recovery snapshot behind a fixed public failure boundary."""
        snapshot = None
        report: AssessmentReport | None = None
        signal: BaseException | None = None
        failed = False
        try:
            snapshot = self.runtime.assessment_snapshot(self.runtime.config.local_actor)
            report = self._assessment_service.assess(snapshot)
        except Exception:  # noqa: BLE001 - expose one fixed assessment-read failure
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            snapshot = None
        if signal is not None:
            caught_signal = signal
            signal = None
            report = None
            del self
            raise caught_signal.with_traceback(None)
        if failed:
            return None
        return report

    def assessment_summary(self) -> dict[str, object] | None:
        """Return the complete deterministic report for one current visible snapshot."""
        report = self._assessment_report()
        if report is None:
            return None
        bounded: dict[str, object] | None = None
        signal: BaseException | None = None
        try:
            bounded = _bounded_assessment_response(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "assessment": report.model_dump(mode="json"),
                    "semantic_digest": report.semantic_digest,
                }
            )
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            report = None
        if signal is not None:
            caught_signal = signal
            signal = None
            bounded = None
            del self
            raise caught_signal.with_traceback(None)
        return bounded

    def assessment_scorecard(self, reference: str) -> dict[str, object] | None:
        """Return visible node and branch scorecards sharing one exact stable reference."""
        report = self._assessment_report()
        if report is None:
            return None
        node = next((item for item in report.nodes if item.node_id == reference), None)
        branch = next((item for item in report.branches if item.branch_id == reference), None)
        if node is None and branch is None:
            return None
        bounded: dict[str, object] | None = None
        signal: BaseException | None = None
        try:
            bounded = _bounded_assessment_response(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "semantic_digest": report.semantic_digest,
                    "reference": reference,
                    "node": None if node is None else node.model_dump(mode="json"),
                    "branch": None if branch is None else branch.model_dump(mode="json"),
                }
            )
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            report = None
            node = None
            branch = None
            reference = ""
        if signal is not None:
            caught_signal = signal
            signal = None
            bounded = None
            del self
            raise caught_signal.with_traceback(None)
        return bounded

    def assessment_gaps(
        self,
        *,
        limit: int = 20,
        health: AssessmentHealth | None = None,
    ) -> dict[str, object] | None:
        """Return a bounded stable prefix of visible rubric gaps."""
        report = self._assessment_report()
        if report is None:
            return None
        gaps = tuple(item for item in report.gaps if health is None or item.severity is health)
        bounded: dict[str, object] | None = None
        signal: BaseException | None = None
        try:
            bounded = _bounded_assessment_response(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "semantic_digest": report.semantic_digest,
                    "health": None if health is None else health.value,
                    "limit": limit,
                    "gaps": [item.model_dump(mode="json") for item in gaps[:limit]],
                }
            )
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            signal = _scrub_signal(caught)
        finally:
            report = None
            gaps = ()
        if signal is not None:
            caught_signal = signal
            signal = None
            bounded = None
            del self
            raise caught_signal.with_traceback(None)
        return bounded

    def _snapshot(self) -> _ReadSnapshot | None:
        try:
            config_read = self.runtime.workspace_directory.read_relative(
                "config.yaml", nonblocking=True
            )
            config = ProjectConfig.model_validate_json(
                json.dumps(load_strict_yaml_mapping_bytes(config_read.content))
            )
            if (
                config.project_id != self.runtime.config.project_id
                or config.graph_path != self.runtime.config.graph_path
            ):
                return None
            captured = self.runtime.transactions.snapshot().content
            graph_content = captured.get("graph")
            if graph_content is None:
                return None
            graph = parse_graph(graph_content)
            records, _, _ = parse_evidence_lines(captured.get("evidence"))
            versions = parse_case_versions(captured.get("cases"))
            latest: dict[str, ReconciliationCase] = {}
            for case in versions:
                latest[case.id] = case
            principals = frozenset(
                {
                    config.local_actor,
                    *configured_actor_principals(self.runtime, actor=config.local_actor),
                    *policy_actor_aliases(self.runtime, actor=config.local_actor),
                }
            )
            authorized_evidence = tuple(
                record for record in records if evidence_allowed(record, principals)
            )
            nodes = tuple(
                node
                for node in graph.nodes
                if refs_allowed(node.evidence_refs, records, principals)
            )
            node_ids = {node.id for node in nodes}
            authorized_cases = tuple(
                sorted(
                    (
                        case
                        for case in latest.values()
                        if is_nonterminal_case_status(case.status)
                        and refs_allowed(case.all_evidence_refs, records, principals)
                        and case.subject_ref in node_ids
                        and all(reference in node_ids for reference in case.affected_refs)
                    ),
                    key=lambda case: case.id,
                )
            )
            edges = tuple(
                edge for edge in graph.edges if edge.from_id in node_ids and edge.to_id in node_ids
            )
            authorized_graph = graph.model_copy(update={"nodes": nodes, "edges": edges})
            return _ReadSnapshot(
                config,
                authorized_graph,
                authorized_evidence,
                authorized_cases,
                principals,
            )
        except Exception:  # noqa: BLE001 - expose only an inert unavailable result
            return None

    def context(
        self,
        task: str,
        *,
        symbol: str | None = None,
        output_format: Literal["json", "markdown"] = "json",
    ) -> dict[str, object] | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        provider = ContextProvider(
            snapshot.graph,
            snapshot.cases,
            snapshot.config,
            snapshot.evidence,
        )
        pack = (
            provider.for_symbol(symbol, actor=snapshot.principals)
            if symbol is not None
            else provider.for_task(task, actor=snapshot.principals)
        )
        payload = cast(dict[str, object], pack.model_dump(mode="json"))
        if output_format == "markdown":
            return {
                "schema_version": _SCHEMA_VERSION,
                "format": "markdown",
                "content": _context_markdown(payload),
            }
        return payload

    def status(self) -> dict[str, object] | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        return {
            "schema_version": _SCHEMA_VERSION,
            "project_id": snapshot.config.project_id,
            "graph_version": snapshot.graph.version,
            "node_count": len(snapshot.graph.nodes),
            "edge_count": len(snapshot.graph.edges),
            "evidence_count": len(snapshot.evidence),
            "open_case_count": sum(
                is_nonterminal_case_status(case.status) for case in snapshot.cases
            ),
        }

    def explain(self, reference: str) -> dict[str, object] | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        nodes = tuple(node for node in snapshot.graph.nodes if node.id == reference)
        cases = tuple(case for case in snapshot.cases if case.id == reference)
        evidence = tuple(
            record
            for record in snapshot.evidence
            if reference in {record.id, record.external_object_id, record.source_locator}
        )
        if not (nodes or cases or evidence):
            return None
        return {
            "schema_version": _SCHEMA_VERSION,
            "reference": reference,
            "nodes": [_model(node) for node in nodes],
            "cases": [_model(case) for case in cases],
            "evidence": [_model(record) for record in evidence],
        }

    def impact(self, reference: str) -> dict[str, object] | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        by_id = {node.id: node for node in snapshot.graph.nodes}
        subject = by_id.get(reference)
        if subject is None:
            return None
        dependencies: list[Node] = []
        dependents: list[Node] = []
        edges = []
        for edge in snapshot.graph.edges:
            if edge.to_id == reference:
                dependencies.append(by_id[edge.from_id])
                edges.append(edge)
            elif edge.from_id == reference:
                dependents.append(by_id[edge.to_id])
                edges.append(edge)
        cases = tuple(
            case for case in snapshot.cases if reference in {case.subject_ref, *case.affected_refs}
        )
        return {
            "schema_version": _SCHEMA_VERSION,
            "reference": reference,
            "subject": _model(subject),
            "dependencies": [_model(node) for node in sorted(dependencies, key=lambda x: x.id)],
            "dependents": [_model(node) for node in sorted(dependents, key=lambda x: x.id)],
            "edges": [_model(edge) for edge in sorted(edges, key=lambda x: x.id)],
            "cases": [_model(case) for case in cases],
        }

    def drift(
        self, output_format: Literal["json", "markdown"] = "json"
    ) -> dict[str, object] | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        cases = tuple(case for case in snapshot.cases if is_nonterminal_case_status(case.status))
        if output_format == "markdown":
            return {
                "schema_version": _SCHEMA_VERSION,
                "format": "markdown",
                "content": render_drift_report(cases),
            }
        return {
            "schema_version": _SCHEMA_VERSION,
            "cases": [_model(case) for case in cases],
            "review_required": bool(cases),
        }

    def reconcile_list(
        self, status: ReconciliationStatus | None = None
    ) -> dict[str, object] | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        cases = tuple(case for case in snapshot.cases if status is None or case.status is status)
        return {
            "schema_version": _SCHEMA_VERSION,
            "cases": [_model(case) for case in cases],
        }

    def reconcile_show(self, case_id: str) -> dict[str, object] | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        case = next((item for item in snapshot.cases if item.id == case_id), None)
        if case is None:
            return None
        evidence = tuple(
            record for record in snapshot.evidence if record.id in case.all_evidence_refs
        )
        return {
            "schema_version": _SCHEMA_VERSION,
            "case": _model(case),
            "evidence": [_model(record) for record in evidence],
        }

    def validation(self) -> dict[str, object] | None:
        try:
            report = validate_project_directory(self.runtime.project_directory)
            return cast(dict[str, object], report.model_dump(mode="json"))
        except Exception:  # noqa: BLE001 - expose only an inert unavailable result
            return None

    def evidence_resource(self, evidence_id: str) -> str | None:
        if not _valid_identifier(evidence_id):
            return None
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        selected = next((item for item in snapshot.evidence if item.id == evidence_id), None)
        if selected is None:
            return None
        versions = tuple(
            item
            for item in snapshot.evidence
            if item.external_object_id == selected.external_object_id
        )
        return _json(
            {
                "schema_version": _SCHEMA_VERSION,
                "evidence": _model(selected),
                "versions": [_model(item) for item in versions],
            }
        )

    def node_resource(self, node_id: str) -> str | None:
        if not _valid_identifier(node_id):
            return None
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        selected = next((item for item in snapshot.graph.nodes if item.id == node_id), None)
        if selected is None:
            return None
        edges = tuple(
            edge for edge in snapshot.graph.edges if node_id in {edge.from_id, edge.to_id}
        )
        return _json(
            {
                "schema_version": _SCHEMA_VERSION,
                "node": _model(selected),
                "edges": [_model(edge) for edge in edges],
            }
        )

    def case_resource(self, case_id: str) -> str | None:
        if not _valid_identifier(case_id):
            return None
        payload = self.reconcile_show(case_id)
        return None if payload is None else _json(payload)

    def schema_resource(self, model_name: str) -> str | None:
        if not _valid_identifier(model_name):
            return None
        try:
            schema = json.loads(schema_bytes(model_name))
        except (ValueError, json.JSONDecodeError):
            return None
        return _json(
            {
                "schema_version": _SCHEMA_VERSION,
                "name": model_name,
                "schema": schema,
            }
        )

    def drift_report_resource(self) -> str | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        cases = tuple(case for case in snapshot.cases if is_nonterminal_case_status(case.status))
        return "<!-- intent-schema-version: 1 -->\n" + render_drift_report(cases)


def _available(payload: dict[str, object] | None) -> dict[str, object]:
    if payload is None:
        raise MCPError(INTERNAL_ERROR, "intent read is unavailable") from None
    return payload


def _found(payload: dict[str, object] | None) -> dict[str, object]:
    if payload is None:
        raise MCPError(INVALID_PARAMS, "intent object was not found") from None
    return payload


def validate_assessment_tool_call(name: str, arguments: dict[str, object]) -> None:
    """Validate exact raw assessment arguments before SDK coercion or handler lookup."""
    from intent_engineering.integrations.mcp_server.intent_workflow import (
        _require_exact_json,
    )

    signal: BaseException | None = None
    try:
        if type(name) is not str or name not in _ASSESSMENT_TOOL_NAMES:
            raise ValueError("invalid assessment request")
        if type(arguments) is not dict:
            raise ValueError("invalid assessment request")
        _require_exact_json(arguments)
        keys = set(dict.keys(arguments))
        if name == "intent_assessment_summary":
            if keys:
                raise ValueError("invalid assessment request")
        elif name == "intent_assessment_scorecard":
            if (
                keys != {"reference"}
                or _input_string(
                    dict.__getitem__(arguments, "reference"),
                    maximum=512,
                    identifier=True,
                )
                is None
            ):
                raise ValueError("invalid assessment request")
        else:
            if not keys.issubset({"limit", "health"}):
                raise ValueError("invalid assessment request")
            if "limit" in keys:
                limit = dict.__getitem__(arguments, "limit")
                if type(limit) is not int or not 1 <= limit <= _MAX_ASSESSMENT_GAPS:
                    raise ValueError("invalid assessment request")
            if "health" in keys:
                health = dict.__getitem__(arguments, "health")
                if (
                    health is not None
                    and _input_string(
                        health,
                        maximum=6,
                        choices=frozenset({"green", "orange", "red"}),
                    )
                    is None
                ):
                    raise ValueError("invalid assessment request")
    except Exception:  # noqa: BLE001 - one fixed raw assessment boundary
        raise ValueError("invalid intent tool arguments") from None
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        signal = _scrub_signal(caught)
    finally:
        name = ""
        arguments = {}
    if signal is not None:
        caught_signal = signal
        signal = None
        raise caught_signal.with_traceback(None)


def register_read_tools(server: MCPServer, services: McpReadServices) -> None:
    """Register the exact version-1 read-only tool surface."""

    @server.tool(name="intent_context", annotations=_READ_ONLY, structured_output=True)
    def intent_context(
        task: _TaskInput,
        format: _FormatInput = "json",
        symbol: _IdentifierInput | None = None,
    ) -> dict[str, object]:
        """Return bounded intent, requirement, constraint, code, test, and drift context."""
        selected_task = _input_string(task, maximum=4096)
        selected_format = _input_string(
            format,
            maximum=8,
            choices=frozenset({"json", "markdown"}),
        )
        selected_symbol = (
            None if symbol is None else _input_string(symbol, maximum=512, identifier=True)
        )
        symbol_was_supplied = symbol is not None
        del task, format, symbol
        if (
            selected_task is None
            or selected_format is None
            or (symbol_was_supplied and selected_symbol is None)
        ):
            del selected_task, selected_format, selected_symbol
            _invalid_input()
        payload = services.context(
            selected_task,
            symbol=selected_symbol,
            output_format=cast(Literal["json", "markdown"], selected_format),
        )
        del selected_task, selected_format, selected_symbol
        return _available(payload)

    @server.tool(name="intent_explain", annotations=_READ_ONLY, structured_output=True)
    def intent_explain(reference: _IdentifierInput) -> dict[str, object]:
        """Explain one authorized graph node, evidence object, or reconciliation case."""
        selected = _input_string(reference, maximum=512, identifier=True)
        del reference
        if selected is None:
            _invalid_input()
        payload = services.explain(selected)
        del selected
        return _found(payload)

    @server.tool(name="intent_impact", annotations=_READ_ONLY, structured_output=True)
    def intent_impact(reference: _IdentifierInput) -> dict[str, object]:
        """Return authorized direct graph impact and reconciliation cases for a node."""
        selected = _input_string(reference, maximum=512, identifier=True)
        del reference
        if selected is None:
            _invalid_input()
        payload = services.impact(selected)
        del selected
        return _found(payload)

    @server.tool(name="intent_drift", annotations=_READ_ONLY, structured_output=True)
    def intent_drift(
        format: _FormatInput = "json",
    ) -> dict[str, object]:
        """Return authorized nonterminal reconciliation cases or a deterministic report."""
        selected = _input_string(
            format,
            maximum=8,
            choices=frozenset({"json", "markdown"}),
        )
        del format
        if selected is None:
            _invalid_input()
        payload = services.drift(cast(Literal["json", "markdown"], selected))
        del selected
        return _available(payload)

    @server.tool(name="intent_status", annotations=_READ_ONLY, structured_output=True)
    def intent_status() -> dict[str, object]:
        """Summarize the authorized durable graph, evidence, and case state."""
        return _available(services.status())

    @server.tool(
        name="intent_assessment_summary",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def intent_assessment_summary() -> dict[str, object]:
        """Return the current explainable assessment for one authorized snapshot."""
        return _available(services.assessment_summary())

    @server.tool(
        name="intent_assessment_scorecard",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def intent_assessment_scorecard(reference: _IdentifierInput) -> dict[str, object]:
        """Return one authorized node and branch assessment by exact reference."""
        selected = _input_string(reference, maximum=512, identifier=True)
        del reference
        if selected is None:
            _invalid_input()
        payload = services.assessment_scorecard(selected)
        del selected
        return _found(payload)

    @server.tool(
        name="intent_assessment_gaps",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def intent_assessment_gaps(
        limit: _AssessmentLimitInput = 20,
        health: _AssessmentHealthInput | None = None,
    ) -> dict[str, object]:
        """Return a bounded stable list of authorized assessment gaps."""
        selected_limit = (
            limit if type(limit) is int and 1 <= limit <= _MAX_ASSESSMENT_GAPS else None
        )
        selected_health = (
            None
            if health is None
            else _input_string(
                health,
                maximum=6,
                choices=frozenset({"green", "orange", "red"}),
            )
        )
        health_was_supplied = health is not None
        del limit, health
        if selected_limit is None or (health_was_supplied and selected_health is None):
            _invalid_input()
        payload = services.assessment_gaps(
            limit=selected_limit,
            health=None if selected_health is None else AssessmentHealth(selected_health),
        )
        del selected_limit, selected_health
        return _available(payload)

    @server.tool(name="intent_validate", annotations=_READ_ONLY, structured_output=True)
    def intent_validate() -> dict[str, object]:
        """Deeply validate the local workspace and return fixed diagnostics."""
        return _available(services.validation())

    @server.tool(name="intent_reconcile_list", annotations=_READ_ONLY, structured_output=True)
    def intent_reconcile_list(
        status: _StatusInput | None = None,
    ) -> dict[str, object]:
        """List authorized durable reconciliation cases in stable order."""
        selected = (
            None
            if status is None
            else _input_string(
                status,
                maximum=32,
                choices=frozenset(item.value for item in ReconciliationStatus),
            )
        )
        status_was_supplied = status is not None
        del status
        if status_was_supplied and selected is None:
            _invalid_input()
        payload = services.reconcile_list(
            None if selected is None else ReconciliationStatus(selected)
        )
        del selected
        return _available(payload)

    @server.tool(name="intent_reconcile_show", annotations=_READ_ONLY, structured_output=True)
    def intent_reconcile_show(case_id: _IdentifierInput) -> dict[str, object]:
        """Return one authorized reconciliation packet and its evidence."""
        selected = _input_string(case_id, maximum=512, identifier=True)
        del case_id
        if selected is None:
            _invalid_input()
        payload = services.reconcile_show(selected)
        del selected
        return _found(payload)
