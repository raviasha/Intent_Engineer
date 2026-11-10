"""Repository-scoped GitHub evidence connector."""

from __future__ import annotations

import json
import re
from asyncio import CancelledError
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import ClassVar, Literal, cast
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, ValidationError, field_serializer, field_validator

from intent_engineering.capture.base import (
    ConnectorError,
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.capture.github.client import GitHubClient
from intent_engineering.capture.github.errors import GitHubCheckpointError
from intent_engineering.capture.github.models import (
    GitHubCommit,
    GitHubCommitAuthor,
    GitHubCommitData,
    GitHubIssue,
    GitHubIssueComment,
    GitHubLabel,
    GitHubMilestone,
    GitHubPullRequest,
    GitHubRef,
    GitHubReviewComment,
    GitHubUser,
    PageResult,
)
from intent_engineering.core.models import EvidenceRecord, JsonValue
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage.jsonl.strict import loads_strict_json

_OWNER = re.compile(r"(?!-)(?!.*--)[A-Za-z0-9-]{1,39}(?<!-)\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_ETAG_MAX_LENGTH = 256
_CURSOR_MAX_BYTES = 65_536
_NO_COMMIT_TIME = datetime(1970, 1, 1, tzinfo=UTC)

_ENDPOINTS = ("issues", "pull_requests", "commits", "issue_comments", "review_comments")


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _valid_etag(value: str) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= _ETAG_MAX_LENGTH
        and value.isascii()
        and all(0x20 <= ord(character) < 0x7F for character in value)
    )


class GitHubCheckpoint(StrictModel):
    """Canonical repository-bound cursor for one GitHub connector instance."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )

    cursor_schema_version: Literal[1] = 1
    repository: str
    etags: Mapping[str, str] = Field(default_factory=dict)
    newest_updated_at: datetime | None = None
    newest_commit_sha: str | None = None

    @field_validator("cursor_schema_version", mode="before")
    @classmethod
    def _strict_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("cursor schema version must be an integer")
        return value

    @field_validator("repository")
    @classmethod
    def _canonical_repository(cls, value: str) -> str:
        owner, separator, repository = value.partition("/")
        if (
            not separator
            or "/" in repository
            or _OWNER.fullmatch(owner) is None
            or _REPOSITORY.fullmatch(repository) is None
            or value != value.lower()
        ):
            raise ValueError("repository must be canonical")
        return value

    @field_validator("etags")
    @classmethod
    def _validate_etags(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if any(type(key) is not str or key not in _ENDPOINTS for key in value):
            raise ValueError("unknown endpoint ETag")
        if any(not _valid_etag(item) for item in value.values()):
            raise ValueError("invalid endpoint ETag")
        return MappingProxyType(dict(sorted(value.items())))

    @field_serializer("etags")
    def _serialize_etags(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    @field_validator("newest_updated_at", mode="before")
    @classmethod
    def _validate_updated_at(cls, value: object) -> datetime | None:
        if value is None:
            return None
        if type(value) is str:
            try:
                value = datetime.fromisoformat(value)
            except ValueError:
                raise ValueError("invalid checkpoint timestamp") from None
        if type(value) is not datetime:
            raise ValueError("invalid checkpoint timestamp")
        if value.tzinfo is None:
            raise ValueError("checkpoint timestamp requires a timezone")
        return value.astimezone(UTC)

    @field_validator("newest_commit_sha")
    @classmethod
    def _validate_commit_sha(cls, value: str | None) -> str | None:
        if value is not None and _SHA.fullmatch(value) is None:
            raise ValueError("invalid checkpoint commit SHA")
        return value

    def encode(self) -> str:
        payload: dict[str, object] = {
            "cursor_schema_version": self.cursor_schema_version,
            "etags": dict(self.etags),
            "newest_commit_sha": self.newest_commit_sha,
            "newest_updated_at": (
                _format_datetime(self.newest_updated_at)
                if self.newest_updated_at is not None
                else None
            ),
            "repository": self.repository,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    @classmethod
    def decode(cls, value: str, *, expected_repository: str) -> GitHubCheckpoint:
        checkpoint: GitHubCheckpoint | None = None
        payload: object = None
        try:
            if type(value) is not str or len(value.encode("utf-8")) > _CURSOR_MAX_BYTES:
                raise ValueError
            payload = loads_strict_json(value)
            if type(payload) is not dict:
                raise ValueError
            checkpoint = cls.model_validate(payload)
            if checkpoint.repository != expected_repository or checkpoint.encode() != value:
                raise ValueError
        except (UnicodeError, TypeError, ValidationError, ValueError):
            checkpoint = None
        if checkpoint is None:
            payload = None
            del value
            raise GitHubCheckpointError()
        return checkpoint


def _split_provider_fields(
    payload: Mapping[str, object],
    known: Sequence[str],
) -> dict[str, object]:
    if type(payload) is not dict and type(payload) is not MappingProxyType:
        raise ValueError("provider item must be an object")
    result = {key: payload[key] for key in known if key in payload}
    result["extra"] = {key: value for key, value in payload.items() if key not in known}
    return result


def _decode_user(value: object) -> GitHubUser | None:
    if value is None:
        return None
    return GitHubUser.model_validate(
        _split_provider_fields(cast(Mapping[str, object], value), ("id", "login", "html_url"))
    )


def _decode_label(value: object) -> GitHubLabel:
    return GitHubLabel.model_validate(
        _split_provider_fields(cast(Mapping[str, object], value), ("name",))
    )


def _decode_milestone(value: object) -> GitHubMilestone | None:
    if value is None:
        return None
    return GitHubMilestone.model_validate(
        _split_provider_fields(cast(Mapping[str, object], value), ("title",))
    )


def _decode_ref(value: object) -> GitHubRef:
    return GitHubRef.model_validate(
        _split_provider_fields(cast(Mapping[str, object], value), ("ref", "sha"))
    )


def _decode_commit_author(value: object) -> GitHubCommitAuthor | None:
    if value is None:
        return None
    return GitHubCommitAuthor.model_validate(
        _split_provider_fields(cast(Mapping[str, object], value), ("name", "date"))
    )


def _base_issue_input(payload: Mapping[str, object]) -> dict[str, object]:
    prepared = _split_provider_fields(
        payload,
        (
            "id",
            "number",
            "title",
            "body",
            "state",
            "user",
            "labels",
            "milestone",
            "updated_at",
            "html_url",
        ),
    )
    prepared["user"] = _decode_user(prepared.get("user"))
    labels = prepared.get("labels")
    if type(labels) is not list and type(labels) is not tuple:
        raise ValueError("labels must be an array")
    prepared["labels"] = tuple(_decode_label(item) for item in cast(Sequence[object], labels))
    prepared["milestone"] = _decode_milestone(prepared.get("milestone"))
    return prepared


def _decode_issue(payload: Mapping[str, object]) -> GitHubIssue:
    return GitHubIssue.model_validate(_base_issue_input(payload))


def _decode_pull_request(payload: Mapping[str, object]) -> GitHubPullRequest:
    prepared = _base_issue_input(payload)
    provider_extra = dict(cast(Mapping[str, object], prepared["extra"]))
    for field in ("base", "head", "merge_commit_sha"):
        if field not in provider_extra:
            raise ValueError("missing pull request field")
        prepared[field] = provider_extra.pop(field)
    prepared["extra"] = provider_extra
    prepared["base"] = _decode_ref(prepared["base"])
    prepared["head"] = _decode_ref(prepared["head"])
    return GitHubPullRequest.model_validate(prepared)


def _decode_commit(payload: Mapping[str, object]) -> GitHubCommit:
    prepared = _split_provider_fields(payload, ("sha", "html_url", "commit", "author", "committer"))
    prepared["author"] = _decode_user(prepared.get("author"))
    prepared["committer"] = _decode_user(prepared.get("committer"))
    commit_value = cast(Mapping[str, object], prepared.get("commit"))
    commit = _split_provider_fields(commit_value, ("message", "author", "committer"))
    commit["author"] = _decode_commit_author(commit.get("author"))
    commit["committer"] = _decode_commit_author(commit.get("committer"))
    prepared["commit"] = GitHubCommitData.model_validate(commit)
    return GitHubCommit.model_validate(prepared)


def _decode_issue_comment(payload: Mapping[str, object]) -> GitHubIssueComment:
    prepared = _split_provider_fields(
        payload, ("id", "body", "user", "updated_at", "html_url", "issue_url")
    )
    prepared["user"] = _decode_user(prepared.get("user"))
    return GitHubIssueComment.model_validate(prepared)


def _decode_review_comment(payload: Mapping[str, object]) -> GitHubReviewComment:
    prepared = _split_provider_fields(
        payload,
        (
            "id",
            "body",
            "user",
            "updated_at",
            "html_url",
            "pull_request_url",
            "path",
            "line",
        ),
    )
    prepared["user"] = _decode_user(prepared.get("user"))
    return GitHubReviewComment.model_validate(prepared)


def _semantic_hash(payload: Mapping[str, JsonValue]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


def _validated_html_locator(
    value: str,
    expected_path: str | tuple[str, ...],
    *,
    expected_fragment: str = "",
) -> str:
    parsed = urlsplit(value)
    expected_paths = (expected_path,) if isinstance(expected_path, str) else expected_path
    canonical_path = next(
        (
            candidate
            for candidate in expected_paths
            if parsed.path.casefold() == candidate.casefold()
        ),
        None,
    )
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or canonical_path is None
        or parsed.fragment != expected_fragment
    ):
        raise ValueError("invalid GitHub locator")
    fragment = f"#{expected_fragment}" if expected_fragment else ""
    return f"https://github.com{canonical_path}{fragment}"


def _linked_number(value: str, expected_prefix: str) -> int:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.casefold().startswith(expected_prefix.casefold())
    ):
        raise ValueError("invalid linked GitHub object")
    suffix = parsed.path[len(expected_prefix) :]
    if not suffix.isascii() or not suffix.isdecimal() or int(suffix) < 1:
        raise ValueError("invalid linked GitHub object")
    return int(suffix)


class GitHubConnector:
    """Capture five GitHub object kinds behind the provider-neutral Connector port."""

    connector_type: ClassVar[str] = "github"

    def __init__(self, client: GitHubClient, *, owner: str, repository: str) -> None:
        if (
            _OWNER.fullmatch(owner) is None
            or _REPOSITORY.fullmatch(repository) is None
            or repository in {".", ".."}
        ):
            raise ValueError("invalid GitHub repository")
        self._client = client
        self.owner = owner.lower()
        self.repository_name = repository.lower()
        self.repository = f"{self.owner}/{self.repository_name}"
        self.connector_id = f"github:{self.repository}"
        self._cache: dict[tuple[str, str], RawSourceObject] = {}
        self._last_discovered: tuple[SourceObject, ...] = ()
        self._next_cursor: str | None = None
        self._checkpoint_pending = False
        self._discovery_failed = False

    @property
    def _endpoint_specs(self) -> tuple[tuple[str, str, Mapping[str, str]], ...]:
        base = f"/repos/{self.owner}/{self.repository_name}"
        return (
            (
                "issues",
                f"{base}/issues",
                {"state": "all", "per_page": "100", "sort": "updated", "direction": "asc"},
            ),
            (
                "pull_requests",
                f"{base}/pulls",
                {"state": "all", "per_page": "100", "sort": "updated", "direction": "asc"},
            ),
            ("commits", f"{base}/commits", {"per_page": "100"}),
            (
                "issue_comments",
                f"{base}/issues/comments",
                {"per_page": "100", "sort": "updated", "direction": "asc"},
            ),
            (
                "review_comments",
                f"{base}/pulls/comments",
                {"per_page": "100", "sort": "updated", "direction": "asc"},
            ),
        )

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        """Return a deterministic partial batch and defer any later failure to checkpointing."""
        if self._checkpoint_pending:
            raise ConnectorError("GitHub discovery failed")
        self._cache = {}
        self._last_discovered = ()
        self._next_cursor = None
        self._checkpoint_pending = False
        self._discovery_failed = False
        prior: GitHubCheckpoint | None = None
        try:
            prior = (
                GitHubCheckpoint(repository=self.repository)
                if cursor is None
                else GitHubCheckpoint.decode(cursor, expected_repository=self.repository)
            )
        except (TypeError, ValueError):
            prior = None
        if prior is None:
            cursor = None
            raise ConnectorError("GitHub discovery failed")
        self._checkpoint_pending = True
        discovered: tuple[SourceObject, ...] | None = None
        try:
            discovered = await self._discover_generation(prior, cursor)
        except CancelledError:
            prior = None
            cursor = None
            self.abort_sync()
            raise
        except Exception:  # noqa: BLE001 - fixed provider boundary raised below
            discovered = None
        if discovered is None:
            prior = None
            cursor = None
            self.abort_sync()
            raise ConnectorError("GitHub discovery failed")
        return discovered

    async def _discover_generation(
        self,
        prior: GitHubCheckpoint,
        cursor: str | None,
    ) -> tuple[SourceObject, ...]:
        """Populate one already-acquired generation, allowing partial endpoint durability."""

        etags = dict(prior.etags)
        newest_updated_at = prior.newest_updated_at
        newest_commit_sha = prior.newest_commit_sha
        for endpoint, path, params in self._endpoint_specs:
            try:
                result = await self._client.get_pages(path, params, etag=prior.etags.get(endpoint))
                endpoint_records = self._decode_page(endpoint, result)
                if result.not_modified:
                    if endpoint_records:
                        raise ValueError("not-modified endpoint returned objects")
                elif result.etag is None:
                    etags.pop(endpoint, None)
                else:
                    if not _valid_etag(result.etag):
                        raise ValueError("invalid ETag")
                    etags[endpoint] = result.etag
                if endpoint == "commits" and endpoint_records:
                    newest_commit_sha = endpoint_records[0][1].external_version
                for key, raw in endpoint_records:
                    if key in self._cache:
                        raise ValueError("duplicate GitHub discovery identity")
                    self._cache[key] = raw
                    if endpoint != "commits":
                        newest_updated_at = max(
                            newest_updated_at or raw.observed_at,
                            raw.observed_at,
                        )
            except Exception:  # noqa: BLE001 - provider boundary discards every failure object
                self._discovery_failed = True
                break

        discovered = tuple(
            sorted(
                (
                    SourceObject(
                        external_object_id=raw.external_object_id,
                        external_version=raw.external_version,
                        locator=raw.source_locator,
                    )
                    for raw in self._cache.values()
                ),
                key=lambda item: (item.external_object_id, item.external_version),
            )
        )
        next_state = GitHubCheckpoint(
            repository=self.repository,
            etags=etags,
            newest_updated_at=newest_updated_at,
            newest_commit_sha=newest_commit_sha,
        )
        self._next_cursor = next_state.encode() if discovered else cursor
        self._last_discovered = discovered
        return discovered

    def _decode_page(
        self,
        endpoint: str,
        result: PageResult,
    ) -> tuple[tuple[tuple[str, str], RawSourceObject], ...]:
        if result.not_modified:
            return ()
        raws: list[tuple[tuple[str, str], RawSourceObject]] = []
        seen: set[tuple[str, str]] = set()
        for item in result.items:
            if endpoint == "issues" and "pull_request" in item:
                continue
            raw = self._raw_from_provider(endpoint, item)
            key = (raw.external_object_id, raw.external_version)
            if key in seen:
                raise ValueError("duplicate GitHub discovery identity")
            seen.add(key)
            raws.append((key, raw))
        return tuple(raws)

    def _raw_from_provider(
        self,
        endpoint: str,
        item: Mapping[str, object],
    ) -> RawSourceObject:
        if endpoint == "issues":
            return self._raw_issue(_decode_issue(item))
        if endpoint == "pull_requests":
            return self._raw_pull_request(_decode_pull_request(item))
        if endpoint == "commits":
            return self._raw_commit(_decode_commit(item))
        if endpoint == "issue_comments":
            return self._raw_issue_comment(_decode_issue_comment(item))
        if endpoint == "review_comments":
            return self._raw_review_comment(_decode_review_comment(item))
        raise ValueError("unknown GitHub endpoint")

    def _raw(
        self,
        *,
        external_object_id: str,
        external_version: str,
        author: str | None,
        observed_at: datetime,
        locator: str,
        payload: Mapping[str, JsonValue],
    ) -> RawSourceObject:
        return RawSourceObject(
            connector_type=self.connector_type,
            external_object_id=external_object_id,
            external_version=external_version,
            author=author,
            observed_at=observed_at,
            source_locator=locator,
            content_hash=_semantic_hash(payload),
            payload=payload,
        )

    def _raw_issue(self, item: GitHubIssue) -> RawSourceObject:
        updated = _format_datetime(item.updated_at)
        payload: dict[str, JsonValue] = {
            "kind": "issue",
            "repository": self.repository,
            "provider_id": item.id,
            "number": item.number,
            "title": item.title,
            "body": item.body,
            "state": item.state,
            "labels": cast(JsonValue, sorted({label.name for label in item.labels})),
            "milestone": item.milestone.title if item.milestone is not None else None,
            "updated_at": updated,
        }
        return self._raw(
            external_object_id=f"github:{self.repository}:issue:{item.number}",
            external_version=updated,
            author=item.user.login if item.user is not None else None,
            observed_at=item.updated_at,
            locator=_validated_html_locator(
                item.html_url, f"/{self.owner}/{self.repository_name}/issues/{item.number}"
            ),
            payload=payload,
        )

    def _raw_pull_request(self, item: GitHubPullRequest) -> RawSourceObject:
        updated = _format_datetime(item.updated_at)
        for sha_value in (item.base.sha, item.head.sha):
            if _SHA.fullmatch(sha_value.lower()) is None:
                raise ValueError("invalid pull request SHA")
        merge_sha = item.merge_commit_sha.lower() if item.merge_commit_sha is not None else None
        if merge_sha is not None and _SHA.fullmatch(merge_sha) is None:
            raise ValueError("invalid merge SHA")
        payload: dict[str, JsonValue] = {
            "kind": "pull_request",
            "repository": self.repository,
            "provider_id": item.id,
            "number": item.number,
            "title": item.title,
            "body": item.body,
            "state": item.state,
            "labels": cast(JsonValue, sorted({label.name for label in item.labels})),
            "milestone": item.milestone.title if item.milestone is not None else None,
            "base_ref": item.base.ref,
            "base_sha": item.base.sha.lower(),
            "head_ref": item.head.ref,
            "head_sha": item.head.sha.lower(),
            "merge_commit_sha": merge_sha,
            "updated_at": updated,
        }
        return self._raw(
            external_object_id=f"github:{self.repository}:pull_request:{item.number}",
            external_version=updated,
            author=item.user.login if item.user is not None else None,
            observed_at=item.updated_at,
            locator=_validated_html_locator(
                item.html_url, f"/{self.owner}/{self.repository_name}/pull/{item.number}"
            ),
            payload=payload,
        )

    def _raw_commit(self, item: GitHubCommit) -> RawSourceObject:
        commit_sha = item.sha.lower()
        if _SHA.fullmatch(commit_sha) is None:
            raise ValueError("invalid commit SHA")
        embedded_actor = item.commit.author
        committed = item.commit.committer or embedded_actor
        observed_at = committed.date if committed is not None else _NO_COMMIT_TIME
        author = (
            item.author.login
            if item.author is not None
            else embedded_actor.name
            if embedded_actor is not None
            else None
        )
        payload: dict[str, JsonValue] = {
            "kind": "commit",
            "repository": self.repository,
            "sha": commit_sha,
            "message": item.commit.message,
            "committed_at": _format_datetime(observed_at),
        }
        return self._raw(
            external_object_id=f"github:{self.repository}:commit:{commit_sha}",
            external_version=commit_sha,
            author=author,
            observed_at=observed_at,
            locator=_validated_html_locator(
                item.html_url, f"/{self.owner}/{self.repository_name}/commit/{commit_sha}"
            ),
            payload=payload,
        )

    def _raw_issue_comment(self, item: GitHubIssueComment) -> RawSourceObject:
        number = _linked_number(
            item.issue_url,
            f"/repos/{self.owner}/{self.repository_name}/issues/",
        )
        updated = _format_datetime(item.updated_at)
        payload: dict[str, JsonValue] = {
            "kind": "issue_comment",
            "repository": self.repository,
            "provider_id": item.id,
            "body": item.body,
            "issue_number": number,
            "updated_at": updated,
        }
        return self._raw(
            external_object_id=f"github:{self.repository}:issue_comment:{item.id}",
            external_version=updated,
            author=item.user.login if item.user is not None else None,
            observed_at=item.updated_at,
            locator=_validated_html_locator(
                item.html_url,
                (
                    f"/{self.owner}/{self.repository_name}/issues/{number}",
                    f"/{self.owner}/{self.repository_name}/pull/{number}",
                ),
                expected_fragment=f"issuecomment-{item.id}",
            ),
            payload=payload,
        )

    def _raw_review_comment(self, item: GitHubReviewComment) -> RawSourceObject:
        number = _linked_number(
            item.pull_request_url,
            f"/repos/{self.owner}/{self.repository_name}/pulls/",
        )
        updated = _format_datetime(item.updated_at)
        payload: dict[str, JsonValue] = {
            "kind": "review_comment",
            "repository": self.repository,
            "provider_id": item.id,
            "body": item.body,
            "pull_request_number": number,
            "path": item.path,
            "line": item.line,
            "updated_at": updated,
        }
        return self._raw(
            external_object_id=f"github:{self.repository}:review_comment:{item.id}",
            external_version=updated,
            author=item.user.login if item.user is not None else None,
            observed_at=item.updated_at,
            locator=_validated_html_locator(
                item.html_url,
                f"/{self.owner}/{self.repository_name}/pull/{number}",
                expected_fragment=f"discussion_r{item.id}",
            ),
            payload=payload,
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        """Return only the exact immutable object cached by the latest discovery."""
        raw = self._cache.get((object_id, version))
        if raw is None:
            raise ConnectorError("GitHub fetch failed")
        return raw

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        """Normalize through the shared provider-neutral evidence constructor."""
        cached = self._cache.get((raw.external_object_id, raw.external_version))
        if raw.connector_type != self.connector_type or cached != raw:
            raise ConnectorError("GitHub normalization failed")
        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        """Consume only the immediately preceding successful discovery checkpoint."""
        consumed_evidence = tuple(normalize_raw_source(raw) for raw in self._cache.values())
        return self.finalize_checkpoint(discovered, consumed_evidence)

    def finalize_checkpoint(
        self,
        discovered: Sequence[SourceObject],
        consumed_evidence: Sequence[EvidenceRecord],
    ) -> str | None:
        """Finalize the active generation against its exact durable consumed ledger."""
        supplied = tuple(discovered)
        if (
            not self._checkpoint_pending
            or supplied != self._last_discovered
            or self._discovery_failed
        ):
            self.abort_sync()
            raise ConnectorError("GitHub checkpoint failed")
        if not consumed_evidence:
            empty_cursor = self._next_cursor
            self.abort_sync()
            return empty_cursor
        cursor: str | None = None
        failed = False
        record: EvidenceRecord | None = None
        kind: str | None = None
        state: GitHubCheckpoint | None = None
        mutable_records: list[EvidenceRecord] = []
        commit_records: list[EvidenceRecord] = []
        try:
            state = (
                GitHubCheckpoint(repository=self.repository)
                if self._next_cursor is None
                else GitHubCheckpoint.decode(
                    self._next_cursor,
                    expected_repository=self.repository,
                )
            )
            for record in consumed_evidence:
                kind = self._scoped_record_kind(record)
                if kind is None:
                    raise ValueError("foreign GitHub evidence")
                if kind == "commit":
                    commit_records.append(record)
                else:
                    mutable_records.append(record)
            newest_updated_at = (
                max(record.observed_at for record in mutable_records) if mutable_records else None
            )
            newest_commit_sha = state.newest_commit_sha
            consumed_commit_shas = {record.external_version for record in commit_records}
            if consumed_commit_shas:
                if newest_commit_sha is None:
                    newest_commit_sha = min(consumed_commit_shas)
                elif newest_commit_sha not in consumed_commit_shas:
                    raise ValueError("GitHub head is not durable")
            elif newest_commit_sha is not None:
                raise ValueError("GitHub head evidence is missing")
            cursor = GitHubCheckpoint(
                repository=self.repository,
                etags=state.etags,
                newest_updated_at=newest_updated_at,
                newest_commit_sha=newest_commit_sha,
            ).encode()
        except (TypeError, ValidationError, ValueError):
            failed = True
        if failed:
            consumed_evidence = ()
            mutable_records = []
            commit_records = []
            record = None
            kind = None
            state = None
            self.abort_sync()
            raise ConnectorError("GitHub checkpoint failed")
        self.abort_sync()
        return cursor

    def abort_sync(self) -> None:
        """Invalidate every object and cursor tied to the current generation."""
        self._cache = {}
        self._last_discovered = ()
        self._next_cursor = None
        self._checkpoint_pending = False
        self._discovery_failed = False

    def _scoped_record_kind(self, record: EvidenceRecord) -> str | None:
        if record.connector_type != self.connector_type:
            return None
        prefix = f"github:{self.repository}:"
        if not record.external_object_id.startswith(prefix):
            return None
        suffix = record.external_object_id.removeprefix(prefix)
        kind, separator, identity = suffix.partition(":")
        if not separator:
            return None
        payload = record.model_dump(mode="json")["payload"]
        if payload.get("repository") != self.repository or payload.get("kind") != kind:
            return None
        if kind == "commit":
            if (
                _SHA.fullmatch(identity) is None
                or record.external_version != identity
                or payload.get("sha") != identity
            ):
                return None
            return kind
        if kind not in {"issue", "pull_request", "issue_comment", "review_comment"}:
            return None
        if not identity.isascii() or not identity.isdecimal() or identity.startswith("0"):
            return None
        identity_field = "number" if kind in {"issue", "pull_request"} else "provider_id"
        canonical_observed_at = _format_datetime(record.observed_at)
        if (
            payload.get(identity_field) != int(identity)
            or record.external_version != canonical_observed_at
            or payload.get("updated_at") != canonical_observed_at
        ):
            return None
        return kind
