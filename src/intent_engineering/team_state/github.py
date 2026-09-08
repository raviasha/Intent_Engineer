"""Secret-safe GitHub adapter for reviewed team-state publication pull requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

import anyio
from pydantic import ConfigDict, Field

from intent_engineering.capture.github.models import PageResult
from intent_engineering.core.models._base import StrictModel
from intent_engineering.team_state.models import PreparedPublication

_REPOSITORY = re.compile(r"(?!-)(?!.*--)[a-z0-9-]{1,39}(?<!-)/[a-z0-9][a-z0-9._-]{0,99}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_REQUIRED_CHECK = "Intent Engineering / state"


class GitHubTeamStateError(ValueError):
    """One fixed public failure that retains no provider or credential material."""

    def __init__(self) -> None:
        super().__init__("GitHub team-state operation unavailable")


@dataclass(frozen=True, slots=True)
class GitHubJsonResponse:
    status_code: int
    payload: Mapping[str, object]
    headers: Mapping[str, str]


class GitHubTeamStateApi(Protocol):
    async def request_json_object(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> GitHubJsonResponse: ...

    async def get_pages(
        self,
        path: str,
        params: Mapping[str, str],
        etag: str | None = None,
    ) -> PageResult: ...

    async def aclose(self) -> None: ...


class GitHubTeamStateStatus(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)

    repository_id: str
    repository_node_id: str
    account_id: str
    login: str
    scopes: tuple[str, ...]
    private: bool
    default_branch: str
    default_branch_commit: str | None = None
    branch: Literal["intent-state"] = "intent-state"
    branch_commit: str | None = None
    branch_present: bool
    protection_compatible: bool
    codeowners_present: bool


class PublicationPullRequest(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)

    repository_id: str
    number: int = Field(gt=0)
    url: str
    created: bool


class GitHubProtectionPreview(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True)

    repository_id: str
    branch: Literal["intent-state"] = "intent-state"
    branch_creation_required: bool
    requires_change: bool
    digest: str


def _integer(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _string(value: object) -> str | None:
    return value if type(value) is str and value else None


def _scopes(headers: Mapping[str, str]) -> tuple[str, ...]:
    raw = headers.get("X-OAuth-Scopes")
    if type(raw) is not str:
        raise GitHubTeamStateError()
    values = tuple(sorted(item.strip() for item in raw.split(",") if item.strip()))
    if not values or len(values) != len(set(values)):
        raise GitHubTeamStateError()
    return values


class GitHubTeamStateClient:
    """Validate exact GitHub authority and open one idempotent publication PR."""

    def __init__(
        self,
        api: GitHubTeamStateApi,
        *,
        expected_account_id: str,
        expected_login: str,
        allow_public_repository: bool = False,
    ) -> None:
        if (
            type(expected_account_id) is not str
            or not expected_account_id.isdecimal()
            or type(expected_login) is not str
            or not expected_login
        ):
            raise GitHubTeamStateError()
        self._api = api
        self._account_id = expected_account_id
        self._login = expected_login
        self._allow_public = allow_public_repository
        self._reviewed: GitHubTeamStateStatus | None = None

    async def _inspect(self, repository: str) -> GitHubTeamStateStatus:
        if type(repository) is not str or _REPOSITORY.fullmatch(repository) is None:
            raise GitHubTeamStateError()
        repository_path = f"/repos/{repository}"
        repo = await self._api.request_json_object("GET", repository_path)
        repo_id = _integer(repo.payload.get("id"))
        full_name = _string(repo.payload.get("full_name"))
        private = repo.payload.get("private")
        default_branch = _string(repo.payload.get("default_branch"))
        scopes = _scopes(repo.headers)
        if (
            repo.status_code != 200
            or repo_id is None
            or full_name != repository
            or type(private) is not bool
            or default_branch is None
            or (private and "repo" not in scopes)
            or (not private and (not self._allow_public or "public_repo" not in scopes))
        ):
            raise GitHubTeamStateError()

        user = await self._api.request_json_object("GET", "/user")
        account_id = _integer(user.payload.get("id"))
        login = _string(user.payload.get("login"))
        if str(account_id) != self._account_id or login != self._login:
            raise GitHubTeamStateError()

        branch = await self._api.request_json_object(
            "GET",
            f"{repository_path}/branches/intent-state",
            allowed_statuses=frozenset({200, 404}),
        )
        branch_commit: str | None = None
        default_branch_commit: str | None = None
        protection_compatible = False
        if branch.status_code == 200:
            commit = branch.payload.get("commit")
            branch_commit = _string(commit.get("sha")) if type(commit) is dict else None
            if (
                branch.payload.get("name") != "intent-state"
                or branch_commit is None
                or _COMMIT.fullmatch(branch_commit) is None
            ):
                raise GitHubTeamStateError()
            protection = await self._api.request_json_object(
                "GET",
                f"{repository_path}/branches/intent-state/protection",
                allowed_statuses=frozenset({200, 404}),
            )
            if protection.status_code == 200:
                protection_compatible = branch.payload.get(
                    "protected"
                ) is True and self._compatible_protection(protection.payload)
        elif branch.status_code != 404:
            raise GitHubTeamStateError()
        else:
            default = await self._api.request_json_object(
                "GET",
                f"{repository_path}/branches/{default_branch}",
            )
            default_commit = default.payload.get("commit")
            default_branch_commit = (
                _string(default_commit.get("sha")) if type(default_commit) is dict else None
            )
            if (
                default.payload.get("name") != default_branch
                or default_branch_commit is None
                or _COMMIT.fullmatch(default_branch_commit) is None
            ):
                raise GitHubTeamStateError()

        codeowners = await self._api.request_json_object(
            "GET",
            f"{repository_path}/contents/.github/CODEOWNERS",
            params={"ref": default_branch},
            allowed_statuses=frozenset({200, 404}),
        )
        if codeowners.status_code not in {200, 404}:
            raise GitHubTeamStateError()
        codeowners_present = codeowners.status_code == 200
        if codeowners_present and (
            codeowners.payload.get("path") != ".github/CODEOWNERS"
            or codeowners.payload.get("type") != "file"
        ):
            raise GitHubTeamStateError()
        return GitHubTeamStateStatus(
            repository_id=f"github.com/{repository}",
            repository_node_id=str(repo_id),
            account_id=str(account_id),
            login=login,
            scopes=scopes,
            private=private,
            default_branch=default_branch,
            default_branch_commit=default_branch_commit,
            branch_commit=branch_commit,
            branch_present=branch.status_code == 200,
            protection_compatible=protection_compatible,
            codeowners_present=codeowners_present,
        )

    @staticmethod
    def _compatible_protection(payload: Mapping[str, object]) -> bool:
        admins = payload.get("enforce_admins")
        reviews = payload.get("required_pull_request_reviews")
        checks = payload.get("required_status_checks")
        if type(admins) is not dict or type(reviews) is not dict or type(checks) is not dict:
            return False
        contexts = checks.get("contexts")
        return (
            admins.get("enabled") is True
            and reviews.get("dismiss_stale_reviews") is True
            and reviews.get("require_code_owner_reviews") is True
            and type(reviews.get("required_approving_review_count")) is int
            and reviews["required_approving_review_count"] >= 1
            and checks.get("strict") is True
            and type(contexts) is list
            and _REQUIRED_CHECK in contexts
        )

    @staticmethod
    def _protection_payload() -> dict[str, object]:
        return {
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": True,
                "required_approving_review_count": 1,
            },
            "required_status_checks": {
                "contexts": [_REQUIRED_CHECK],
                "strict": True,
            },
            "restrictions": None,
        }

    def protection_preview(self) -> GitHubProtectionPreview:
        reviewed = self._reviewed
        if reviewed is None:
            raise GitHubTeamStateError()
        content = json.dumps(
            {
                "branch_commit": reviewed.branch_commit,
                "default_branch_commit": reviewed.default_branch_commit,
                "protection": self._protection_payload(),
                "repository_id": reviewed.repository_id,
                "repository_node_id": reviewed.repository_node_id,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return GitHubProtectionPreview(
            repository_id=reviewed.repository_id,
            branch_creation_required=not reviewed.branch_present,
            requires_change=not reviewed.protection_compatible,
            digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        )

    async def configure_protection(self, confirmation: str) -> GitHubTeamStateStatus:
        reviewed = self._reviewed
        if reviewed is None:
            raise GitHubTeamStateError()
        preview = self.protection_preview()
        if confirmation != preview.digest:
            raise GitHubTeamStateError()
        if not preview.requires_change:
            return reviewed
        repository = reviewed.repository_id.removeprefix("github.com/")
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            live = await self._inspect(repository)
            if live != reviewed:
                raise GitHubTeamStateError()
            if not reviewed.branch_present:
                tree = await self._api.request_json_object(
                    "POST",
                    f"/repos/{repository}/git/trees",
                    payload={"tree": []},
                    allowed_statuses=frozenset({201}),
                )
                tree_sha = _string(tree.payload.get("sha"))
                if tree_sha is None or _COMMIT.fullmatch(tree_sha) is None:
                    raise GitHubTeamStateError()
                identity = {
                    "date": "1970-01-01T00:00:00Z",
                    "email": "intent-state@localhost",
                    "name": "Intent Engineering",
                }
                commit = await self._api.request_json_object(
                    "POST",
                    f"/repos/{repository}/git/commits",
                    payload={
                        "author": identity,
                        "committer": identity,
                        "message": "Initialize isolated intent state",
                        "parents": [],
                        "tree": tree_sha,
                    },
                    allowed_statuses=frozenset({201}),
                )
                branch_commit = _string(commit.payload.get("sha"))
                commit_tree = commit.payload.get("tree")
                if (
                    branch_commit is None
                    or _COMMIT.fullmatch(branch_commit) is None
                    or type(commit_tree) is not dict
                    or commit_tree.get("sha") != tree_sha
                    or commit.payload.get("parents") != []
                ):
                    raise GitHubTeamStateError()
                created = await self._api.request_json_object(
                    "POST",
                    f"/repos/{repository}/git/refs",
                    payload={"ref": "refs/heads/intent-state", "sha": branch_commit},
                    allowed_statuses=frozenset({201}),
                )
                created_object = created.payload.get("object")
                if (
                    created.payload.get("ref") != "refs/heads/intent-state"
                    or type(created_object) is not dict
                    or created_object.get("sha") != branch_commit
                ):
                    raise GitHubTeamStateError()
                confirmed = await self._api.request_json_object(
                    "GET",
                    f"/repos/{repository}/branches/intent-state",
                )
                confirmed_commit = confirmed.payload.get("commit")
                if (
                    confirmed.payload.get("name") != "intent-state"
                    or type(confirmed_commit) is not dict
                    or confirmed_commit.get("sha") != branch_commit
                ):
                    raise GitHubTeamStateError()
                reviewed = reviewed.model_copy(
                    update={
                        "branch_commit": branch_commit,
                        "branch_present": True,
                        "default_branch_commit": None,
                    }
                )
                self._reviewed = reviewed
            response = await self._api.request_json_object(
                "PUT",
                f"/repos/{repository}/branches/intent-state/protection",
                payload=self._protection_payload(),
            )
            if not self._compatible_protection(response.payload):
                raise GitHubTeamStateError()
            updated = reviewed.model_copy(update={"protection_compatible": True})
            self._reviewed = updated
            return updated
        except cancelled_class:
            with anyio.CancelScope(shield=True):
                await self._api.aclose()
            raise
        except GitHubTeamStateError:
            raise
        except Exception as error:  # noqa: BLE001 - replace the provider boundary
            error.__traceback__ = None
            raise GitHubTeamStateError() from None

    async def inspect(self, repository: str) -> GitHubTeamStateStatus:
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            reviewed = await self._inspect(repository)
        except cancelled_class:
            with anyio.CancelScope(shield=True):
                await self._api.aclose()
            raise
        except GitHubTeamStateError:
            raise
        except Exception as error:  # noqa: BLE001 - replace the provider boundary
            error.__traceback__ = None
            raise GitHubTeamStateError() from None
        self._reviewed = reviewed
        return reviewed

    async def open_publication_pr(self, publication: PreparedPublication) -> PublicationPullRequest:
        reviewed = self._reviewed
        if reviewed is None or type(publication) is not PreparedPublication:
            raise GitHubTeamStateError()
        publication = PreparedPublication.model_validate(publication.model_dump(mode="python"))
        repository = reviewed.repository_id.removeprefix("github.com/")
        if publication.repository_id != reviewed.repository_id:
            raise GitHubTeamStateError()
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            current = await self._inspect(repository)
            if current != reviewed or not current.protection_compatible:
                raise GitHubTeamStateError()
            pages = await self._api.get_pages(
                f"/repos/{repository}/pulls",
                {
                    "base": "intent-state",
                    "head": f"{repository.split('/', 1)[0]}:{publication.branch}",
                    "per_page": "100",
                    "state": "open",
                },
            )
            matches = tuple(
                item
                for item in pages.items
                if self._matches_publication_pr(item, publication.branch, repository)
            )
            if len(matches) > 1:
                raise GitHubTeamStateError()
            if matches:
                return self._pull_request(reviewed.repository_id, matches[0], created=False)
            body = {
                "base": "intent-state",
                "body": f"Encrypted intent state `{publication.manifest.bundle_digest}`.",
                "draft": False,
                "head": publication.branch,
                "title": f"Publish intent state v{publication.manifest.graph_version}",
            }
            response = await self._api.request_json_object(
                "POST",
                f"/repos/{repository}/pulls",
                payload=body,
                allowed_statuses=frozenset({201, 422}),
            )
            if response.status_code == 422:
                repeated = await self._api.get_pages(
                    f"/repos/{repository}/pulls",
                    {
                        "base": "intent-state",
                        "head": f"{repository.split('/', 1)[0]}:{publication.branch}",
                        "per_page": "100",
                        "state": "open",
                    },
                )
                matches = tuple(
                    item
                    for item in repeated.items
                    if self._matches_publication_pr(item, publication.branch, repository)
                )
                if len(matches) != 1:
                    raise GitHubTeamStateError()
                return self._pull_request(reviewed.repository_id, matches[0], created=False)
            return self._pull_request(reviewed.repository_id, response.payload, created=True)
        except cancelled_class:
            with anyio.CancelScope(shield=True):
                await self._api.aclose()
            raise
        except GitHubTeamStateError:
            raise
        except Exception as error:  # noqa: BLE001 - replace the provider boundary
            error.__traceback__ = None
            raise GitHubTeamStateError() from None

    @staticmethod
    def _matches_publication_pr(item: Mapping[str, object], branch: str, repository: str) -> bool:
        head = item.get("head")
        base = item.get("base")
        head_repo = head.get("repo") if isinstance(head, Mapping) else None
        base_repo = base.get("repo") if isinstance(base, Mapping) else None
        return (
            isinstance(head, Mapping)
            and head.get("ref") == branch
            and isinstance(head_repo, Mapping)
            and head_repo.get("full_name") == repository
            and isinstance(base, Mapping)
            and base.get("ref") == "intent-state"
            and isinstance(base_repo, Mapping)
            and base_repo.get("full_name") == repository
        )

    @staticmethod
    def _pull_request(
        repository_id: str, payload: Mapping[str, object], *, created: bool
    ) -> PublicationPullRequest:
        number = _integer(payload.get("number"))
        url = _string(payload.get("html_url"))
        if (
            number is None
            or url is None
            or url
            != f"https://github.com/{repository_id.removeprefix('github.com/')}/pull/{number}"
        ):
            raise GitHubTeamStateError()
        return PublicationPullRequest(
            repository_id=repository_id,
            number=number,
            url=url,
            created=created,
        )

    async def aclose(self) -> None:
        await self._api.aclose()


__all__ = [
    "GitHubJsonResponse",
    "GitHubProtectionPreview",
    "GitHubTeamStateClient",
    "GitHubTeamStateError",
    "GitHubTeamStateStatus",
    "PublicationPullRequest",
]
