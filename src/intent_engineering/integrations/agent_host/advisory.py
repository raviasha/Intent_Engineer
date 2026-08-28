"""Read-only, token-free routing for advisory human-prompt hooks."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal, cast

from pydantic import (
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from intent_engineering.cli.intent_workflow import _snapshot_config
from intent_engineering.cli.runtime import Runtime
from intent_engineering.intent_workflow.models import ClarificationSession
from intent_engineering.intent_workflow.onboarding import (
    OnboardingRuntime,
    OnboardingState,
    inspect_onboarding,
)
from intent_engineering.storage.secure import SecureDirectory

from .base import _HostModel

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROMPT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_ID_BYTES = 2 * 1024
_MAX_PROMPT_BYTES = 16 * 1024
_MAX_REPOSITORY_BYTES = 4 * 1024
_MAX_EVENT_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 128
_MAX_JSON_NODES = 65_536
_OFFER = (
    "This repository has not been onboarded into Intent Engineering. Start guided onboarding now?"
)
CODEX_ADVISORY_FALLBACK = (
    "Intent advisory prompt routing is unavailable. Do not mutate the intent graph, "
    "infer authorization, or treat this advisory as enforcement."
)


class AdvisoryPromptError(ValueError):
    """One fixed, context-free advisory routing failure."""

    def __init__(self) -> None:
        super().__init__("intent advisory prompt unavailable")


def _identity(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_ID_BYTES
    ):
        raise ValueError("invalid advisory prompt event")
    return value


def _prompt(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _PROMPT_CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_PROMPT_BYTES
    ):
        raise ValueError("invalid advisory prompt event")
    return value


def _repository(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or _CONTROL.search(value)
        or len(value.encode("utf-8")) > _MAX_REPOSITORY_BYTES
    ):
        raise ValueError("invalid advisory prompt event")
    path = Path(value)
    if not path.is_absolute() or Path(os.path.abspath(path)) != path:
        raise ValueError("invalid advisory prompt event")
    return value


def _timestamp_input(value: object, info: ValidationInfo) -> object:
    if info.mode != "json":
        return value
    if type(value) is not str:
        raise ValueError("invalid advisory prompt event")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid advisory prompt event") from None
    canonical = parsed.isoformat()
    if canonical.endswith("+00:00"):
        canonical = canonical[:-6] + "Z"
    if value != canonical:
        raise ValueError("advisory timestamp must use canonical UTC Z form")
    return parsed


def _utc(value: datetime) -> datetime:
    offset = value.utcoffset() if type(value) is datetime and value.tzinfo is not None else None
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("advisory timestamp must use UTC")
    return value.astimezone(UTC)


def _require_exact_json(value: object) -> None:
    pending: list[tuple[object, int, bool]] = [(value, 0, False)]
    active: set[int] = set()
    seen: set[int] = set()
    node_count = 0
    utf8_bytes = 0
    item: object = None
    nested: object = None
    key: object = None
    try:
        while pending:
            item, depth, leaving = pending.pop()
            if leaving:
                active.remove(id(item))
                item = None
                continue
            node_count += 1
            if depth > _MAX_JSON_DEPTH or node_count > _MAX_JSON_NODES:
                raise ValueError("invalid advisory prompt event")
            if type(item) is dict:
                identity = id(item)
                if identity in active or identity in seen:
                    raise ValueError("invalid advisory prompt event")
                active.add(identity)
                seen.add(identity)
                pending.append((item, depth, True))
                for key, nested in dict.items(cast(dict[object, object], item)):
                    if type(key) is not str:
                        raise ValueError("invalid advisory prompt event")
                    node_count += 1
                    utf8_bytes += len(key.encode("utf-8"))
                    if node_count > _MAX_JSON_NODES or utf8_bytes > _MAX_EVENT_BYTES:
                        raise ValueError("invalid advisory prompt event")
                    pending.append((nested, depth + 1, False))
            elif type(item) is list:
                identity = id(item)
                if identity in active or identity in seen:
                    raise ValueError("invalid advisory prompt event")
                active.add(identity)
                seen.add(identity)
                pending.append((item, depth, True))
                pending.extend(
                    (nested, depth + 1, False) for nested in list.__iter__(cast(list[object], item))
                )
            elif type(item) is str:
                utf8_bytes += len(item.encode("utf-8"))
                if utf8_bytes > _MAX_EVENT_BYTES:
                    raise ValueError("invalid advisory prompt event")
            elif item is not None and type(item) not in {bool, int, float}:
                raise ValueError("invalid advisory prompt event")
    finally:
        value = item = nested = key = None
        pending.clear()
        active.clear()
        seen.clear()


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in dict.items(cast(dict[str, object], value))}
        )
    if type(value) is list:
        return tuple(_freeze_json(item) for item in list.__iter__(cast(list[object], value)))
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


class PromptEvent(_HostModel):
    """One exact human prompt event received from an advisory host hook."""

    schema_version: Literal[1] = 1
    session_id: str
    turn_id: str
    repository: str
    actor: str
    prompt: str
    created_at: datetime

    @field_validator("session_id", "turn_id", "actor")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _identity(value)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        return _repository(value)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        return _prompt(value)

    @field_validator("created_at", mode="before")
    @classmethod
    def parse_json_timestamp(cls, value: object, info: ValidationInfo) -> object:
        return _timestamp_input(value, info)

    @field_validator("created_at")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        return _utc(value)


class CodexUserPromptSubmitEvent(_HostModel):
    """The exact documented Codex ``UserPromptSubmit`` command-hook input."""

    session_id: str
    transcript_path: str | None
    cwd: str
    hook_event_name: Literal["UserPromptSubmit"]
    model: str
    turn_id: str
    permission_mode: Literal["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"]
    prompt: str
    agent_id: str | None = None
    agent_type: str | None = None

    @field_validator("session_id", "turn_id", "model")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _identity(value)

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        return _repository(value)

    @field_validator("transcript_path")
    @classmethod
    def validate_transcript_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _repository(value)

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        return _prompt(value)

    @field_validator("agent_id", "agent_type")
    @classmethod
    def validate_optional_host_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _identity(value)

    @model_validator(mode="after")
    def reject_explicit_null_host_identity(self) -> CodexUserPromptSubmitEvent:
        if any(
            field in self.model_fields_set and getattr(self, field) is None
            for field in ("agent_id", "agent_type")
        ):
            raise ValueError("invalid advisory prompt event")
        return self


class PromptRoute(_HostModel):
    """Detached advisory instruction containing no authorization capability."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    schema_version: Literal[1] = 1
    action: Literal["offer_onboarding", "classify", "answer_clarification", "continue"]
    message: Annotated[str, Field(min_length=1, max_length=2048)]
    mcp_tool: str | None
    arguments: Mapping[str, object]
    advisory: Literal[True] = True
    authorization_issued: Literal[False] = False

    @field_validator("arguments", mode="before")
    @classmethod
    def copy_arguments(cls, value: object) -> object:
        _require_exact_json(value)
        if type(value) is not dict:
            raise ValueError("invalid advisory prompt route")
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return json.loads(encoded)

    @field_validator("arguments")
    @classmethod
    def freeze_arguments(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return cast(Mapping[str, object], _freeze_json(dict(value)))

    @field_serializer("arguments")
    def serialize_arguments(self, value: Mapping[str, object]) -> dict[str, object]:
        return cast(dict[str, object], _thaw_json(value))

    @model_validator(mode="after")
    def validate_route(self) -> PromptRoute:
        expected_tool = {
            "offer_onboarding": None,
            "classify": "intent_advisory_preflight",
            "answer_clarification": "intent_clarification_answer",
            "continue": None,
        }[self.action]
        if self.mcp_tool != expected_tool:
            raise ValueError("invalid advisory prompt route")
        return self


def parse_prompt_event(value: object) -> PromptEvent:
    """Parse one bounded exact JSON tree without retaining its raw representation."""
    encoded: bytes | None = None
    event: PromptEvent | None = None
    failed = False
    try:
        _require_exact_json(value)
        if type(value) is not dict:
            raise ValueError("invalid advisory prompt event")
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _MAX_EVENT_BYTES:
            raise ValueError("invalid advisory prompt event")
        event = PromptEvent.model_validate_json(encoded)
    except (TypeError, ValueError):
        failed = True
    finally:
        value = None
        encoded = None
    if failed or event is None:
        raise ValueError("invalid advisory prompt event") from None
    return event


def parse_codex_prompt_event(value: object) -> CodexUserPromptSubmitEvent:
    """Parse one bounded, exact Codex prompt-hook object."""
    encoded: bytes | None = None
    event: CodexUserPromptSubmitEvent | None = None
    failed = False
    try:
        _require_exact_json(value)
        if type(value) is not dict:
            raise ValueError("invalid Codex prompt event")
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _MAX_EVENT_BYTES:
            raise ValueError("invalid Codex prompt event")
        event = CodexUserPromptSubmitEvent.model_validate_json(encoded)
    except (TypeError, ValueError):
        failed = True
    finally:
        value = None
        encoded = None
    if failed or event is None:
        raise ValueError("invalid Codex prompt event") from None
    return event


def codex_prompt_context(route: PromptRoute) -> str:
    """Render a bounded prompt-free instruction for the Codex hook response."""
    if route.action == "offer_onboarding":
        return _OFFER
    if route.action == "classify":
        conversation_ref = route.arguments.get("conversation_ref")
        if type(conversation_ref) is not str:
            raise ValueError("invalid advisory prompt route")
        encoded_ref = json.dumps(conversation_ref, ensure_ascii=False, separators=(",", ":"))
        return (
            "action=classify. First call public MCP tool intent_context with the current human "
            "request for bounded repository context. Form a classification draft, then call "
            f"public MCP tool intent_advisory_preflight with conversation_ref={encoded_ref}, the "
            "current human request, and that draft. Ask returned questions and follow only the "
            "persisted clarification workflow before implementation."
        )
    if route.action == "answer_clarification":
        safe_arguments = {
            key: route.arguments[key]
            for key in ("session_id", "question_id", "actor", "answered_at")
        }
        encoded = json.dumps(
            safe_arguments,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        context = (
            "action=answer_clarification. Call public MCP tool "
            f"intent_clarification_answer with {encoded} and the current human prompt as answer. "
            "Do not classify the answer again."
        )
        if len(context) > 2048:
            raise ValueError("invalid advisory prompt route")
        return context
    return route.message


def codex_prompt_output(context: str) -> dict[str, object]:
    """Build the documented Codex prompt-hook output wrapper."""
    if type(context) is not str or not context or len(context) > 2048 or _CONTROL.search(context):
        raise ValueError("invalid Codex prompt context")
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }


def repository_matches(directory: SecureDirectory, repository: str) -> bool:
    """Compare an event repository with one already-held project descriptor."""
    selected: SecureDirectory | None = None
    try:
        selected = SecureDirectory.open(Path(repository))
        return selected.identity == directory.identity
    except Exception:  # noqa: BLE001 - unsafe and missing paths are the same denial
        return False
    finally:
        repository = ""
        if selected is not None:
            selected.close()


def unavailable_prompt_route() -> PromptRoute:
    """Return the one stable fail-closed advisory fallback."""
    return PromptRoute(
        action="continue",
        message="Intent advisory prompt routing is unavailable.",
        mcp_tool=None,
        arguments={},
    )


class AdvisoryPromptRouter:
    """Read held onboarding/session state and select one public MCP next action."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def _route(self, event: PromptEvent) -> PromptRoute:
        if type(event) is not PromptEvent:
            raise ValueError("invalid advisory prompt event")
        checked = PromptEvent.model_validate_json(event.model_dump_json())
        if not repository_matches(self._runtime.project_directory, checked.repository):
            raise ValueError("advisory repository mismatch")
        config, _config_bytes = _snapshot_config(self._runtime)
        if checked.actor != config.local_actor:
            raise ValueError("advisory actor mismatch")
        onboarding = inspect_onboarding(cast(OnboardingRuntime, self._runtime))
        if onboarding.state is not OnboardingState.READY:
            return PromptRoute(
                action="offer_onboarding",
                message=_OFFER,
                mcp_tool=None,
                arguments={},
            )
        events = self._runtime.intent_proposals.clarification_events()
        latest: dict[str, ClarificationSession] = {}
        for item in events:
            session = item.session
            latest[session.id] = session
        active = tuple(
            session
            for session in latest.values()
            if session.conversation_ref == checked.session_id and session.status == "open"
        )
        if len(active) > 1:
            raise ValueError("ambiguous advisory clarification")
        if active:
            session = active[0]
            answered = {item.question_id for item in session.answers}
            question = next(
                (item for item in session.questions if item.id not in answered),
                None,
            )
            if question is None:
                return PromptRoute(
                    action="continue",
                    message="Clarification answers are complete; continue the governed proposal flow.",
                    mcp_tool=None,
                    arguments={},
                )
            return PromptRoute(
                action="answer_clarification",
                message="Route this answer to the active Intent Engineering clarification.",
                mcp_tool="intent_clarification_answer",
                arguments={
                    "session_id": session.id,
                    "question_id": question.id,
                    "answer": checked.prompt,
                    "actor": checked.actor,
                    "answered_at": checked.created_at.isoformat().replace("+00:00", "Z"),
                },
            )
        return PromptRoute(
            action="classify",
            message="Classify this prompt through Intent Engineering before implementation.",
            mcp_tool="intent_advisory_preflight",
            arguments={"conversation_ref": checked.session_id},
        )

    def route(self, event: PromptEvent) -> PromptRoute:
        result: PromptRoute | None = None
        signal: BaseException | None = None
        failed = False
        try:
            result = self._route(event)
        except Exception:  # noqa: BLE001 - fixed context-free advisory boundary
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            event = cast(PromptEvent, None)
            self = cast(AdvisoryPromptRouter, None)  # noqa: PLW0642 - scrub traceback state
        if signal is not None:
            detached_signal = signal
            signal = None
            raise detached_signal.with_traceback(None)
        if failed or result is None:
            raise AdvisoryPromptError() from None
        return result


__all__ = [
    "CODEX_ADVISORY_FALLBACK",
    "AdvisoryPromptError",
    "AdvisoryPromptRouter",
    "CodexUserPromptSubmitEvent",
    "PromptEvent",
    "PromptRoute",
    "codex_prompt_context",
    "codex_prompt_output",
    "parse_codex_prompt_event",
    "parse_prompt_event",
    "repository_matches",
    "unavailable_prompt_route",
]
