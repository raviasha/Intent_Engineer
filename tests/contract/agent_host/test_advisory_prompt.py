"""Strict contracts for token-free advisory prompt routing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError
from typer.testing import CliRunner

import intent_engineering.cli.app as app_module
import intent_engineering.integrations.agent_host.advisory as advisory_module
from intent_engineering.cli.app import app
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import Graph, Node, NodeType
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.agent_host.advisory import (
    AdvisoryPromptError,
    AdvisoryPromptRouter,
    PromptEvent,
    PromptRoute,
    codex_prompt_context,
    parse_codex_prompt_event,
    parse_prompt_event,
    readiness_prompt_route,
)
from intent_engineering.intent_workflow.conversation import ConversationCapture
from intent_engineering.intent_workflow.readiness import (
    EnsurePreset,
    EnsureResult,
    EnsureStatus,
    ReadinessTarget,
)
from tests.helpers.readiness import apply_baseline

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
OFFER = (
    "This repository has not been onboarded into Intent Engineering. Start guided onboarding now?"
)
TURN_7_CONVERSATION_REF = (
    "codex-prompt:v1:"
    "ee992e532c89c226e193af5b2cfe3c40dbd3596b37804968fa1548be18a0437d:"
    "75bb37228f98a1e72918a0dcc06aadebf6e602b29ef2447e366303dba85beea1:"
    "42696c6976e08e7338133134cbd82654ea97d770557563d3e81754f8a1a511de"
)


def _event(
    project: Path,
    *,
    prompt: str = "Add team sharing",
    session_id: str = "codex:thread-3",
    turn_id: str = "turn-7",
) -> PromptEvent:
    return PromptEvent(
        session_id=session_id,
        turn_id=turn_id,
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
    apply_baseline(
        runtime,
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
        ),
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


def test_readiness_attention_routes_to_a_fixed_human_review_instruction() -> None:
    """Catches open intent review work being bypassed by an advisory prompt hook."""
    route = readiness_prompt_route(
        EnsureResult(
            status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
            attention_route=ReadinessTarget.INBOX,
            graph_version=7,
            pending_proposal_ids=("proposal:private",),
            open_case_ids=("case:private",),
        )
    )

    assert route == PromptRoute(
        action="human_attention_required",
        message=(
            "Intent Engineering requires human review in the local Inbox before "
            "implementation. Do not implement or resolve the intent work automatically."
        ),
        mcp_tool=None,
        arguments={},
    )
    rendered = route.model_dump_json()
    assert "proposal:private" not in rendered
    assert "case:private" not in rendered


def test_readiness_attention_names_the_exact_bounded_ui_route() -> None:
    """Catches a proposal review result being misdirected to the generic Inbox view."""
    result = EnsureResult(
        status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
        attention_route=ReadinessTarget.PROPOSAL,
        graph_version=7,
        pending_proposal_ids=("proposal:private",),
        open_case_ids=(),
    )

    context = advisory_module.readiness_context(result)

    assert context == (
        "Intent Engineering requires human review in the local Proposal review view before "
        "implementation. Do not implement or resolve the intent work automatically."
    )
    assert "proposal:private" not in context


def test_readiness_onboarding_reuses_the_guided_onboarding_offer() -> None:
    """Catches the first-prompt readiness gate giving a different onboarding journey."""
    route = readiness_prompt_route(
        EnsureResult(
            status=EnsureStatus.ONBOARDING_REQUIRED,
            attention_route=ReadinessTarget.ONBOARDING,
            graph_version=0,
            pending_proposal_ids=(),
            open_case_ids=(),
        )
    )

    assert route.action == "offer_onboarding"
    assert route.message == OFFER
    assert route.mcp_tool is None
    assert route.arguments == {}


def test_readiness_state_failure_blocks_implementation_in_official_hook_context() -> None:
    """Catches an invalid future readiness result degrading to bare advisory fallback."""
    route = readiness_prompt_route(
        EnsureResult(
            status=EnsureStatus.SHARED_STATE_INVALID,
            attention_route=ReadinessTarget.TEAM_STATE,
            graph_version=0,
            pending_proposal_ids=(),
            open_case_ids=(),
        )
    )

    assert route.action == "human_attention_required"
    assert route.mcp_tool is None
    assert route.arguments == {}
    assert codex_prompt_context(route) == (
        "action=human_attention_required. Intent Engineering cannot verify local readiness. "
        "Open the local Team state view before governed implementation; do not implement or "
        "resolve intent work automatically."
    )


def test_offline_stale_readiness_routes_to_the_exact_team_state_view() -> None:
    """Catches stale shared state being treated as ready or routed to an unrelated review view."""
    result = EnsureResult(
        status=EnsureStatus.OFFLINE_STALE,
        attention_route=ReadinessTarget.TEAM_STATE,
        graph_version=7,
        pending_proposal_ids=("proposal:private",),
        open_case_ids=("case:private",),
    )

    context = advisory_module.readiness_context(result)
    route = readiness_prompt_route(result)

    assert context == (
        "Intent Engineering is using a stale verified local baseline while shared state is "
        "offline. Open the local Team state view before governed implementation; do not publish "
        "or resolve intent state automatically."
    )
    assert route == PromptRoute(
        action="human_attention_required",
        message=context,
        mcp_tool=None,
        arguments={},
    )
    rendered = route.model_dump_json()
    assert "proposal:private" not in rendered
    assert "case:private" not in rendered
    assert "token" not in rendered.casefold()


def test_initialized_repository_routes_once_to_public_preflight(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    event = _event(project)

    route = AdvisoryPromptRouter(runtime).route(event)

    assert route.action == "classify"
    assert route.mcp_tool == "intent_advisory_preflight"
    assert route.arguments == {
        "conversation_ref": TURN_7_CONVERSATION_REF,
        "request_evidence_ref": runtime.evidence_store.ledger("conversation:codex")[0].evidence.id,
    }
    captured = runtime.evidence_store.ledger("conversation:codex")
    assert len(captured) == 1
    assert captured[0].predecessor_id is None
    assert captured[0].evidence.external_object_id == TURN_7_CONVERSATION_REF
    assert captured[0].evidence.source_locator == TURN_7_CONVERSATION_REF
    assert captured[0].evidence.author == "agent:codex"
    assert captured[0].evidence.payload == {"role": "agent", "content": event.prompt}
    assert "token" not in json.dumps(route.model_dump(mode="json")).lower()


def test_same_host_turn_reuses_ref_while_distinct_turns_do_not(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    router = AdvisoryPromptRouter(_ready_runtime(project))

    first = router.route(_event(project))
    after_first = _durable_bytes(project)
    retry = router.route(_event(project))
    assert _durable_bytes(project) == after_first
    with pytest.raises(AdvisoryPromptError):
        router.route(_event(project, prompt="Add team export"))
    assert _durable_bytes(project) == after_first
    identical_next_turn = router.route(_event(project, turn_id="turn-8"))
    changed_next_turn = router.route(_event(project, prompt="Add team export", turn_id="turn-9"))

    assert first.arguments["conversation_ref"] == retry.arguments["conversation_ref"]
    assert first.arguments["request_evidence_ref"] == retry.arguments["request_evidence_ref"]
    assert identical_next_turn.arguments["conversation_ref"] != first.arguments["conversation_ref"]
    assert changed_next_turn.arguments["conversation_ref"] not in {
        first.arguments["conversation_ref"],
        identical_next_turn.arguments["conversation_ref"],
    }
    assert getattr(advisory_module, "CODEX_CONVERSATION_REF_BYTES", None) == 210
    assert tuple(
        len(route.arguments["conversation_ref"].encode("utf-8"))
        for route in (first, retry, identical_next_turn, changed_next_turn)
    ) == (210, 210, 210, 210)
    assert tuple(
        item.evidence.payload["content"]
        for item in router._runtime.evidence_store.ledger("conversation:codex")
    ) == (
        "Add team sharing",
        "Add team sharing",
        "Add team export",
    )


def test_turn_ref_is_bounded_and_delimiter_safe_without_raw_host_identity(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    router = AdvisoryPromptRouter(_ready_runtime(project))

    left = router.route(_event(project, session_id="a:b", turn_id="c"))
    right = router.route(_event(project, session_id="a", turn_id="b:c"))
    maximum = router.route(_event(project, session_id="s" * 2048, turn_id="t" * 2048))

    assert left.arguments["conversation_ref"] != right.arguments["conversation_ref"]
    maximum_ref = maximum.arguments["conversation_ref"]
    assert type(maximum_ref) is str
    assert len(maximum_ref.encode("utf-8")) <= 512
    assert "s" * 64 not in maximum_ref
    assert "t" * 64 not in maximum_ref
    for session_id, turn_id in (("bad\nsession", "turn"), ("session", "bad\x00turn")):
        with pytest.raises(ValidationError):
            _event(project, session_id=session_id, turn_id=turn_id)


def test_official_event_accepts_optional_host_identity_and_multiline_prompt_without_authority(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    payload = {
        "session_id": "codex:thread-3",
        "transcript_path": None,
        "cwd": str(project),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5.6-sol",
        "turn_id": "turn-7",
        "permission_mode": "default",
        "prompt": "First line\n\tsecond line",
        "agent_id": "host-agent-7",
        "agent_type": "reviewer",
    }

    event = parse_codex_prompt_event(payload)

    assert event.prompt == "First line\n\tsecond line"
    assert event.agent_id == "host-agent-7"
    assert event.agent_type == "reviewer"


@pytest.mark.parametrize("field", ["agent_id", "agent_type"])
def test_optional_host_identity_rejects_null_subclasses_and_controls(
    tmp_path: Path,
    field: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    base = {
        "session_id": "codex:thread-3",
        "transcript_path": None,
        "cwd": str(project),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5.6-sol",
        "turn_id": "turn-7",
        "permission_mode": "default",
        "prompt": "work",
    }
    for bad in (None, type("Text", (str,), {})("host-agent"), "bad\nidentity"):
        with pytest.raises(ValueError, match="invalid Codex prompt event"):
            parse_codex_prompt_event({**base, field: bad})


@pytest.mark.parametrize("control", ["\x00", "\x07", "\x0b", "\x0c", "\x1f", "\x7f"])
def test_official_prompt_rejects_disallowed_controls(tmp_path: Path, control: str) -> None:
    project = tmp_path / "project"
    project.mkdir()
    payload = {
        "session_id": "codex:thread-3",
        "transcript_path": None,
        "cwd": str(project),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5.6-sol",
        "turn_id": "turn-7",
        "permission_mode": "default",
        "prompt": f"before{control}after",
    }
    with pytest.raises(ValueError, match="invalid Codex prompt event"):
        parse_codex_prompt_event(payload)


def test_prompt_route_arguments_are_recursively_frozen_detached_and_token_safe(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    route = AdvisoryPromptRouter(runtime).route(_event(project))

    with pytest.raises(TypeError):
        route.arguments["authorization_token"] = "PRIVATE-INJECTED-TOKEN"

    source = {"context": {"items": ["safe"]}}
    nested = PromptRoute(
        action="continue",
        message="Continue safely.",
        mcp_tool=None,
        arguments=source,
    )
    source["context"]["items"].append("source-mutated")
    context = nested.arguments["context"]
    with pytest.raises(TypeError):
        context["authorization_token"] = "PRIVATE-NESTED-TOKEN"  # type: ignore[index]
    items = context["items"]  # type: ignore[index]
    with pytest.raises(AttributeError):
        items.append("PRIVATE-NESTED-TOKEN")  # type: ignore[union-attr]

    dumped = nested.model_dump(mode="json")
    dumped["arguments"]["context"]["items"].append("detached")
    assert nested.model_dump(mode="json")["arguments"] == {"context": {"items": ["safe"]}}
    assert "token" not in json.dumps(route.model_dump(mode="json")).casefold()


def test_active_clarification_hook_answer_requires_independent_human_authority(
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
        conversation_ref = TURN_7_CONVERSATION_REF
        opened_by = "agent:codex"
        status = "open"
        questions = (Question(),)
        answers: tuple[object, ...] = ()

    class Event:
        session = Session()

    monkeypatch.setattr(runtime.intent_proposals, "clarification_events", lambda: (Event(),))
    answer = _event(project, prompt="Workspace admins only", turn_id="turn-8")

    route = AdvisoryPromptRouter(runtime).route(answer)

    assert route.action == "human_confirmation_required"
    assert route.mcp_tool is None
    assert route.arguments == {}
    evidence = runtime.evidence_store.ledger("conversation:codex")
    assert len(evidence) == 1
    assert evidence[0].evidence.external_object_id != Session.conversation_ref
    assert evidence[0].evidence.author == "agent:codex"
    assert evidence[0].evidence.payload == {"role": "agent", "content": answer.prompt}
    assert advisory_module.codex_prompt_context(route) == (
        "action=human_confirmation_required. This advisory hook cannot authenticate a local "
        "human answer or approval. Keep the clarification pending until an independently "
        "authenticated non-MCP local human workflow records it."
    )


@pytest.mark.parametrize(
    "prompt",
    ["confirm sha256:" + "3" * 64, "no", "decline", "confirm sha256:" + "9" * 64],
)
def test_proposed_clarification_routes_to_exact_public_preview_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prompt: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    proposal_id = "proposal:sha256:" + "2" * 64

    class Session:
        id = "clarification:sha256:" + "1" * 64
        conversation_ref = TURN_7_CONVERSATION_REF
        opened_by = "agent:codex"
        status = "proposed"
        questions: tuple[object, ...] = ()
        answers: tuple[object, ...] = ()

    event = type(
        "Event",
        (),
        {"session": Session(), "event_type": "proposed", "proposal_id": proposal_id},
    )()

    monkeypatch.setattr(runtime.intent_proposals, "clarification_events", lambda: (event,))

    route = AdvisoryPromptRouter(runtime).route(_event(project, prompt=prompt, turn_id="turn-8"))

    assert route.action == "review_clarification_proposal"
    assert route.mcp_tool == "intent_clarification_show"
    assert route.arguments == {"proposal_id": proposal_id}
    context = advisory_module.codex_prompt_context(route)
    assert "intent_clarification_show" in context
    assert "exact proposal_digest" in context
    assert "decline" in context.casefold()
    assert "intent_clarification_confirm" not in context
    assert "independently authenticated non-MCP local human workflow" in context


@pytest.mark.parametrize("status", ["open", "proposed"])
def test_two_active_clarifications_in_same_host_lineage_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    router = AdvisoryPromptRouter(runtime)
    first_ref = TURN_7_CONVERSATION_REF
    second_ref = router.route(_event(project, turn_id="turn-8")).arguments["conversation_ref"]

    class Question:
        id = "audience"

    def event(session_id: str, conversation_ref: object, proposal_id: str):
        session = type(
            "Session",
            (),
            {
                "id": session_id,
                "conversation_ref": conversation_ref,
                "opened_by": "agent:codex",
                "status": status,
                "questions": (Question(),),
                "answers": (),
            },
        )()
        return type("Event", (), {"session": session, "proposal_id": proposal_id})()

    monkeypatch.setattr(
        runtime.intent_proposals,
        "clarification_events",
        lambda: (
            event(
                "clarification:sha256:" + "1" * 64,
                first_ref,
                "proposal:sha256:" + "3" * 64,
            ),
            event(
                "clarification:sha256:" + "2" * 64,
                second_ref,
                "proposal:sha256:" + "4" * 64,
            ),
        ),
    )

    with pytest.raises(AdvisoryPromptError):
        router.route(_event(project, prompt="Workspace admins", turn_id="turn-9"))


@pytest.mark.parametrize("status", ["open", "proposed"])
def test_clarification_lineage_rejects_actor_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)

    session = type(
        "Session",
        (),
        {
            "id": "clarification:sha256:" + "1" * 64,
            "conversation_ref": TURN_7_CONVERSATION_REF,
            "opened_by": "other",
            "status": status,
            "questions": (),
            "answers": (),
        },
    )()
    event = type(
        "Event",
        (),
        {"session": session, "proposal_id": "proposal:sha256:" + "2" * 64},
    )()

    monkeypatch.setattr(runtime.intent_proposals, "clarification_events", lambda: (event,))

    with pytest.raises(AdvisoryPromptError):
        AdvisoryPromptRouter(runtime).route(_event(project, turn_id="turn-8"))


@pytest.mark.parametrize("status", ["open", "proposed"])
def test_active_clarification_from_another_host_session_is_not_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    router = AdvisoryPromptRouter(runtime)
    other_ref = router.route(
        _event(project, session_id="codex:other-thread", turn_id="turn-1")
    ).arguments["conversation_ref"]

    session = type(
        "Session",
        (),
        {
            "id": "clarification:sha256:" + "1" * 64,
            "conversation_ref": other_ref,
            "opened_by": "agent:codex",
            "status": status,
            "questions": (),
            "answers": (),
        },
    )()
    event = type(
        "Event",
        (),
        {"session": session, "proposal_id": "proposal:sha256:" + "2" * 64},
    )()

    monkeypatch.setattr(runtime.intent_proposals, "clarification_events", lambda: (event,))

    with pytest.raises(AdvisoryPromptError):
        router.route(_event(project, turn_id="turn-8"))


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
            mcp_tool="intent_advisory_preflight",
            arguments={"conversation_ref": "codex:thread-3"},
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


def test_hook_capture_cancellation_rolls_back_and_scrubs_exact_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    marker = "PRIVATE-CANCELLED-HOOK-PROMPT-8198"
    event = _event(project, prompt=marker)
    before = _durable_bytes(project)
    original = ConversationCapture.record_turn
    signal = _CancellationSignal()

    def capture_then_cancel(self: ConversationCapture, **kwargs: object):
        original(self, **kwargs)  # type: ignore[arg-type]
        raise signal

    monkeypatch.setattr(ConversationCapture, "record_turn", capture_then_cancel)
    with pytest.raises(_CancellationSignal) as cancelled:
        AdvisoryPromptRouter(runtime).route(event)

    assert cancelled.value is signal
    after = _durable_bytes(project)
    assert {name: content for name, content in after.items() if not name.endswith(".lock")} == {
        name: content for name, content in before.items() if not name.endswith(".lock")
    }
    assert tuple(runtime.evidence_store.ledger("conversation:codex")) == ()
    assert marker not in _repository_traceback_values(cancelled.value)


def test_hidden_cli_reads_one_bounded_object_and_emits_fixed_secret_free_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    structlog.reset_defaults()
    request.addfinalizer(structlog.reset_defaults)
    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    project = tmp_path / "project"
    project.mkdir()
    _ready_runtime(project)
    monkeypatch.setattr(
        app_module,
        "_developer_readiness",
        lambda _project, _preset: EnsureResult(
            status=EnsureStatus.READY,
            attention_route=ReadinessTarget.HOME,
            graph_version=1,
            pending_proposal_ids=(),
            open_case_ids=(),
        ),
    )
    monkeypatch.chdir(project)
    runner = CliRunner()
    valid = runner.invoke(app, ["agent-prompt-hook"], input=_event(project).model_dump_json())
    assert valid.exit_code == 0
    assert json.loads(valid.stdout)["mcp_tool"] == "intent_advisory_preflight"
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


def test_hidden_cli_blocks_prompt_capture_when_readiness_needs_human_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Catches the developer preset classifying a prompt before an existing review gate."""
    structlog.reset_defaults()
    request.addfinalizer(structlog.reset_defaults)
    monkeypatch.setattr("intent_engineering.cli.app._configure_logging", lambda: None)
    project = tmp_path / "project"
    project.mkdir()
    runtime = _ready_runtime(project)
    runtime.close()
    before = _durable_bytes(project)
    calls: list[tuple[Path, EnsurePreset]] = []

    def attention(project_value: Path, preset: EnsurePreset) -> EnsureResult:
        calls.append((project_value, preset))
        return EnsureResult(
            status=EnsureStatus.HUMAN_ATTENTION_REQUIRED,
            attention_route=ReadinessTarget.INBOX,
            graph_version=1,
            pending_proposal_ids=(),
            open_case_ids=("case:private",),
        )

    monkeypatch.setattr(app_module, "_developer_readiness", attention)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(app, ["agent-prompt-hook"], input=_event(project).model_dump_json())

    assert result.exit_code == 0
    assert json.loads(result.stdout) == PromptRoute(
        action="human_attention_required",
        message=(
            "Intent Engineering requires human review in the local Inbox before "
            "implementation. Do not implement or resolve the intent work automatically."
        ),
        mcp_tool=None,
        arguments={},
    ).model_dump(mode="json")
    assert calls == [(project, EnsurePreset.DEVELOPER)]
    assert _durable_bytes(project) == before


def test_hidden_cli_leaves_structlog_usable_for_later_library_calls(capsys) -> None:
    structlog.get_logger("intent_engineering.sync.orchestrator").info(
        "advisory_prompt_contract_logging_probe"
    )

    assert "advisory_prompt_contract_logging_probe" in capsys.readouterr().out
