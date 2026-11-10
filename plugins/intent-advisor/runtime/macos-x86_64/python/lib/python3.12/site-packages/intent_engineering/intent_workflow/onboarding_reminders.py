"""User-local onboarding reminders; never graph state or authorization evidence."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.secure import SecureDirectory, SecureFile

_LIMIT = 256


class _Reminder(StrictModel):
    schema_version: Literal[1] = 1
    remaining_tasks: Annotated[int, Field(ge=0, le=100)]
    pending: Annotated[tuple[str, ...], Field(max_length=_LIMIT)] = ()
    completed: Annotated[tuple[str, ...], Field(max_length=_LIMIT)] = ()
    setup_status: Literal["idle", "prepared", "running", "complete"] = "idle"
    chat_id: Annotated[str | None, Field(max_length=512)] = None
    sources: Annotated[tuple[str, ...], Field(max_length=16)] = ()
    return_thread_id: Annotated[str | None, Field(max_length=512)] = None
    return_host_id: Annotated[str | None, Field(max_length=512)] = None
    return_session_id: Annotated[str | None, Field(max_length=512)] = None
    assessment_run_id: Annotated[str | None, Field(max_length=512)] = None
    delivered_update_id: Annotated[str | None, Field(max_length=128)] = None


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _state_file(project: Path, *, create: bool) -> SecureFile | None:
    # Include the directory identity so a replacement checkout cannot inherit consent.
    with closing(SecureDirectory.open(project)) as directory:
        info = os.fstat(directory.descriptor)
        key = _digest((str(directory.path), info.st_dev, info.st_ino))
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    if not root.is_absolute():
        raise ValueError("onboarding reminder unavailable")
    root = root / "intent-engineering" / "onboarding"
    if not create and not root.exists():
        return None
    with closing(SecureDirectory.open(root, create=create)) as directory:
        return directory.file(f"{key}.json")


def _load(target: SecureFile) -> _Reminder | None:
    raw = target.read_optional_nonblocking(max_bytes=65536)
    return _Reminder.model_validate_json(raw) if raw is not None else None


def defer_onboarding(project: Path, *, after_tasks: int = 3) -> None:
    """Remember an explicit deferral without creating a project workspace."""
    if type(after_tasks) is not int or not 1 <= after_tasks <= 100:
        raise ValueError("reminder interval must be between 1 and 100 tasks")
    target = _state_file(project, create=True)
    assert target is not None
    try:
        with same_path_lock(target):
            target.atomic_write(_Reminder(remaining_tasks=after_tasks).model_dump_json().encode())
    finally:
        target.close()


def offer_on_prompt(project: Path, session_id: str, turn_id: str) -> bool:
    """Check the countdown and enroll one deferred work turn for a later Stop event."""
    target = _state_file(project, create=False)
    if target is None:
        return True
    try:
        with same_path_lock(target):
            state = _load(target)
            if state is not None and state.setup_status in {"prepared", "running", "complete"}:
                return False
            if state is None or state.remaining_tasks == 0:
                return True
            key = _digest((session_id, turn_id))
            if key not in (*state.pending, *state.completed):
                # Retain bounded state. A dropped old turn never advances the countdown.
                state = state.model_copy(update={"pending": (*state.pending, key)[-_LIMIT:]})
                target.atomic_write(state.model_dump_json().encode())
            return False
    finally:
        target.close()


def onboarding_chat_state(
    project: Path,
    *,
    prepare: bool = False,
    sources: tuple[str, ...] = (),
    chat_id: str | None = None,
    finished: bool = False,
    return_thread_id: str | None = None,
    return_host_id: str | None = None,
    return_session_id: str | None = None,
    assessment_run_id: str | None = None,
    delivered_update_id: str | None = None,
) -> dict[str, object]:
    """Persist a host-created chat handoff, never create a chat or imply source authentication."""
    allowed = {"notion", "confluence", "slack", "google-drive", "github", "local"}
    if set(sources) - allowed or len(sources) != len(set(sources)):
        raise ValueError("unsupported onboarding sources")
    if chat_id is not None and (
        not chat_id or len(chat_id) > 512 or any(ord(c) < 32 for c in chat_id)
    ):
        raise ValueError("invalid onboarding chat ID")
    route = {
        "return_thread_id": return_thread_id,
        "return_host_id": return_host_id,
        "return_session_id": return_session_id,
    }
    for value in (*route.values(), assessment_run_id, delivered_update_id):
        if value is not None and (not value or len(value) > 512 or any(ord(c) < 32 for c in value)):
            raise ValueError("invalid onboarding return route")
    if any(value is not None for value in route.values()) and not prepare:
        raise ValueError("return route is captured during preparation")
    target = _state_file(project, create=prepare or chat_id is not None or finished)
    state = _Reminder(remaining_tasks=0)
    if target is not None:
        try:
            with same_path_lock(target):
                state = _load(target) or state
                for key, value in route.items():
                    if value is not None:
                        if getattr(state, key) not in (None, value):
                            raise ValueError("return route already exists")
                        state = state.model_copy(update={key: value})
                if prepare and state.setup_status == "idle":
                    state = state.model_copy(
                        update={"setup_status": "prepared", "sources": sources}
                    )
                if chat_id is not None:
                    if state.chat_id is not None and state.chat_id != chat_id:
                        raise ValueError("an onboarding chat already exists")
                    state = state.model_copy(update={"setup_status": "running", "chat_id": chat_id})
                if finished:
                    if state.chat_id is None:
                        raise ValueError("no onboarding chat is running")
                    updates: dict[str, object] = {"setup_status": "complete"}
                    if assessment_run_id is not None:
                        updates["assessment_run_id"] = assessment_run_id
                        if state.assessment_run_id != assessment_run_id:
                            updates["delivered_update_id"] = None
                    state = state.model_copy(update=updates)
                if delivered_update_id is not None:
                    if (
                        state.setup_status != "complete"
                        or state.assessment_run_id != assessment_run_id
                    ):
                        raise ValueError(
                            "completed assessment changed before delivery acknowledgement"
                        )
                    state = state.model_copy(update={"delivered_update_id": delivered_update_id})
                if prepare or chat_id is not None or finished or delivered_update_id is not None:
                    target.atomic_write(state.model_dump_json().encode())
        finally:
            target.close()
    return {
        "status": state.setup_status,
        "chat_id": state.chat_id,
        "sources": list(state.sources),
        "remaining_tasks": state.remaining_tasks,
        "return_thread_id": state.return_thread_id,
        "return_host_id": state.return_host_id,
        "return_session_id": state.return_session_id,
        "assessment_run_id": state.assessment_run_id,
        "delivered_update_id": state.delivered_update_id,
    }


def complete_turn(project: Path, session_id: str, turn_id: str) -> None:
    """Count a previously enrolled completed turn at most once."""
    target = _state_file(project, create=False)
    if target is None:
        return
    try:
        with same_path_lock(target):
            state = _load(target)
            key = _digest((session_id, turn_id))
            if state is None or key not in state.pending or key in state.completed:
                return
            state = state.model_copy(
                update={
                    "remaining_tasks": max(0, state.remaining_tasks - 1),
                    "pending": tuple(item for item in state.pending if item != key),
                    "completed": (*state.completed, key)[-_LIMIT:],
                }
            )
            target.atomic_write(state.model_dump_json().encode())
    finally:
        target.close()
