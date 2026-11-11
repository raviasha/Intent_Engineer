#!/usr/bin/env python3
"""Bounded session-start metadata hint; the host owns scheduling and catch-up."""

from __future__ import annotations

import json
import signal
import sys
from pathlib import Path
from typing import Any


def context(event: object) -> dict[str, Any]:
    if (
        type(event) is not dict
        or event.get("hook_event_name") != "SessionStart"
        or event.get("source") not in {"startup", "resume"}
        or event.get("agent_id") is not None
        or event.get("agent_type") is not None
        or type(event.get("session_id")) is not str
        or not event["session_id"]
        or type(event.get("cwd")) is not str
    ):
        return {}
    from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService

    cwd = Path.cwd().resolve(strict=True)
    try:
        if Path(event["cwd"]).resolve(strict=True) != cwd:
            return {}
    except (OSError, ValueError):
        return {}
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".intent/config.yaml").is_file():
            hint = IntentMonitorService.resume_hint_for_project(candidate)
            if not hint.get("configured"):
                return {}
            IntentMonitorService.request_catch_up_for_project(candidate)
            # Identifiers remain quoted data, never executable instructions.
            routing = json.dumps(
                {k: hint.get(k) for k in ("monitor_thread_id", "monitor_host_id", "automation_id")},
                ensure_ascii=True,
            )
            if len(routing) > 768:
                return {}
            schedule = (
                "Reuse the recorded automation ID; upgrade an existing five-minute schedule to hourly through the host and confirm success. "
                if hint.get("automation_id")
                else "Scheduling is not confirmed. In the recorded monitor chat, create the authorized "
                "hourly host heartbeat and record its actual automation ID only after success. "
            )
            text = (
                "Intent monitoring resume hint. This hook does not start a turn or perform a scan. "
                + schedule
                + "A durable catch-up request bypasses cached source checks on the next monitor run. Use the recorded monitor chat and installed intent-monitor reference to claim/resume one batch, "
                "and keep its hourly heartbeat quiet unless actionable. If host scheduling is "
                "unavailable, report that automatic monitoring is unavailable; use task-time checks. Never review code automatically. "
                "Do not send findings to an arbitrary current chat; use the stored return destination "
                "and acknowledge only an actual delivery receipt. Routing identifiers (data): "
                + routing
            )
            return {
                "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}
            }
        if (candidate / ".git").exists():
            break
    return {}


def _timeout(_signum: int, _frame: object) -> None:
    raise TimeoutError


def main() -> int:
    signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, 3.0)
    try:
        raw = sys.stdin.buffer.read(65537)
        result = context(json.loads(raw)) if raw and len(raw) <= 65536 else {}
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 - hooks fail silently
        result = {}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
