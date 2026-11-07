"""CLI adapter for bounded, voluntary graph-enrichment sessions."""

# ruff: noqa: B008

from __future__ import annotations

import sys
import traceback
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

import typer

from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import Runtime, load_runtime
from intent_engineering.intent_workflow.enrichment import (
    EnrichmentQuestion,
    GraphEnrichmentService,
)
from intent_engineering.intent_workflow.enrichment_models import EnrichmentSession


class RefineAction(StrEnum):
    START = "start"
    CURRENT = "current"
    PAUSE = "pause"
    RESUME = "resume"


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _scrub_signal(error: BaseException) -> BaseException:
    old_traceback = error.__traceback__
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    if old_traceback is not None:
        traceback.clear_frames(old_traceback)
    old_traceback = None
    return error


def _question_payload(question: object | None) -> dict[str, object] | None:
    if question is None:
        return None
    if isinstance(question, EnrichmentQuestion):
        return cast(dict[str, object], question.model_dump(mode="json"))
    candidate = cast(Any, question)
    return {
        "gap_id": candidate.gap_id,
        "node_id": candidate.node_id,
        "dimension": candidate.dimension,
        "prompt": candidate.prompt,
        "reason": candidate.reason,
    }


def _session_payload(
    session: EnrichmentSession | object,
    question: EnrichmentQuestion | object | None,
) -> dict[str, object]:
    candidate = cast(Any, session)
    return {
        "session": cast(dict[str, object], candidate.model_dump(mode="json")),
        "question": _question_payload(question),
    }


def _current_question(
    service: GraphEnrichmentService,
    session: EnrichmentSession | object,
) -> EnrichmentQuestion | object | None:
    candidate = cast(Any, session)
    return service.next_question(candidate.id) if candidate.status == "open" else None


def _apply_tty_action(
    service: GraphEnrichmentService,
    session: EnrichmentSession | object,
    question: EnrichmentQuestion | object,
) -> EnrichmentSession | object:
    active_session = cast(Any, session)
    active_question = cast(Any, question)
    raw_answer = ""
    try:
        typer.echo(f"{active_question.prompt}\nWhy this matters: {active_question.reason}")
        raw_answer = typer.prompt(
            "Answer (:skip or :pause)",
            hide_input=True,
            confirmation_prompt=False,
        )
        if raw_answer == ":pause":
            return service.pause(active_session.id)
        if raw_answer == ":skip":
            return service.skip(active_session.id, active_question.gap_id)
        return service.answer(active_session.id, active_question.gap_id, raw_answer)
    finally:
        raw_answer = ""
        question = cast(EnrichmentQuestion, None)
        active_question = None
        active_session = None


def refine_command(
    project: Path = typer.Option(Path("."), "--project"),
    minutes: int | None = typer.Option(None, "--minutes"),
    focus: str | None = typer.Option(None, "--focus"),
    session_id: str | None = typer.Option(None, "--session"),
    action: RefineAction = typer.Option(RefineAction.START, "--action"),
    output_format: OutputFormat = typer.Option(OutputFormat.TEXT, "--format"),
) -> None:
    """Start or resume one question-at-a-time graph improvement session."""
    if minutes is not None and minutes not in {5, 15, 30}:
        raise typer.BadParameter("must be 5, 15, or 30", param_hint="--minutes")
    if action is RefineAction.START:
        if minutes is None and focus is None:
            raise typer.BadParameter(
                "provide --minutes or --focus",
                param_hint="--minutes/--focus",
            )
        if session_id is not None:
            raise typer.BadParameter("cannot be used when starting", param_hint="--session")
    elif session_id is None:
        raise typer.BadParameter("is required for this action", param_hint="--session")
    elif minutes is not None or focus is not None:
        raise typer.BadParameter("scope is only valid when starting", param_hint="--action")

    runtime: Runtime | None = None
    service: GraphEnrichmentService | None = None
    session: EnrichmentSession | object | None = None
    question: EnrichmentQuestion | object | None = None
    payload: dict[str, object] | None = None
    signal: BaseException | None = None
    failed = False
    try:
        runtime = load_runtime(project)
        service = GraphEnrichmentService(runtime, actor=runtime.config.local_actor)
        if action is RefineAction.START:
            bounded_minutes = cast(Literal[5, 15, 30] | None, minutes)
            session = service.start(bounded_minutes, focus)
        elif action is RefineAction.CURRENT:
            session = service.current(cast(str, session_id))
        elif action is RefineAction.PAUSE:
            session = service.pause(cast(str, session_id))
        else:
            session = service.resume(cast(str, session_id))
        question = _current_question(service, session)
        if output_format is OutputFormat.TEXT and _stdin_is_tty() and question is not None:
            session = _apply_tty_action(service, session, question)
            question = _current_question(service, session)
        payload = _session_payload(session, question)
    except Exception as caught:  # noqa: BLE001 - fixed public CLI failure
        _scrub_signal(caught)
        failed = True
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        signal = _scrub_signal(caught)
    finally:
        project = Path()
        focus = None
        session_id = None
        question = None
        session = None
        service = None
        if runtime is not None:
            try:
                runtime.close()
            except Exception as caught:  # noqa: BLE001 - fixed close failure
                _scrub_signal(caught)
                failed = True
                payload = None
            except BaseException as caught:  # noqa: BLE001 - preserve close cancellation
                cleaned = _scrub_signal(caught)
                if signal is None:
                    signal = cleaned
            runtime = None
    if signal is not None:
        detached = signal
        signal = None
        payload = None
        raise detached.with_traceback(None)
    if failed or payload is None:
        payload = None
        typer.echo("intent error: graph enrichment unavailable", err=True)
        raise typer.Exit(1) from None
    try:
        emit(payload, output_format)
    except Exception as caught:  # noqa: BLE001 - fixed public emit failure
        _scrub_signal(caught)
        payload = None
        typer.echo("intent error: graph enrichment unavailable", err=True)
        raise typer.Exit(1) from None
    except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
        detached = _scrub_signal(caught)
        payload = None
        raise detached.with_traceback(None)
    finally:
        payload = None


__all__ = ["RefineAction", "refine_command"]
