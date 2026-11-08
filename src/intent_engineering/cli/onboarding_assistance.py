"""Convenience operations around the existing governed onboarding service."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from intent_engineering.cli.intent_workflow import (
    _bootstrap_service,
    _canonical_scope,
    _existing_onboarding_result,
    _inspect_onboarding,
    _principals,
    _snapshot_config,
)
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.bootstrap import BootstrapSubmission
from intent_engineering.intent_workflow.onboarding import OnboardingState
from intent_engineering.intent_workflow.onboarding_discovery import discover_prds
from intent_engineering.intent_workflow.onboarding_reminders import defer_onboarding


def discover_onboarding(project: Path) -> dict[str, object]:
    exclusions: tuple[str, ...] = ()
    if os.path.lexists(project / ".intent"):
        runtime = load_runtime(project)
        try:
            status = _inspect_onboarding(runtime)
            if status.state is not OnboardingState.REQUIRED:
                return _existing_onboarding_result(runtime, status).model_dump(mode="json")
            exclusions = runtime.config.source_exclusions
        finally:
            runtime.close()
    candidates = discover_prds(project, exclusions)
    return {
        "state": "onboarding_required",
        "prd_candidates": list(candidates),
        "next_action": (
            "use_prd"
            if len(candidates) == 1
            else "choose_prd"
            if candidates
            else "draft_prd_from_repository"
        ),
        "authorization_issued": False,
    }


def defer_result(project: Path, after_tasks: int) -> dict[str, object]:
    # Deferral only affects an absent baseline, never a pending review or damaged workspace.
    if os.path.lexists(project / ".intent"):
        runtime = load_runtime(project)
        try:
            status = _inspect_onboarding(runtime)
            if status.state is not OnboardingState.REQUIRED:
                return _existing_onboarding_result(runtime, status).model_dump(mode="json")
        finally:
            runtime.close()
    defer_onboarding(project, after_tasks=after_tasks)
    return {"state": "deferred", "remind_after_tasks": after_tasks, "authorization_issued": False}


def submit_onboarding(project: Path, submission_path: str) -> dict[str, object]:
    scope = _canonical_scope(submission_path)
    if ".intent" in PurePosixPath(scope).parts or ".git" in PurePosixPath(scope).parts:
        raise ValueError("submission must be outside canonical state")
    runtime = load_runtime(project)
    try:
        status = _inspect_onboarding(runtime)
        if status.state is not OnboardingState.REQUIRED:
            return _existing_onboarding_result(runtime, status).model_dump(mode="json")
        raw = runtime.project_directory.read_relative(scope, nonblocking=True, max_bytes=1048576)
        submission = BootstrapSubmission.model_validate_json(raw.content)
        config, _ = _snapshot_config(runtime)
        _bootstrap_service(runtime, config).propose(submission, _principals(runtime, config))
        return _existing_onboarding_result(runtime, _inspect_onboarding(runtime)).model_dump(
            mode="json"
        )
    finally:
        runtime.close()
