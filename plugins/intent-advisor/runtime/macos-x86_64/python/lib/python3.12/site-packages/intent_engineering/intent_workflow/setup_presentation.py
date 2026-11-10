"""Read-only setup-chat presentation; never applied to silent worker results."""

import hashlib
import json
from typing import Any


def with_setup_chat_update(result: dict[str, Any]) -> dict[str, Any]:
    """Describe what the host should show, without claiming delivery or editing state."""
    status = result["status"]
    ready = status in {"ready", "ready_with_findings"}
    accessible = status != "unavailable"
    messages = {
        "not_started": "Graph capture does not complete reconciliation. Assessment has not started.",
        "running": "The graph is captured; intent, requirements and code comparison is in progress.",
        "incomplete": "Assessment is incomplete; comparisons or source evidence remain unresolved.",
        "stale": "Assessment inputs changed. Refresh and reassess before reporting current findings.",
        "unavailable": "Assessment is unavailable. Authorized evidence access is required.",
        "ready": "Assessment complete. No gaps found within the assessed scope.",
        "ready_with_findings": "Assessment complete. Review the gaps and recommended changes below.",
    }
    actions = {
        "not_started": "Start the initial reconciliation with explicit source and file scope.",
        "running": "Continue reviewing and checkpointing remaining comparisons, then call finish.",
        "incomplete": "Resolve missing or unknown comparisons and pending source reads, then finish.",
        "stale": "Re-read changed inputs and start a new assessment with a new turn ID.",
        "unavailable": "Restore authorized access or assess a newly selected accessible scope.",
    }
    reviewed = result.get("completed_comparisons", 0) if accessible else 0
    update: dict[str, Any] = {
        "kind": "complete" if ready else "progress" if status == "running" else "needs_attention",
        "status": status,
        "message": messages[status],
        "scope": result.get("scope") if accessible else None,
        "reviewed": reviewed,
        "total": reviewed + len(result.get("missing_comparisons", [])) if accessible else 0,
        "unknown": sum(row["status"] == "unknown" for row in result.get("comparisons", []))
        if accessible
        else 0,
        "findings": result.get("findings", []) if ready else [],
        "limitations": result.get("limitations", []) if accessible else [],
        "next_action": "Present findings, recommendations, evidence and limitations in this setup "
        "chat now. Recommendations do not authorize code or source edits."
        if ready
        else actions[status],
    }
    identity = {"run_id": result.get("run_id") if accessible else None, **update}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    update["id"] = f"setup-chat:{digest}"
    return {**result, "chat_update": update}
