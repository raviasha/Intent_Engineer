"""CLI fallback for conversational intent, including repositories without a workspace."""

# ruff: noqa: B008

import json
import os
from pathlib import Path, PurePosixPath

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_models import BackgroundSubmission
from intent_engineering.storage.secure import SecureDirectory


def background_command(
    project: Path = typer.Option(Path("."), "--project"),
    task: str | None = typer.Option(None, "--task"),
    session_id: str | None = typer.Option(None, "--session-id"),
    submission: str | None = typer.Option(None, "--submission"),
    submission_schema: bool = typer.Option(False, "--submission-schema"),
) -> None:
    """Read relevant context or maintain clear intent from a bounded conversation draft."""
    runtime = None
    try:
        if submission_schema:
            result = BackgroundSubmission.model_json_schema()
        elif submission is not None:
            path = PurePosixPath(submission)
            if path.is_absolute() or any(p in {"..", ".intent", ".git"} for p in path.parts):
                raise ValueError("submission must be a relative file outside canonical state")
            directory = SecureDirectory.open(project)
            try:
                raw = directory.read_relative(submission, nonblocking=True, max_bytes=1048576)
                draft = BackgroundSubmission.model_validate_json(raw.content)
            finally:
                directory.close()
            if not os.path.lexists(project / ".intent"):
                if draft.classification == "unrelated":
                    typer.echo(
                        json.dumps(
                            {
                                "status": "continue",
                                "question": None,
                                "graph_version": 0,
                                "human_approval": False,
                            }
                        )
                    )
                    return
                initialize_project(project)
            runtime = load_runtime(project)
            result = BackgroundIntentService(runtime).submit(draft)
        else:
            if not task or not session_id:
                raise ValueError("context requires task and session ID")
            if not os.path.lexists(project / ".intent"):
                result = {
                    "graph_version": 0,
                    "context": {},
                    "questions": [],
                    "answers": [],
                    "disagreements": [],
                    "pending_proposal_ids": [],
                    "scope": "intent_requirements",
                    "human_approval": False,
                }
            else:
                runtime = load_runtime(project)
                result = BackgroundIntentService(runtime).context(task=task, session_id=session_id)
        typer.echo(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        # No source text or private evidence from validation exceptions reaches the terminal.
        raise typer.BadParameter(
            "background intent unavailable; no intent update completed"
        ) from error
    finally:
        if runtime is not None:
            runtime.close()
