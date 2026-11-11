"""Run with packaged Python: exercise a relocated installation using synthetic data."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.policy.project import initialize_project
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_models import BackgroundSubmission
from intent_engineering.intent_workflow.code_reconciliation import CodeReconciliationService
from intent_engineering.intent_workflow.initial_reconciliation import InitialReconciliationService
from intent_engineering.intent_workflow.initial_reconciliation_models import (
    Comparison,
    InitialReconciliationRequest,
)
from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService, MonitorRequest

NOW = datetime(2026, 11, 10, tzinfo=UTC)


class FixtureReviewer:
    """Deterministic semantic fixture; packaging tests never invoke a paid model."""

    def review(self, inputs, slots):
        code = [item["node_id"] for item in inputs["code_refs"]]
        rows = []
        for slot in slots:
            related = {
                "intent_requirements": ["req:local"],
                "requirements_intent": ["intent:privacy"],
                "requirements_code": code,
                "code_requirements": ["req:local"],
                "intent_code": code,
                "document_requirements": ["req:local"],
            }[slot["axis"]]
            gap = slot["axis"] == "requirements_code"
            rows.append(
                Comparison.model_validate(
                    {
                        **slot,
                        "related_refs": related,
                        "status": "partial" if gap else "aligned",
                        "evidence_refs": inputs["evidence_refs"],
                        "explanation": "Synthetic local-storage comparison.",
                        "authority_basis": "Captured local-only intent.",
                        "recommendation": "Restore local storage." if gap else None,
                        "recommendation_target": "code" if gap else None,
                        "confidence": 0.8,
                    }
                )
            )
        return tuple(rows)


async def check_mcp(launcher, project, environment):
    server = StdioServerParameters(
        command=str(launcher), args=["mcp", "--project", str(project)], env=environment
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        result = await session.initialize()
        assert result.server_info.name
        assert (await session.list_tools()).tools
        assessment = await session.call_tool(
            "intent_initial_reconciliation",
            {"draft": {"action": "status", "session_id": "smoke", "turn_id": "status"}},
        )
        update = assessment.structured_content["chat_update"]
        assert update["kind"] == "complete"
        assert update["findings"][0]["recommendation"] == "Restore local storage."


def smoke(plugin: Path) -> None:
    launcher = plugin / "scripts/intent"
    # No global intent, developer venv, Python or Codex CLI path is inherited.
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    with tempfile.TemporaryDirectory(prefix="intent-package-smoke-") as temp:
        root = Path(temp).resolve()
        project = root / "existing project"
        project.mkdir()
        environment["XDG_STATE_HOME"] = str(root / "state")
        environment["HOME"] = str(root / "home")
        os.environ["XDG_STATE_HOME"] = environment["XDG_STATE_HOME"]
        (root / "home").mkdir()
        help_result = subprocess.run(
            [str(launcher), "--help"],
            cwd=root,
            env=environment,
            capture_output=True,
            check=True,
            timeout=20,
        )
        assert b"reconcile-code" in help_result.stdout
        for name in ("prompt-hook", "stop-hook", "reconcile-hook", "monitor-hook"):
            result = subprocess.run(
                [str(plugin / "scripts" / name)],
                input=b"{}",
                env=environment,
                cwd=project,
                capture_output=True,
                check=True,
                timeout=20,
            )
            assert isinstance(json.loads(result.stdout), dict)
        initialize_project(project)
        (project / "prd.md").write_text("Store project data locally.")
        (project / "app.py").write_text("def store(data):\n    return data\n")
        runtime = load_runtime(project)
        try:
            BackgroundIntentService(runtime, clock=lambda: NOW).submit(
                BackgroundSubmission.model_validate(
                    {
                        "session_id": "setup",
                        "turn_id": "intent",
                        "graph_version": 0,
                        "user_statement": "Keep data local. Store project data locally.",
                        "classification": "clear",
                        "understanding": "Synthetic privacy fixture.",
                        "nodes": [
                            {
                                "id": ident,
                                "type": kind,
                                "label": label,
                                "source_mode": "explicit",
                                "confidence": 0.95,
                                "basis": "Synthetic user statement",
                            }
                            for ident, kind, label in [
                                ("intent:privacy", "PRODUCT_INTENT", "Keep data local."),
                                ("req:local", "REQUIREMENT", "Store project data locally."),
                            ]
                        ],
                    }
                )
            )
            service = InitialReconciliationService(runtime, clock=lambda: NOW)

            def call(action, **kwargs):
                return service.run(
                    InitialReconciliationRequest.model_validate(
                        {"action": action, "session_id": "setup", "turn_id": action, **kwargs}
                    )
                )

            started = call(
                "start",
                scope="Synthetic project",
                files=[
                    {"path": "prd.md", "role": "requirements"},
                    {"path": "app.py", "role": "code"},
                ],
            )
            inputs = service.inputs(started["run_id"])
            code = inputs["code_refs"][0]["node_id"]
            doc = inputs["requirements_refs"][0]["node_id"]
            slots = [
                {"axis": axis, "subject_ref": subject}
                for axis, subject in [
                    ("intent_requirements", "intent:privacy"),
                    ("requirements_intent", "req:local"),
                    ("requirements_code", "req:local"),
                    ("code_requirements", code),
                    ("intent_code", "intent:privacy"),
                    ("document_requirements", doc),
                ]
            ]
            comparisons = [
                item.model_dump(mode="json") for item in FixtureReviewer().review(inputs, slots)
            ]
            for item in comparisons:
                item.update(status="aligned", recommendation=None, recommendation_target=None)
            call("checkpoint", run_id=started["run_id"], comparisons=comparisons)
            assert call("finish", run_id=started["run_id"])["status"] == "ready"
            changed = "def store(data):\n    send_to_cloud(data)\n"
            (project / "app.py").write_text(changed)
            result = CodeReconciliationService(
                runtime, runner=FixtureReviewer(), clock=lambda: NOW, sleeper=lambda _: None
            ).run()
            assert result["status"] == "ready_with_findings", result
            assert result["notify_user"] is False
            assert "chat_update" not in result
            public = subprocess.run(
                [str(launcher), "initial-reconciliation", "--project", str(project)],
                env=environment,
                capture_output=True,
                check=True,
                timeout=20,
            )
            update = json.loads(public.stdout)["chat_update"]
            assert update["kind"] == "complete"
            assert update["findings"][0]["recommendation"] == "Restore local storage."
            onboarding = [str(launcher), "background-onboarding", "--project", str(project)]
            for options in (
                [
                    "--prepare",
                    "--return-thread-id",
                    "smoke-parent",
                    "--return-session-id",
                    "smoke-session",
                ],
                ["--chat-id", "smoke-setup"],
                ["--finished", "--assessment-run-id", result["run_id"]],
            ):
                packet = json.loads(
                    subprocess.run(
                        [*onboarding, *options],
                        env=environment,
                        capture_output=True,
                        check=True,
                        timeout=20,
                    ).stdout
                )
            handoff = packet["return_handoff"]
            assert handoff["status"] == "pending" and handoff["thread_id"] == "smoke-parent"
            assert (
                handoff["assessment"]["findings"][0]["recommendation"] == "Restore local storage."
            )
            acknowledged = json.loads(
                subprocess.run(
                    [*onboarding, "--handoff-delivered", handoff["id"]],
                    env=environment,
                    capture_output=True,
                    check=True,
                    timeout=20,
                ).stdout
            )
            assert acknowledged["return_handoff"]["status"] == "delivered"
            assert acknowledged["return_handoff"]["message_sent_by_runtime"] is False
            monitor = IntentMonitorService(runtime, clock=lambda: NOW)
            monitor.run(
                MonitorRequest(
                    action="configure",
                    prd_paths=("prd.md",),
                    session_id="smoke-monitor",
                    monitor_thread_id="smoke-setup",
                    monitor_host_id="smoke-host",
                    return_thread_id="smoke-parent",
                    return_host_id="smoke-host",
                )
            )
            cycle = monitor.run(MonitorRequest(action="begin", worker_id="smoke-worker"))
            assert cycle["scheduling_packet"]["interval_minutes"] == 60
            assert (
                monitor.run(MonitorRequest(action="begin", worker_id="other-worker"))["status"]
                == "busy"
            )
            review = BackgroundIntentService(runtime, clock=lambda: NOW).submit(
                BackgroundSubmission.model_validate(
                    {
                        "session_id": "smoke-monitor",
                        "turn_id": cycle["cycle_id"],
                        "graph_version": runtime.graph_store.load().version,
                        "user_statement": "Store project data locally.",
                        "classification": "aligned",
                        "understanding": "PRD remains aligned.",
                        "relevant_node_ids": ["intent:privacy", "req:local"],
                        "evidence_refs": cycle["required_evidence_refs"],
                    }
                )
            )
            done = monitor.run(
                MonitorRequest(
                    action="finish",
                    worker_id="smoke-worker",
                    cycle_id=cycle["cycle_id"],
                    interpretation_ref=review["evidence_ref"],
                )
            )
            assert done["status"] == "quiet" and not done["schedule_confirmed"]
            assert (
                monitor.run(MonitorRequest(action="begin", worker_id="smoke-next"))["status"]
                == "quiet"
            )
            (project / "prd.md").write_text("Store project data remotely.")
            changed_cycle = monitor.run(MonitorRequest(action="begin", worker_id="smoke-delivery"))
            recommendation = BackgroundIntentService(runtime, clock=lambda: NOW).submit(
                BackgroundSubmission.model_validate(
                    {
                        "session_id": "smoke-monitor",
                        "turn_id": changed_cycle["cycle_id"],
                        "graph_version": runtime.graph_store.load().version,
                        "user_statement": "Keep data local.",
                        "classification": "clear",
                        "understanding": "Restore the PRD local-storage requirement.",
                        "relevant_node_ids": ["intent:privacy", "req:local"],
                        "evidence_refs": changed_cycle["required_evidence_refs"],
                    }
                )
            )
            pending = monitor.run(
                MonitorRequest(
                    action="finish",
                    worker_id="smoke-delivery",
                    cycle_id=changed_cycle["cycle_id"],
                    interpretation_ref=recommendation["evidence_ref"],
                    actionable=True,
                )
            )
            assert pending["status"] == "actionable"
            assert (
                monitor.run(MonitorRequest(action="begin", worker_id="competitor"))["status"]
                == "busy"
            )
            received = monitor.run(MonitorRequest(action="status"))
            assert received["handoff"] and not received["delivery_authorized"]
            monitor.run(
                MonitorRequest(
                    action="acknowledge",
                    worker_id="smoke-delivery",
                    cycle_id=changed_cycle["cycle_id"],
                    handoff_id=pending["handoff"]["handoff_id"],
                    delivery_receipt="fixture:confirmed-delivery",
                )
            )
            assert monitor.run(MonitorRequest(action="status"))["handoff"] is None
            automatic = CodeReconciliationService(runtime).run(automatic=True)
            assert automatic["status"] == "on_demand_only"
            assert (project / "app.py").read_text() == changed
            from intent_engineering.cli.dev import _start_or_reuse_background_service
            from intent_engineering.intent_workflow.check import SharedStateRestoreStatus

            assert _start_or_reuse_background_service(
                project, SharedStateRestoreStatus.NOT_REQUIRED
            )
            anyio.run(check_mcp, launcher, project, environment)
            event = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "smoke",
                "turn_id": "smoke-turn",
                "cwd": str(project),
                "prompt": "Format README",
                "transcript_path": None,
                "model": "test",
                "permission_mode": "default",
            }
            result = subprocess.run(
                [str(plugin / "scripts/prompt-hook")],
                input=json.dumps(event).encode(),
                cwd=project,
                env=environment,
                capture_output=True,
                check=True,
                timeout=20,
            )
            context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
            assert "unavailable" not in context.lower(), context
            assert "intent_background_refresh" in context, context
        finally:
            runtime.close()
            for metadata in project.rglob("control-plane.json"):
                try:
                    pid = json.loads(metadata.read_text())["pid"]
                    if isinstance(pid, int) and pid > 1:
                        os.kill(pid, signal.SIGINT)
                except (OSError, ValueError, KeyError):
                    pass
    print("PASS: relocated CLI, MCP, hooks and read-only synthetic reconciliation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    smoke(parser.parse_args().plugin.resolve())
