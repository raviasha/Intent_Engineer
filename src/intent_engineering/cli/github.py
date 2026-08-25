"""GitHub-specific CLI diagnostics behind reviewed provider boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

import anyio
from pydantic import ConfigDict

from intent_engineering.capture.github.auth import (
    CredentialSource,
    GitHubCredentials,
    GitHubTokenRunner,
    run_gh_token,
)
from intent_engineering.capture.github.client import GitHubClient
from intent_engineering.capture.github.errors import (
    GitHubApiError,
    GitHubAuthError,
    GitHubNotFound,
    GitHubPermissionError,
    GitHubProtocolError,
    GitHubRateLimitError,
    GitHubTransientError,
)
from intent_engineering.cli.runtime import (
    GitHubClientFactory,
    Runtime,
    github_repository_scope,
)
from intent_engineering.core.models._base import StrictModel


class GitHubDoctorDiagnostic(StrictModel):
    """One fixed, redacted failure identity for machine and human output."""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str


class GitHubDoctorResult(StrictModel):
    """Versioned GitHub-access diagnostic containing only safe scalars."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    healthy: bool
    repository: str
    credential_source: CredentialSource | None = None
    access: Literal["accessible", "unavailable"] = "unavailable"
    rate_limit: int | None = None
    rate_remaining: int | None = None
    rate_used: int | None = None
    rate_reset_at: datetime | None = None
    rate_resource: str | None = None
    error: GitHubDoctorDiagnostic | None = None

    @classmethod
    def failed(
        cls,
        *,
        repository: str,
        diagnostic: GitHubDoctorDiagnostic,
        credential_source: CredentialSource | None = None,
    ) -> GitHubDoctorResult:
        return cls(
            healthy=False,
            repository=repository,
            credential_source=credential_source,
            error=diagnostic,
        )


def _diagnostic(
    error: GitHubApiError | GitHubProtocolError | GitHubTransientError,
) -> GitHubDoctorDiagnostic:
    if isinstance(error, GitHubRateLimitError):
        code = "github.rate_limit"
    elif isinstance(error, GitHubPermissionError):
        code = "github.permission"
    elif isinstance(error, GitHubNotFound):
        code = "github.not_found"
    elif isinstance(error, GitHubProtocolError):
        code = "github.protocol"
    elif isinstance(error, GitHubTransientError):
        code = "github.transport"
    else:
        code = "github.api"
    return GitHubDoctorDiagnostic(code=code, message=str(error))


def _default_client(credentials: GitHubCredentials) -> GitHubClient:
    return GitHubClient(credentials)


async def check_github(
    runtime: Runtime,
    *,
    env: Mapping[str, object],
    token_runner: GitHubTokenRunner = run_gh_token,
    client_factory: GitHubClientFactory = _default_client,
) -> GitHubDoctorResult:
    """Probe repository access once and return a deterministic redacted result."""
    repository = github_repository_scope(env)
    try:
        credentials = GitHubCredentials.resolve(env, token_runner)
    except GitHubAuthError as error:
        env = {}
        return GitHubDoctorResult.failed(
            repository=repository,
            diagnostic=GitHubDoctorDiagnostic(
                code="github.authentication",
                message=str(error),
            ),
        )
    env = {}

    cancelled_class = anyio.get_cancelled_exc_class()
    client: GitHubClient | None = None
    construction_failed = False
    try:
        client = client_factory(credentials)
    except cancelled_class:
        raise
    except BaseException:  # noqa: BLE001 - discard injected/provider construction failures
        construction_failed = True
    if construction_failed or client is None:
        return GitHubDoctorResult.failed(
            repository=repository,
            credential_source=credentials.source,
            diagnostic=GitHubDoctorDiagnostic(
                code="github.client",
                message="GitHub client initialization failed.",
            ),
        )

    status = None
    diagnostic: GitHubDoctorDiagnostic | None = None
    try:
        status = await client.get_repository_status(repository)
    except cancelled_class:
        try:
            with anyio.CancelScope(shield=True):
                await client.aclose()
        except BaseException:  # noqa: BLE001, S110 - cancellation remains visible
            pass
        raise
    except (GitHubApiError, GitHubProtocolError, GitHubTransientError) as error:
        diagnostic = _diagnostic(error)
    except BaseException:  # noqa: BLE001 - discard unexpected provider failures
        diagnostic = GitHubDoctorDiagnostic(
            code="github.client",
            message="GitHub repository access check failed.",
        )

    close_failed = False
    try:
        with anyio.CancelScope(shield=True):
            await client.aclose()
    except cancelled_class:
        raise
    except BaseException:  # noqa: BLE001 - replace with one fixed close diagnostic
        close_failed = True
    if diagnostic is not None:
        return GitHubDoctorResult.failed(
            repository=repository,
            credential_source=credentials.source,
            diagnostic=diagnostic,
        )
    if close_failed:
        return GitHubDoctorResult.failed(
            repository=repository,
            credential_source=credentials.source,
            diagnostic=GitHubDoctorDiagnostic(
                code="github.close",
                message="GitHub client cleanup failed.",
            ),
        )
    if status is None:  # pragma: no cover - every branch above returns a result
        raise AssertionError("GitHub doctor must produce a result")
    return GitHubDoctorResult(
        healthy=True,
        repository=status.repository,
        credential_source=credentials.source,
        access="accessible",
        rate_limit=status.rate_limit,
        rate_remaining=status.rate_remaining,
        rate_used=status.rate_used,
        rate_reset_at=status.rate_reset_at,
        rate_resource=status.rate_resource,
    )
