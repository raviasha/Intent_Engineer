"""Public CLI for host-scheduled intent/source/PRD monitoring."""

# ruff: noqa: B008

import json
from pathlib import Path, PurePosixPath

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService, MonitorRequest
from intent_engineering.storage.secure import SecureDirectory


def monitor_intent_command(
    project: Path = typer.Option(Path("."), "--project"),
    submission: str | None = typer.Option(None, "--submission"),
    submission_schema: bool = typer.Option(False, "--submission-schema"),
) -> None:
    """Configure, resume or inspect an intent/PRD monitor; the host owns its schedule."""
    runtime = None
    try:
        if submission_schema:
            result = MonitorRequest.model_json_schema()
        else:
            if submission is None:
                draft = MonitorRequest(action="status")
            else:
                path = PurePosixPath(submission)
                if path.is_absolute() or any(p in {"..", ".intent", ".git"} for p in path.parts):
                    raise ValueError("submission must be outside canonical state")
                directory = SecureDirectory.open(project)
                try:
                    raw = directory.read_relative(submission, nonblocking=True, max_bytes=4194304)
                    draft = MonitorRequest.model_validate_json(raw.content)
                finally:
                    directory.close()
            runtime = load_runtime(project)
            result = IntentMonitorService(runtime).run(draft)
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter(
            "intent monitoring unavailable; no completion or delivery is confirmed"
        ) from error
    finally:
        if runtime is not None:
            runtime.close()
