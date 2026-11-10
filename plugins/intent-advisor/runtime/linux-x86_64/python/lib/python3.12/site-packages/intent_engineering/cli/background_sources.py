"""Capture a bounded source read from a user-authorized host connector."""

# ruff: noqa: B008

import json
import os
from pathlib import Path, PurePosixPath

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background_sources import BackgroundSource, capture_source
from intent_engineering.storage.secure import SecureDirectory


def background_source_command(
    project: Path = typer.Option(Path("."), "--project"),
    submission: str | None = typer.Option(None, "--submission"),
    submission_schema: bool = typer.Option(False, "--submission-schema"),
) -> None:
    """Preserve provider provenance from a real read; does not enable a connection itself."""
    runtime = None
    try:
        if submission_schema:
            result = BackgroundSource.model_json_schema()
        else:
            if not submission:
                raise ValueError("source submission required")
            path = PurePosixPath(submission)
            if path.is_absolute() or any(p in {"..", ".intent", ".git"} for p in path.parts):
                raise ValueError("submission must be a relative file outside canonical state")
            directory = SecureDirectory.open(project)
            try:
                raw = directory.read_relative(submission, nonblocking=True, max_bytes=1048576)
                draft = BackgroundSource.model_validate_json(raw.content)
            finally:
                directory.close()
            if not os.path.lexists(project / ".intent"):
                initialize_project(project)
            runtime = load_runtime(project)
            result = capture_source(runtime, draft)
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter("background source capture unavailable") from error
    finally:
        if runtime is not None:
            runtime.close()
