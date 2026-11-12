"""Explicit MCP surface for local conversational intent maintenance."""

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from intent_engineering.cli.runtime import Runtime
from intent_engineering.intent_workflow.background import BackgroundIntentService
from intent_engineering.intent_workflow.background_models import BackgroundSubmission
from intent_engineering.intent_workflow.background_sources import BackgroundSource, capture_source
from intent_engineering.intent_workflow.initial_reconciliation import InitialReconciliationService
from intent_engineering.intent_workflow.initial_reconciliation_models import (
    InitialReconciliationRequest,
)
from intent_engineering.intent_workflow.intent_monitor import IntentMonitorService, MonitorRequest
from intent_engineering.intent_workflow.setup_presentation import with_setup_chat_update
from intent_engineering.intent_workflow.source_refresh import (
    RefreshRequest,
    SourceRefreshService,
    SourceSelection,
    SourceSignal,
)


def register_background_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_initial_reconciliation(draft: InitialReconciliationRequest) -> dict[str, Any]:
        """Start, checkpoint, inspect or finish background intent/requirements/code comparison.

        The host reads selected sources and interprets meaning in the setup chat while normal
        work continues. Use explicit scoped files, cite snapshot evidence, review all returned
        missing comparison slots, and checkpoint to resume. Finish atomically publishes graph
        traces and issues only after complete current comparisons. No assessed code is written.
        Source intent has priority; test-to-code tracing remains deferred. This tool never
        launches a worker or claims that graph links prove semantic satisfaction.
        In the setup chat, render each changed chat_update milestone even when notify_user is
        false (that flag concerns final findings). On complete, automatically show findings,
        recommendations, evidence, scope and limitations; do not wait for a user status request.
        Deduplicate repeated update IDs. Keep working until ready or explain what blocks setup.
        """
        try:
            return with_setup_chat_update(InitialReconciliationService(runtime).run(draft))
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("initial reconciliation unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_monitor(draft: MonitorRequest) -> dict[str, Any]:
        """Configure and drive periodic intent/PRD checks in the existing host setup chat.

        The host owns scheduling and reads selected sources with its authorized tools. Record
        automation_id only after actual host scheduler success; use hourly scheduling and update
        existing five-minute schedules in place. Use a stable worker_id for each host run across
        begin/finish and sender status/acknowledge. A busy result means another worker owns the
        batch: stop without provider reads or delivery. Each scheduled hourly begin requires
        force_refresh=True; task-time and within-batch checks can reuse validated receipts. Task
        alignment still runs. Reopen catch-up bypasses cache via durable hook metadata/force_refresh.
        Begin resumes a cycle and returns
        source-refresh work plus verified PRD evidence; use the returned session_id and cycle_id
        for the evidence-backed background interpretation. Finish validates current evidence.
        Stay silent for unchanged/aligned cycles. Deliver only a returned actionable handoff
        to its recorded original agent with trusted human authorization and live matching-worker
        sender status with delivery_authorized=True. Retain the lease until confirmed receipt is
        acknowledged. Status without a worker is read-only for the recipient, who may acknowledge
        an actual received packet without claiming sender rights. Preserve the default model. No code review or document/code edits.
        """
        try:
            return IntentMonitorService(runtime).run(draft)
        except Exception:  # noqa: BLE001 - redacted public boundary
            raise ToolError("intent monitoring unavailable") from None

    service = BackgroundIntentService(runtime)
    refresh = SourceRefreshService(runtime)

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_sources(selection: SourceSelection | None = None) -> dict[str, Any]:
        """List bounded source scopes or record their explicit user designation and authority.

        Does not connect a provider or subscribe to notifications. Never infer a selection
        from arbitrary old evidence. Omit selection to list; enabled=false disables a scope.
        """
        try:
            return refresh.sources(selection)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("source refresh unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_source_signal(draft: SourceSignal) -> dict[str, Any]:
        """Invalidate a selected source after a real provider change notification.

        This is a local bridge, not an authenticated webhook listener. Event silence does
        not establish freshness. Signals never change intent or make a source fresh.
        """
        try:
            return refresh.signal(draft)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("source refresh unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_refresh(draft: RefreshRequest) -> dict[str, Any]:
        """Start a dev-task refresh, then submit actual provider checks and interpretation.

        First call without observations. Host tools perform bounded reads outside this tool.
        A metadata validator must cover the whole scope including edits, membership,
        deletions and access changes. Otherwise fetch content; complete=false cannot be fresh.
        Use returned generations. Cite required_evidence_refs in a current background
        interpretation, then pass its evidence_ref as reconciliation_ref. A ready result
        verifies this cooperative workflow, not arbitrary editor or shell actions.
        """
        try:
            return refresh.refresh(draft)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("source refresh unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_capture_source(draft: BackgroundSource) -> dict[str, str]:
        """Preserve a real authorized connector read with source provenance, locally.

        This records host-reported evidence. It does not connect or authenticate a provider.
        """
        try:
            return capture_source(runtime, draft)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("background source capture unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_context(task: str, session_id: str) -> dict[str, Any]:
        """Read relevant intent, prior chat answers and pending data without a review gate."""
        try:
            return service.context(task=task, session_id=session_id)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("background intent context unavailable") from None

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )
    )
    def intent_background_submit(draft: BackgroundSubmission) -> dict[str, Any]:
        """Record a chat question/answer or apply clear intent with evidence and ChangeSet history.

        This local maintenance path creates no approvals and leaves legacy proposals intact.
        User words are agent-reported statements; agent inferences remain inferred. Ask only
        consequential missing-meaning or conflict questions in the working chat, reusing answers.
        Capture supported meaning during brainstorming without waiting for a capture request or code
        task, including features absent from connected sources. For mixed clear intent and a gap,
        submit the clear portion first, then a question-only submission at the new graph version.
        Derive distinct stable turn IDs for capture, question and source interpretation; exact
        retries reuse their ID. Agent suggestions and ambiguous replies are not user decisions.
        """
        try:
            return service.submit(draft)
        except Exception:  # noqa: BLE001 - fixed public boundary
            raise ToolError("background intent update unavailable") from None
