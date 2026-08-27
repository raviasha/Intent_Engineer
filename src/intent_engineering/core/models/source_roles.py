"""Explicit source authority roles configured by local projects."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import ConfigDict, Field

from intent_engineering.core.models._base import StrictModel


class SourceRole(StrEnum):
    """Interpretive role of one configured source scope; never an automatic truth selector."""

    DECLARED_INTENT = "declared_intent"
    DECISION = "decision"
    PROPOSED_INTENT = "proposed_intent"
    IMPLEMENTATION_EVIDENCE = "implementation_evidence"
    OPERATING_CONTEXT = "operating_context"


class SourceRoleAssignment(StrictModel):
    """One exact connector scope and the role it plays in interpretation."""

    model_config = ConfigDict(frozen=True, strict=True)

    # Production MCP connector identities contain four SHA-256 components plus
    # the configured connector id.  Keep the field bounded while allowing that
    # canonical identity to be assigned an explicit source role.
    connector_id: Annotated[str, Field(min_length=1, max_length=512)]
    scope: Annotated[str, Field(min_length=1, max_length=2048)]
    role: SourceRole
    inherited: bool
