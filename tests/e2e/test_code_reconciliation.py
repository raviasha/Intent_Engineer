"""Public CLI and hook entry points never block or continue the user's chat."""

import json

import pytest
from click import unstyle
from typer.testing import CliRunner

from intent_engineering.cli.app import app


@pytest.mark.parametrize("color", [False, True])
def test_cli_exposes_background_reconciliation_status_and_control(color):
    result = CliRunner().invoke(app, ["reconcile-code", "--help"], color=color)
    assert result.exit_code == 0
    for flag in ("--run", "--enable", "--disable", "--project"):
        assert flag in unstyle(result.output)


@pytest.mark.parametrize(
    "event",
    [
        None,
        {},
        {"hook_event_name": "PermissionRequest"},
        {"hook_event_name": "PostToolUse", "cwd": "/unrelated", "session_id": "chat"},
        {"hook_event_name": "PostToolUse", "agent_id": "child"},
    ],
)
def test_invalid_unrelated_or_subagent_hooks_are_silent(tmp_path, monkeypatch, event):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["agent-reconcile-hook"], input=json.dumps(event))
    assert result.exit_code == 0
    assert result.output == "{}\n"
    assert list(tmp_path.iterdir()) == []


def test_valid_legacy_hook_does_not_dispatch_review(tmp_path, monkeypatch):
    from intent_engineering.core.policy.project import initialize_project
    from intent_engineering.intent_workflow.code_reconciliation import CodeReconciliationService

    initialize_project(tmp_path)
    child = tmp_path / "src"
    child.mkdir()
    monkeypatch.chdir(child)
    roots = []

    def run(service, *, automatic=False):
        assert automatic is True
        roots.append(service.runtime.root)
        return {"status": "running", "notify_user": False}

    monkeypatch.setattr(CodeReconciliationService, "run", run)
    result = CliRunner().invoke(
        app,
        ["agent-reconcile-hook"],
        input=json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "chat",
                "cwd": str(child),
                "tool_name": "apply_patch",
                "tool_input": {},
            }
        ),
    )
    assert result.exit_code == 0
    assert result.output == "{}\n"
    assert roots == []


def test_hook_worker_failure_has_no_warning_question_or_continuation(tmp_path, monkeypatch):
    from intent_engineering.core.policy.project import initialize_project
    from intent_engineering.intent_workflow.code_reconciliation import CodeReconciliationService

    initialize_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fail(service, *, automatic=False):
        raise RuntimeError("PRIVATE runner error")

    monkeypatch.setattr(CodeReconciliationService, "run", fail)
    result = CliRunner().invoke(
        app,
        ["agent-reconcile-hook"],
        input=json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "chat",
                "cwd": str(tmp_path),
            }
        ),
    )
    assert result.exit_code == 0
    assert result.output == "{}\n"


def test_cli_enable_cannot_restore_automatic_review(tmp_path, monkeypatch):
    from intent_engineering.core.policy.project import initialize_project

    initialize_project(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    result = CliRunner().invoke(app, ["reconcile-code", "--project", str(tmp_path), "--enable"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["enabled"] is False
    assert payload["execution_mode"] == "on_demand"
