"""On-demand code inspection and inert legacy hook entry point."""

# ruff: noqa: B008

from __future__ import annotations

import json
from pathlib import Path

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.code_reconciliation import CodeReconciliationService


def reconcile_code_command(
    project: Path = typer.Option(Path("."), "--project"),
    run: bool = typer.Option(False, "--run"),
    enable: bool = typer.Option(
        False, "--enable", help="Legacy option; automatic review stays off."
    ),
    disable: bool = typer.Option(False, "--disable", help="Cancel the current review."),
) -> None:
    """Inspect status or explicitly run a code review. Automatic reviews are retired."""
    runtime = None
    try:
        if sum((run, enable, disable)) > 1:
            raise ValueError("choose one operation")
        runtime = load_runtime(project.resolve(strict=True))
        service = CodeReconciliationService(runtime)
        result = (
            service.configure(enabled=enable)
            if enable or disable
            else (service.run() if run else service.status())
        )
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter("background code reconciliation unavailable") from error
    finally:
        if runtime is not None:
            runtime.close()


def agent_reconcile_hook_command() -> None:
    """Keep old installed hook bindings silent without dispatching any work."""
    typer.echo("{}")
