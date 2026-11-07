"""Public enrollment file boundaries exercise real filesystem and canonical exports."""

from __future__ import annotations

import os
import stat

import pytest
from typer.testing import CliRunner

from tests.unit.team_state.test_enrollment import _join


def test_public_files_roundtrip_with_owner_only_exclusive_creation(tmp_path):
    """Catches writing an overwriteable or noncanonical public exchange file."""
    from intent_engineering.cli import team_enrollment

    _, _, _, invite, response = _join(tmp_path)
    destination = tmp_path / "invite.json"
    team_enrollment.write_public_file(destination, invite)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert team_enrollment.read_invite(destination) == invite
    before = destination.read_bytes()
    with pytest.raises(ValueError, match="team enrollment unavailable"):
        team_enrollment.write_public_file(destination, response)
    assert destination.read_bytes() == before
    result = tmp_path / "response.json"
    team_enrollment.write_public_file(result, response)
    assert team_enrollment.read_response(result) == response
    assert b"one-time-github-oauth-proof" not in result.read_bytes()


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo", "oversized", "duplicate"))
def test_public_input_rejects_unsafe_or_noncanonical_files(tmp_path, kind):
    """Catches blocking special-file reads, link aliasing and unbounded/duplicate input."""
    from intent_engineering.cli import team_enrollment

    target = tmp_path / "input.json"
    if kind in {"symlink", "hardlink"}:
        original = tmp_path / "original.json"
        original.write_bytes(b"{}")
        if kind == "symlink":
            target.symlink_to(original)
        else:
            os.link(original, target)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "oversized":
        target.write_bytes(b"x" * (32 * 1024 + 1))
    else:
        target.write_bytes(b'{"schema_version":2,"schema_version":2}')
    with pytest.raises(ValueError, match="team enrollment unavailable"):
        team_enrollment.read_invite(target)


def test_public_output_does_not_follow_symlinks(tmp_path):
    """Catches a supplied output alias modifying an unrelated existing file."""
    from intent_engineering.cli import team_enrollment

    _, _, _, invite, _ = _join(tmp_path)
    original = tmp_path / "original"
    original.write_bytes(b"keep")
    target = tmp_path / "invite.json"
    target.symlink_to(original)
    with pytest.raises(ValueError, match="team enrollment unavailable"):
        team_enrollment.write_public_file(target, invite)
    assert original.read_bytes() == b"keep"


def test_join_command_stages_one_repository_bound_public_input_and_opens_review(
    tmp_path, monkeypatch
):
    """Catches a successful join command with no runnable durable browser handoff."""
    from intent_engineering.cli import dev, team_enrollment
    from intent_engineering.cli.app import app
    from intent_engineering.cli.runtime import load_runtime
    from intent_engineering.control_plane.team_enrollment import load_enrollment_request
    from tests.helpers.shared_state import git, ready_project

    project = tmp_path / "project"
    project.mkdir()
    ready_project(project)
    git(project, "init", "--initial-branch=main")
    git(project, "remote", "add", "origin", "https://github.com/acme/project.git")
    _, _, _, invite, _ = _join(tmp_path)
    input_file = tmp_path / "invite.json"
    team_enrollment.write_public_file(input_file, invite)
    output_file = tmp_path / "response.json"
    opened = []
    monkeypatch.setattr(dev, "dev_command", lambda **kwargs: opened.append(kwargs))
    result = CliRunner().invoke(
        app,
        [
            "team",
            "join",
            "--project",
            str(project),
            "--invite",
            str(input_file),
            "--output",
            str(output_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output == "Complete the join review in your local browser.\n"
    runtime = load_runtime(project)
    try:
        request = load_enrollment_request(runtime)
        assert request.action == "join"
        assert request.invite == invite
        assert request.output == str(output_file)
        assert request.project_id == "project"
        assert request.repository_id == "github.com/acme/project"
    finally:
        runtime.close()
    assert opened[0]["project"] == project
    assert not output_file.exists()


def test_cli_enrollment_inputs_fail_with_fixed_errors_without_handoff(tmp_path):
    """Catches reflection of public response fields or paths through CLI exceptions."""
    from intent_engineering.cli.app import app

    path = tmp_path / "PRIVATE-TOKEN-input.json"
    path.write_bytes(b'{"PRIVATE-TOKEN":"private-key"}')
    for command, option in (("join", "--invite"), ("approve-join", "--response")):
        arguments = ["team", command, "--project", str(tmp_path), option, str(path)]
        if command == "join":
            arguments.extend(["--output", str(tmp_path / "response.json")])
        result = CliRunner().invoke(app, arguments)
        assert result.exit_code == 1
        assert result.output == "intent error: team enrollment unavailable\n"
        assert not (tmp_path / ".intent/team-enrollment-session.json").exists()
