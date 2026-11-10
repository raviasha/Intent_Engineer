"""Bounded JSON bridges for host-reported source refresh operations."""

# ruff: noqa: B008

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.source_refresh import (
    RefreshRequest,
    SourceRefreshService,
    SourceSelection,
    SourceSignal,
)
from intent_engineering.storage.secure import SecureDirectory


def _run(
    model: type[SourceSelection] | type[SourceSignal] | type[RefreshRequest],
    project: Path,
    submission: str | None,
    submission_schema: bool,
) -> None:
    runtime = None
    try:
        result: dict[str, Any]
        if submission_schema:
            result = model.model_json_schema()
        else:
            draft = None
            if submission is not None:
                path = PurePosixPath(submission)
                if path.is_absolute() or any(p in {"..", ".intent", ".git"} for p in path.parts):
                    raise ValueError("submission must be a relative file outside canonical state")
                directory = SecureDirectory.open(project)
                try:
                    raw = directory.read_relative(submission, nonblocking=True, max_bytes=1048576)
                    draft = model.model_validate_json(raw.content)
                finally:
                    directory.close()
            elif model is not SourceSelection:
                raise ValueError("submission required")
            if not os.path.lexists(project / ".intent"):
                if draft is None:
                    typer.echo(
                        json.dumps(
                            {
                                "sources": [],
                                "subscriptions_configured": False,
                                "capture_method": "host_reported",
                            }
                        )
                    )
                    return
                initialize_project(project)
            runtime = load_runtime(project)
            service = SourceRefreshService(runtime)
            if isinstance(draft, SourceSignal):
                result = service.signal(draft)
            elif isinstance(draft, RefreshRequest):
                result = service.refresh(draft)
            else:
                result = service.sources(draft)
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter("source refresh unavailable") from error
    finally:
        if runtime is not None:
            runtime.close()


def background_sources_command(
    project: Path = typer.Option(Path("."), "--project"),
    submission: str | None = typer.Option(None, "--submission"),
    submission_schema: bool = typer.Option(False, "--submission-schema"),
) -> None:
    """List source scopes or record an explicit user source selection."""
    _run(SourceSelection, project, submission, submission_schema)


def background_refresh_command(
    project: Path = typer.Option(Path("."), "--project"),
    submission: str | None = typer.Option(None, "--submission"),
    submission_schema: bool = typer.Option(False, "--submission-schema"),
) -> None:
    """Plan task-bound reads and validate host observations and interpretation receipts."""
    _run(RefreshRequest, project, submission, submission_schema)


def background_source_signal_command(
    project: Path = typer.Option(Path("."), "--project"),
    submission: str | None = typer.Option(None, "--submission"),
    submission_schema: bool = typer.Option(False, "--submission-schema"),
) -> None:
    """Invalidate one selected source; does not create a provider subscription."""
    _run(SourceSignal, project, submission, submission_schema)
