"""Typer entrypoint for the local-first Intent Engineering workflow."""

# ruff: noqa: B008

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import anyio
import structlog
import typer

from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import Runtime, load_runtime, new_run_id, resolve_connectors
from intent_engineering.core.models import (
    ChangeSet,
    ReconciliationStatus,
    ResolutionAction,
    is_nonterminal_case_status,
)
from intent_engineering.core.policy import (
    ProjectAlreadyInitialized,
    ProjectNotInitialized,
    initialize_project,
)
from intent_engineering.reconcile import transition_case
from intent_engineering.render import GraphRenderer
from intent_engineering.sync.models import SyncRunResult, SyncRunStatus

app = typer.Typer(
    name="intent",
    help="Intent Engineering: evidence-backed intent, drift, and reconciliation.",
    no_args_is_help=True,
)
reconcile_app = typer.Typer(help="Inspect and resolve durable reconciliation cases.")
app.add_typer(reconcile_app, name="reconcile")


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
    except (OSError, TypeError, ValueError) as error:
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


def _invoke_sync(runtime: Runtime, sources: str) -> SyncRunResult:
    """Cross exactly one AnyIO boundary for one CLI sync-like command."""
    return anyio.run(runtime.sync.run, new_run_id(), resolve_connectors(runtime, sources))


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
    """Validate the canonical graph and local configuration."""
    runtime = _runtime(project)
    try:
        graph = runtime.graph_store.load()
    except (OSError, ValueError) as error:
        _runtime_error(error)
        raise typer.Exit(1) from error
    emit({"valid": True, "graph_id": graph.id, "graph_version": graph.version}, output_format)


def _sync_command(runtime: Runtime, sources: str, output_format: OutputFormat) -> None:
    try:
        result = _invoke_sync(runtime, sources)
    except (OSError, ValueError) as error:
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
    _sync_command(_runtime(project), sources, output_format)


@app.command("sync")
def sync_command(
    project: Path = typer.Option(Path("."), "--project"),
    sources: str = typer.Option("markdown,git", "--sources"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Synchronize Markdown and Git evidence into the local graph runtime."""
    _sync_command(_runtime(project), sources, output_format)


@app.command("drift")
def drift_command(
    project: Path = typer.Option(Path("."), "--project"),
    require_review: bool = typer.Option(False, "--require-review"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Report open and proposed reconciliation cases requiring attention."""
    cases = tuple(
        case for case in _runtime(project).cases() if is_nonterminal_case_status(case.status)
    )
    emit({"cases": cases, "review_required": bool(cases) or require_review}, output_format)
    _exit_for_review(bool(cases) or require_review)


@app.command("status")
def status_command(
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Summarize durable graph, evidence, and reconciliation state."""
    runtime = _runtime(project)
    graph = runtime.graph_store.load()
    cases = runtime.cases()
    emit(
        {
            "project_id": runtime.config.project_id,
            "graph_version": graph.version,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "evidence_count": len(runtime.evidence()),
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
    graph = runtime.graph_store.load()
    matching_nodes = tuple(node for node in graph.nodes if node.id == reference)
    matching_cases = tuple(case for case in runtime.cases() if case.id == reference)
    matching_evidence = tuple(
        record
        for record in runtime.evidence()
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
    provider = _runtime(project).context()
    pack = provider.for_symbol(symbol) if symbol is not None else provider.for_task(task)
    emit(pack, output_format)


@reconcile_app.command("list")
def reconcile_list_command(
    project: Path = typer.Option(Path("."), "--project"),
    status: ReconciliationStatus | None = typer.Option(None, "--status"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """List durable reconciliation cases in stable ID order."""
    emit({"cases": _runtime(project).case_store.list(status)}, output_format)


@reconcile_app.command("show")
def reconcile_show_command(
    case_id: str,
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Show one durable reconciliation case and its lifecycle history."""
    runtime = _runtime(project)
    try:
        case = runtime.case_store.get(case_id)
    except KeyError as error:
        typer.echo("intent error: reconciliation case was not found", err=True)
        raise typer.Exit(1) from error
    emit({"case": case}, output_format)


def _resolution_changeset(runtime: Runtime, case_id: str, action: ResolutionAction) -> ChangeSet:
    graph = runtime.graph_store.load()
    case = runtime.case_store.get(case_id)
    material = f"{case.id}\x00{graph.version}\x00{action.value}\x00" + "\x00".join(
        case.all_evidence_refs
    )
    return ChangeSet(
        id=f"changeset:resolve:{sha256(material.encode('utf-8')).hexdigest()}",
        actor=runtime.config.local_actor,
        timestamp=datetime.now(UTC),
        baseline_graph_version=graph.version,
        evidence_refs=case.all_evidence_refs,
        nodes_added=(),
        nodes_updated=(),
        nodes_superseded=(),
        edges_added=(),
        edges_updated=(),
        edges_superseded=(),
        confidence_changes=(),
        implementation_status_changes=(),
        reconciliation_cases_created=(),
        reconciliation_cases_resolved=(case.id,),
        validation_status="validated",
    )


@reconcile_app.command("resolve")
def reconcile_resolve_command(
    case_id: str,
    action: ResolutionAction = typer.Option(ResolutionAction.UPDATE_IMPLEMENTATION, "--action"),
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Apply a validated ChangeSet, then persist the audited case transition."""
    runtime = _runtime(project)
    try:
        case = runtime.case_store.get(case_id)
        changeset = _resolution_changeset(runtime, case_id, action)
        runtime.graph_store.apply(changeset)
        resolved = transition_case(
            case,
            ReconciliationStatus.RESOLVED,
            runtime.config.local_actor,
            changeset.timestamp,
            action,
            changeset.id,
        )
        runtime.case_store.put(resolved)
    except (KeyError, ValueError) as error:
        _runtime_error(error)
        raise typer.Exit(1) from error
    emit({"case": resolved, "changeset": changeset}, output_format)


@app.command("render")
def render_command(
    project: Path = typer.Option(Path("."), "--project"),
    output: Path | None = typer.Option(None, "--output"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Render non-canonical Markdown and Mermaid graph views through the safe renderer."""
    runtime = _runtime(project)
    output_dir = output if output is not None else runtime.workspace / "cache" / "render"
    try:
        markdown, mermaid = GraphRenderer(runtime.graph_store, runtime.cases()).render_all(
            output_dir
        )
    except (OSError, ValueError) as error:
        _runtime_error(error)
        raise typer.Exit(1) from error
    emit({"markdown": str(markdown), "mermaid": str(mermaid)}, output_format)


@app.command("doctor")
def doctor_command(
    project: Path = typer.Option(Path("."), "--project"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Check local workspace structure and canonical graph readability."""
    runtime = _runtime(project)
    expected = (
        "config.yaml",
        "graph.yaml",
        "evidence",
        "reconciliation",
        "history",
        "approvals",
        "cache",
    )
    missing = tuple(name for name in expected if not (runtime.workspace / name).exists())
    try:
        runtime.graph_store.load()
    except (OSError, ValueError) as error:
        _runtime_error(error)
        raise typer.Exit(1) from error
    emit({"healthy": not missing, "missing": missing}, output_format)
    if missing:
        raise typer.Exit(1)


def main() -> None:
    _configure_logging()
    app()


if __name__ == "__main__":
    main()
