"""Public CLI and hook entry points never block or continue the user's chat."""

import json

import pytest
from typer.testing import CliRunner

from intent_engineering.cli.app import app


def test_cli_exposes_background_reconciliation_status_and_control(tmp_path):
    result = CliRunner().invoke(app, ["reconcile-code", "--help"])
    assert result.exit_code == 0
    for flag in ("--run", "--enable", "--disable", "--project"):
        assert flag in result.output


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


def test_valid_hook_dispatches_for_the_enclosing_project_silently(tmp_path, monkeypatch):
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
    assert roots == [tmp_path]


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
