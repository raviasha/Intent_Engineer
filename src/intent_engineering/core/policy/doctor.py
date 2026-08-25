"""Workspace doctor compatibility wrapper over the shared deep validator."""

from __future__ import annotations

from pathlib import Path

from intent_engineering.validation import ValidationDiagnostic, validate_project


def inspect_workspace(root: Path) -> tuple[bool, tuple[ValidationDiagnostic, ...]]:
    """Return health and the shared deterministic, non-sensitive diagnostics."""
    report = validate_project(root)
    return report.valid, report.diagnostics
