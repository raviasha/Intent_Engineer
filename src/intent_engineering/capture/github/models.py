"""Strict immutable provider-local GitHub response models."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, cast

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from intent_engineering.core.models._base import StrictModel


def freeze_provider_value(value: object) -> object:
    """Recursively detach and freeze JSON-shaped provider values."""
    if type(value) is dict:
        raw = cast(dict[object, object], value)
        return MappingProxyType(
            {str(key): freeze_provider_value(item) for key, item in raw.items()}
        )
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): freeze_provider_value(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(freeze_provider_value(item) for item in cast(list[object], value))
    if type(value) is tuple:
        return tuple(freeze_provider_value(item) for item in cast(tuple[object, ...], value))
    return value


def thaw_provider_value(value: object) -> object:
    """Return a detached JSON-compatible copy of a frozen provider value."""
    if isinstance(value, Mapping):
        return {str(key): thaw_provider_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_provider_value(item) for item in value]
    return value


def _prepare_model_input(value: object) -> object:
    """Copy frozen mappings into structures accepted by strict nested Pydantic models."""
    if isinstance(value, Mapping):
        return {str(key): _prepare_model_input(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_prepare_model_input(item) for item in value)
    return value


def _parse_github_datetime(value: object) -> object:
    if type(value) is not str:
        return value
    parse_failed = False
    try:
        parsed = datetime.fromisoformat(str.replace(value, "Z", "+00:00"))
    except ValueError:
        parse_failed = True
        parsed = datetime.min.replace(tzinfo=UTC)
    if parse_failed or parsed.tzinfo is None:
        raise ValueError("GitHub timestamp must be an ISO 8601 value")
    return parsed.astimezone(UTC)


type GitHubDateTime = Annotated[datetime, BeforeValidator(_parse_github_datetime)]


class GitHubProviderModel(StrictModel):
    """Base for strict responses with one explicit unknown-field container."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )

    extra: Mapping[str, object] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_frozen_provider_mapping(cls, value: object) -> object:
        return _prepare_model_input(value)

    @field_validator("extra", mode="after")
    @classmethod
    def _freeze_extra(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        frozen = freeze_provider_value(value)
        assert isinstance(frozen, Mapping)
        return frozen

    @field_serializer("extra")
    def _serialize_extra(self, value: Mapping[str, object]) -> object:
        return thaw_provider_value(value)


class GitHubUser(GitHubProviderModel):
    """GitHub actor fields used by evidence normalization."""

    id: int
    login: str
    html_url: str


class GitHubLabel(GitHubProviderModel):
    """Issue/PR label fields used by evidence normalization."""

    name: str


class GitHubMilestone(GitHubProviderModel):
    """Issue/PR milestone fields used by evidence normalization."""

    title: str


class GitHubRef(GitHubProviderModel):
    """Pull-request branch identity used by evidence normalization."""

    ref: str
    sha: str


class GitHubIssue(GitHubProviderModel):
    """GitHub issue fields used by evidence normalization."""

    id: int
    number: int
    title: str
    body: str | None
    state: str
    user: GitHubUser
    labels: tuple[GitHubLabel, ...]
    milestone: GitHubMilestone | None
    updated_at: GitHubDateTime
    html_url: str


class GitHubPullRequest(GitHubIssue):
    """GitHub pull-request fields used by evidence normalization."""

    base: GitHubRef
    head: GitHubRef
    merge_commit_sha: str | None


class GitHubCommitAuthor(GitHubProviderModel):
    """Immutable authored timestamp embedded in a commit response."""

    name: str
    date: GitHubDateTime


class GitHubCommitData(GitHubProviderModel):
    """Canonical commit message and authored identity."""

    message: str
    author: GitHubCommitAuthor


class GitHubCommit(GitHubProviderModel):
    """GitHub commit fields used by evidence normalization."""

    sha: str
    html_url: str
    commit: GitHubCommitData
    author: GitHubUser | None


class GitHubIssueComment(GitHubProviderModel):
    """GitHub issue-comment fields used by evidence normalization."""

    id: int
    body: str
    user: GitHubUser
    updated_at: GitHubDateTime
    html_url: str
    issue_url: str


class GitHubReviewComment(GitHubProviderModel):
    """GitHub review-comment fields used by evidence normalization."""

    id: int
    body: str
    user: GitHubUser
    updated_at: GitHubDateTime
    html_url: str
    pull_request_url: str
    path: str
    line: int | None


class PageResult(StrictModel):
    """One immutable ordered pagination result and its first-page validator."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )

    items: tuple[Mapping[str, object], ...]
    etag: str | None
    not_modified: bool = False

    @field_validator("items", mode="after")
    @classmethod
    def _freeze_items(
        cls, value: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        return tuple(freeze_provider_value(item) for item in value)  # type: ignore[misc]

    @field_serializer("items")
    def _serialize_items(self, value: tuple[Mapping[str, object], ...]) -> object:
        return thaw_provider_value(value)

    @model_validator(mode="after")
    def _validate_not_modified(self) -> PageResult:
        if self.not_modified and (self.items or self.etag is None):
            raise ValueError("not-modified results require an ETag and no items")
        return self
