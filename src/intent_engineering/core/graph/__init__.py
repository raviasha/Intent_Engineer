"""Pure graph mutation and invariant enforcement."""

from intent_engineering.core.graph.applier import (
    DuplicateIdentity,
    StaleGraphVersion,
    UnknownIdentity,
    apply_changeset,
)

__all__ = ["DuplicateIdentity", "StaleGraphVersion", "UnknownIdentity", "apply_changeset"]
