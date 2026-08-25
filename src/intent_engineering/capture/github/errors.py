"""Redacted public errors for local GitHub authentication."""

GITHUB_AUTH_ERROR_MESSAGE = (
    "GitHub authentication unavailable; set GH_TOKEN or run `gh auth login`."
)


class GitHubAuthError(RuntimeError):
    """Report an actionable authentication failure without retaining provider output."""

    def __init__(self) -> None:
        super().__init__(GITHUB_AUTH_ERROR_MESSAGE)
