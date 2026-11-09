"""Prepare an explicit host-chat handoff for optional source-connected onboarding."""

# ruff: noqa: B008

import json
from pathlib import Path

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.onboarding_reminders import (
    defer_onboarding,
    onboarding_chat_state,
)


def background_onboarding_command(
    project: Path = typer.Option(Path("."), "--project"),
    prepare: bool = typer.Option(False, "--prepare"),
    source: list[str] = typer.Option([], "--source"),
    chat_id: str | None = typer.Option(None, "--chat-id"),
    defer: bool = typer.Option(False, "--defer"),
    after_tasks: int = typer.Option(3, "--after-tasks", min=1, max=100),
    finished: bool = typer.Option(False, "--finished"),
) -> None:
    """Offer/prepare source setup in a separate chat, or remember a completed-turn deferral."""
    try:
        if sum((prepare, chat_id is not None, defer, finished)) > 1:
            raise ValueError("choose one onboarding operation")
        project = project.resolve(strict=True)
        if defer:
            defer_onboarding(project, after_tasks=after_tasks)
        if finished:
            runtime = load_runtime(project)
            try:
                if not runtime.graph_store.load().nodes:
                    raise ValueError("graph capture is not complete")
            finally:
                runtime.close()
        result = onboarding_chat_state(
            project, prepare=prepare, sources=tuple(source), chat_id=chat_id, finished=finished
        )
        if prepare:
            result.update(
                {
                    "title": f"Intent setup — {project.name}",
                    "project": str(project),
                    "chat_created": False,
                    "parent_action": "continue the original task",
                    "prompt": (
                        f"Create or enrich the intent graph for the repository at {project}. "
                        "The user accepted setup in this separate chat; the original development "
                        "chat continues independently. Ask which source connections and bounded "
                        f"spaces/pages/channels to use; initial choices: {', '.join(source) or 'not yet selected'}. "
                        "Offer Notion, Confluence, Slack, Google Drive, GitHub and local docs. Reuse "
                        "available connections; use host plugin discovery/connection tools for missing "
                        "sources and let the user complete provider authentication here. Never claim "
                        "a connection is enabled until a real read succeeds. Capture authorized source "
                        "content with provider, object ID, revision, author, timestamp and locator "
                        "through intent background-source or configured intent connectors. Reuse "
                        "pending proposal evidence but never approve/activate old proposals. Build clear "
                        "intent and requirements using intent_background_submit / intent background; "
                        "retain agent inferences and disagreements. Ask only consequential questions "
                        "here. No routine dashboard, WebAuthn graph approval, report/export or code/test "
                        "matching. When a usable graph exists, record intent background-onboarding "
                        "--project . --finished and briefly summarize completion in this chat. If setup "
                        "is deferred or cannot proceed, use --defer so the parent can remind after three "
                        "completed turns. Do not send messages to colleagues or alter source documents."
                    ),
                }
            )
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter("background onboarding unavailable") from error
