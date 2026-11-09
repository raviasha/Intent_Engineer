"""Prepare an explicit host-chat handoff for optional source-connected onboarding."""

# ruff: noqa: B008

import json
from pathlib import Path

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.initial_reconciliation import InitialReconciliationService
from intent_engineering.intent_workflow.initial_reconciliation_models import (
    InitialReconciliationRequest,
)
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
        assessment = None
        if finished:
            runtime = load_runtime(project)
            try:
                if not runtime.graph_store.load().nodes:
                    raise ValueError("graph capture is not complete")
                assessment = InitialReconciliationService(runtime).run(
                    InitialReconciliationRequest(
                        action="status",
                        session_id="onboarding",
                        turn_id="finish-check",
                    )
                )
                if assessment["status"] not in {"ready", "ready_with_findings"}:
                    raise ValueError("initial reconciliation is not complete and current")
            finally:
                runtime.close()
        result = onboarding_chat_state(
            project, prepare=prepare, sources=tuple(source), chat_id=chat_id, finished=finished
        )
        if assessment is not None:
            result["reconciliation"] = assessment
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
                        "here. No routine dashboard, WebAuthn graph approval or report/export. "
                        "Read the intent-advisor initial-reconciliation reference. Compare selected "
                        "source intent, PRD requirements and current code with source intent highest. "
                        "Use intent_initial_reconciliation / intent initial-reconciliation to start "
                        "with explicit relevant files, checkpoint all five comparison directions "
                        "and every PRD snapshot against captured requirements, "
                        "and finish. Flag missing, partial, contradictory and unsupported coverage "
                        "with evidence and recommended changes; never modify assessed code or sources. "
                        "Test-to-code tracing remains deferred. Take the time needed for quality, "
                        "checkpoint/resume without blocking the original work, and revalidate changed "
                        "inputs. Present consolidated issues only after graph assessment is ready. "
                        "Only after a current ready assessment, record intent background-onboarding "
                        "--project . --finished and briefly summarize completion in this chat. If setup "
                        "is deferred or cannot proceed, use --defer so the parent can remind after three "
                        "completed turns. Do not send messages to colleagues or alter source documents."
                    ),
                }
            )
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter("background onboarding unavailable") from error
