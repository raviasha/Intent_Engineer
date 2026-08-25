"""GitHub capture primitives."""

from intent_engineering.capture.github.auth import (
    CredentialSource,
    GitHubCredentials,
    GitHubTokenRunner,
    run_gh_token,
)
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.github.connector import GitHubCheckpoint, GitHubConnector
from intent_engineering.capture.github.errors import (
    GitHubApiError,
    GitHubAuthError,
    GitHubCheckpointError,
    GitHubNotFound,
    GitHubPermissionError,
    GitHubProtocolError,
    GitHubRateLimitError,
    GitHubTransientError,
)
from intent_engineering.capture.github.models import (
    GitHubCommit,
    GitHubIssue,
    GitHubIssueComment,
    GitHubPullRequest,
    GitHubReviewComment,
    GitHubUser,
    PageResult,
)

__all__ = [
    "CredentialSource",
    "GitHubApiError",
    "GitHubAuthError",
    "GitHubCheckpoint",
    "GitHubCheckpointError",
    "GitHubClient",
    "GitHubCommit",
    "GitHubConnector",
    "GitHubCredentials",
    "GitHubIssue",
    "GitHubIssueComment",
    "GitHubNotFound",
    "GitHubPermissionError",
    "GitHubProtocolError",
    "GitHubPullRequest",
    "GitHubRateLimitError",
    "GitHubReviewComment",
    "GitHubTokenRunner",
    "GitHubTransientError",
    "GitHubUser",
    "PageResult",
    "RetryPolicy",
    "run_gh_token",
]
