"""Secret-safe GitHub adapter for reviewed team-state publication pull requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

import anyio
from pydantic import ConfigDict, Field

from intent_engineering.capture.github.models import PageResult
from intent_engineering.core.models._base import StrictModel
from intent_engineering.team_state.models import PreparedPublication

_REPOSITORY = re.compile(r"(?!-)(?!.*--)[a-z0-9-]{1,39}(?<!-)/[a-z0-9][a-z0-9._-]{0,99}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_REQUIRED_CHECK = "Intent Engineering / state"
_MAX_PROTECTION_SNAPSHOT_BYTES = 64 * 1024
_MAX_PROTECTION_COLLECTION_ITEMS = 256
_MAX_PROTECTION_DEPTH = 8
_MAX_PROTECTION_STRING_LENGTH = 4096

ProtectionContext = Annotated[str, Field(min_length=1, max_length=255)]


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


class GitHubProtectionPolicy(StrictModel):
    """Bounded, secret-safe projection plus exact digest of one provider policy."""

    model_config = ConfigDict(frozen=True, strict=True)

    snapshot_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    enforce_admins: bool | None
    allow_deletions: bool | None
    allow_force_pushes: bool | None
    required_linear_history: bool | None
    dismiss_stale_reviews: bool | None
    require_code_owner_reviews: bool | None
    required_approving_review_count: int | None = Field(default=None, ge=0, le=6)
    required_status_checks_strict: bool | None
    required_status_check_contexts: tuple[ProtectionContext, ...] = Field(max_length=100)
    restrictions_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


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
    protection_policy: GitHubProtectionPolicy | None = None
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
    before_policy: GitHubProtectionPolicy | None
    after_policy: GitHubProtectionPolicy
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


def _permissions(payload: object) -> tuple[str, ...]:
    names = ("admin", "maintain", "pull", "push", "triage")
    if not isinstance(payload, Mapping) or any(
        type(payload.get(name)) is not bool for name in names
    ):
        raise GitHubTeamStateError()
    granted = tuple(name for name in names if payload[name] is True)
    if not ({"admin", "maintain"} & set(granted)) or "push" not in granted or "pull" not in granted:
        raise GitHubTeamStateError()
    return granted


def _bounded_json(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_PROTECTION_DEPTH:
        raise GitHubTeamStateError()
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        if not value or len(value) > _MAX_PROTECTION_STRING_LENGTH:
            raise GitHubTeamStateError()
        return value
    if type(value) is list:
        if len(value) > _MAX_PROTECTION_COLLECTION_ITEMS:
            raise GitHubTeamStateError()
        return [_bounded_json(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_PROTECTION_COLLECTION_ITEMS:
            raise GitHubTeamStateError()
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 255:
                raise GitHubTeamStateError()
            normalized[key] = _bounded_json(item, depth=depth + 1)
        return normalized
    raise GitHubTeamStateError()


def _json_digest(value: object) -> str:
    canonical = json.dumps(
        _bounded_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(canonical) > _MAX_PROTECTION_SNAPSHOT_BYTES:
        raise GitHubTeamStateError()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _enabled(value: object) -> bool | None:
    if type(value) is not dict:
        return None
    enabled = value.get("enabled")
    if type(enabled) is not bool:
        return None
    return enabled


def _protection_policy(payload: Mapping[str, object]) -> GitHubProtectionPolicy:
    reviews = payload.get("required_pull_request_reviews")
    checks = payload.get("required_status_checks")
    review_mapping = reviews if type(reviews) is dict else {}
    check_mapping = checks if type(checks) is dict else {}
    count = review_mapping.get("required_approving_review_count")
    if type(count) is not int or not 0 <= count <= 6:
        count = None
    contexts = check_mapping.get("contexts")
    if (
        type(contexts) is not list
        or len(contexts) > 100
        or any(type(item) is not str or not item or len(item) > 255 for item in contexts)
        or len(contexts) != len(set(contexts))
    ):
        contexts = []
    restrictions_digest = (
        _json_digest(payload["restrictions"]) if "restrictions" in payload else None
    )
    return GitHubProtectionPolicy(
        snapshot_digest=_json_digest(dict(payload)),
        enforce_admins=_enabled(payload.get("enforce_admins")),
        allow_deletions=_enabled(payload.get("allow_deletions")),
        allow_force_pushes=_enabled(payload.get("allow_force_pushes")),
        required_linear_history=_enabled(payload.get("required_linear_history")),
        dismiss_stale_reviews=(
            review_mapping.get("dismiss_stale_reviews")
            if type(review_mapping.get("dismiss_stale_reviews")) is bool
            else None
        ),
        require_code_owner_reviews=(
            review_mapping.get("require_code_owner_reviews")
            if type(review_mapping.get("require_code_owner_reviews")) is bool
            else None
        ),
        required_approving_review_count=count,
        required_status_checks_strict=(
            check_mapping.get("strict") if type(check_mapping.get("strict")) is bool else None
        ),
        required_status_check_contexts=tuple(contexts),
        restrictions_digest=restrictions_digest,
    )


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
        _permissions(repo.payload.get("permissions"))
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
        protection_policy: GitHubProtectionPolicy | None = None
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
                protection_policy = _protection_policy(protection.payload)
                protection_compatible = branch.payload.get(
                    "protected"
                ) is True and self._compatible_protection(protection_policy)
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
            protection_policy=protection_policy,
            codeowners_present=codeowners_present,
        )

    @staticmethod
    def _compatible_protection(policy: GitHubProtectionPolicy) -> bool:
        return (
            policy.enforce_admins is True
            and policy.allow_deletions is False
            and policy.allow_force_pushes is False
            and policy.required_linear_history is True
            and policy.dismiss_stale_reviews is True
            and policy.require_code_owner_reviews is False
            and policy.required_approving_review_count is not None
            and policy.required_approving_review_count >= 1
            and policy.required_status_checks_strict is True
            and _REQUIRED_CHECK in policy.required_status_check_contexts
        )

    @staticmethod
    def _protection_payload() -> dict[str, object]:
        return {
            "enforce_admins": True,
            "allow_deletions": False,
            "allow_force_pushes": False,
            "required_linear_history": True,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": False,
                "required_approving_review_count": 1,
            },
            "required_status_checks": {
                "contexts": [_REQUIRED_CHECK],
                "strict": True,
            },
            "restrictions": None,
        }

    @staticmethod
    def _required_policy() -> GitHubProtectionPolicy:
        return _protection_policy(
            {
                "enforce_admins": {"enabled": True},
                "allow_deletions": {"enabled": False},
                "allow_force_pushes": {"enabled": False},
                "required_linear_history": {"enabled": True},
                "required_pull_request_reviews": {
                    "dismiss_stale_reviews": True,
                    "require_code_owner_reviews": False,
                    "required_approving_review_count": 1,
                },
                "required_status_checks": {
                    "contexts": [_REQUIRED_CHECK],
                    "strict": True,
                },
                "restrictions": None,
            }
        )

    def protection_preview(self) -> GitHubProtectionPreview:
        reviewed = self._reviewed
        if reviewed is None:
            raise GitHubTeamStateError()
        before_policy = reviewed.protection_policy
        after_policy = (
            before_policy
            if reviewed.protection_compatible and before_policy is not None
            else self._required_policy()
        )
        content = json.dumps(
            {
                "after_policy": after_policy.model_dump(mode="json"),
                "before_policy": (
                    None if before_policy is None else before_policy.model_dump(mode="json")
                ),
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
            before_policy=before_policy,
            after_policy=after_policy,
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
        if reviewed.branch_present:
            raise GitHubTeamStateError()
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
                live_created = await self._inspect(repository)
                if live_created != reviewed:
                    raise GitHubTeamStateError()
            response = await self._api.request_json_object(
                "PUT",
                f"/repos/{repository}/branches/intent-state/protection",
                payload=self._protection_payload(),
            )
            response_policy = _protection_policy(response.payload)
            if not self._compatible_protection(response_policy):
                raise GitHubTeamStateError()
            updated = reviewed.model_copy(
                update={
                    "protection_compatible": True,
                    "protection_policy": response_policy,
                }
            )
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

    async def _require_live_publication(
        self,
        repository: str,
        publication: PreparedPublication,
        expected_head_commit: str,
        expected_base_commit: str,
    ) -> None:
        current = await self._inspect(repository)
        if (
            current != self._reviewed
            or current.branch_commit != expected_base_commit
            or not current.protection_compatible
        ):
            raise GitHubTeamStateError()
        response = await self._api.request_json_object(
            "GET",
            f"/repos/{repository}/git/ref/heads/{publication.branch}",
        )
        self._validate_publication_ref(response.payload, publication.branch, expected_head_commit)

    async def open_publication_pr(
        self,
        publication: PreparedPublication,
        *,
        expected_head_commit: str,
    ) -> PublicationPullRequest:
        reviewed = self._reviewed
        if reviewed is None or type(publication) is not PreparedPublication:
            raise GitHubTeamStateError()
        publication = PreparedPublication.model_validate(publication.model_dump(mode="python"))
        repository = reviewed.repository_id.removeprefix("github.com/")
        expected_base_commit = reviewed.branch_commit
        if (
            publication.repository_id != reviewed.repository_id
            or type(expected_head_commit) is not str
            or _COMMIT.fullmatch(expected_head_commit) is None
            or expected_base_commit is None
            or _COMMIT.fullmatch(expected_base_commit) is None
        ):
            raise GitHubTeamStateError()
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            await self._require_live_publication(
                repository, publication, expected_head_commit, expected_base_commit
            )
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
                if self._matches_publication_pr(
                    item,
                    publication.branch,
                    repository,
                    expected_head_commit,
                    expected_base_commit,
                )
            )
            if len(matches) > 1:
                raise GitHubTeamStateError()
            if matches:
                await self._require_live_publication(
                    repository, publication, expected_head_commit, expected_base_commit
                )
                return self._pull_request(
                    reviewed.repository_id,
                    matches[0],
                    publication.branch,
                    expected_head_commit,
                    expected_base_commit,
                    created=False,
                )
            body = {
                "base": "intent-state",
                "body": f"Encrypted intent state `{publication.manifest.bundle_digest}`.",
                "draft": False,
                "head": publication.branch,
                "title": f"Publish intent state v{publication.manifest.graph_version}",
            }
            await self._require_live_publication(
                repository, publication, expected_head_commit, expected_base_commit
            )
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
                    if self._matches_publication_pr(
                        item,
                        publication.branch,
                        repository,
                        expected_head_commit,
                        expected_base_commit,
                    )
                )
                if len(matches) != 1:
                    raise GitHubTeamStateError()
                await self._require_live_publication(
                    repository, publication, expected_head_commit, expected_base_commit
                )
                return self._pull_request(
                    reviewed.repository_id,
                    matches[0],
                    publication.branch,
                    expected_head_commit,
                    expected_base_commit,
                    created=False,
                )
            return self._pull_request(
                reviewed.repository_id,
                response.payload,
                publication.branch,
                expected_head_commit,
                expected_base_commit,
                created=True,
            )
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
    def _validate_publication_ref(
        payload: Mapping[str, object], branch: str, expected_head_commit: str
    ) -> None:
        target = payload.get("object")
        if (
            payload.get("ref") != f"refs/heads/{branch}"
            or not isinstance(target, Mapping)
            or target.get("type") != "commit"
            or target.get("sha") != expected_head_commit
        ):
            raise GitHubTeamStateError()

    @staticmethod
    def _matches_publication_pr(
        item: Mapping[str, object],
        branch: str,
        repository: str,
        expected_head_commit: str,
        expected_base_commit: str,
    ) -> bool:
        head = item.get("head")
        base = item.get("base")
        head_repo = head.get("repo") if isinstance(head, Mapping) else None
        base_repo = base.get("repo") if isinstance(base, Mapping) else None
        return (
            isinstance(head, Mapping)
            and head.get("ref") == branch
            and head.get("sha") == expected_head_commit
            and isinstance(head_repo, Mapping)
            and head_repo.get("full_name") == repository
            and isinstance(base, Mapping)
            and base.get("ref") == "intent-state"
            and base.get("sha") == expected_base_commit
            and isinstance(base_repo, Mapping)
            and base_repo.get("full_name") == repository
        )

    @staticmethod
    def _pull_request(
        repository_id: str,
        payload: Mapping[str, object],
        branch: str,
        expected_head_commit: str,
        expected_base_commit: str,
        *,
        created: bool,
    ) -> PublicationPullRequest:
        number = _integer(payload.get("number"))
        url = _string(payload.get("html_url"))
        if (
            number is None
            or url is None
            or url
            != f"https://github.com/{repository_id.removeprefix('github.com/')}/pull/{number}"
            or not GitHubTeamStateClient._matches_publication_pr(
                payload,
                branch,
                repository_id.removeprefix("github.com/"),
                expected_head_commit,
                expected_base_commit,
            )
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
    "GitHubProtectionPolicy",
    "GitHubProtectionPreview",
    "GitHubTeamStateClient",
    "GitHubTeamStateError",
    "GitHubTeamStateStatus",
    "PublicationPullRequest",
]
