"""Preview-first CLI orchestration for GitHub-backed team state."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal, Protocol

import anyio
import typer
from pydantic import ConfigDict, Field

from intent_engineering.cli.output import OutputFormat, emit
from intent_engineering.cli.runtime import github_repository_scope, load_runtime
from intent_engineering.control_plane.models import HumanDecisionPayload
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.core.models._base import StrictModel
from intent_engineering.team_state.github import (
    GitHubProtectionPreview,
    GitHubTeamStateStatus,
    PublicationPullRequest,
)
from intent_engineering.team_state.models import PreparedPublication, RecipientRecord
from intent_engineering.team_state.publication import PublicationPreview
from intent_engineering.team_state.suggestions import (
    CodeSuggestionPreview,
    preview_code_suggestions,
)

team_app = typer.Typer(help="Configure and inspect shared intent state.")
team_enable_app = typer.Typer(help="Enable one reviewed team-state provider.")
team_app.add_typer(team_enable_app, name="enable")


class GitHubEnablePreview(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)

    state: Literal["confirmation_required"] = "confirmation_required"
    project_id: str
    repository_id: str
    actor: str
    codeowners_path: Literal[".github/CODEOWNERS"] = ".github/CODEOWNERS"
    workflow_path: Literal[".github/workflows/intent-state.yml"] = (
        ".github/workflows/intent-state.yml"
    )
    codeowners_suggestion: str
    workflow_suggestion: str
    code_suggestions: CodeSuggestionPreview | None = None
    preview_digest: str


class GitHubEnableResult(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)

    state: Literal[
        "webauthn_confirmation_required",
        "protection_confirmation_required",
        "bootstrap_required",
        "published",
    ]
    repository_id: str
    preview_digest: str
    protection_digest: str | None = None
    pull_request_url: str | None = None
    control_plane_path: str | None = None


class VerifiedProtectionAuthorization(StrictModel):
    """Strict projection emitted only after platform verification of this mutation."""

    model_config = ConfigDict(frozen=True, strict=True)

    project_id: str
    repository_id: str
    actor: str
    action: Literal["configure_github_branch_protection"] = "configure_github_branch_protection"
    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    credential_id: str = Field(min_length=1, max_length=4096)
    verified_at: datetime
    expires_at: datetime


class GitHubEnableClient(Protocol):
    async def inspect(self, repository: str) -> GitHubTeamStateStatus: ...

    def protection_preview(self) -> GitHubProtectionPreview: ...

    async def configure_protection(self, confirmation: str) -> GitHubTeamStateStatus: ...

    async def open_publication_pr(
        self, publication: PreparedPublication
    ) -> PublicationPullRequest: ...

    async def aclose(self) -> None: ...


class PublicationPreparation(Protocol):
    def bind_publication_base_commit(self, commit: str) -> None: ...

    def preview(self, *, now: datetime) -> PublicationPreview: ...

    def prepare(self, decision: VerifiedHumanDecision, *, now: datetime) -> PreparedPublication: ...


@dataclass(frozen=True, slots=True)
class GitHubEnablementServices:
    github: GitHubEnableClient
    publication: PublicationPreparation
    verify_protection: Callable[
        [GitHubProtectionPreview], Awaitable[VerifiedProtectionAuthorization]
    ]
    enroll: Callable[[GitHubEnablePreview, GitHubTeamStateStatus], Awaitable[RecipientRecord]]
    stage_code_suggestions: Callable[[GitHubEnablePreview], None]
    verify_publication: Callable[[HumanDecisionPayload], Awaitable[VerifiedHumanDecision]]
    clock: Callable[[], datetime]


class GitHubEnablementWorkflow:
    """Run reviewed provider setup with injected WebAuthn and publication boundaries."""

    def __init__(self, preview: GitHubEnablePreview, services: GitHubEnablementServices) -> None:
        self._preview = GitHubEnablePreview.model_validate(preview.model_dump(mode="python"))
        self._services = services

    async def run(
        self,
        *,
        repository: str,
        preview_confirmation: str,
        protection_confirmation: str | None,
    ) -> GitHubEnableResult:
        preview = self._preview
        if (
            preview_confirmation != preview.preview_digest
            or preview.repository_id != f"github.com/{repository}"
        ):
            raise ValueError("GitHub team enablement preview changed")
        protection: GitHubProtectionPreview | None = None
        try:
            status = await self._services.github.inspect(repository)
            if status.repository_id != preview.repository_id:
                raise ValueError("GitHub team enablement preview changed")
            protection = self._services.github.protection_preview()
            if protection.repository_id != preview.repository_id:
                raise ValueError("GitHub team enablement preview changed")
            if protection.requires_change and protection_confirmation is None:
                return GitHubEnableResult(
                    state="protection_confirmation_required",
                    repository_id=preview.repository_id,
                    preview_digest=preview.preview_digest,
                    protection_digest=protection.digest,
                )
            if protection.requires_change:
                if protection_confirmation != protection.digest:
                    raise ValueError("GitHub branch-protection preview changed")
                verified_protection = await self._services.verify_protection(protection)
                verified_now = self._services.clock()
                if (
                    type(verified_now) is not datetime
                    or verified_now.tzinfo is None
                    or verified_now.utcoffset() != timedelta(0)
                ):
                    raise ValueError("GitHub branch-protection authority changed")
                if (
                    type(verified_protection) is not VerifiedProtectionAuthorization
                    or verified_protection.project_id != preview.project_id
                    or verified_protection.repository_id != preview.repository_id
                    or verified_protection.actor != preview.actor
                    or verified_protection.preview_digest != protection.digest
                    or verified_protection.result_digest != protection.digest
                    or verified_protection.verified_at.tzinfo is None
                    or verified_protection.verified_at.utcoffset() != timedelta(0)
                    or verified_protection.expires_at.tzinfo is None
                    or verified_protection.expires_at.utcoffset() != timedelta(0)
                    or verified_protection.verified_at > verified_now
                    or verified_now > verified_protection.expires_at
                ):
                    raise ValueError("GitHub branch-protection authority changed")
                status = await self._services.github.configure_protection(protection_confirmation)
            if status.branch_commit is None:
                raise ValueError("GitHub state branch unavailable")
            self._services.publication.bind_publication_base_commit(status.branch_commit)
            recipient = await self._services.enroll(preview, status)
            if (
                recipient.project_id != preview.project_id
                or recipient.repository_id != preview.repository_id
                or recipient.actor != preview.actor
                or recipient.github_account_id != status.account_id
                or recipient.github_login != status.login
            ):
                raise ValueError("GitHub team enrollment changed")
            self._services.stage_code_suggestions(preview)
            now = self._services.clock()
            if now.tzinfo is None or now.utcoffset() != timedelta(0):
                raise ValueError("GitHub team enablement clock unavailable")
            publication_preview = self._services.publication.preview(now=now)
            publication_decision = await self._services.verify_publication(
                publication_preview.payload
            )
            prepared = self._services.publication.prepare(publication_decision, now=now)
            pull_request = await self._services.github.open_publication_pr(prepared)
            return GitHubEnableResult(
                state="published",
                repository_id=preview.repository_id,
                preview_digest=preview.preview_digest,
                protection_digest=protection.digest,
                pull_request_url=pull_request.url,
            )
        finally:
            with anyio.CancelScope(shield=True):
                await self._services.github.aclose()


def _digest(payload: dict[str, object]) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def build_github_enable_preview(project: Path, repository: str) -> GitHubEnablePreview:
    """Return the stable, network-free provider setup proposal."""
    runtime = load_runtime(project)
    try:
        repository_id = f"github.com/{repository}"
        login = repository.split("/", 1)[0]
        codeowners_suggestion = (
            f"/.intent/ @{login}\n/.github/workflows/intent-state.yml @{login}\n"
        )
        workflow_suggestion = (
            "name: Intent state\n"
            "on:\n"
            "  pull_request:\n"
            "    branches: [intent-state]\n"
            "permissions:\n"
            "  contents: read\n"
            "jobs:\n"
            "  validate:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: intent check --shared-state\n"
        )
        suggestions = (
            preview_code_suggestions(project, codeowners_suggestion, workflow_suggestion)
            if (project / ".git").exists()
            else None
        )
        payload: dict[str, object] = {
            "actor": runtime.config.local_actor,
            "codeowners_path": ".github/CODEOWNERS",
            "codeowners_suggestion": codeowners_suggestion,
            "project_id": runtime.config.project_id,
            "repository_id": repository_id,
            "workflow_path": ".github/workflows/intent-state.yml",
            "workflow_suggestion": workflow_suggestion,
            "code_suggestions": None
            if suggestions is None
            else suggestions.model_dump(mode="json"),
        }
        return GitHubEnablePreview(
            project_id=runtime.config.project_id,
            repository_id=repository_id,
            actor=runtime.config.local_actor,
            codeowners_suggestion=codeowners_suggestion,
            workflow_suggestion=workflow_suggestion,
            code_suggestions=suggestions,
            preview_digest=_digest(payload),
        )
    finally:
        runtime.close()


async def run_confirmed_github_enablement(
    project: Path,
    repository: str,
    *,
    preview_confirmation: str,
    protection_confirmation: str | None,
    workflow: GitHubEnablementWorkflow | None = None,
) -> GitHubEnableResult:
    """Enter the WebAuthn-backed setup boundary after an exact local preview."""
    preview = build_github_enable_preview(project, repository)
    if preview_confirmation != preview.preview_digest:
        raise ValueError("GitHub team enablement preview changed")
    if workflow is not None:
        return await workflow.run(
            repository=repository,
            preview_confirmation=preview_confirmation,
            protection_confirmation=protection_confirmation,
        )
    from intent_engineering.team_state.setup import save_setup_request

    runtime = load_runtime(project)
    try:
        save_setup_request(runtime, preview)
    finally:
        runtime.close()
    return GitHubEnableResult(
        state="webauthn_confirmation_required",
        repository_id=preview.repository_id,
        preview_digest=preview.preview_digest,
        control_plane_path="/",
    )


@team_enable_app.command("github")
def enable_github_command(
    project: Annotated[Path, typer.Option("--project")] = Path("."),
    confirm_preview: Annotated[str | None, typer.Option("--confirm-preview")] = None,
    confirm_protection: Annotated[str | None, typer.Option("--confirm-protection")] = None,
    output_format: Annotated[OutputFormat, typer.Option("--format")] = OutputFormat.TEXT,
) -> None:
    """Preview GitHub team setup before any credential or network access."""
    repository = github_repository_scope(os.environ)
    preview = build_github_enable_preview(project, repository)
    if confirm_preview is None:
        emit(preview, output_format)
        raise typer.Exit(4)
    if confirm_preview != preview.preview_digest:
        typer.echo("intent error: GitHub team enablement preview changed", err=True)
        raise typer.Exit(1)

    async def run() -> GitHubEnableResult:
        return await run_confirmed_github_enablement(
            project,
            repository,
            preview_confirmation=confirm_preview,
            protection_confirmation=confirm_protection,
        )

    result = anyio.run(run)
    emit(result, output_format)
    if result.state == "webauthn_confirmation_required":
        from intent_engineering.cli.dev import dev_command

        dev_command(project=project, prd=None, no_open=False, offline=False, status=False)
    if result.state != "published":
        raise typer.Exit(4)


__all__ = [
    "GitHubEnablePreview",
    "GitHubEnableResult",
    "GitHubEnablementServices",
    "GitHubEnablementWorkflow",
    "VerifiedProtectionAuthorization",
    "build_github_enable_preview",
    "enable_github_command",
    "run_confirmed_github_enablement",
    "team_app",
]
