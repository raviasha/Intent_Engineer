"""CLI for the host-operated, resumable initial three-way assessment."""

# ruff: noqa: B008

import json
from pathlib import Path, PurePosixPath

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.initial_reconciliation import InitialReconciliationService
from intent_engineering.intent_workflow.initial_reconciliation_models import (
    InitialReconciliationRequest,
)
from intent_engineering.storage.secure import SecureDirectory


def initial_reconciliation_command(
    project: Path = typer.Option(Path("."), "--project"),
    submission: str | None = typer.Option(None, "--submission"),
    submission_schema: bool = typer.Option(False, "--submission-schema"),
) -> None:
    """Start/checkpoint/finish an assessment, or read its latest state without a submission."""
    runtime = None
    try:
        if submission_schema:
            result = InitialReconciliationRequest.model_json_schema()
        else:
            if submission is None:
                draft = InitialReconciliationRequest(
                    action="status", session_id="cli", turn_id="status"
                )
            else:
                path = PurePosixPath(submission)
                if path.is_absolute() or any(p in {"..", ".intent", ".git"} for p in path.parts):
                    raise ValueError("submission must be outside canonical state")
                directory = SecureDirectory.open(project)
                try:
                    raw = directory.read_relative(submission, nonblocking=True, max_bytes=4194304)
                    draft = InitialReconciliationRequest.model_validate_json(raw.content)
                finally:
                    directory.close()
            runtime = load_runtime(project)
            result = InitialReconciliationService(runtime).run(draft)
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter(
            "initial reconciliation unavailable; no assessment update completed"
        ) from error
    finally:
        if runtime is not None:
            runtime.close()
