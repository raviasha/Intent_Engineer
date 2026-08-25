"""Local-only GitHub credential resolution with a redacted public boundary."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from enum import StrEnum

from pydantic import ConfigDict, Field, SecretStr

from intent_engineering.capture.github.errors import GitHubAuthError
from intent_engineering.core.models._base import StrictModel

_GH_AUTH_TOKEN_ARGV = ["gh", "auth", "token"]
_GITHUB_TOKEN_OVERRIDE_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

type GitHubTokenRunner = Callable[[list[str]], str]


class CredentialSource(StrEnum):
    """Supported local sources for GitHub credentials."""

    ENVIRONMENT = "environment"
    GITHUB_CLI = "github_cli"


def _normalize_token(value: object) -> str | None:
    """Normalize only exact built-in strings without invoking provider overrides."""
    if type(value) is not str:
        return None
    normalized = str.strip(value)
    return normalized or None


def run_gh_token(argv: list[str]) -> str:
    """Run GitHub CLI with structured arguments and return only normalized stdout."""
    child_env = os.environ.copy()
    for name in _GITHUB_TOKEN_OVERRIDE_NAMES:
        child_env.pop(name, None)
    failed = False
    try:
        completed = subprocess.run(
            argv,
            check=True,
            capture_output=True,
            text=True,
            shell=False,
            env=child_env,
        )
    except (OSError, subprocess.SubprocessError):
        failed = True
    if failed:
        raise GitHubAuthError()
    token = _normalize_token(completed.stdout)
    if token is None:
        raise GitHubAuthError()
    return token


class GitHubCredentials(StrictModel):
    """Provider-local credentials whose secret is excluded from public representations."""

    model_config = ConfigDict(frozen=True, strict=True)

    token: SecretStr = Field(exclude=True, repr=False)
    source: CredentialSource

    @classmethod
    def resolve(
        cls,
        env: Mapping[str, object],
        run: GitHubTokenRunner = run_gh_token,
    ) -> GitHubCredentials:
        """Resolve a nonblank environment token before consulting the GitHub CLI."""
        invalid_environment = False
        try:
            raw_environment_token = env.get("GH_TOKEN")
        # A caller-supplied Mapping may raise any exception containing sensitive input.
        except Exception:  # noqa: BLE001 - redact the full provider boundary
            invalid_environment = True
            raw_environment_token = None
        if invalid_environment or (
            raw_environment_token is not None and type(raw_environment_token) is not str
        ):
            raise GitHubAuthError()

        environment_token = _normalize_token(raw_environment_token)
        if environment_token is not None:
            return cls(
                token=SecretStr(environment_token),
                source=CredentialSource.ENVIRONMENT,
            )

        runner_failed = False
        try:
            cli_output = run(list(_GH_AUTH_TOKEN_ARGV))
        # Injected runners are untrusted boundaries and may retain captured provider output.
        except Exception:  # noqa: BLE001 - replace every runner failure with one safe error
            runner_failed = True
            cli_output = ""
        if runner_failed:
            raise GitHubAuthError()

        cli_token = _normalize_token(cli_output)
        if cli_token is None:
            raise GitHubAuthError()
        return cls(token=SecretStr(cli_token), source=CredentialSource.GITHUB_CLI)
