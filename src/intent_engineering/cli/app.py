"""Typer entrypoint for the local-first Intent Engineering workflow."""

# ruff: noqa: B008

from __future__ import annotations

import os
from pathlib import Path
from typing import cast

import anyio
import structlog
import typer

from intent_engineering.capture.base import Connector
from intent_engineering.cli.connectors import (
    configured_actor_principals,
    connector_catalog,
    connectors_app,
)
from intent_engineering.cli.github import GitHubDoctorResult, check_github
from intent_engineering.cli.intent_workflow import (
    bootstrap_command,
    proposals_app,
    sources_app,
)
from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import (
    GitHubConfigurationError,
    Runtime,
    github_repository_scope,
    load_runtime,
    new_run_id,
    parse_sources,
    run_selected_sync,
    validate_github_environment,
)
from intent_engineering.cli.writes import policy_actor_aliases, write_app
from intent_engineering.context import ContextProvider
from intent_engineering.core.models import (
    EvidenceRecord,
    Graph,
    ReconciliationCase,
    ReconciliationStatus,
    ResolutionAction,
    is_nonterminal_case_status,
)
from intent_engineering.core.policy import (
    ProjectAlreadyInitialized,
    ProjectNotInitialized,
    evidence_allowed,
    initialize_project,
    refs_allowed,
)
from intent_engineering.reconcile import ResolutionUnavailable
from intent_engineering.render import GraphRenderer, render_drift_report
from intent_engineering.storage.interfaces import GraphStore
from intent_engineering.sync.models import SyncRunResult, SyncRunStatus
from intent_engineering.validation import validate_project

app = typer.Typer(
    name="intent",
    help="Intent Engineering: evidence-backed intent, drift, and reconciliation.",
    no_args_is_help=True,
)
reconcile_app = typer.Typer(help="Inspect and resolve durable reconciliation cases.")
doctor_app = typer.Typer(
    help="Check local workspace and provider health.", invoke_without_command=True
)
app.add_typer(reconcile_app, name="reconcile")
app.add_typer(doctor_app, name="doctor")
app.add_typer(connectors_app, name="connectors")
app.add_typer(write_app, name="write")
app.add_typer(sources_app, name="sources")
app.add_typer(proposals_app, name="proposals")
app.command("bootstrap")(bootstrap_command)


def _configure_logging() -> None:
    """Keep library operational logs off the structured stdout protocol."""
    structlog.configure(
        processors=[structlog.processors.JSONRenderer()],
        logger_factory=structlog.WriteLoggerFactory(__import__("sys").stderr),
    )


def _runtime(project: Path) -> Runtime:
    try:
        return load_runtime(project)
    except ProjectNotInitialized as error:
        raise typer.Exit(code=_runtime_error(error)) from error
    except Exception as error:
        raise typer.Exit(code=_runtime_error(error)) from error


def _runtime_error(error: Exception) -> int:
    """Report one non-sensitive operational failure without filesystem details."""
    if isinstance(error, ProjectNotInitialized):
        message = "local project is not initialized"
    elif isinstance(error, ProjectAlreadyInitialized):
        message = "local workspace already exists"
    else:
        message = "local operation failed"
    typer.echo(f"intent error: {message}", err=True)
    return 1


def _graph(runtime: Runtime) -> Graph:
    """Load canonical graph state while preserving the CLI's redacted error contract."""
    try:
        return runtime.graph_store.load()
    except Exception as error:
        _runtime_error(error)
        raise typer.Exit(1) from error


def _cases(runtime: Runtime) -> tuple[ReconciliationCase, ...]:
    """Read durable cases while preserving the CLI's redacted error contract."""
    try:
        return runtime.cases()
    except (OSError, TypeError, ValueError) as error:
        _runtime_error(error)
        raise typer.Exit(1) from error


def _evidence(runtime: Runtime) -> tuple[EvidenceRecord, ...]:
    """Read persisted evidence while preserving the CLI's redacted error contract."""
    try:
        return runtime.evidence()
    except (OSError, TypeError, ValueError) as error:
        _runtime_error(error)
        raise typer.Exit(1) from error


def _authorized_principals(runtime: Runtime) -> frozenset[str]:
    """Resolve the local actor through fail-closed provider and policy registries."""
    return frozenset(
        {
            runtime.config.local_actor,
            *configured_actor_principals(runtime),
            *policy_actor_aliases(runtime),
        }
    )


def _authorized_evidence(
    runtime: Runtime,
    principals: frozenset[str] | None = None,
) -> tuple[EvidenceRecord, ...]:
    """Project only evidence that the configured local actor may read."""
    selected_principals = _authorized_principals(runtime) if principals is None else principals
    return tuple(
        record for record in _evidence(runtime) if evidence_allowed(record, selected_principals)
    )


def _authorized_cases(
    runtime: Runtime,
    principals: frozenset[str] | None = None,
) -> tuple[ReconciliationCase, ...]:
    """Project only cases whose full evidence packet is locally readable."""
    selected_principals = _authorized_principals(runtime) if principals is None else principals
    records = _evidence(runtime)
    return tuple(
        case
        for case in _cases(runtime)
        if refs_allowed(case.all_evidence_refs, records, selected_principals)
    )


def _authorized_graph(
    runtime: Runtime,
    principals: frozenset[str] | None = None,
) -> Graph:
    """Project canonical state to locally readable topology without mutating it."""
    selected_principals = _authorized_principals(runtime) if principals is None else principals
    graph = _graph(runtime)
    records = _evidence(runtime)
    nodes = tuple(
        node
        for node in graph.nodes
        if refs_allowed(node.evidence_refs, records, selected_principals)
    )
    node_ids = {node.id for node in nodes}
    edges = tuple(
        edge for edge in graph.edges if edge.from_id in node_ids and edge.to_id in node_ids
    )
    return graph.model_copy(update={"nodes": nodes, "edges": edges})


def _invoke_sync(runtime: Runtime, sources: str) -> SyncRunResult:
    """Cross exactly one AnyIO boundary for one CLI sync-like command."""

    async def run() -> SyncRunResult:
        mcp_connectors: tuple[Connector, ...] = ()
        if "mcp" in parse_sources(sources):
            mcp_connectors = connector_catalog(runtime).read_connectors()
        return await run_selected_sync(
            runtime,
            sources,
            new_run_id(),
            mcp_connectors=mcp_connectors,
        )

    return anyio.run(run)


def _exit_for_review(required: bool) -> None:
    if required:
        raise typer.Exit(code=4)


@app.callback()
def default() -> None:
    """Run the Intent Engineering command group."""
    _configure_logging()


@app.command("init")
def init_command(
    project: Path = typer.Option(Path("."), "--project"),
    force: bool = typer.Option(False, "--force"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Initialize the complete local .intent workspace."""
    try:
        initialized = initialize_project(project, force=force)
    except (OSError, ProjectAlreadyInitialized) as error:
        raise typer.Exit(code=_runtime_error(error)) from error
    emit({"project_id": initialized.root.name, "workspace": ".intent"}, output_format)


@app.command("validate")
def validate_command(
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Validate one recovered, consistent snapshot of all canonical local state."""
    report = validate_project(project)
    emit(report, output_format)
    if not report.valid:
        raise typer.Exit(1)


def _sync_command(runtime: Runtime, sources: str, output_format: OutputFormat) -> None:
    try:
        parse_sources(sources)
    except ValueError as error:
        raise typer.BadParameter("invalid source selection", param_hint="--sources") from error
    try:
        result = _invoke_sync(runtime, sources)
    except Exception as error:
        _runtime_error(error)
        raise typer.Exit(1) from error
    emit(result, output_format)
    if result.status is SyncRunStatus.PARTIAL:
        raise typer.Exit(3)
    if result.status is SyncRunStatus.FAILED:
        raise typer.Exit(1)


@app.command("ingest")
def ingest_command(
    project: Path = typer.Option(Path("."), "--project"),
    sources: str = typer.Option("markdown", "--sources"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Capture local evidence through the selected local source connectors."""
    _validate_sources(sources)
    _validate_github_scope(sources)
    _sync_command(_runtime(project), sources, output_format)


@app.command("sync")
def sync_command(
    project: Path = typer.Option(Path("."), "--project"),
    sources: str = typer.Option("markdown,git", "--sources"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Synchronize selected local, GitHub, and MCP evidence through one graph runtime."""
    _validate_sources(sources)
    _validate_github_scope(sources)
    _sync_command(_runtime(project), sources, output_format)


@app.command("mcp")
def mcp_command(
    project: Path = typer.Option(Path("."), "--project"),
) -> None:
    """Serve authorized local Intent context over the protocol-clean MCP stdio transport."""
    from intent_engineering.integrations.mcp_server import run_stdio

    abort: BaseException | None = None
    try:
        run_stdio(project)
        ok = True
    except Exception:  # noqa: BLE001 - retain no runtime/parser/provider failure
        ok = False
    except BaseException as error:  # noqa: BLE001 - preserve detached interrupt/cancellation
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        abort = error
        ok = False
    del project
    if abort is not None:
        raise abort
    if not ok:
        typer.echo("intent error: MCP server failed", err=True)
        raise typer.Exit(1) from None


def _validate_sources(sources: str) -> None:
    """Reject command usage before touching project state or opening runtime stores."""
    try:
        parse_sources(sources)
    except ValueError as error:
        raise typer.BadParameter("invalid source selection", param_hint="--sources") from error


def _validate_github_scope(sources: str) -> None:
    try:
        validate_github_environment(sources, os.environ)
    except GitHubConfigurationError as error:
        raise typer.BadParameter(
            "invalid or missing GitHub repository scope",
            param_hint="GITHUB_REPOSITORY",
        ) from error


def _validate_report_output(output: Path) -> None:
    protected = {".git", ".intent"}
    if output.suffix.casefold() != ".md" or any(
        part.casefold() in protected for part in output.parts
    ):
        raise ValueError("unsafe report target")


@app.command("drift")
def drift_command(
    project: Path = typer.Option(Path("."), "--project"),
    require_review: bool = typer.Option(False, "--require-review"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    """Report open and proposed reconciliation cases requiring attention."""
    if output is not None and output_format is not OutputFormat.MARKDOWN:
        raise typer.BadParameter("--output requires --format markdown", param_hint="--output")
    runtime = _runtime(project)
    principals = _authorized_principals(runtime)
    cases = tuple(
        case
        for case in _authorized_cases(runtime, principals)
        if is_nonterminal_case_status(case.status)
    )
    if output_format is OutputFormat.MARKDOWN:
        report = render_drift_report(cases)
        if output is not None:
            target = None
            try:
                _validate_report_output(output)
                target = runtime.project_directory.file(output)
                target.atomic_write(report.encode("utf-8"), reject_target_races=True)
            except (OSError, TypeError, ValueError) as error:
                _runtime_error(error)
                raise typer.Exit(1) from error
            finally:
                if target is not None:
                    target.close()
        typer.echo(report, nl=False)
        _exit_for_review(require_review)
        return
    emit({"cases": cases, "review_required": bool(cases) or require_review}, output_format)
    _exit_for_review(require_review)


@app.command("status")
def status_command(
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Summarize durable graph, evidence, and reconciliation state."""
    runtime = _runtime(project)
    principals = _authorized_principals(runtime)
    graph = _authorized_graph(runtime, principals)
    cases = _authorized_cases(runtime, principals)
    emit(
        {
            "project_id": runtime.config.project_id,
            "graph_version": graph.version,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "evidence_count": len(_authorized_evidence(runtime, principals)),
            "open_case_count": sum(is_nonterminal_case_status(case.status) for case in cases),
        },
        output_format,
    )


@app.command("explain")
def explain_command(
    reference: str = typer.Argument(..., metavar="ID_OR_PATH"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Explain the local graph node, evidence object, or reconciliation case by reference."""
    runtime = _runtime(project)
    principals = _authorized_principals(runtime)
    graph = _graph(runtime)
    records = _evidence(runtime)
    matching_nodes = tuple(
        node
        for node in graph.nodes
        if node.id == reference and refs_allowed(node.evidence_refs, records, principals)
    )
    matching_cases = tuple(
        case for case in _authorized_cases(runtime, principals) if case.id == reference
    )
    matching_evidence = tuple(
        record
        for record in _authorized_evidence(runtime, principals)
        if reference in {record.id, record.external_object_id, record.source_locator}
    )
    if not (matching_nodes or matching_cases or matching_evidence):
        typer.echo("intent error: local reference was not found", err=True)
        raise typer.Exit(1)
    emit(
        {
            "reference": reference,
            "nodes": matching_nodes,
            "cases": matching_cases,
            "evidence": matching_evidence,
        },
        output_format,
    )


@app.command("context")
def context_command(
    task: str = typer.Option(..., "--task"),
    symbol: str | None = typer.Option(None, "--symbol"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Build a conservative, bounded context pack for a task or exact symbol."""
    runtime = _runtime(project)
    principals = _authorized_principals(runtime)
    provider = ContextProvider(
        _graph(runtime),
        _authorized_cases(runtime, principals),
        runtime.config,
        _authorized_evidence(runtime, principals),
    )
    pack = (
        provider.for_symbol(symbol, actor=principals)
        if symbol is not None
        else provider.for_task(task, actor=principals)
    )
    emit(pack, output_format)


@reconcile_app.command("list")
def reconcile_list_command(
    project: Path = typer.Option(Path("."), "--project"),
    status: ReconciliationStatus | None = typer.Option(None, "--status"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """List durable reconciliation cases in stable ID order."""
    runtime = _runtime(project)
    cases = _authorized_cases(runtime, _authorized_principals(runtime))
    if status is not None:
        cases = tuple(case for case in cases if case.status is status)
    emit({"cases": cases}, output_format)


@reconcile_app.command("show")
def reconcile_show_command(
    case_id: str,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Show one durable reconciliation case and its lifecycle history."""
    runtime = _runtime(project)
    principals = _authorized_principals(runtime)
    try:
        case = runtime.case_store.get(case_id)
        if not refs_allowed(
            case.all_evidence_refs,
            _evidence(runtime),
            principals,
        ):
            raise KeyError(case_id)
    except KeyError as error:
        typer.echo("intent error: reconciliation case was not found", err=True)
        raise typer.Exit(1) from error
    emit({"case": case}, output_format)


@reconcile_app.command("resolve")
def reconcile_resolve_command(
    case_id: str,
    action: ResolutionAction = typer.Option(ResolutionAction.UPDATE_IMPLEMENTATION, "--action"),
    approve: str | None = typer.Option(None, "--approve"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Apply a validated ChangeSet, then persist the audited case transition."""
    runtime = _runtime(project)
    try:
        resolved, changeset, approval = runtime.resolution.resolve(case_id, action, approve=approve)
    except ResolutionUnavailable as error:
        _runtime_error(error)
        raise typer.Exit(1) from error
    if approval is not None:
        emit({"case": resolved, "changeset": changeset, "approval": approval}, output_format)
        raise typer.Exit(4)
    emit({"case": resolved, "changeset": changeset}, output_format)


@app.command("render")
def render_command(
    project: Path = typer.Option(Path("."), "--project"),
    output: Path | None = typer.Option(None, "--output"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Render non-canonical Markdown and Mermaid graph views through the safe renderer."""
    runtime = _runtime(project)
    principals = _authorized_principals(runtime)
    output_dir = output if output is not None else runtime.workspace / "cache" / "render"
    try:

        class _ProjectionStore:
            def load(self) -> Graph:
                return _authorized_graph(runtime, principals)

        markdown, mermaid = GraphRenderer(
            cast(GraphStore, _ProjectionStore()), _authorized_cases(runtime, principals)
        ).render_all(output_dir)
    except Exception as error:
        _runtime_error(error)
        raise typer.Exit(1) from error
    emit({"markdown": str(markdown), "mermaid": str(mermaid)}, output_format)


@doctor_app.callback(invoke_without_command=True)
def doctor_command(
    context: typer.Context,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Check local workspace health through the shared deep validation service."""
    if context.invoked_subcommand is not None:
        return
    report = validate_project(project)
    emit(
        {
            "schema_version": report.schema_version,
            "healthy": report.valid,
            "diagnostics": report.diagnostics,
        },
        output_format,
    )
    if not report.valid:
        raise typer.Exit(1)


@doctor_app.command("github")
def doctor_github_command(
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Check locally authenticated GitHub repository access without exposing credentials."""
    try:
        github_repository_scope(os.environ)
    except GitHubConfigurationError as error:
        raise typer.BadParameter(
            "invalid or missing GitHub repository scope",
            param_hint="GITHUB_REPOSITORY",
        ) from error
    runtime = _runtime(project)

    async def run() -> GitHubDoctorResult:
        return await check_github(runtime, env=os.environ)

    result = anyio.run(run)
    emit(result, output_format)
    if not result.healthy:
        raise typer.Exit(1)


def main() -> None:
    _configure_logging()
    app()


if __name__ == "__main__":
    main()
