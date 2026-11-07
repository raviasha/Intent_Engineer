"""Shared local workspace validation application service."""

from intent_engineering.validation.service import (
    MAX_CANONICAL_FILE_BYTES,
    MAX_CANONICAL_SNAPSHOT_BYTES,
    DiagnosticSeverity,
    ValidationDiagnostic,
    ValidationReport,
    WorkspaceValidationService,
    validate_canonical_snapshot,
    validate_project,
    validate_project_directory,
)

__all__ = [
    "MAX_CANONICAL_FILE_BYTES",
    "MAX_CANONICAL_SNAPSHOT_BYTES",
    "DiagnosticSeverity",
    "ValidationDiagnostic",
    "ValidationReport",
    "WorkspaceValidationService",
    "validate_canonical_snapshot",
    "validate_project",
    "validate_project_directory",
]
