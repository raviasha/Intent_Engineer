"""Manual inspection and silent asynchronous hook entry points."""

# ruff: noqa: B008

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from types import FrameType

import typer

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.code_reconciliation import CodeReconciliationService


def reconcile_code_command(
    project: Path = typer.Option(Path("."), "--project"),
    run: bool = typer.Option(False, "--run"),
    enable: bool = typer.Option(False, "--enable"),
    disable: bool = typer.Option(False, "--disable"),
) -> None:
    """Inspect background status, run a review, or control automatic reviews locally."""
    runtime = None
    try:
        if sum((run, enable, disable)) > 1:
            raise ValueError("choose one operation")
        runtime = load_runtime(project.resolve(strict=True))
        service = CodeReconciliationService(runtime)
        result = (
            service.configure(enabled=enable)
            if enable or disable
            else (service.run() if run else service.status())
        )
        typer.echo(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        raise typer.BadParameter("background code reconciliation unavailable") from error
    finally:
        if runtime is not None:
            runtime.close()


def _cancel(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def agent_reconcile_hook_command() -> None:
    """Run only as a trusted async hook. Emit no guidance, warning or continuation."""
    runtime = None
    previous = signal.signal(signal.SIGTERM, _cancel)
    try:
        raw = sys.stdin.buffer.read(65537)
        if not raw or len(raw) > 65536:
            return
        event = json.loads(raw)
        if (
            type(event) is not dict
            or event.get("hook_event_name")
            not in {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"}
            or event.get("agent_id") is not None
            or event.get("agent_type") is not None
            or type(event.get("session_id")) is not str
            or not event["session_id"]
            or type(event.get("cwd")) is not str
        ):
            return
        cwd = Path.cwd().resolve(strict=True)
        if Path(event["cwd"]).resolve(strict=True) != cwd:
            return
        for candidate in (cwd, *cwd.parents):
            if (candidate / ".intent" / "config.yaml").is_file():
                runtime = load_runtime(candidate)
                CodeReconciliationService(runtime).run(automatic=True)
                break
            if (candidate / ".git").exists():
                break
    except (Exception, KeyboardInterrupt):  # noqa: BLE001, S110 - hook failures remain quiet
        pass
    finally:
        if runtime is not None:
            runtime.close()
        signal.signal(signal.SIGTERM, previous)
        typer.echo("{}")
