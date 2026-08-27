"""Bounded provider-neutral MCP port for reviewed intent onboarding."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Never, Protocol, cast

from mcp import MCPError
from mcp.server.mcpserver import MCPServer
from mcp.types import INVALID_PARAMS, ToolAnnotations
from pydantic import BeforeValidator, ConfigDict, Field, field_validator

from intent_engineering.cli.intent_workflow import (
    _bootstrap_service,
    _principals,
    _snapshot_config,
    proposal_payload,
)
from intent_engineering.cli.runtime import Runtime
from intent_engineering.core.models._base import StrictModel
from intent_engineering.intent_workflow.bootstrap import BootstrapSubmission

_PROPOSE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_SHOW = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_CONFIRM = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
_MAX_REQUEST_BYTES = 1_048_576
_MAX_CONFIRMED_NODES = 10_000
_PROPOSAL_ID = r"^proposal:sha256:[0-9a-f]{64}$"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
type _ProposalIdInput = Annotated[str, Field(pattern=_PROPOSAL_ID)]
type _DigestInput = Annotated[str, Field(pattern=_DIGEST)]
type _NodeIdsInput = Annotated[
    list[str],
    Field(min_length=1, max_length=_MAX_CONFIRMED_NODES),
]


def _submission_input(value: object) -> BootstrapSubmission:
    if isinstance(value, BootstrapSubmission):
        encoded = _canonical_json(value.model_dump(mode="json"))
    elif type(value) is dict:
        encoded = _canonical_json(value)
    else:
        raise ValueError("invalid workflow request")
    return BootstrapSubmission.model_validate_json(encoded)


type _BootstrapSubmissionInput = Annotated[
    BootstrapSubmission,
    BeforeValidator(_submission_input),
]


def _canonical_json(value: object) -> bytes:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise ValueError("workflow request is too large")
    return encoded


class _Request(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BootstrapProposeRequest(_Request):
    submission: BootstrapSubmission

    @field_validator("submission")
    @classmethod
    def require_bounded_submission(cls, value: BootstrapSubmission) -> BootstrapSubmission:
        _canonical_json(value.model_dump(mode="json"))
        return value


class ProposalShowRequest(_Request):
    proposal_id: Annotated[str, Field(pattern=_PROPOSAL_ID)]


class ProposalConfirmRequest(_Request):
    proposal_id: Annotated[str, Field(pattern=_PROPOSAL_ID)]
    proposal_digest: Annotated[str, Field(pattern=_DIGEST)]
    confirmed_node_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512)], ...],
        Field(min_length=1, max_length=_MAX_CONFIRMED_NODES),
    ]

    @field_validator("confirmed_node_ids")
    @classmethod
    def require_unique_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            any(ord(character) < 32 or ord(character) == 127 for character in value)
            for value in values
        ):
            raise ValueError("invalid workflow request")
        return values


class IntentWorkflowPort(Protocol):
    """Narrow workflow contract exposed to MCP registration."""

    async def bootstrap_propose(
        self, submission: BootstrapSubmission
    ) -> dict[str, object]: ...

    async def proposal_show(self, proposal_id: object) -> dict[str, object]: ...

    async def proposal_confirm(
        self,
        proposal_id: object,
        proposal_digest: object,
        confirmed_node_ids: object,
    ) -> dict[str, object]: ...


def _fixed_arguments() -> Never:
    raise MCPError(INVALID_PARAMS, "invalid intent workflow arguments") from None


class McpIntentWorkflowServices:
    """Production adapter over one held runtime and Task 3 governance service."""

    def __init__(self, runtime: Runtime, *, clock: Callable[[], datetime]) -> None:
        self.runtime = runtime
        self._clock = clock

    @staticmethod
    def _rejected() -> dict[str, object]:
        return {
            "schema_version": "1",
            "status": "rejected",
            "reason": "intent_workflow_unavailable",
        }

    async def bootstrap_propose(
        self, submission: BootstrapSubmission
    ) -> dict[str, object]:
        encoded: bytes | None = None
        candidate: BootstrapSubmission | None = None
        try:
            config, _ = _snapshot_config(self.runtime)
            encoded = _canonical_json(submission.model_dump(mode="json"))
            candidate = BootstrapSubmission.model_validate_json(encoded)
            review = _bootstrap_service(self.runtime, config).propose(
                candidate,
                _principals(self.runtime, config),
            )
            return {
                "schema_version": "1",
                "status": "proposed",
                "proposal_id": review.proposal_id,
                "proposal_digest": review.proposal_digest,
                "graph_version": review.baseline_graph_version,
            }
        except Exception:  # noqa: BLE001 - fixed public result has no submission detail
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            submission = cast(BootstrapSubmission, None)
            candidate = None
            encoded = None

    async def proposal_show(self, proposal_id: object) -> dict[str, object]:
        try:
            config, _ = _snapshot_config(self.runtime)
            if type(proposal_id) is not str or re.fullmatch(_PROPOSAL_ID, proposal_id) is None:
                return self._rejected()
            payload = proposal_payload(self.runtime, config, proposal_id)
            return {"schema_version": "1", "status": "proposed", "proposal": payload}
        except Exception:  # noqa: BLE001 - unauthorized and missing stay indistinguishable
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            proposal_id = None

    async def proposal_confirm(
        self,
        proposal_id: object,
        proposal_digest: object,
        confirmed_node_ids: object,
    ) -> dict[str, object]:
        try:
            config, config_bytes = _snapshot_config(self.runtime)
            request = ProposalConfirmRequest.model_validate(
                {
                    "proposal_id": proposal_id,
                    "proposal_digest": proposal_digest,
                    "confirmed_node_ids": confirmed_node_ids,
                }
            )
            preview = proposal_payload(self.runtime, config, request.proposal_id)
            if (
                preview["proposal_digest"] != request.proposal_digest
                or _snapshot_config(self.runtime) != (config, config_bytes)
            ):
                return self._rejected()
            at = self._clock()
            if at.tzinfo is None or at.utcoffset() is None:
                return self._rejected()
            graph = _bootstrap_service(self.runtime, config).activate(
                request.proposal_id,
                confirmed_node_ids=request.confirmed_node_ids,
                actor=config.local_actor,
                at=at.astimezone(UTC),
            )
            return {
                "schema_version": "1",
                "status": "activated",
                "proposal_id": request.proposal_id,
                "graph_version": graph.version,
            }
        except Exception:  # noqa: BLE001 - fixed public result has no confirmation detail
            return self._rejected()
        except BaseException as error:
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
            raise
        finally:
            proposal_id = proposal_digest = confirmed_node_ids = None


def load_intent_workflow_services(
    runtime: Runtime,
    *,
    clock: Callable[[], datetime] | None = None,
) -> McpIntentWorkflowServices:
    """Bind workflow tools to the exact already-held runtime snapshot."""
    return McpIntentWorkflowServices(
        runtime,
        clock=(lambda: datetime.now(UTC)) if clock is None else clock,
    )


def register_intent_workflow_tools(
    server: MCPServer,
    services: IntentWorkflowPort,
) -> None:
    """Register only proposal onboarding, inspection, and governed confirmation."""

    @server.tool(
        name="intent_bootstrap_propose",
        annotations=_PROPOSE,
        structured_output=True,
    )
    async def bootstrap_propose(submission: _BootstrapSubmissionInput) -> dict[str, object]:
        try:
            request = BootstrapProposeRequest.model_validate({"submission": submission})
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            submission = cast(BootstrapSubmission, None)
            _fixed_arguments()
        try:
            detached = BootstrapSubmission.model_validate_json(
                request.submission.model_dump_json()
            )
            return await services.bootstrap_propose(detached)
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            submission = cast(BootstrapSubmission, None)
            detached = cast(BootstrapSubmission, None)
            request = cast(BootstrapProposeRequest, None)

    @server.tool(
        name="intent_proposal_show",
        annotations=_SHOW,
        structured_output=True,
    )
    async def proposal_show(proposal_id: _ProposalIdInput) -> dict[str, object]:
        try:
            request = ProposalShowRequest.model_validate({"proposal_id": proposal_id})
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            proposal_id = ""
            _fixed_arguments()
        try:
            return await services.proposal_show(request.proposal_id)
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            proposal_id = ""
            request = cast(ProposalShowRequest, None)

    @server.tool(
        name="intent_proposal_confirm",
        annotations=_CONFIRM,
        structured_output=True,
    )
    async def proposal_confirm(
        proposal_id: _ProposalIdInput,
        proposal_digest: _DigestInput,
        confirmed_node_ids: _NodeIdsInput,
    ) -> dict[str, object]:
        try:
            request = ProposalConfirmRequest.model_validate(
                {
                    "proposal_id": proposal_id,
                    "proposal_digest": proposal_digest,
                    "confirmed_node_ids": tuple(confirmed_node_ids),
                }
            )
        except Exception:  # noqa: BLE001 - one fixed pre-handler boundary
            proposal_id = proposal_digest = ""
            confirmed_node_ids.clear()
            _fixed_arguments()
        try:
            return await services.proposal_confirm(
                request.proposal_id,
                request.proposal_digest,
                request.confirmed_node_ids,
            )
        except Exception:  # noqa: BLE001 - one fixed handler boundary
            _fixed_arguments()
        finally:
            proposal_id = proposal_digest = ""
            confirmed_node_ids.clear()
            request = cast(ProposalConfirmRequest, None)


__all__ = [
    "IntentWorkflowPort",
    "McpIntentWorkflowServices",
    "load_intent_workflow_services",
    "register_intent_workflow_tools",
]
