"""Strict contracts for token-free advisory prompt routing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import Graph, Node, NodeType
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.agent_host.advisory import (
    AdvisoryPromptError,
    AdvisoryPromptRouter,
    PromptEvent,
    PromptRoute,
    parse_prompt_event,
)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
OFFER = (
    "This repository has not been onboarded into Intent Engineering. "
    "Start guided onboarding now?"
)


def _event(project: Path, *, prompt: str = "Add team sharing") -> PromptEvent:
    return PromptEvent(
        session_id="codex:thread-3",
        turn_id="turn-7",
        repository=str(project),
        actor="local",
        prompt=prompt,
        created_at=NOW,
    )


def _durable_bytes(project: Path) -> dict[str, bytes]:
    workspace = project / ".intent"
    return {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _ready_runtime(project: Path):
    initialize_project(project)
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id=f"graph:{project.name}",
            version=1,
            name=project.name,
            nodes=(
                Node(
                    id="intent:sharing",
                    type=NodeType.PRODUCT_INTENT,
                    label="Share reports safely",
                    status="active",
                    created_by="local",
                    created_at=NOW,
                    last_modified_by="local",
                    last_modified_at=NOW,
                ),
            ),
            edges=(),
        )
    )
    return runtime


def test_uninitialized_repository_returns_onboarding_offer_without_writes(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    runtime = load_runtime(project)
    router = AdvisoryPromptRouter(runtime)
    before = _durable_bytes(project)

    route = router.route(_event(project))

    assert route.action == "offer_onboarding"
    assert route.message == OFFER
    assert route.mcp_tool is None
    assert route.arguments == {}
    assert _durable_bytes(project) == before


def test_initialized_repository_routes_once_to_public_preflight(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    event = _event(project)

    route = AdvisoryPromptRouter(runtime).route(event)

    assert route.action == "classify"
    assert route.mcp_tool == "intent_preflight"
    assert route.arguments == {"task": event.prompt}
    assert "token" not in json.dumps(route.model_dump(mode="json")).lower()


def test_active_clarification_answer_routes_without_reclassification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)

    class Question:
        id = "audience"

    class Session:
        id = "clarification:sha256:" + "1" * 64
        conversation_ref = "codex:thread-3"
        status = "open"
        questions = (Question(),)
        answers: tuple[object, ...] = ()

    class Event:
        session = Session()

    monkeypatch.setattr(runtime.intent_proposals, "clarification_events", lambda: (Event(),))
    answer = _event(project, prompt="Workspace admins only")

    route = AdvisoryPromptRouter(runtime).route(answer)

    assert route.action == "answer_clarification"
    assert route.mcp_tool == "intent_clarification_answer"
    assert route.arguments == {
        "session_id": Session.id,
        "question_id": "audience",
        "answer": answer.prompt,
        "actor": "local",
        "answered_at": "2026-08-28T12:00:00Z",
    }


def test_prompt_records_are_strict_frozen_detached_and_require_canonical_timestamp(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    payload = _event(project).model_dump(mode="json")
    parsed = parse_prompt_event(payload)
    assert parsed == _event(project)
    with pytest.raises(ValidationError):
        parsed.prompt = "changed"  # type: ignore[misc]
    with pytest.raises((TypeError, ValueError), match="invalid advisory prompt event"):
        parse_prompt_event({**payload, "actor": type("Text", (str,), {})("local")})
    with pytest.raises((TypeError, ValueError), match="invalid advisory prompt event"):
        parse_prompt_event(type("Mapping", (dict,), {})(payload))
    with pytest.raises(ValueError, match="canonical UTC"):
        PromptEvent.model_validate_json(
            json.dumps({**payload, "created_at": "2026-08-28T12:00:00+00:00"})
        )
    with pytest.raises(ValidationError):
        PromptRoute(
            action="classify",
            message="Classify prompt through Intent Engineering.",
            mcp_tool="intent_preflight",
            arguments={"task": "work"},
            advisory=True,
            authorization_issued=True,
        )


@pytest.mark.parametrize("shape", ["cycle", "depth", "nodes", "utf8"])
def test_raw_prompt_event_rejects_cyclic_or_excessive_json_shape(
    tmp_path: Path,
    shape: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    payload: dict[str, object] = _event(project).model_dump(mode="json")
    if shape == "cycle":
        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        payload["unknown"] = cycle
    elif shape == "depth":
        root: list[object] = []
        cursor = root
        for _index in range(160):
            child: list[object] = []
            cursor.append(child)
            cursor = child
        payload["unknown"] = root
    elif shape == "nodes":
        payload["unknown"] = [None] * 70_000
    else:
        payload["prompt"] = "é" * 20_000

    with pytest.raises(ValueError, match="invalid advisory prompt event"):
        parse_prompt_event(payload)


@pytest.mark.parametrize("variant", ["other", "symlink"])
def test_router_rejects_cross_repository_and_symlink_aliases(
    tmp_path: Path,
    variant: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    selected = tmp_path / "other"
    if variant == "other":
        selected.mkdir()
    else:
        selected.symlink_to(project, target_is_directory=True)
    marker = "PRIVATE-CROSS-REPOSITORY-PROMPT"

    with pytest.raises(AdvisoryPromptError) as caught:
        AdvisoryPromptRouter(runtime).route(_event(selected, prompt=marker))

    assert caught.value.args == ("intent advisory prompt unavailable",)
    assert marker not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


class _CancellationSignal(BaseException):
    pass


def _repository_traceback_values(error: BaseException) -> str:
    values: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            values.extend(repr(value) for value in traceback.tb_frame.f_locals.values())
        traceback = traceback.tb_next
    return " ".join(values)


def test_router_fixed_failure_and_cancellation_tracebacks_drop_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import intent_engineering.integrations.agent_host.advisory as advisory_module

    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    marker = "PRIVATE-PROMPT-TRACEBACK-8197"
    event = _event(project, prompt=marker)
    monkeypatch.setattr(
        advisory_module,
        "inspect_onboarding",
        lambda _runtime: (_ for _ in ()).throw(RuntimeError(marker)),
    )
    with pytest.raises(AdvisoryPromptError) as failed:
        AdvisoryPromptRouter(runtime).route(event)
    assert failed.value.args == ("intent advisory prompt unavailable",)
    assert marker not in repr(failed.value)
    assert marker not in _repository_traceback_values(failed.value)

    signal = _CancellationSignal()
    monkeypatch.setattr(
        advisory_module,
        "inspect_onboarding",
        lambda _runtime: (_ for _ in ()).throw(signal),
    )
    with pytest.raises(_CancellationSignal) as cancelled:
        AdvisoryPromptRouter(runtime).route(event)
    assert cancelled.value is signal
    assert marker not in _repository_traceback_values(cancelled.value)


def test_hidden_cli_reads_one_bounded_object_and_emits_fixed_secret_free_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _ready_runtime(project)
    monkeypatch.chdir(project)
    runner = CliRunner()
    valid = runner.invoke(app, ["agent-prompt-hook"], input=_event(project).model_dump_json())
    assert valid.exit_code == 0
    assert json.loads(valid.stdout)["mcp_tool"] == "intent_preflight"
    assert valid.stderr == ""

    marker = "PRIVATE-HOOK-MALFORMED-8197"
    malformed = runner.invoke(app, ["agent-prompt-hook"], input=f'{{"prompt":"{marker}"')
    assert malformed.exit_code == 1
    assert marker not in malformed.stdout + malformed.stderr
    assert json.loads(malformed.stdout) == {
        "schema_version": 1,
        "action": "continue",
        "message": "Intent advisory prompt routing is unavailable.",
        "mcp_tool": None,
        "arguments": {},
        "advisory": True,
        "authorization_issued": False,
    }

    oversized = runner.invoke(app, ["agent-prompt-hook"], input="x" * 70_000)
    assert oversized.exit_code == 1
    assert "x" * 64 not in oversized.stdout + oversized.stderr
