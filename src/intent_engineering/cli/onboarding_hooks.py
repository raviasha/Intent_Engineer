"""Advisory-only creation routing and non-blocking task completion notifications."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from intent_engineering.integrations.agent_host.advisory import (
    CodexUserPromptSubmitEvent,
    PromptRoute,
    parse_codex_prompt_event,
    repository_matches,
)
from intent_engineering.intent_workflow.onboarding_reminders import complete_turn, offer_on_prompt
from intent_engineering.storage.secure import SecureDirectory


def background_onboarding_route(
    project: Path,
    event: CodexUserPromptSubmitEvent,
    default: PromptRoute,
) -> PromptRoute:
    """Offer optional source-connected setup without gating the user's development task."""
    if event.agent_id is not None or event.agent_type is not None:
        return default
    if os.path.lexists(project / ".intent"):
        from intent_engineering.cli.runtime import load_readiness_runtime

        try:
            runtime = load_readiness_runtime(project)
            if runtime.graph_store.load().nodes:
                return default
        except Exception:  # noqa: BLE001 - relevant reads report state errors, never infer absence
            return default
    normalized = " ".join(event.prompt.casefold().split()).rstrip(".!?")
    create_requested = normalized in {
        "create intent graph",
        "create the intent graph",
        "please create intent graph",
        "please create the intent graph",
    }
    if not create_requested and not offer_on_prompt(project, event.session_id, event.turn_id):
        return default
    return PromptRoute(
        action="offer_background_onboarding",
        mcp_tool=None,
        arguments={},
        message=(
            "No active intent graph is available. Offer once: create it in a separate chat and "
            "connect existing sources such as Notion, Confluence or Slack, or defer for now. "
            "Reuse the user's answer; continue the original task without waiting for setup. "
            "If accepted, run intent background-onboarding --prepare --project .; use the host "
            "create_thread tool with the returned prompt/project, then persist the returned chat ID "
            "using --chat-id. Ask source/account/scope questions and complete connection setup in "
            "that new chat. If deferred, run intent background-onboarding --defer --project .; "
            "remind after three completed turns. Never fabricate a chat ID or connection. "
            + default.message
        )[:2048],
    )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def onboarding_prompt_route(
    project: Path,
    event: CodexUserPromptSubmitEvent,
    route: PromptRoute | None,
) -> PromptRoute | None:
    normalized = " ".join(event.prompt.casefold().split()).rstrip(".!?")
    create_requested = normalized in {
        "create intent graph",
        "create the intent graph",
        "please create intent graph",
        "please create the intent graph",
    }
    if route is None:
        if create_requested and event.agent_id is None and event.agent_type is None:
            return PromptRoute(
                action="continue",
                message=(
                    "An approved intent graph already exists. Reuse it; "
                    "open intent dev --project . to view or improve it."
                ),
                mcp_tool=None,
                arguments={},
            )
        return None
    if route.action != "offer_onboarding":
        return route
    if event.agent_id is not None or event.agent_type is not None:
        return PromptRoute(
            action="continue",
            message="The parent task manages intent onboarding.",
            mcp_tool=None,
            arguments={},
        )
    # Match standalone intent only; quoted mentions and negated requests are ordinary prompts.
    if create_requested:
        return PromptRoute(
            action="start_onboarding",
            message=(
                "The user requested intent graph creation. Do not ask again whether to start. "
                "Run intent onboard --discover --project . --format json and follow the "
                "intent-advisor skill to prepare the PRD and graph for one local review."
            ),
            mcp_tool=None,
            arguments={},
        )
    if offer_on_prompt(project, event.session_id, event.turn_id):
        return route
    return PromptRoute(
        action="continue",
        message=(
            "Intent graph creation is deferred for this repository. "
            "Continue the user's task without another onboarding offer. "
            "An explicit request to create the intent graph can start it now."
        ),
        mcp_tool=None,
        arguments={},
    )


def agent_stop_hook_command() -> None:
    """Acknowledge one completion. Never request continuation, block or expose hook content."""
    directory = None
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized event")
        value = json.loads(raw, object_pairs_hook=_object)
        if type(value) is not dict or "prompt" in value:
            raise ValueError("invalid stop event")
        active = value.pop("stop_hook_active")
        message = value.pop("last_assistant_message")
        if (
            value.get("hook_event_name") != "Stop"
            or type(active) is not bool
            or (message is not None and type(message) is not str)
        ):
            raise ValueError("invalid stop event")
        value.update(hook_event_name="UserPromptSubmit", prompt="completion")
        event = parse_codex_prompt_event(value)
        directory = SecureDirectory.open(Path.cwd())
        if (
            not repository_matches(directory, event.cwd)
            or active
            or event.agent_id is not None
            or event.agent_type is not None
        ):
            return
        complete_turn(Path.cwd(), event.session_id, event.turn_id)
    except Exception:  # noqa: BLE001, S110 - reminder failures must never block or leak content
        pass
    finally:
        if directory is not None:
            directory.close()
        typer.echo("{}")
