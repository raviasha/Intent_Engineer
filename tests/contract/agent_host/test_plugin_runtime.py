"""Exercise installed launchers without any system Python or intent dependency."""

import os
import platform
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[3] / "plugins" / "intent-advisor"


def test_bundled_launcher_preserves_cwd_and_arguments_without_system_python(tmp_path):
    package = tmp_path / "plugin with spaces"
    shutil.copytree(PLUGIN, package)
    target = {"Darwin": "macos", "Linux": "linux"}[platform.system()]
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64"}[platform.machine()]
    runtime = package / "runtime" / f"{target}-{arch}" / "python" / "bin" / "python3"
    runtime.parent.mkdir(parents=True)
    runtime.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
    runtime.chmod(0o755)
    launcher = package / "scripts" / "intent"
    assert launcher.exists(), "packaged launcher is missing"
    result = subprocess.run(
        [str(launcher), "context", "--task", "a task with spaces"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        str(tmp_path),
        "-B",
        "-I",
        str(package / "scripts" / "entry.py"),
        "context",
        "--task",
        "a task with spaces",
    ]


def test_missing_bundled_runtime_does_not_fall_back_to_system_python(tmp_path):
    package = tmp_path / "plugin"
    shutil.copytree(PLUGIN, package)
    launcher = package / "scripts" / "intent"
    assert launcher.exists(), "packaged launcher is missing"
    result = subprocess.run(
        [str(launcher), "--help"],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "runtime" in result.stderr.lower()
    assert not result.stdout


def test_desktop_codex_is_resolved_without_global_cli(tmp_path, monkeypatch):
    from intent_engineering.intent_workflow import codex_review

    monkeypatch.setattr(codex_review.shutil, "which", lambda _: None)
    app = tmp_path / "ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
    app.parent.mkdir(parents=True)
    app.write_text("#!/bin/sh\nexit 0\n")
    app.chmod(0o755)
    resolver = getattr(codex_review, "resolve_codex_executable", None)
    assert resolver is not None, "desktop executable discovery is missing"
    assert resolver(application_roots=(tmp_path,)) == str(app)
    app.chmod(0o644)
    assert resolver(application_roots=(tmp_path,)) is None


@pytest.mark.parametrize("hook", ["prompt-hook", "stop-hook", "reconcile-hook", "monitor-hook"])
def test_development_hook_uses_explicit_interpreter(tmp_path, hook):
    import sys

    result = subprocess.run(
        [str(PLUGIN / "scripts" / hook)],
        input=b"{}",
        capture_output=True,
        check=False,
        env={**os.environ, "INTENT_DEVELOPMENT_PYTHON": sys.executable},
    )
    assert result.returncode == 0


def test_unsupported_platform_has_actionable_error(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text('#!/bin/sh\nprintf "Windows\\n"\n')
    uname.chmod(0o755)
    result = subprocess.run(
        [str(PLUGIN / "scripts/intent"), "--help"],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert "unsupported platform" in result.stderr
    assert not result.stdout


def test_release_cannot_use_developer_interpreter_when_runtime_missing(tmp_path):
    package = tmp_path / "plugin"
    shutil.copytree(PLUGIN, package)
    (package / "distribution.json").write_text("{}")
    result = subprocess.run(
        [str(package / "scripts/intent"), "--help"],
        env={"PATH": "/usr/bin:/bin", "INTENT_DEVELOPMENT_PYTHON": "/bin/echo"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert "runtime missing" in result.stderr
    assert not result.stdout


def test_host_hook_command_handles_plugin_path_with_spaces(tmp_path):
    import json
    import sys

    package = tmp_path / "plugin with spaces"
    shutil.copytree(PLUGIN, package)
    hooks = json.loads((package / "hooks/hooks.json").read_text())
    command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        input=b"{}",
        env={
            "PATH": "/usr/bin:/bin",
            "PLUGIN_ROOT": str(package),
            "INTENT_DEVELOPMENT_PYTHON": sys.executable,
        },
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "hookSpecificOutput" in json.loads(result.stdout)


def test_background_python_child_does_not_mutate_imported_package(tmp_path, monkeypatch):
    from intent_engineering.cli import dev

    module = tmp_path / "packaged_module.py"
    module.write_text("value = 42\n")
    monkeypatch.setattr(
        dev,
        "_BACKGROUND_ENTRYPOINT",
        "import sys; sys.path.insert(0, sys.argv[2]); import packaged_module",
    )
    subprocess.run(
        dev._background_argv(tmp_path),
        env=dev._background_environment(),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert not list(tmp_path.rglob("*.pyc"))


def test_mcp_configuration_runs_from_codex_cache_without_host_placeholder_expansion(tmp_path):
    import json

    server = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["intent_advisor"]
    version = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())["version"]
    profile = tmp_path / "Codex profile"
    launcher = (
        profile
        / "plugins/cache/intent-engineering-local/intent-advisor"
        / version
        / "scripts/intent"
    )
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
    launcher.chmod(0o755)
    assert Path(server["command"]).is_absolute(), "legacy MCP does not expand PLUGIN_ROOT"
    result = subprocess.run(
        [server["command"], *server["args"]],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "CODEX_HOME": str(profile)},
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.splitlines() == [str(tmp_path), "mcp", "--project", "."]
    assert "CODEX_HOME" in server["env_vars"]
