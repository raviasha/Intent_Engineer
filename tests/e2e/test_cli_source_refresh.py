"""Public CLI source selection, task checks and signal invalidation."""

import json
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from intent_engineering.cli.app import app
from tests.integration.intent_workflow.test_source_refresh import request, selection


def invoke(root, command, draft=None):
    args = [command, "--project", str(root)]
    if draft is not None:
        (root / "draft.json").write_text(json.dumps(draft))
        args += ["--submission", "draft.json"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


@pytest.mark.parametrize(
    "command,field",
    [
        ("background-sources", "authoritative"),
        ("background-refresh", "observations"),
        ("background-source-signal", "event_id"),
    ],
)
def test_schema_does_not_initialize_project(tmp_path, command, field):
    result = CliRunner().invoke(app, [command, "--project", str(tmp_path), "--submission-schema"])
    assert result.exit_code == 0, result.output
    assert field in json.loads(result.output)["properties"]
    assert not (tmp_path / ".intent").exists()


def test_select_refresh_signal_and_exact_task_binding(tmp_path):
    result = invoke(tmp_path, "background-sources", selection().model_dump(mode="json"))
    assert not result["subscriptions_configured"]
    assert len(invoke(tmp_path, "background-sources")["sources"]) == 1
    started = invoke(tmp_path, "background-refresh", request().model_dump(mode="json"))
    assert started["status"] == "refresh_required"
    signal = {
        "source_id": "source:privacy",
        "event_id": "event:1",
        "observed_at": datetime.now(UTC).isoformat(),
    }
    invalidated = invoke(tmp_path, "background-source-signal", signal)
    assert invalidated["generation"] == 2
    assert invoke(tmp_path, "background-source-signal", signal) == invalidated
    (tmp_path / "draft.json").write_text(request(task="different").model_dump_json())
    denied = CliRunner().invoke(
        app, ["background-refresh", "--project", str(tmp_path), "--submission", "draft.json"]
    )
    assert denied.exit_code != 0
    assert "source refresh unavailable" in denied.output


def test_canonical_or_outside_submission_paths_are_rejected(tmp_path):
    for path in ("../secret.json", ".intent/config.yaml", "/tmp/source.json"):
        result = CliRunner().invoke(
            app, ["background-refresh", "--project", str(tmp_path), "--submission", path]
        )
        assert result.exit_code != 0
    assert not (tmp_path / ".intent").exists()
