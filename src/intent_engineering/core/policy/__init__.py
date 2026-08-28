"""Policies governing a local Intent Engineering workspace."""

from intent_engineering.core.policy.access import evidence_allowed, refs_allowed
from intent_engineering.core.policy.doctor import inspect_workspace
from intent_engineering.core.policy.project import (
    ProjectAlreadyInitialized,
    ProjectNotInitialized,
    initialize_project,
)

__all__ = [
    "ProjectAlreadyInitialized",
    "ProjectNotInitialized",
    "evidence_allowed",
    "initialize_project",
    "inspect_workspace",
    "refs_allowed",
]
