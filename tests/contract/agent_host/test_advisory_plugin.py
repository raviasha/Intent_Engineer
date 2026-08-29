"""Offline contract tests for the advisory Codex plugin bundle."""

from __future__ import annotations

import json
import mmap
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import Graph, Node, NodeType
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.integrations.agent_host.advisory import codex_conversation_ref

REPO_ROOT = Path(__file__).parents[3]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "intent-advisor"
HOOK = PLUGIN_ROOT / "scripts" / "prompt-hook"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
FALLBACK = (
    "Intent advisory prompt routing is unavailable. Do not mutate the intent graph, "
    "infer authorization, or treat this advisory as enforcement."
)


def _official_event(
    project: Path,
    *,
    prompt: str = "Add team sharing",
    session_id: str = "codex:thread-3",
    turn_id: str = "turn-7",
    agent_id: str | None = None,
    agent_type: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "session_id": session_id,
        "transcript_path": None,
        "cwd": str(project),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-5.6-sol",
        "turn_id": turn_id,
        "permission_mode": "default",
        "prompt": prompt,
    }
    if agent_id is not None:
        event["agent_id"] = agent_id
    if agent_type is not None:
        event["agent_type"] = agent_type
    return event


def _run_hook(
    project: Path,
    payload: bytes,
    *,
    path: str | None = None,
    timeout: float = 5,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["PATH"] = path or f"{REPO_ROOT / '.venv' / 'bin'}:/usr/bin:/bin"
    return subprocess.run(
        [str(HOOK)],
        cwd=project,
        env=environment,
        input=payload,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _installed_hook_schema(kind: str) -> dict[str, object]:
    binary = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    marker = f'"title": "user-prompt-submit.command.{kind}"'.encode()
    prefix = b'{\n  "$schema": "http://json-schema.org/draft-07/schema#"'
    with (
        binary.open("rb") as stream,
        mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        marker_at = data.find(marker)
        assert marker_at >= 0
        start = data.rfind(prefix, max(0, marker_at - 65536), marker_at)
        assert start >= 0
        decoded = data[start : start + 65536].decode("utf-8", errors="ignore")
    schema, _end = json.JSONDecoder().raw_decode(decoded)
    assert type(schema) is dict
    return schema


def _run_official_hook(
    project: Path,
    *,
    event_project: Path | None = None,
    prompt: str = "Add team sharing",
    path: str | None = None,
    timeout: float = 5,
) -> subprocess.CompletedProcess[bytes]:
    payload = _official_event(event_project or project, prompt=prompt)
    return _run_hook(
        project,
        json.dumps(payload, separators=(",", ":")).encode(),
        path=path,
        timeout=timeout,
    )


def _output(completed: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
    assert completed.returncode == 0
    assert completed.stderr == b""
    return json.loads(completed.stdout)


def _additional_context(completed: subprocess.CompletedProcess[bytes]) -> str:
    output = _output(completed)
    assert set(output) == {"hookSpecificOutput"}
    specific = output["hookSpecificOutput"]
    assert type(specific) is dict
    assert set(specific) == {"hookEventName", "additionalContext"}
    assert specific["hookEventName"] == "UserPromptSubmit"
    context = specific["additionalContext"]
    assert type(context) is str
    return context


def _durable_bytes(project: Path) -> dict[str, bytes]:
    workspace = project / ".intent"
    if not workspace.exists():
        return {}
    return {
        str(path.relative_to(workspace)): path.read_bytes()
        for path in sorted(workspace.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _ready_project(project: Path) -> None:
    initialize_project(project)
    runtime = load_runtime(project)
    runtime.graph_store.initialize(
        Graph(
            id="graph:plugin-test",
            version=1,
            name="Plugin test",
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


def _fake_intent(directory: Path, source: str) -> Path:
    executable = directory / "intent"
    executable.write_text(f"#!/usr/bin/python3\n{source}\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def test_advisory_plugin_declares_official_prompt_hook_and_repository_mcp() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_bytes())
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_bytes())
    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_bytes())

    assert manifest == {
        "name": "intent-advisor",
        "version": "0.1.0",
        "description": "Route Codex prompts through advisory Intent Engineering workflows.",
        "author": {"name": "Intent Engineering"},
        "skills": "./skills/",
        "interface": {
            "displayName": "Intent Advisor",
            "shortDescription": "Route work through repository intent.",
            "longDescription": (
                "Adds advisory onboarding, preflight, and clarification guidance backed by "
                "local Intent Engineering workflows."
            ),
            "developerName": "Intent Engineering",
            "category": "Developer Tools",
            "capabilities": [],
            "defaultPrompt": "Check this task against the repository intent workflow.",
        },
        "mcpServers": "./.mcp.json",
    }
    assert hooks == {
        "hooks": {
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "${PLUGIN_ROOT}/scripts/prompt-hook",
                            "timeout": 5,
                            "statusMessage": "Checking repository intent",
                            "additionalContextLimit": 2048,
                        }
                    ]
                }
            ]
        }
    }
    assert mcp == {
        "mcpServers": {
            "intent_advisor": {
                "command": "intent",
                "args": ["mcp", "--project", "."],
            }
        }
    }
    assert "cwd" not in mcp["mcpServers"]["intent_advisor"]
    assert stat.S_IMODE(HOOK.stat().st_mode) & stat.S_IXUSR


def test_real_hook_offers_onboarding_without_writes_or_prompt_echo(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    before = _durable_bytes(project)
    marker = "PRIVATE-UNINITIALIZED-PROMPT-8197"

    completed = _run_official_hook(project, prompt=marker)

    context = _additional_context(completed)
    assert "Start guided onboarding now?" in context
    assert marker not in completed.stdout.decode()
    assert _durable_bytes(project) == before


def test_real_hook_routes_ready_prompt_to_preflight_without_secret_or_echo(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _ready_project(project)
    before = _durable_bytes(project)
    marker = "PRIVATE-READY-PROMPT-8197"

    completed = _run_official_hook(project, prompt=marker)

    context = _additional_context(completed)
    assert "intent_advisory_preflight" in context
    assert "request_evidence_ref=" in context
    assert "current human request" not in context
    expected_ref = codex_conversation_ref("codex:thread-3", "turn-7", marker)
    assert f'conversation_ref="{expected_ref}"' in context
    lowered = completed.stdout.decode().casefold()
    assert marker.casefold() not in lowered
    assert "authorization" not in lowered
    assert "capability" not in lowered
    assert "token" not in lowered
    after = _durable_bytes(project)
    assert after.keys() - before.keys() == {".config.yaml.lock", "evidence/evidence.jsonl"}
    assert "evidence/evidence.jsonl" not in before
    assert after["evidence/evidence.jsonl"]
    for path in before:
        assert after[path] == before[path]
    captured = load_runtime(project).evidence_store.ledger("conversation:codex")
    assert len(captured) == 1
    assert captured[0].evidence.external_object_id == expected_ref
    assert captured[0].evidence.payload == {"role": "human", "content": marker}


def test_real_hook_uses_retry_stable_turn_specific_ref_without_host_id_leakage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _ready_project(project)
    session_marker = "PRIVATE:SESSION:8197"
    turn_marker = "PRIVATE:TURN:8197"
    first = _official_event(
        project,
        session_id=session_marker,
        turn_id=turn_marker,
    )
    next_turn = _official_event(
        project,
        session_id=session_marker,
        turn_id="PRIVATE:TURN:8198",
    )
    altered_same_turn = _official_event(
        project,
        prompt="Add team export",
        session_id=session_marker,
        turn_id=turn_marker,
    )

    first_context = _additional_context(
        _run_hook(project, json.dumps(first, separators=(",", ":")).encode())
    )
    after_first = _durable_bytes(project)
    retry_context = _additional_context(
        _run_hook(project, json.dumps(first, separators=(",", ":")).encode())
    )
    assert _durable_bytes(project) == after_first
    next_context = _additional_context(
        _run_hook(project, json.dumps(next_turn, separators=(",", ":")).encode())
    )
    after_next = _durable_bytes(project)
    altered_context = _additional_context(
        _run_hook(project, json.dumps(altered_same_turn, separators=(",", ":")).encode())
    )

    assert first_context == retry_context
    assert first_context != next_context
    assert altered_context == FALLBACK
    assert _durable_bytes(project) == after_next
    combined = first_context + retry_context + next_context + altered_context
    assert session_marker not in combined
    assert turn_marker not in combined
    assert "PRIVATE:TURN:8198" not in combined


def test_real_hook_accepts_installed_official_optional_identity_and_multiline_prompt(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _ready_project(project)
    event = _official_event(
        project,
        prompt="Add team sharing\n\tfor workspace admins",
        agent_id="host-agent-7",
        agent_type="reviewer",
    )
    input_schema = _installed_hook_schema("input")
    Draft7Validator(input_schema).validate(event)

    completed = _run_hook(project, json.dumps(event).encode())
    output = _output(completed)

    Draft7Validator(_installed_hook_schema("output")).validate(output)
    context = _additional_context(completed)
    assert "intent_advisory_preflight" in context
    assert "host-agent-7" not in context
    assert "reviewer" not in context
    assert "workspace admins" not in context


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.pop("turn_id"),
        lambda payload: payload.update({"unknown": True}),
        lambda payload: payload.update({"hook_event_name": "PreToolUse"}),
        lambda payload: payload.update({"permission_mode": "root"}),
    ],
)
def test_malformed_official_event_returns_one_fixed_advisory_fallback(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], object],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    payload = _official_event(project, prompt="PRIVATE-MALFORMED-PROMPT-8197")
    mutation(payload)

    completed = _run_hook(project, json.dumps(payload).encode())

    assert _additional_context(completed) == FALLBACK
    assert b"PRIVATE-MALFORMED-PROMPT-8197" not in completed.stdout


def test_invalid_utf8_event_returns_fixed_advisory_fallback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    completed = _run_hook(project, b'{"prompt":"PRIVATE-UTF8-8197\xff"}')

    assert _additional_context(completed) == FALLBACK
    assert b"PRIVATE-UTF8-8197" not in completed.stdout + completed.stderr


def test_missing_cli_returns_fixed_fallback_with_strict_streams(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    completed = _run_official_hook(project, path="/usr/bin:/bin")

    assert _additional_context(completed) == FALLBACK


def test_unbounded_child_output_returns_bounded_fallback(tmp_path: Path) -> None:
    project = tmp_path / "project"
    fake_bin = tmp_path / "fake-bin"
    project.mkdir()
    fake_bin.mkdir()
    marker = "PRIVATE-CHILD-OUTPUT-8197"
    _fake_intent(
        fake_bin,
        "import sys, time\n"
        f"sys.stderr.write('{marker}')\n"
        "sys.stdout.write('x' * 131072)\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)",
    )

    started = time.monotonic()
    completed = _run_official_hook(project, path=f"{fake_bin}:/usr/bin:/bin", timeout=5)

    assert time.monotonic() - started < 4
    assert _additional_context(completed) == FALLBACK
    assert len(completed.stdout) <= 4096
    assert marker.encode() not in completed.stdout + completed.stderr


def test_child_timeout_returns_fixed_fallback_promptly(tmp_path: Path) -> None:
    project = tmp_path / "project"
    fake_bin = tmp_path / "fake-bin"
    project.mkdir()
    fake_bin.mkdir()
    marker = "PRIVATE-TIMED-OUT-CHILD-8197"
    _fake_intent(
        fake_bin,
        f"import sys, time\nsys.stderr.write('{marker}')\nsys.stderr.flush()\ntime.sleep(30)",
    )

    started = time.monotonic()
    completed = _run_official_hook(project, path=f"{fake_bin}:/usr/bin:/bin", timeout=5)

    assert 1.5 < time.monotonic() - started < 4
    assert _additional_context(completed) == FALLBACK
    assert marker.encode() not in completed.stdout + completed.stderr


def test_cross_project_session_reuse_returns_fallback_without_writes(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    attacker = tmp_path / "attacker"
    selected.mkdir()
    attacker.mkdir()
    _ready_project(selected)
    _ready_project(attacker)
    selected_before = _durable_bytes(selected)
    attacker_before = _durable_bytes(attacker)

    completed = _run_official_hook(selected, event_project=attacker)

    assert _additional_context(completed) == FALLBACK
    assert _durable_bytes(selected) == selected_before
    assert _durable_bytes(attacker) == attacker_before


@pytest.mark.parametrize("attack", ["fifo", "symlink"])
def test_special_project_files_fail_promptly_without_replacement(
    tmp_path: Path,
    attack: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    initialize_project(project)
    config = project / ".intent" / "config.yaml"
    config.unlink()
    if attack == "fifo":
        os.mkfifo(config)
    else:
        outside = tmp_path / "outside.yaml"
        outside.write_text("PRIVATE-SPECIAL-FILE-8197", encoding="utf-8")
        config.symlink_to(outside)
    before = os.lstat(config)

    completed = _run_official_hook(project, timeout=3)

    assert _additional_context(completed) == FALLBACK
    after = os.lstat(config)
    assert stat.S_IFMT(after.st_mode) == stat.S_IFMT(before.st_mode)


def test_hook_cancellation_terminates_owned_cli_and_emits_no_child_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    fake_bin = tmp_path / "fake-bin"
    pid_file = tmp_path / "child.pid"
    project.mkdir()
    fake_bin.mkdir()
    marker = "PRIVATE-CANCELLED-CHILD-8197"
    _fake_intent(
        fake_bin,
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"sys.stderr.write('{marker}')\n"
        "sys.stderr.flush()\n"
        "time.sleep(30)",
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    process = subprocess.Popen(
        [sys.executable, str(HOOK)],
        cwd=project,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(_official_event(project)).encode())
    process.stdin.close()
    process.stdin = None
    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=3)

    assert process.returncode == 0
    assert stderr == b""
    assert marker.encode() not in stdout
    assert (
        _additional_context(
            subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
        )
        == FALLBACK
    )
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_hook_reaps_process_group_when_cli_leader_exits_before_child(tmp_path: Path) -> None:
    project = tmp_path / "project"
    fake_bin = tmp_path / "fake-bin"
    pid_file = tmp_path / "grandchild.pid"
    project.mkdir()
    fake_bin.mkdir()
    _fake_intent(
        fake_bin,
        "import os, pathlib, signal, sys, time\n"
        "child = os.fork()\n"
        "if child:\n"
        "    raise SystemExit(1)\n"
        "sys.stdin.close(); sys.stdout.close(); sys.stderr.close()\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
        "time.sleep(30)",
    )

    completed = _run_official_hook(project, path=f"{fake_bin}:/usr/bin:/bin", timeout=5)

    assert _additional_context(completed) == FALLBACK
    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_plugin_configured_mcp_missing_project_is_protocol_clean(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    mcp = json.loads((PLUGIN_ROOT / ".mcp.json").read_bytes())["mcpServers"]["intent_advisor"]
    executable = REPO_ROOT / ".venv" / "bin" / mcp["command"]

    completed = subprocess.run(
        [str(executable), *mcp["args"]],
        cwd=project,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b"intent error: MCP server failed\n"


def test_skill_routes_clarifications_and_states_advisory_mcp_failure_boundary() -> None:
    skill = (PLUGIN_ROOT / "skills" / "intent-advisor" / "SKILL.md").read_text(encoding="utf-8")

    assert "action=answer_clarification" in skill
    assert "intent_clarification_answer" in skill
    assert "Do not classify the answer again" in skill
    assert "action=review_clarification_proposal" in skill
    assert "intent_clarification_show" in skill
    assert "proposal_digest" in skill
    assert "decline" in skill.casefold()
    assert "no_semantic_impact" in skill
    assert "new_or_ambiguous" in skill
    assert "conflicting" in skill
    assert "mechanical" not in skill.casefold()
    assert "non_requirement" not in skill.casefold()
    assert "ask the user for the PRD path" in skill
    assert "intent onboard --project . --prd <confirmed path> --yes" in skill
    assert "request_evidence_ref" in skill
    assert "answer_evidence_ref" in skill
    assert "current human request" not in skill
    assert "current human prompt as `answer`" not in skill
    assert "intent_advisory_preflight" in skill
    assert "intent_preflight" not in skill.replace("intent_advisory_preflight", "")
    assert "MCP tools are unavailable" in skill
    assert "MandatoryHookUnavailable" in skill
    assert "capability token" in skill
    assert "advisory" in skill.casefold()
