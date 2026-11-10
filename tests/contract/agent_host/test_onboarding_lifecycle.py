import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from intent_engineering.intent_workflow.onboarding_reminders import defer_onboarding
from tests.contract.agent_host.test_advisory_plugin import REPO_ROOT, _run_official_hook


def _stop(project: Path, **updates: object) -> None:
    event = {
        "session_id": "codex:thread-3",
        "turn_id": "turn-7",
        "cwd": str(project),
        "hook_event_name": "Stop",
        "transcript_path": None,
        "model": "test",
        "permission_mode": "default",
        "stop_hook_active": False,
        "last_assistant_message": "Completed.",
        **updates,
    }
    environment = os.environ.copy()
    environment["INTENT_DEVELOPMENT_PYTHON"] = sys.executable
    environment["PATH"] = f"{REPO_ROOT / '.venv/bin'}:/usr/bin:/bin"
    result = subprocess.run(
        [str(REPO_ROOT / "plugins/intent-advisor/scripts/stop-hook")],
        input=json.dumps(event),
        text=True,
        capture_output=True,
        cwd=project,
        env=environment,
        timeout=7,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}


def _context(project: Path, prompt: str = "Update the documentation") -> str:
    result = _run_official_hook(project, prompt=prompt)
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_actual_hooks_respect_defer_and_only_count_a_normal_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "cache"))
    project = tmp_path / "repo"
    project.mkdir()
    defer_onboarding(project, after_tasks=1)
    assert "intent_background_context" in _context(project)
    _stop(project, stop_hook_active=True)
    _stop(project, agent_id="child", agent_type="worker")
    assert "intent_background_context" in _context(project)
    _stop(project)
    assert "intent_background_context" in _context(project)
    assert not (project / ".intent").exists()


def test_explicit_creation_overrides_deferral_without_creating_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "cache"))
    defer_onboarding(tmp_path)
    context = _context(tmp_path, "Create intent graph")
    assert "separate chat" in context
    assert "Notion" in context
    assert "intent_background_context" in context
    assert "intent_background_submit" in context
    assert not (tmp_path / ".intent").exists()


def test_quoted_or_negated_creation_does_not_override_deferral(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "cache"))
    defer_onboarding(tmp_path)
    assert "intent_background_context" in _context(tmp_path, 'Do not "create intent graph" yet')


def test_create_request_reuses_ready_graph_and_preserves_attention_routes(tmp_path: Path) -> None:
    from intent_engineering.cli.onboarding_hooks import onboarding_prompt_route
    from intent_engineering.integrations.agent_host.advisory import (
        CodexUserPromptSubmitEvent,
        PromptRoute,
    )
    from tests.contract.agent_host.test_advisory_plugin import _official_event

    event = CodexUserPromptSubmitEvent.model_validate(
        _official_event(tmp_path, prompt="create intent graph")
    )
    ready = onboarding_prompt_route(tmp_path, event, None)
    assert ready is not None
    assert "already exists" in ready.message
    assert ready.action == "continue"
    attention = PromptRoute(
        action="human_attention_required",
        message="Review pending proposal",
        mcp_tool=None,
        arguments={},
    )
    assert onboarding_prompt_route(tmp_path, event, attention) == attention
