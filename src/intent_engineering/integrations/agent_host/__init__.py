"""Host-neutral intent-aware agent lifecycle contracts."""

from intent_engineering.integrations.agent_host.base import (
    AgentHostAdapter,
    HostTask,
    HostTaskResult,
    IntentAgentHostAdapter,
    IntentWorkflowPort,
    MandatoryHookUnavailable,
    MutationDecision,
    MutationReason,
)

__all__ = [
    "AgentHostAdapter",
    "HostTask",
    "HostTaskResult",
    "IntentAgentHostAdapter",
    "IntentWorkflowPort",
    "MandatoryHookUnavailable",
    "MutationDecision",
    "MutationReason",
]
