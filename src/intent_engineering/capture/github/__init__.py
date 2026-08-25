"""GitHub capture primitives."""

from intent_engineering.capture.github.auth import (
    CredentialSource,
    GitHubCredentials,
    GitHubTokenRunner,
    run_gh_token,
)
from intent_engineering.capture.github.errors import GitHubAuthError

__all__ = [
    "CredentialSource",
    "GitHubAuthError",
    "GitHubCredentials",
    "GitHubTokenRunner",
    "run_gh_token",
]
