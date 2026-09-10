"""Secret-safe GitHub adapter for reviewed team-state publication pull requests."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, cast

import anyio
from pydantic import ConfigDict, Field

from intent_engineering.capture.github.models import PageResult
from intent_engineering.core.models._base import StrictModel
from intent_engineering.team_state.enrollment import (
    EnrollmentTransitionProofV2,
    PreparedEnrollmentPublicationV2,
    authenticate_enrollment_publication,
)
from intent_engineering.team_state.models import PreparedPublication
from intent_engineering.team_state.publication import PreparedPublicationV2, PreparedV1Migration

_REPOSITORY = re.compile(r"(?!-)(?!.*--)[a-z0-9-]{1,39}(?<!-)/[a-z0-9][a-z0-9._-]{0,99}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_REQUIRED_CHECK = "Intent Engineering / state"
_CODE_REQUIRED_CHECK = "Intent Engineering / check"
_GITHUB_ACTIONS_APP_ID = 15368
_MAX_PROTECTION_SNAPSHOT_BYTES = 64 * 1024
_MAX_PROTECTION_COLLECTION_ITEMS = 256
_MAX_PROTECTION_DEPTH = 8
_MAX_PROTECTION_STRING_LENGTH = 4096
_MAX_TOOLING_FILE_BYTES = 512 * 1024
_MAX_TOOLING_TREE_ENTRIES = 256
_MAX_RUNNER_GROUP_PAGES = 10
_RUNNER_GROUPS_PER_PAGE = 100
_MAX_RUNNER_GROUPS = _MAX_RUNNER_GROUP_PAGES * _RUNNER_GROUPS_PER_PAGE

ProtectionContext = Annotated[str, Field(min_length=1, max_length=255)]
StatusCheckAppId = Annotated[int, Field(ge=-1)]
PublicationArtifacts = (
    PreparedPublication
    | PreparedV1Migration
    | PreparedPublicationV2
    | PreparedEnrollmentPublicationV2
)


def _publication_artifacts(
    value: object,
    transition_proof: EnrollmentTransitionProofV2 | None = None,
) -> PublicationArtifacts:
    try:
        if type(value) is PreparedPublication:
            if transition_proof is not None:
                raise GitHubTeamStateError()
            return PreparedPublication.model_validate(value.model_dump(mode="python"))
        if type(value) is PreparedPublicationV2:
            if transition_proof is not None:
                raise GitHubTeamStateError()
            publication_v2 = value
            return PreparedPublicationV2(
                repository_id=publication_v2.repository_id,
                branch=publication_v2.branch,
                manifest=publication_v2.manifest,
                manifest_bytes=publication_v2.manifest_bytes,
                bundle=publication_v2.bundle,
                envelope=publication_v2.envelope,
                signatures=publication_v2.signatures,
                bundle_path=publication_v2.bundle_path,
                signature_path=publication_v2.signature_path,
                authority=publication_v2.authority,
            )
        if type(value) is PreparedV1Migration:
            if transition_proof is not None:
                raise GitHubTeamStateError()
            migration = value
            return PreparedV1Migration(
                repository_id=migration.repository_id,
                branch=migration.branch,
                manifest=migration.manifest,
                manifest_bytes=migration.manifest_bytes,
                bundle=migration.bundle,
                envelope=migration.envelope,
                signatures=migration.signatures,
                bundle_path=migration.bundle_path,
                signature_path=migration.signature_path,
                authority=migration.authority,
            )
        if type(value) is PreparedEnrollmentPublicationV2:
            if transition_proof is None:
                raise GitHubTeamStateError()
            enrollment_publication = value
            candidate = PreparedEnrollmentPublicationV2(
                repository_id=enrollment_publication.repository_id,
                branch=enrollment_publication.branch,
                manifest=enrollment_publication.manifest,
                manifest_bytes=enrollment_publication.manifest_bytes,
                bundle=enrollment_publication.bundle,
                envelope=enrollment_publication.envelope,
                signatures=enrollment_publication.signatures,
                bundle_path=enrollment_publication.bundle_path,
                signature_path=enrollment_publication.signature_path,
                authority=enrollment_publication.authority,
            )
            return authenticate_enrollment_publication(candidate, transition_proof)
    except Exception as error:  # noqa: BLE001 - fixed public adapter boundary
        error.__traceback__ = None
        raise GitHubTeamStateError() from None
    raise GitHubTeamStateError()


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

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        accept: str,
    ) -> bytes: ...

    async def aclose(self) -> None: ...


class GitHubRequiredStatusCheck(StrictModel):
    """One provider-bound required check returned by branch protection."""

    model_config = ConfigDict(frozen=True, strict=True)

    context: ProtectionContext
    app_id: StatusCheckAppId | None


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
    bypass_pull_request_allowances_empty: bool | None
    required_status_checks_strict: bool | None
    required_status_check_contexts: tuple[ProtectionContext, ...] = Field(max_length=100)
    required_status_checks: tuple[GitHubRequiredStatusCheck, ...] = Field(max_length=100)
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


class GitHubDefaultBranchTooling(StrictModel):
    """Exact protected default-branch tooling reviewed before state mutations."""

    model_config = ConfigDict(frozen=True, strict=True)

    repository_id: str
    branch: str = Field(min_length=1, max_length=255)
    commit: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    protection_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    codeowners_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workflow_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    check_workflow_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workflows_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runner_group_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runner_membership_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    required_workflow_ruleset_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class GitHubDefaultBranchBaseline(StrictModel):
    """Exact pre-staging proof that code changes cannot bypass human review."""

    model_config = ConfigDict(frozen=True, strict=True)

    repository_id: str
    branch: str = Field(min_length=1, max_length=255)
    commit: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    protection_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


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
    if "admin" not in granted or "push" not in granted or "pull" not in granted:
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


def _bypass_allowances_empty(value: object) -> bool | None:
    if value is None:
        return True
    if type(value) is not dict:
        return None
    if set(value) != {"users", "teams", "apps"}:
        return None
    if any(type(value[name]) is not list for name in ("users", "teams", "apps")):
        return None
    return all(value[name] == [] for name in ("users", "teams", "apps"))


def _required_checks(value: object) -> tuple[GitHubRequiredStatusCheck, ...]:
    if type(value) is not list or len(value) > 100:
        return ()
    parsed: list[GitHubRequiredStatusCheck] = []
    identities: set[tuple[str, int | None]] = set()
    for item in value:
        if type(item) is not dict:
            return ()
        context = item.get("context")
        app_id = item.get("app_id")
        if (
            type(context) is not str
            or not context
            or len(context) > 255
            or (app_id is not None and (type(app_id) is not int or app_id < -1))
        ):
            return ()
        identity = (context, app_id)
        if identity in identities:
            return ()
        identities.add(identity)
        parsed.append(GitHubRequiredStatusCheck(context=context, app_id=app_id))
    return tuple(parsed)


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
        bypass_pull_request_allowances_empty=_bypass_allowances_empty(
            review_mapping.get("bypass_pull_request_allowances")
        ),
        required_status_checks_strict=(
            check_mapping.get("strict") if type(check_mapping.get("strict")) is bool else None
        ),
        required_status_check_contexts=tuple(contexts),
        required_status_checks=_required_checks(check_mapping.get("checks")),
        restrictions_digest=restrictions_digest,
    )


def _sha(value: object) -> str:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise GitHubTeamStateError()
    return value


def _tree_entries(payload: Mapping[str, object], tree_sha: str) -> dict[str, tuple[str, str, str]]:
    entries = payload.get("tree")
    if (
        payload.get("sha") != tree_sha
        or payload.get("truncated") is not False
        or type(entries) is not list
        or len(entries) > _MAX_TOOLING_TREE_ENTRIES
    ):
        raise GitHubTeamStateError()
    parsed: dict[str, tuple[str, str, str]] = {}
    for entry in entries:
        if (
            not isinstance(entry, Mapping)
            or type(entry.get("path")) is not str
            or not entry["path"]
            or len(entry["path"]) > 255
            or type(entry.get("mode")) is not str
            or type(entry.get("type")) is not str
            or entry["path"] in parsed
        ):
            raise GitHubTeamStateError()
        parsed[entry["path"]] = (entry["mode"], entry["type"], _sha(entry.get("sha")))
    return parsed


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
        self._tooling: GitHubDefaultBranchTooling | None = None
        self._baseline: GitHubDefaultBranchBaseline | None = None

    async def _organization_runner_groups(
        self, owner: str, repository: str
    ) -> list[Mapping[str, object]]:
        groups: list[Mapping[str, object]] = []
        seen_ids: set[int] = set()
        expected_total: int | None = None
        for page in range(1, _MAX_RUNNER_GROUP_PAGES + 1):
            response = await self._api.request_json_object(
                "GET",
                f"/orgs/{owner}/actions/runner-groups",
                params={
                    "page": str(page),
                    "per_page": str(_RUNNER_GROUPS_PER_PAGE),
                    "visible_to_repository": repository,
                },
            )
            total = response.payload.get("total_count")
            page_groups = response.payload.get("runner_groups")
            if (
                type(total) is not int
                or total < 0
                or total > _MAX_RUNNER_GROUPS
                or type(page_groups) is not list
                or len(page_groups) > _RUNNER_GROUPS_PER_PAGE
                or (expected_total is not None and total != expected_total)
            ):
                raise GitHubTeamStateError()
            expected_total = total
            for group in page_groups:
                if not isinstance(group, Mapping):
                    raise GitHubTeamStateError()
                group_id = _integer(group.get("id"))
                if group_id is None or group_id in seen_ids:
                    raise GitHubTeamStateError()
                seen_ids.add(group_id)
                groups.append(group)
            if len(groups) == total:
                return groups
            if len(groups) > total or len(page_groups) != _RUNNER_GROUPS_PER_PAGE:
                raise GitHubTeamStateError()
        raise GitHubTeamStateError()

    async def _required_workflow_rule_snapshot(
        self,
        reviewed: GitHubTeamStateStatus,
        *,
        target_branch: str,
        workflow_path: str,
        do_not_enforce_on_create: bool,
        commit_sha: str,
    ) -> dict[str, object]:
        repository = reviewed.repository_id.removeprefix("github.com/")
        owner, _ = repository.split("/", 1)
        effective_rules = await self._api.get_pages(
            f"/repos/{repository}/rules/branches/{target_branch}",
            {"per_page": "100"},
        )
        effective_items = effective_rules.model_dump(mode="json")["items"]
        workflow_rules = [
            rule
            for rule in effective_items
            if type(rule) is dict and rule.get("type") == "workflows"
        ]
        if effective_rules.not_modified or len(workflow_rules) != 1:
            raise GitHubTeamStateError()
        workflow_rule = workflow_rules[0]
        parameters = workflow_rule.get("parameters")
        ruleset_id = _integer(workflow_rule.get("ruleset_id"))
        source_type = workflow_rule.get("ruleset_source_type")
        source = workflow_rule.get("ruleset_source")
        configured_workflows = parameters.get("workflows") if type(parameters) is dict else None
        configured_workflow = (
            configured_workflows[0]
            if type(configured_workflows) is list and len(configured_workflows) == 1
            else None
        )
        expected_workflow = {
            "path": workflow_path,
            "ref": f"refs/heads/{reviewed.default_branch}",
            "repository_id": int(reviewed.repository_node_id),
        }
        if (
            type(parameters) is not dict
            or set(parameters) != {"do_not_enforce_on_create", "workflows"}
            or parameters.get("do_not_enforce_on_create") is not do_not_enforce_on_create
            or type(configured_workflow) is not dict
            or set(configured_workflow) not in (set(expected_workflow), {*expected_workflow, "sha"})
            or any(
                configured_workflow.get(key) != value for key, value in expected_workflow.items()
            )
            or ("sha" in configured_workflow and configured_workflow.get("sha") != commit_sha)
            or ruleset_id is None
            or source_type not in {"Repository", "Organization"}
            or (source_type == "Repository" and source != repository)
            or (source_type == "Organization" and source != owner)
        ):
            raise GitHubTeamStateError()
        ruleset_path = (
            f"/repos/{repository}/rulesets/{ruleset_id}"
            if source_type == "Repository"
            else f"/orgs/{owner}/rulesets/{ruleset_id}"
        )
        ruleset = await self._api.request_json_object("GET", ruleset_path)
        rules = ruleset.payload.get("rules")
        if (
            ruleset.payload.get("id") != ruleset_id
            or ruleset.payload.get("target") != "branch"
            or ruleset.payload.get("enforcement") != "active"
            or ruleset.payload.get("bypass_actors") != []
            or type(rules) is not list
            or len(
                [
                    rule
                    for rule in rules
                    if type(rule) is dict
                    and rule.get("type") == "workflows"
                    and rule.get("parameters") == parameters
                ]
            )
            != 1
        ):
            raise GitHubTeamStateError()
        return {
            "target_branch": target_branch,
            "effective_rule": dict(workflow_rule),
            "ruleset": dict(ruleset.payload),
        }

    async def verify_default_branch_baseline(self) -> GitHubDefaultBranchBaseline:
        """Bind the protected, human-reviewed default branch before staging tooling."""
        reviewed = self._reviewed
        if reviewed is None:
            raise GitHubTeamStateError()
        repository = reviewed.repository_id.removeprefix("github.com/")
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            branch = await self._api.request_json_object(
                "GET", f"/repos/{repository}/branches/{reviewed.default_branch}"
            )
            commit = branch.payload.get("commit")
            commit_sha = _sha(commit.get("sha") if isinstance(commit, Mapping) else None)
            protection = await self._api.request_json_object(
                "GET", f"/repos/{repository}/branches/{reviewed.default_branch}/protection"
            )
            reviews = protection.payload.get("required_pull_request_reviews")
            approvals = (
                reviews.get("required_approving_review_count") if type(reviews) is dict else None
            )
            bypass = (
                _bypass_allowances_empty(reviews.get("bypass_pull_request_allowances"))
                if type(reviews) is dict
                else None
            )
            if (
                branch.payload.get("name") != reviewed.default_branch
                or branch.payload.get("protected") is not True
                or _enabled(protection.payload.get("enforce_admins")) is not True
                or _enabled(protection.payload.get("allow_deletions")) is not False
                or _enabled(protection.payload.get("allow_force_pushes")) is not False
                or type(reviews) is not dict
                or reviews.get("dismiss_stale_reviews") is not True
                or type(approvals) is not int
                or approvals < 1
                or bypass is not True
            ):
                raise GitHubTeamStateError()
            baseline = GitHubDefaultBranchBaseline(
                repository_id=reviewed.repository_id,
                branch=reviewed.default_branch,
                commit=commit_sha,
                protection_digest=_json_digest(dict(protection.payload)),
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
        self._baseline = baseline
        return baseline

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
            or "admin:org" not in scopes
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
            and policy.bypass_pull_request_allowances_empty is True
            and policy.required_status_checks_strict is True
            and GitHubRequiredStatusCheck(
                context=_REQUIRED_CHECK,
                app_id=_GITHUB_ACTIONS_APP_ID,
            )
            in policy.required_status_checks
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
                "bypass_pull_request_allowances": {
                    "users": [],
                    "teams": [],
                    "apps": [],
                },
            },
            "required_status_checks": {
                "checks": [
                    {
                        "context": _REQUIRED_CHECK,
                        "app_id": _GITHUB_ACTIONS_APP_ID,
                    }
                ],
                "contexts": [],
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
                    "bypass_pull_request_allowances": {
                        "users": [],
                        "teams": [],
                        "apps": [],
                    },
                },
                "required_status_checks": {
                    "checks": [
                        {
                            "context": _REQUIRED_CHECK,
                            "app_id": _GITHUB_ACTIONS_APP_ID,
                        }
                    ],
                    "contexts": [],
                    "strict": True,
                },
                "restrictions": None,
            }
        )

    async def _default_branch_tooling(
        self,
        reviewed: GitHubTeamStateStatus,
        *,
        codeowners: bytes,
        workflow: bytes,
        check_workflow: bytes,
        runner_id: str,
    ) -> GitHubDefaultBranchTooling:
        repository = reviewed.repository_id.removeprefix("github.com/")
        branch = await self._api.request_json_object(
            "GET", f"/repos/{repository}/branches/{reviewed.default_branch}"
        )
        commit = branch.payload.get("commit")
        commit_sha = _sha(commit.get("sha") if isinstance(commit, Mapping) else None)
        if (
            branch.payload.get("name") != reviewed.default_branch
            or branch.payload.get("protected") is not True
        ):
            raise GitHubTeamStateError()
        protection = await self._api.request_json_object(
            "GET", f"/repos/{repository}/branches/{reviewed.default_branch}/protection"
        )
        reviews = protection.payload.get("required_pull_request_reviews")
        approvals = (
            reviews.get("required_approving_review_count") if type(reviews) is dict else None
        )
        bypass = reviews.get("bypass_pull_request_allowances") if type(reviews) is dict else None
        bypass_empty = bypass is None or (
            type(bypass) is dict
            and all(bypass.get(name) == [] for name in ("users", "teams", "apps"))
        )
        default_policy = _protection_policy(protection.payload)
        if (
            _enabled(protection.payload.get("enforce_admins")) is not True
            or _enabled(protection.payload.get("allow_deletions")) is not False
            or _enabled(protection.payload.get("allow_force_pushes")) is not False
            or type(reviews) is not dict
            or reviews.get("require_code_owner_reviews") is not True
            or reviews.get("dismiss_stale_reviews") is not True
            or type(approvals) is not int
            or approvals < 1
            or not bypass_empty
            or default_policy.required_status_checks_strict is not True
            or GitHubRequiredStatusCheck(
                context=_CODE_REQUIRED_CHECK,
                app_id=_GITHUB_ACTIONS_APP_ID,
            )
            not in default_policy.required_status_checks
        ):
            raise GitHubTeamStateError()
        owner, _ = repository.split("/", 1)
        groups = await self._organization_runner_groups(owner, repository)
        expected_workflows = sorted(
            (
                (
                    f"{repository}/.github/workflows/intent-check.yml@refs/heads/"
                    f"{reviewed.default_branch}"
                ),
                (
                    f"{repository}/.github/workflows/intent-state.yml@refs/heads/"
                    f"{reviewed.default_branch}"
                ),
            )
        )
        matching_groups = [
            group
            for group in groups
            if group.get("name") == "intent-state"
            and group.get("visibility") == "selected"
            and group.get("default") is False
            and group.get("restricted_to_workflows") is True
            and group.get("selected_workflows") == expected_workflows
            and _integer(group.get("id")) is not None
        ]
        if len(matching_groups) != 1:
            raise GitHubTeamStateError()
        group_id = matching_groups[0]["id"]
        runner_page = await self._api.get_pages(
            f"/orgs/{owner}/actions/runner-groups/{group_id}/runners",
            {"per_page": "100"},
        )
        runners = runner_page.model_dump(mode="json")["items"]
        matching_runners = [
            runner
            for runner in runners
            if runner.get("name") == runner_id
            and _integer(runner.get("id")) is not None
            and type(runner.get("labels")) is list
            and {"self-hosted", "intent-state"}
            <= {
                label.get("name")
                for label in runner["labels"]
                if type(label) is dict and type(label.get("name")) is str
            }
        ]
        if runner_page.not_modified or len(matching_runners) != 1:
            raise GitHubTeamStateError()
        state_workflow_rule = await self._required_workflow_rule_snapshot(
            reviewed,
            target_branch="intent-state",
            workflow_path=".github/workflows/intent-state.yml",
            do_not_enforce_on_create=True,
            commit_sha=commit_sha,
        )
        check_workflow_rule = await self._required_workflow_rule_snapshot(
            reviewed,
            target_branch=reviewed.default_branch,
            workflow_path=".github/workflows/intent-check.yml",
            do_not_enforce_on_create=False,
            commit_sha=commit_sha,
        )
        commit_response = await self._api.request_json_object(
            "GET", f"/repos/{repository}/git/commits/{commit_sha}"
        )
        root = commit_response.payload.get("tree")
        if commit_response.payload.get("sha") != commit_sha or not isinstance(root, Mapping):
            raise GitHubTeamStateError()
        root_sha = _sha(root.get("sha"))
        root_response = await self._api.request_json_object(
            "GET", f"/repos/{repository}/git/trees/{root_sha}"
        )
        root_entries = _tree_entries(root_response.payload, root_sha)
        github_entry = root_entries.get(".github")
        if github_entry is None or github_entry[:2] != ("040000", "tree"):
            raise GitHubTeamStateError()
        github_response = await self._api.request_json_object(
            "GET", f"/repos/{repository}/git/trees/{github_entry[2]}"
        )
        github_entries = _tree_entries(github_response.payload, github_entry[2])
        codeowners_entry = github_entries.get("CODEOWNERS")
        workflows_entry = github_entries.get("workflows")
        if (
            codeowners_entry is None
            or codeowners_entry[:2] != ("100644", "blob")
            or workflows_entry is None
            or workflows_entry[:2] != ("040000", "tree")
        ):
            raise GitHubTeamStateError()
        workflows_response = await self._api.request_json_object(
            "GET", f"/repos/{repository}/git/trees/{workflows_entry[2]}"
        )
        workflow_entries = _tree_entries(workflows_response.payload, workflows_entry[2])
        intended = workflow_entries.get("intent-state.yml")
        intended_check = workflow_entries.get("intent-check.yml")
        if (
            intended is None
            or intended[:2] != ("100644", "blob")
            or intended_check is None
            or intended_check[:2] != ("100644", "blob")
        ):
            raise GitHubTeamStateError()
        if (
            await self._api.request_bytes(
                "GET",
                f"/repos/{repository}/git/blobs/{codeowners_entry[2]}",
                max_bytes=_MAX_TOOLING_FILE_BYTES,
                accept="application/vnd.github.raw+json",
            )
            != codeowners
        ):
            raise GitHubTeamStateError()
        if (
            await self._api.request_bytes(
                "GET",
                f"/repos/{repository}/git/blobs/{intended[2]}",
                max_bytes=_MAX_TOOLING_FILE_BYTES,
                accept="application/vnd.github.raw+json",
            )
            != workflow
        ):
            raise GitHubTeamStateError()
        if (
            await self._api.request_bytes(
                "GET",
                f"/repos/{repository}/git/blobs/{intended_check[2]}",
                max_bytes=_MAX_TOOLING_FILE_BYTES,
                accept="application/vnd.github.raw+json",
            )
            != check_workflow
        ):
            raise GitHubTeamStateError()
        other_workflows: list[dict[str, str]] = []
        for path, (mode, kind, sha) in sorted(workflow_entries.items()):
            if path in {"intent-check.yml", "intent-state.yml"} or not path.endswith(
                (".yml", ".yaml")
            ):
                continue
            if (mode, kind) != ("100644", "blob"):
                raise GitHubTeamStateError()
            content = await self._api.request_bytes(
                "GET",
                f"/repos/{repository}/git/blobs/{sha}",
                max_bytes=_MAX_TOOLING_FILE_BYTES,
                accept="application/vnd.github.raw+json",
            )
            other_workflows.append(
                {"path": path, "digest": f"sha256:{hashlib.sha256(content).hexdigest()}"}
            )
        return GitHubDefaultBranchTooling(
            repository_id=reviewed.repository_id,
            branch=reviewed.default_branch,
            commit=commit_sha,
            protection_digest=_json_digest(dict(protection.payload)),
            codeowners_digest=f"sha256:{hashlib.sha256(codeowners).hexdigest()}",
            workflow_digest=f"sha256:{hashlib.sha256(workflow).hexdigest()}",
            check_workflow_digest=f"sha256:{hashlib.sha256(check_workflow).hexdigest()}",
            workflows_digest=_json_digest(other_workflows),
            runner_group_digest=_json_digest(matching_groups[0]),
            runner_membership_digest=_json_digest(matching_runners[0]),
            required_workflow_ruleset_digest=_json_digest(
                {
                    "state": state_workflow_rule,
                    "check": check_workflow_rule,
                }
            ),
        )

    async def verify_default_branch_tooling(
        self,
        *,
        codeowners: bytes,
        workflow: bytes,
        check_workflow: bytes,
        runner_id: str,
    ) -> GitHubDefaultBranchTooling:
        """Bind exact reviewed validator bytes to one protected default-branch commit."""
        reviewed = self._reviewed
        if (
            reviewed is None
            or type(codeowners) is not bytes
            or not codeowners
            or len(codeowners) > _MAX_TOOLING_FILE_BYTES
            or type(workflow) is not bytes
            or not workflow
            or len(workflow) > _MAX_TOOLING_FILE_BYTES
            or type(check_workflow) is not bytes
            or not check_workflow
            or len(check_workflow) > _MAX_TOOLING_FILE_BYTES
            or type(runner_id) is not str
            or not runner_id
            or len(runner_id) > 64
        ):
            raise GitHubTeamStateError()
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            tooling = await self._default_branch_tooling(
                reviewed,
                codeowners=codeowners,
                workflow=workflow,
                check_workflow=check_workflow,
                runner_id=runner_id,
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
        self._tooling = tooling
        return tooling

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

    async def configure_protection(
        self,
        confirmation: str,
        *,
        record_bootstrap: Callable[[str, str], None] | None = None,
        expected_bootstrap_anchor: str | None = None,
    ) -> GitHubTeamStateStatus:
        reviewed = self._reviewed
        if reviewed is None:
            raise GitHubTeamStateError()
        preview = self.protection_preview()
        if confirmation != preview.digest:
            raise GitHubTeamStateError()
        if not preview.requires_change:
            return reviewed
        if reviewed.branch_present and reviewed.branch_commit != expected_bootstrap_anchor:
            raise GitHubTeamStateError()
        repository = reviewed.repository_id.removeprefix("github.com/")
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            live = await self._inspect(repository)
            if live != reviewed:
                raise GitHubTeamStateError()
            if reviewed.branch_present:
                commit = await self._api.request_json_object(
                    "GET", f"/repos/{repository}/git/commits/{expected_bootstrap_anchor}"
                )
                tree = commit.payload.get("tree")
                if (
                    commit.payload.get("sha") != expected_bootstrap_anchor
                    or commit.payload.get("parents") != []
                    or type(tree) is not dict
                ):
                    raise GitHubTeamStateError()
                tree_sha = _sha(tree.get("sha"))
                empty = await self._api.request_json_object(
                    "GET", f"/repos/{repository}/git/trees/{tree_sha}"
                )
                if (
                    empty.payload.get("sha") != tree_sha
                    or empty.payload.get("tree") != []
                    or empty.payload.get("truncated") is not False
                ):
                    raise GitHubTeamStateError()
            else:
                tree = await self._api.request_json_object(
                    "POST",
                    f"/repos/{repository}/git/trees",
                    payload={"tree": []},
                    allowed_statuses=frozenset({201}),
                )
                tree_sha = _sha(tree.payload.get("sha"))
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
                if record_bootstrap is None:
                    raise GitHubTeamStateError()
                record_bootstrap(branch_commit, tree_sha)
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
        publication: PublicationArtifacts,
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
        publication: PublicationArtifacts,
        *,
        expected_head_commit: str,
        transition_proof: EnrollmentTransitionProofV2 | None = None,
    ) -> PublicationPullRequest:
        reviewed = self._reviewed
        if reviewed is None:
            raise GitHubTeamStateError()
        publication = _publication_artifacts(publication, transition_proof)
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

    async def confirm_publication_merge(
        self,
        publication: PublicationArtifacts,
        *,
        expected_head_commit: str,
        expected_base_commit: str,
        pull_request_number: int,
        transition_proof: EnrollmentTransitionProofV2 | None = None,
    ) -> GitHubTeamStateStatus:
        """Verify that one exact reviewed publication commit is now the protected state head."""
        reviewed = self._reviewed
        if reviewed is None:
            raise GitHubTeamStateError()
        publication = _publication_artifacts(publication, transition_proof)
        repository = reviewed.repository_id.removeprefix("github.com/")
        if (
            publication.repository_id != reviewed.repository_id
            or type(expected_head_commit) is not str
            or _COMMIT.fullmatch(expected_head_commit) is None
            or type(expected_base_commit) is not str
            or _COMMIT.fullmatch(expected_base_commit) is None
            or type(pull_request_number) is not int
            or pull_request_number <= 0
        ):
            raise GitHubTeamStateError()
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            current = await self._inspect(repository)
            merged_commit = current.branch_commit
            if current != reviewed or not current.protection_compatible or merged_commit is None:
                raise GitHubTeamStateError()
            response = await self._api.request_json_object(
                "GET", f"/repos/{repository}/pulls/{pull_request_number}"
            )
            self._pull_request(
                reviewed.repository_id,
                response.payload,
                publication.branch,
                expected_head_commit,
                expected_base_commit,
                created=False,
            )
            if (
                response.payload.get("merged") is not True
                or response.payload.get("merge_commit_sha") != merged_commit
            ):
                raise GitHubTeamStateError()
            await self._verify_merged_publication(
                repository,
                publication,
                merged_commit=merged_commit,
                expected_parent=expected_base_commit,
            )
            return current
        except cancelled_class:
            with anyio.CancelScope(shield=True):
                await self._api.aclose()
            raise
        except GitHubTeamStateError:
            raise
        except Exception as error:  # noqa: BLE001 - replace the provider boundary
            error.__traceback__ = None
            raise GitHubTeamStateError() from None

    async def publication_pull_request_state(
        self,
        publication: PublicationArtifacts,
        *,
        expected_head_commit: str,
        expected_base_commit: str,
        pull_request_number: int,
        transition_proof: EnrollmentTransitionProofV2 | None = None,
    ) -> Literal["open", "closed", "merged"]:
        """Read one exact receipted PR state without trusting a branch name alone."""
        reviewed = self._reviewed
        if (
            reviewed is None
            or _COMMIT.fullmatch(expected_head_commit) is None
            or _COMMIT.fullmatch(expected_base_commit) is None
            or type(pull_request_number) is not int
            or pull_request_number <= 0
        ):
            raise GitHubTeamStateError()
        publication = _publication_artifacts(publication, transition_proof)
        if publication.repository_id != reviewed.repository_id:
            raise GitHubTeamStateError()
        repository = reviewed.repository_id.removeprefix("github.com/")
        try:
            response = await self._api.request_json_object(
                "GET", f"/repos/{repository}/pulls/{pull_request_number}"
            )
            self._pull_request(
                reviewed.repository_id,
                response.payload,
                publication.branch,
                expected_head_commit,
                expected_base_commit,
                created=False,
            )
            if response.payload.get("merged") is True:
                return "merged"
            state = response.payload.get("state")
            if state not in {"open", "closed"}:
                raise GitHubTeamStateError()
            return cast(Literal["open", "closed"], state)
        except GitHubTeamStateError:
            raise
        except Exception as error:  # noqa: BLE001 - replace the provider boundary
            error.__traceback__ = None
            raise GitHubTeamStateError() from None

    async def _verify_merged_publication(
        self,
        repository: str,
        publication: PublicationArtifacts,
        *,
        merged_commit: str,
        expected_parent: str,
    ) -> None:
        commit = await self._api.request_json_object(
            "GET", f"/repos/{repository}/git/commits/{merged_commit}"
        )
        tree = commit.payload.get("tree")
        parents = commit.payload.get("parents")
        if (
            commit.payload.get("sha") != merged_commit
            or not isinstance(tree, Mapping)
            or type(parents) is not list
            or len(parents) != 1
            or not isinstance(parents[0], Mapping)
            or parents[0].get("sha") != expected_parent
        ):
            raise GitHubTeamStateError()
        tree_sha = _sha(tree.get("sha"))
        recursive = await self._api.request_json_object(
            "GET",
            f"/repos/{repository}/git/trees/{tree_sha}",
            params={"recursive": "1"},
        )
        if (
            recursive.payload.get("sha") != tree_sha
            or recursive.payload.get("truncated") is not False
        ):
            raise GitHubTeamStateError()
        artifacts = (
            ("manifest.json", publication.manifest_bytes),
            (publication.bundle_path, publication.bundle),
            (publication.signature_path, publication.signatures),
        )
        required: dict[str, tuple[str, str | None]] = {
            path: ("blob", None) for path, _content in artifacts
        }
        for path, _content in artifacts:
            parts = path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                required.setdefault("/".join(parts[:depth]), ("tree", None))
        entries = recursive.payload.get("tree")
        if type(entries) is not list or len(entries) != len(required):
            raise GitHubTeamStateError()
        blob_shas: dict[str, str] = {}
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or type(entry.get("path")) is not str:
                raise GitHubTeamStateError()
            path = entry["path"]
            expected = required.get(path)
            if expected is None or path in seen:
                raise GitHubTeamStateError()
            kind, _ = expected
            if entry.get("type") != kind or entry.get("mode") != (
                "040000" if kind == "tree" else "100644"
            ):
                raise GitHubTeamStateError()
            sha = _sha(entry.get("sha"))
            if kind == "blob":
                blob_shas[path] = sha
            seen.add(path)
        if seen != set(required):
            raise GitHubTeamStateError()
        for path, expected_content in artifacts:
            blob_sha = blob_shas[path]
            if (
                await self._api.request_bytes(
                    "GET",
                    f"/repos/{repository}/git/blobs/{blob_sha}",
                    max_bytes=len(expected_content),
                    accept="application/vnd.github.raw+json",
                )
                != expected_content
            ):
                raise GitHubTeamStateError()

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
    "GitHubDefaultBranchBaseline",
    "GitHubDefaultBranchTooling",
    "GitHubJsonResponse",
    "GitHubProtectionPolicy",
    "GitHubProtectionPreview",
    "GitHubRequiredStatusCheck",
    "GitHubTeamStateClient",
    "GitHubTeamStateError",
    "GitHubTeamStateStatus",
    "PublicationPullRequest",
]
