"""GitHub adapter contracts for reviewed team-state publication."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

import anyio
import httpx
import pytest
from pydantic import SecretStr

from intent_engineering.capture.github.auth import CredentialSource, GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient, RetryPolicy
from intent_engineering.capture.github.errors import GitHubRateLimitError
from intent_engineering.capture.github.models import PageResult
from intent_engineering.team_state.github import (
    GitHubJsonResponse,
    GitHubTeamStateClient,
    GitHubTeamStateError,
)
from intent_engineering.team_state.models import (
    PreparedPublication,
    TeamStateManifest,
    canonical_manifest_bytes,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
PUBLICATION_COMMIT = "f" * 40
MERGED_COMMIT = "e" * 40
STATE_COMMIT = "a" * 40
GITHUB_ACTIONS_APP_ID = 15368
CODEOWNERS = (
    b"/.intent/ @alice\n"
    b"/.github/workflows/ @alice\n"
    b"/ci/launch.py @alice\n"
    b"/src/intent_engineering/ @alice\n"
)
WORKFLOW = b"name: Intent Engineering\njobs:\n  state:\n    name: Intent Engineering / state\n"


class QueueApi:
    def __init__(
        self,
        responses: list[GitHubJsonResponse],
        pages: PageResult | list[PageResult] | None = None,
        raw: list[bytes] | None = None,
    ) -> None:
        self.responses = responses
        self.pages = pages or PageResult(items=(), etag=None)
        self.raw = raw or []
        self.calls: list[tuple[str, str, object]] = []
        self.closed = False

    async def request_json_object(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> GitHubJsonResponse:
        self.calls.append((method, path, payload if payload is not None else params))
        response = self.responses.pop(0)
        assert response.status_code in allowed_statuses
        return response

    async def get_pages(
        self, path: str, params: Mapping[str, str], etag: str | None = None
    ) -> PageResult:
        self.calls.append(("GET-PAGES", path, dict(params)))
        if type(self.pages) is list:
            return self.pages.pop(0)
        return self.pages

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        accept: str,
    ) -> bytes:
        self.calls.append(("GET-BYTES", path, {"accept": accept, "max_bytes": max_bytes}))
        value = self.raw.pop(0)
        assert len(value) <= max_bytes
        return value

    async def aclose(self) -> None:
        self.closed = True


def _response(
    status: int, payload: Mapping[str, object], *, scopes: str = "repo, admin:org"
) -> GitHubJsonResponse:
    return GitHubJsonResponse(
        status_code=status,
        payload=payload,
        headers={"X-OAuth-Scopes": scopes},
    )


def _inspection(
    *,
    protection_contexts: list[str] | None = None,
    protection_checks: list[dict[str, object]] | None = None,
    state_commit: str = STATE_COMMIT,
) -> list[GitHubJsonResponse]:
    contexts = protection_contexts or ["Intent Engineering / state"]
    checks = (
        protection_checks
        if protection_checks is not None
        else [{"context": "Intent Engineering / state", "app_id": GITHUB_ACTIONS_APP_ID}]
    )
    return [
        _response(
            200,
            {
                "id": 77,
                "full_name": "acme/project",
                "private": True,
                "default_branch": "main",
                "permissions": {
                    "admin": True,
                    "maintain": True,
                    "pull": True,
                    "push": True,
                    "triage": True,
                },
            },
        ),
        _response(200, {"id": 123, "login": "alice"}),
        _response(
            200,
            {
                "name": "intent-state",
                "protected": True,
                "commit": {"sha": state_commit},
            },
        ),
        _response(
            200,
            {
                "enforce_admins": {"enabled": True},
                "allow_deletions": {"enabled": False},
                "allow_force_pushes": {"enabled": False},
                "required_linear_history": {"enabled": True},
                "required_pull_request_reviews": {
                    "dismiss_stale_reviews": True,
                    "required_approving_review_count": 1,
                    "require_code_owner_reviews": False,
                },
                "required_status_checks": {
                    "strict": True,
                    "contexts": contexts,
                    "checks": checks,
                },
            },
        ),
        _response(200, {"path": ".github/CODEOWNERS", "type": "file"}),
    ]


def _created_branch_inspection(commit: str) -> list[GitHubJsonResponse]:
    responses = _inspection()
    responses[2:] = [
        _response(
            200,
            {
                "name": "intent-state",
                "protected": False,
                "commit": {"sha": commit},
            },
        ),
        _response(404, {}),
        _response(404, {}),
    ]
    return responses


def _publication(bundle: bytes = b"encrypted") -> PreparedPublication:
    digest = "sha256:" + hashlib.sha256(bundle).hexdigest()
    manifest = TeamStateManifest(
        project_id="project",
        repository_id="github.com/acme/project",
        graph_version=1,
        parent_bundle_digest="sha256:" + "1" * 64,
        bundle_digest=digest,
        bundle_size=len(bundle),
        recipient_key_ids=("recipient:alice",),
        required_signature_ids=("signer:release",),
        created_at=NOW,
    )
    suffix = f"1-{digest.removeprefix('sha256:')}"
    return PreparedPublication(
        repository_id=manifest.repository_id,
        branch=f"intent-publication/{digest.removeprefix('sha256:')}",
        manifest=manifest,
        manifest_bytes=canonical_manifest_bytes(manifest),
        bundle=bundle,
        signatures=b"{}",
        bundle_path=f"bundles/{suffix}.intent",
        signature_path=f"signatures/{suffix}.json",
    )


def _publication_ref(
    publication: PreparedPublication, *, commit: str = PUBLICATION_COMMIT
) -> GitHubJsonResponse:
    return _response(
        200,
        {
            "ref": f"refs/heads/{publication.branch}",
            "object": {"sha": commit, "type": "commit"},
        },
    )


def _pull(
    publication: PreparedPublication,
    number: int,
    *,
    head_sha: str = PUBLICATION_COMMIT,
    base_sha: str = STATE_COMMIT,
    head_repository: str = "acme/project",
    base_repository: str = "acme/project",
) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/project/pull/{number}",
        "state": "open",
        "merged": False,
        "head": {
            "ref": publication.branch,
            "sha": head_sha,
            "repo": {"full_name": head_repository},
        },
        "base": {
            "ref": "intent-state",
            "sha": base_sha,
            "repo": {"full_name": base_repository},
        },
    }


def _merged_verification(
    publication: PreparedPublication,
    *,
    parent: str = STATE_COMMIT,
    extra_tree_entry: bool = False,
) -> list[GitHubJsonResponse]:
    tree_sha = "4" * 40
    paths = (
        ("manifest.json", "1" * 40),
        (publication.bundle_path, "2" * 40),
        (publication.signature_path, "3" * 40),
    )
    pull = _pull(publication, 43)
    pull.update({"merged": True, "merge_commit_sha": MERGED_COMMIT})
    entries: list[dict[str, object]] = [
        {"path": "bundles", "mode": "040000", "type": "tree", "sha": "5" * 40},
        {"path": "signatures", "mode": "040000", "type": "tree", "sha": "6" * 40},
        *[{"path": path, "mode": "100644", "type": "blob", "sha": sha} for path, sha in paths],
    ]
    if extra_tree_entry:
        entries.append({"path": "unexpected", "mode": "100644", "type": "blob", "sha": "7" * 40})
    return [
        _response(200, pull),
        _response(
            200,
            {
                "sha": MERGED_COMMIT,
                "tree": {"sha": tree_sha},
                "parents": [{"sha": parent}],
            },
        ),
        _response(200, {"sha": tree_sha, "truncated": False, "tree": entries}),
    ]


def _merged_raw(publication: PreparedPublication, *, manifest: bytes | None = None) -> list[bytes]:
    return [
        publication.manifest_bytes if manifest is None else manifest,
        publication.bundle,
        publication.signatures,
    ]


def _default_tooling(
    *,
    codeowners: bytes = CODEOWNERS,
    workflow: bytes = WORKFLOW,
    protected: bool = True,
    extra_workflow: bytes | None = None,
) -> list[GitHubJsonResponse]:
    root_tree = "7" * 40
    github_tree = "8" * 40
    workflows_tree = "9" * 40
    codeowners_sha = "1" * 40
    workflow_sha = "2" * 40
    entries: list[dict[str, object]] = [
        {
            "path": "intent-state.yml",
            "mode": "100644",
            "type": "blob",
            "sha": workflow_sha,
        }
    ]
    responses = [
        _response(
            200,
            {"name": "main", "protected": protected, "commit": {"sha": "d" * 40}},
        ),
        _response(
            200,
            {
                "enforce_admins": {"enabled": True},
                "allow_deletions": {"enabled": False},
                "allow_force_pushes": {"enabled": False},
                "required_pull_request_reviews": {
                    "required_approving_review_count": 1,
                    "require_code_owner_reviews": True,
                    "dismiss_stale_reviews": True,
                    "bypass_pull_request_allowances": {
                        "users": [],
                        "teams": [],
                        "apps": [],
                    },
                },
            },
        ),
        _response(
            200,
            {
                "total_count": 1,
                "runner_groups": [
                    {
                        "id": 42,
                        "name": "intent-state",
                        "visibility": "selected",
                        "default": False,
                        "restricted_to_workflows": True,
                        "selected_workflows": [
                            "acme/project/.github/workflows/intent-state.yml@refs/heads/main"
                        ],
                    }
                ],
            },
        ),
        _response(
            200,
            {
                "id": 91,
                "target": "branch",
                "enforcement": "active",
                "bypass_actors": [],
                "rules": [
                    {
                        "type": "workflows",
                        "parameters": {
                            "do_not_enforce_on_create": True,
                            "workflows": [
                                {
                                    "path": ".github/workflows/intent-state.yml",
                                    "ref": "refs/heads/main",
                                    "repository_id": 77,
                                    "sha": "d" * 40,
                                }
                            ],
                        },
                    }
                ],
            },
        ),
        _response(200, {"sha": "d" * 40, "tree": {"sha": root_tree}}),
        _response(
            200,
            {
                "sha": root_tree,
                "truncated": False,
                "tree": [{"path": ".github", "mode": "040000", "type": "tree", "sha": github_tree}],
            },
        ),
        _response(
            200,
            {
                "sha": github_tree,
                "truncated": False,
                "tree": [
                    {
                        "path": "CODEOWNERS",
                        "mode": "100644",
                        "type": "blob",
                        "sha": codeowners_sha,
                    },
                    {
                        "path": "workflows",
                        "mode": "040000",
                        "type": "tree",
                        "sha": workflows_tree,
                    },
                ],
            },
        ),
    ]
    if extra_workflow is not None:
        entries.append({"path": "spoof.yml", "mode": "100644", "type": "blob", "sha": "3" * 40})
    responses.append(
        _response(
            200,
            {"sha": workflows_tree, "truncated": False, "tree": entries},
        )
    )
    return responses


def _required_workflow_page() -> PageResult:
    return PageResult(
        items=(
            {
                "type": "workflows",
                "ruleset_source_type": "Repository",
                "ruleset_source": "acme/project",
                "ruleset_id": 91,
                "parameters": {
                    "do_not_enforce_on_create": True,
                    "workflows": [
                        {
                            "path": ".github/workflows/intent-state.yml",
                            "ref": "refs/heads/main",
                            "repository_id": 77,
                            "sha": "d" * 40,
                        }
                    ],
                },
            },
        ),
        etag=None,
    )


def _runner_page(*, runner_name: str = "intent-ci") -> PageResult:
    return PageResult(
        items=(
            {
                "id": 314,
                "name": runner_name,
                "status": "online",
                "busy": False,
                "labels": [
                    {"id": 1, "name": "self-hosted", "type": "read-only"},
                    {"id": 2, "name": "intent-state", "type": "custom"},
                ],
            },
        ),
        etag=None,
    )


def _runner_group(group_id: int, *, exact: bool = False) -> dict[str, object]:
    return {
        "id": group_id,
        "name": "intent-state" if exact else f"other-{group_id}",
        "visibility": "selected",
        "default": False,
        "restricted_to_workflows": True,
        "selected_workflows": (
            ["acme/project/.github/workflows/intent-state.yml@refs/heads/main"]
            if exact
            else ["acme/project/.github/workflows/other.yml@refs/heads/main"]
        ),
    }


def _default_tooling_raw(
    *,
    codeowners: bytes = CODEOWNERS,
    workflow: bytes = WORKFLOW,
    extra_workflow: bytes | None = None,
) -> list[bytes]:
    return [codeowners, workflow] + ([] if extra_workflow is None else [extra_workflow])


@pytest.mark.anyio
async def test_inspect_returns_exact_repository_identity_scope_and_protection_status() -> None:
    """Catches enabling a different identity, repository, or incompletely protected branch."""
    api = QueueApi(_inspection())
    client = GitHubTeamStateClient(
        api,
        expected_account_id="123",
        expected_login="alice",
        allow_public_repository=False,
    )

    status = await client.inspect("acme/project")

    assert status.repository_id == "github.com/acme/project"
    assert status.repository_node_id == "77"
    assert status.account_id == "123"
    assert status.login == "alice"
    assert status.scopes == ("admin:org", "repo")
    assert status.branch == "intent-state"
    assert status.branch_commit == "a" * 40
    assert status.protection_compatible is True
    assert status.codeowners_present is True


@pytest.mark.anyio
async def test_default_branch_tooling_is_bound_to_one_exact_protected_commit() -> None:
    """Catches branch protection trusting local-only validator or CODEOWNERS bytes."""
    api = QueueApi(
        _inspection() + _default_tooling(),
        pages=[_runner_page(), _required_workflow_page()],
        raw=_default_tooling_raw(),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    tooling = await client.verify_default_branch_tooling(
        codeowners=CODEOWNERS,
        workflow=WORKFLOW,
        runner_id="intent-ci",
    )

    assert tooling.commit == "d" * 40
    assert tooling.codeowners_digest == "sha256:" + hashlib.sha256(CODEOWNERS).hexdigest()
    assert tooling.workflow_digest == "sha256:" + hashlib.sha256(WORKFLOW).hexdigest()


@pytest.mark.anyio
async def test_runner_group_pagination_finds_the_only_exact_group_on_page_two() -> None:
    """Catches enrollment inspecting only the first object-wrapped runner-group page."""
    tooling = _default_tooling()
    first = _response(
        200,
        {
            "total_count": 101,
            "runner_groups": [_runner_group(1000 + index) for index in range(100)],
        },
    )
    second = _response(
        200,
        {"total_count": 101, "runner_groups": [_runner_group(42, exact=True)]},
    )
    tooling[2:3] = [first, second]
    api = QueueApi(
        _inspection() + tooling,
        pages=[_runner_page(), _required_workflow_page()],
        raw=_default_tooling_raw(),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    result = await client.verify_default_branch_tooling(
        codeowners=CODEOWNERS,
        workflow=WORKFLOW,
        runner_id="intent-ci",
    )

    assert result.runner_group_digest.startswith("sha256:")
    group_calls = [call for call in api.calls if call[1].endswith("/actions/runner-groups")]
    assert [call[2]["page"] for call in group_calls] == ["1", "2"]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["overflow", "repeat", "changed_total"])
async def test_runner_group_pagination_fails_closed_on_unbounded_or_repeated_pages(
    failure: str,
) -> None:
    """Catches pagination loops, duplicate groups, and provider count drift."""
    tooling = _default_tooling()
    if failure == "overflow":
        group_pages = [_response(200, {"total_count": 1001, "runner_groups": []})]
    else:
        first_groups = [_runner_group(1000 + index) for index in range(100)]
        second_total = 102 if failure == "changed_total" else 101
        second_group = first_groups[0] if failure == "repeat" else _runner_group(42, exact=True)
        group_pages = [
            _response(200, {"total_count": 101, "runner_groups": first_groups}),
            _response(
                200,
                {"total_count": second_total, "runner_groups": [second_group]},
            ),
        ]
    tooling[2:3] = group_pages
    api = QueueApi(_inspection() + tooling)
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.verify_default_branch_tooling(
            codeowners=CODEOWNERS,
            workflow=WORKFLOW,
            runner_id="intent-ci",
        )


@pytest.mark.anyio
async def test_default_branch_baseline_blocks_staging_without_reviewed_history() -> None:
    """Catches setup staging security files onto an unprotected direct-push branch."""
    for protected in (False, True):
        responses = _default_tooling(protected=protected)[:2]
        if protected:
            responses[1] = _response(404, {})
        api = QueueApi(_inspection() + responses)
        client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
        await client.inspect("acme/project")

        with pytest.raises(GitHubTeamStateError):
            await client.verify_default_branch_baseline()


@pytest.mark.anyio
async def test_default_branch_baseline_binds_exact_commit_and_protection() -> None:
    api = QueueApi(_inspection() + _default_tooling()[:2])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    baseline = await client.verify_default_branch_baseline()

    assert baseline.commit == "d" * 40
    assert baseline.protection_digest.startswith("sha256:")


@pytest.mark.anyio
async def test_default_branch_tooling_accepts_required_workflow_bound_by_protected_ref() -> None:
    """The optional ruleset SHA need not freeze a moving protected default branch."""
    tooling_responses = _default_tooling()
    page_payload = _required_workflow_page().model_dump(mode="json")["items"][0]
    page_parameters = dict(page_payload["parameters"])
    page_workflow = dict(page_parameters["workflows"][0])
    page_workflow.pop("sha")
    page_parameters["workflows"] = [page_workflow]
    page_payload["parameters"] = page_parameters
    detail = dict(tooling_responses[3].payload)
    rules = [dict(detail["rules"][0])]
    rules[0]["parameters"] = page_parameters
    detail["rules"] = rules
    tooling_responses[3] = _response(200, detail)
    api = QueueApi(
        _inspection() + tooling_responses,
        pages=[_runner_page(), PageResult(items=(page_payload,), etag=None)],
        raw=_default_tooling_raw(),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    result = await client.verify_default_branch_tooling(
        codeowners=CODEOWNERS,
        workflow=WORKFLOW,
        runner_id="intent-ci",
    )

    assert result.commit == "d" * 40


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    ["unprotected", "missing", "wrong", "spoof"],
)
async def test_default_branch_tooling_rejects_missing_wrong_or_spoofable_workflow(
    change: str,
) -> None:
    """Catches an unreviewed Actions workflow being able to impersonate the required check."""
    if change == "unprotected":
        tooling = _default_tooling(protected=False)
    elif change == "missing":
        tooling = _default_tooling(workflow=b"")
    elif change == "wrong":
        tooling = _default_tooling(workflow=b"name: other\n")
    else:
        tooling = _default_tooling(
            extra_workflow=b"jobs:\n  spoof:\n    name: Intent Engineering / state\n"
        )
    raw = (
        _default_tooling_raw(workflow=b"")
        if change == "missing"
        else _default_tooling_raw(workflow=b"name: other\n")
        if change == "wrong"
        else _default_tooling_raw(
            extra_workflow=b"jobs:\n  spoof:\n    name: Intent Engineering / state\n"
        )
        if change == "spoof"
        else _default_tooling_raw()
    )
    client = GitHubTeamStateClient(
        QueueApi(
            _inspection() + tooling,
            pages=[_runner_page(), _required_workflow_page()],
            raw=raw,
        ),
        expected_account_id="123",
        expected_login="alice",
    )
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.verify_default_branch_tooling(
            codeowners=CODEOWNERS,
            workflow=WORKFLOW,
            runner_id="intent-ci",
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "wrong_sha", "enforce_create", "bypass"]
)
async def test_default_branch_tooling_requires_one_exact_unbypassable_workflow_rule(
    change: str,
) -> None:
    """Catches a same-name Actions check satisfying protection without this exact workflow."""
    tooling = _default_tooling()
    page = _required_workflow_page()
    if change == "missing":
        page = PageResult(items=(), etag=None)
    elif change == "duplicate":
        page = PageResult(items=page.items + page.items, etag=None)
    elif change == "wrong_sha":
        item = dict(page.model_dump(mode="json")["items"][0])
        parameters = dict(item["parameters"])
        workflows = [dict(parameters["workflows"][0])]
        workflows[0]["sha"] = "c" * 40
        parameters["workflows"] = workflows
        item["parameters"] = parameters
        page = PageResult(items=(item,), etag=None)
    elif change == "enforce_create":
        item = dict(page.model_dump(mode="json")["items"][0])
        parameters = dict(item["parameters"])
        parameters["do_not_enforce_on_create"] = False
        item["parameters"] = parameters
        page = PageResult(items=(item,), etag=None)
    else:
        detail = dict(tooling[3].payload)
        detail["bypass_actors"] = [{"actor_id": 7, "actor_type": "Team", "bypass_mode": "always"}]
        tooling[3] = _response(200, detail)
    client = GitHubTeamStateClient(
        QueueApi(_inspection() + tooling, pages=[_runner_page(), page]),
        expected_account_id="123",
        expected_login="alice",
    )
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.verify_default_branch_tooling(
            codeowners=CODEOWNERS,
            workflow=WORKFLOW,
            runner_id="intent-ci",
        )


@pytest.mark.anyio
async def test_inspect_rejects_identity_scope_public_policy_and_changed_protection() -> None:
    """Catches provider facts being treated as advisory during authorization."""
    bad_identity = _inspection()
    bad_identity[1] = _response(200, {"id": 999, "login": "mallory"})
    with pytest.raises(GitHubTeamStateError):
        await GitHubTeamStateClient(
            QueueApi(bad_identity), expected_account_id="123", expected_login="alice"
        ).inspect("acme/project")

    missing_scope = _inspection()
    missing_scope[0] = _response(
        200,
        {
            "id": 77,
            "full_name": "acme/project",
            "private": True,
            "default_branch": "main",
            "permissions": {
                "admin": True,
                "maintain": True,
                "pull": True,
                "push": True,
                "triage": True,
            },
        },
        scopes="read:user",
    )
    with pytest.raises(GitHubTeamStateError):
        await GitHubTeamStateClient(
            QueueApi(missing_scope), expected_account_id="123", expected_login="alice"
        ).inspect("acme/project")

    missing_org_scope = _inspection()
    missing_org_scope[0] = _response(
        200,
        missing_org_scope[0].payload,
        scopes="repo",
    )
    with pytest.raises(GitHubTeamStateError):
        await GitHubTeamStateClient(
            QueueApi(missing_org_scope), expected_account_id="123", expected_login="alice"
        ).inspect("acme/project")

    changed = QueueApi(
        _inspection(
            protection_contexts=["another-check"],
            protection_checks=[{"context": "another-check", "app_id": 999}],
        )
    )
    changed_client = GitHubTeamStateClient(
        changed, expected_account_id="123", expected_login="alice"
    )
    changed_status = await changed_client.inspect("acme/project")
    assert changed_status.protection_compatible is False
    assert changed_client.protection_preview().requires_change is True

    public = _inspection()
    public[0] = _response(
        200,
        {
            "id": 77,
            "full_name": "acme/project",
            "private": False,
            "default_branch": "main",
            "permissions": {
                "admin": True,
                "maintain": True,
                "pull": True,
                "push": True,
                "triage": True,
            },
        },
        scopes="public_repo",
    )
    with pytest.raises(GitHubTeamStateError):
        await GitHubTeamStateClient(
            QueueApi(public), expected_account_id="123", expected_login="alice"
        ).inspect("acme/project")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "checks",
    [
        [],
        [{"context": "Intent Engineering / state", "app_id": -1}],
        [{"context": "Intent Engineering / state", "app_id": 999}],
        [{"context": "Intent Engineering / state"}],
        [{"context": "another-check", "app_id": GITHUB_ACTIONS_APP_ID}],
    ],
)
async def test_state_check_requires_exact_github_actions_app_binding(
    checks: list[dict[str, object]],
) -> None:
    """Catches a legacy or attacker-writable status context satisfying state protection."""
    responses = _inspection(protection_checks=checks)
    client = GitHubTeamStateClient(
        QueueApi(responses), expected_account_id="123", expected_login="alice"
    )

    status = await client.inspect("acme/project")

    assert status.protection_compatible is False
    assert client.protection_preview().requires_change is True


@pytest.mark.anyio
async def test_state_protection_rejects_any_pull_request_bypass_actor() -> None:
    responses = _inspection()
    protection = dict(responses[3].payload)
    reviews = dict(protection["required_pull_request_reviews"])  # type: ignore[arg-type]
    reviews["bypass_pull_request_allowances"] = {
        "users": [{"login": "release-bot"}],
        "teams": [],
        "apps": [],
    }
    protection["required_pull_request_reviews"] = reviews
    responses[3] = _response(200, protection)
    client = GitHubTeamStateClient(
        QueueApi(responses), expected_account_id="123", expected_login="alice"
    )

    status = await client.inspect("acme/project")

    assert status.protection_compatible is False
    assert status.protection_policy is not None
    assert status.protection_policy.bypass_pull_request_allowances_empty is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("permissions", "accepted"),
    [
        (
            {"admin": False, "maintain": True, "pull": True, "push": True, "triage": True},
            False,
        ),
        (
            {"admin": True, "maintain": False, "pull": True, "push": True, "triage": True},
            True,
        ),
        (
            {"admin": False, "maintain": False, "pull": True, "push": True, "triage": True},
            False,
        ),
        (
            {"admin": True, "maintain": True, "pull": True, "push": False, "triage": True},
            False,
        ),
        (
            {"admin": True, "maintain": True, "pull": False, "push": True, "triage": True},
            False,
        ),
        (
            {"admin": True, "maintain": True, "pull": True, "push": "yes", "triage": True},
            False,
        ),
        ({"admin": True, "maintain": True, "pull": True, "push": True}, False),
    ],
)
async def test_inspect_requires_real_repository_level_write_and_pr_permissions(
    permissions: Mapping[str, object], accepted: bool
) -> None:
    """Catches token scopes substituting for repository-level setup authority."""
    responses = _inspection()
    repository = dict(responses[0].payload)
    repository["permissions"] = permissions
    responses[0] = _response(200, repository)
    client = GitHubTeamStateClient(
        QueueApi(responses), expected_account_id="123", expected_login="alice"
    )

    if accepted:
        status = await client.inspect("acme/project")
        assert status.repository_id == "github.com/acme/project"
    else:
        with pytest.raises(GitHubTeamStateError):
            await client.inspect("acme/project")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allow_deletions", {"enabled": True}),
        ("allow_force_pushes", {"enabled": True}),
        ("required_linear_history", {"enabled": False}),
        ("required_linear_history", None),
    ],
)
async def test_state_protection_requires_linear_history_and_disallows_destructive_pushes(
    field: str, value: object
) -> None:
    """Catches a protected branch that can still be rewritten, deleted, or merge-committed."""
    responses = _inspection()
    protection = dict(responses[3].payload)
    if value is None:
        protection.pop(field)
    else:
        protection[field] = value
    responses[3] = _response(200, protection)
    client = GitHubTeamStateClient(
        QueueApi(responses), expected_account_id="123", expected_login="alice"
    )

    status = await client.inspect("acme/project")

    assert status.protection_compatible is False
    assert client.protection_preview().requires_change is True


@pytest.mark.anyio
async def test_state_protection_uses_generic_approval_not_default_branch_codeowners() -> None:
    """Catches state PRs requiring CODEOWNERS that exist only on the code branch."""
    compatible = GitHubTeamStateClient(
        QueueApi(_inspection()), expected_account_id="123", expected_login="alice"
    )
    assert (await compatible.inspect("acme/project")).codeowners_present is True
    assert compatible.protection_preview().requires_change is False

    responses = _inspection()
    protection = dict(responses[3].payload)
    reviews = dict(protection["required_pull_request_reviews"])  # type: ignore[arg-type]
    reviews["require_code_owner_reviews"] = True
    protection["required_pull_request_reviews"] = reviews
    responses[3] = _response(200, protection)
    incompatible = GitHubTeamStateClient(
        QueueApi(responses), expected_account_id="123", expected_login="alice"
    )

    assert (await incompatible.inspect("acme/project")).protection_compatible is False


@pytest.mark.anyio
async def test_stronger_compatible_protection_is_never_replaced() -> None:
    """Catches setup weakening approvals, extra checks, or repository restrictions."""
    responses = _inspection(protection_contexts=["Intent Engineering / state", "security"])
    protection = dict(responses[3].payload)
    reviews = dict(protection["required_pull_request_reviews"])  # type: ignore[arg-type]
    reviews["required_approving_review_count"] = 3
    protection["required_pull_request_reviews"] = reviews
    protection["restrictions"] = {"users": [], "teams": ["release"], "apps": []}
    responses[3] = _response(200, protection)
    api = QueueApi(responses)
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    status = await client.inspect("acme/project")

    preview = client.protection_preview()
    updated = await client.configure_protection(
        preview.digest, record_bootstrap=lambda _anchor, _tree: None
    )

    assert preview.branch_creation_required is False
    assert preview.requires_change is False
    assert updated == status
    assert all(call[0] != "PUT" for call in api.calls)

    assert preview.before_policy is not None
    assert preview.before_policy.required_approving_review_count == 3
    assert preview.before_policy.required_status_check_contexts == (
        "Intent Engineering / state",
        "security",
    )
    assert preview.after_policy == preview.before_policy


@pytest.mark.anyio
async def test_preview_exposes_bounded_current_and_required_protection_policies() -> None:
    """Catches a confirmation digest hiding the concrete before/after policy."""
    client = GitHubTeamStateClient(
        QueueApi(_inspection()), expected_account_id="123", expected_login="alice"
    )
    await client.inspect("acme/project")

    preview = client.protection_preview()

    assert preview.before_policy is not None
    assert preview.before_policy.snapshot_digest.startswith("sha256:")
    assert preview.before_policy.required_linear_history is True
    assert preview.before_policy.allow_force_pushes is False
    assert preview.before_policy.allow_deletions is False
    assert preview.after_policy == preview.before_policy


@pytest.mark.anyio
async def test_protection_snapshot_rejects_unbounded_provider_content_secret_safely() -> None:
    """Catches exact drift binding retaining arbitrary or credential-like provider data."""
    secret = "ghp_snapshot_secret_" + "x" * 4096
    responses = _inspection()
    protection = dict(responses[3].payload)
    protection["unexpected"] = secret
    responses[3] = _response(200, protection)
    client = GitHubTeamStateClient(
        QueueApi(responses), expected_account_id="123", expected_login="alice"
    )

    with pytest.raises(GitHubTeamStateError) as caught:
        await client.inspect("acme/project")

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize("drift", ["approval_count", "contexts", "restrictions"])
async def test_compatible_protection_drift_is_rejected_before_pr_reuse(drift: str) -> None:
    """Catches stronger policy details drifting while the compatible boolean stays true."""
    publication = _publication()
    initial = _inspection(protection_contexts=["Intent Engineering / state", "security"])
    initial_policy = dict(initial[3].payload)
    initial_reviews = dict(initial_policy["required_pull_request_reviews"])  # type: ignore[arg-type]
    initial_reviews["required_approving_review_count"] = 2
    initial_policy["required_pull_request_reviews"] = initial_reviews
    initial_policy["restrictions"] = {"users": [], "teams": ["release"], "apps": []}
    initial[3] = _response(200, initial_policy)
    changed = _inspection(protection_contexts=["Intent Engineering / state", "security"])
    changed_policy = dict(changed[3].payload)
    changed_reviews = dict(changed_policy["required_pull_request_reviews"])  # type: ignore[arg-type]
    changed_reviews["required_approving_review_count"] = 2
    changed_policy["required_pull_request_reviews"] = changed_reviews
    changed_policy["restrictions"] = {"users": [], "teams": ["release"], "apps": []}
    if drift == "approval_count":
        changed_reviews["required_approving_review_count"] = 3
    elif drift == "contexts":
        changed_policy["required_status_checks"] = {
            "strict": True,
            "contexts": ["Intent Engineering / state", "security", "audit"],
        }
    else:
        changed_policy["restrictions"] = {
            "users": [],
            "teams": ["release", "operations"],
            "apps": [],
        }
    changed[3] = _response(200, changed_policy)
    api = QueueApi(
        initial + changed + [_publication_ref(publication)],
        PageResult(items=(_pull(publication, 42),), etag=None),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.open_publication_pr(publication, expected_head_commit=PUBLICATION_COMMIT)

    assert all(call[0] not in {"GET-PAGES", "POST"} for call in api.calls)


@pytest.mark.anyio
async def test_existing_incompatible_protection_fails_closed_without_put() -> None:
    """Catches setup replacing unknown existing policy with a weaker guessed policy."""
    incompatible = _inspection(
        protection_contexts=["another-check"],
        protection_checks=[{"context": "another-check", "app_id": 999}],
    )
    configured = _response(200, _inspection()[3].payload)
    api = QueueApi(incompatible + incompatible + [configured])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")
    preview = client.protection_preview()

    with pytest.raises(GitHubTeamStateError):
        await client.configure_protection(preview.digest)

    assert preview.branch_creation_required is False
    assert preview.requires_change is True
    assert all(call[0] != "PUT" for call in api.calls)


@pytest.mark.anyio
async def test_open_publication_pr_rechecks_status_and_reuses_one_exact_open_pr() -> None:
    """Catches duplicate PR creation or stale repository/protection authorization."""
    publication = _publication()
    existing = _pull(publication, 42)
    api = QueueApi(
        _inspection()
        + _inspection()
        + [_publication_ref(publication)]
        + _inspection()
        + [_publication_ref(publication)],
        PageResult(items=(existing,), etag=None),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    pull_request = await client.open_publication_pr(
        publication, expected_head_commit=PUBLICATION_COMMIT
    )

    assert pull_request.number == 42
    assert pull_request.created is False
    assert pull_request.url == "https://github.com/acme/project/pull/42"
    assert all(call[0] != "POST" for call in api.calls)
    assert sum(call[1].endswith(publication.branch) for call in api.calls) == 2


@pytest.mark.anyio
async def test_open_publication_pr_posts_one_exact_payload_and_closes_on_cancellation() -> None:
    """Catches an unbound PR target or cancellation leaking the owned provider client."""
    publication = _publication()
    created = _response(201, _pull(publication, 43))
    api = QueueApi(
        _inspection()
        + _inspection()
        + [_publication_ref(publication)]
        + _inspection()
        + [_publication_ref(publication), created]
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    pull_request = await client.open_publication_pr(
        publication, expected_head_commit=PUBLICATION_COMMIT
    )

    assert pull_request.created is True
    assert api.calls[-1] == (
        "POST",
        "/repos/acme/project/pulls",
        {
            "base": "intent-state",
            "body": f"Encrypted intent state `{publication.manifest.bundle_digest}`.",
            "draft": False,
            "head": publication.branch,
            "title": f"Publish intent state v{publication.manifest.graph_version}",
        },
    )


@pytest.mark.anyio
async def test_open_publication_pr_rejects_status_changed_since_review() -> None:
    """Catches branch protection or repository facts changing after inspection."""
    initial = _inspection()
    changed = _inspection()
    changed[4] = _response(404, {})
    api = QueueApi(initial + changed)
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.open_publication_pr(_publication(), expected_head_commit=PUBLICATION_COMMIT)

    assert all(call[0] != "POST" for call in api.calls)


@pytest.mark.anyio
async def test_open_publication_pr_rejects_a_live_head_other_than_the_published_commit() -> None:
    """Catches a publication branch being moved between Git Data publication and PR creation."""
    publication = _publication()
    api = QueueApi(_inspection() + _inspection() + [_publication_ref(publication, commit="9" * 40)])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.open_publication_pr(publication, expected_head_commit=PUBLICATION_COMMIT)

    assert all(call[0] not in {"GET-PAGES", "POST"} for call in api.calls)


@pytest.mark.anyio
async def test_open_publication_pr_rechecks_the_exact_base_immediately_before_create() -> None:
    """Catches state advancing after the first live check but before PR creation."""
    publication = _publication()
    changed = _inspection()
    changed_branch = dict(changed[2].payload)
    changed_branch["commit"] = {"sha": "9" * 40}
    changed[2] = _response(200, changed_branch)
    api = QueueApi(_inspection() + _inspection() + [_publication_ref(publication)] + changed)
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.open_publication_pr(publication, expected_head_commit=PUBLICATION_COMMIT)

    assert sum(call[0] == "GET-PAGES" for call in api.calls) == 1
    assert all(call[0] != "POST" for call in api.calls)


@pytest.mark.anyio
async def test_open_publication_pr_verifies_created_pr_shas_refs_and_repositories() -> None:
    """Catches GitHub returning a PR that is not bound to the reviewed head and live base."""
    publication = _publication()
    malformed = []
    for section, field, value in (
        ("head", "sha", "9" * 40),
        ("base", "sha", "9" * 40),
        ("head", "ref", "other-head"),
        ("base", "ref", "main"),
    ):
        candidate = _pull(publication, 43)
        projected = dict(candidate[section])  # type: ignore[arg-type]
        projected[field] = value
        candidate[section] = projected
        malformed.append(candidate)
    for section in ("head", "base"):
        candidate = _pull(publication, 43)
        projected = dict(candidate[section])  # type: ignore[arg-type]
        projected["repo"] = {"full_name": "acme/other"}
        candidate[section] = projected
        malformed.append(candidate)

    for payload in malformed:
        api = QueueApi(
            _inspection()
            + _inspection()
            + [_publication_ref(publication)]
            + _inspection()
            + [_publication_ref(publication), _response(201, payload)]
        )
        client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
        await client.inspect("acme/project")

        with pytest.raises(GitHubTeamStateError):
            await client.open_publication_pr(publication, expected_head_commit=PUBLICATION_COMMIT)


@pytest.mark.anyio
async def test_publication_merge_requires_exact_live_state_head_and_prior_pr_receipt() -> None:
    """Catches local trust becoming durable while the reviewed publication is merely open."""
    publication = _publication()
    current = _inspection(state_commit=MERGED_COMMIT)
    api = QueueApi(
        current + current + _merged_verification(publication),
        raw=_merged_raw(publication),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    confirmed = await client.confirm_publication_merge(
        publication,
        expected_head_commit=PUBLICATION_COMMIT,
        expected_base_commit=STATE_COMMIT,
        pull_request_number=43,
    )

    assert confirmed.branch_commit == MERGED_COMMIT


@pytest.mark.anyio
async def test_exact_closed_publication_pr_can_be_selected_for_explicit_restart() -> None:
    publication = _publication()
    pull = _pull(publication, 43)
    pull["state"] = "closed"
    api = QueueApi(_inspection() + [_response(200, pull)])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    state = await client.publication_pull_request_state(
        publication,
        expected_head_commit=PUBLICATION_COMMIT,
        expected_base_commit=STATE_COMMIT,
        pull_request_number=43,
    )

    assert state == "closed"


@pytest.mark.anyio
async def test_publication_merge_streams_a_valid_bundle_larger_than_json_limit() -> None:
    """Catches valid encrypted state being forced through the generic 1 MiB JSON ceiling."""
    publication = _publication(b"x" * (1024 * 1024 + 1))
    current = _inspection(state_commit=MERGED_COMMIT)
    api = QueueApi(
        current + current + _merged_verification(publication),
        raw=_merged_raw(publication),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    confirmed = await client.confirm_publication_merge(
        publication,
        expected_head_commit=PUBLICATION_COMMIT,
        expected_base_commit=STATE_COMMIT,
        pull_request_number=43,
    )

    assert confirmed.branch_commit == MERGED_COMMIT
    bundle_reads = [
        call for call in api.calls if call[0] == "GET-BYTES" and call[2]["max_bytes"] > 1024 * 1024
    ]
    assert len(bundle_reads) == 1
    assert bundle_reads[0][2]["max_bytes"] == len(publication.bundle)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("merged", False),
        ("merge_commit_sha", "9" * 40),
        ("number", 44),
    ],
)
async def test_publication_merge_rejects_an_open_or_changed_pr_receipt(
    field: str, value: object
) -> None:
    publication = _publication()
    current = _inspection(state_commit=MERGED_COMMIT)
    merged = _pull(publication, 43)
    merged.update({"merged": True, "merge_commit_sha": MERGED_COMMIT, field: value})
    verification = _merged_verification(publication)
    verification[0] = _response(200, merged)
    api = QueueApi(current + current + verification, raw=_merged_raw(publication))
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.confirm_publication_merge(
            publication,
            expected_head_commit=PUBLICATION_COMMIT,
            expected_base_commit=STATE_COMMIT,
            pull_request_number=43,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["parent", "tree", "blob"])
async def test_publication_merge_rejects_changed_parent_tree_or_blob(change: str) -> None:
    """Catches a rewritten merge being trusted without exact state artifact verification."""
    publication = _publication()
    current = _inspection(state_commit=MERGED_COMMIT)
    if change == "parent":
        verification = _merged_verification(publication, parent="9" * 40)
    elif change == "tree":
        verification = _merged_verification(publication, extra_tree_entry=True)
    else:
        verification = _merged_verification(publication)
    api = QueueApi(
        current + current + verification,
        raw=_merged_raw(publication, manifest=b"changed")
        if change == "blob"
        else _merged_raw(publication),
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await client.confirm_publication_merge(
            publication,
            expected_head_commit=PUBLICATION_COMMIT,
            expected_base_commit=STATE_COMMIT,
            pull_request_number=43,
        )


@pytest.mark.anyio
async def test_branch_protection_requires_its_exact_second_preview_digest() -> None:
    """Catches branch protection mutation under stale or single-stage consent."""
    missing = _inspection()
    missing[2:] = [
        _response(404, {}),
        _response(200, {"name": "main", "commit": {"sha": "b" * 40}}),
        _response(404, {}),
    ]
    configured = _response(
        200,
        {
            "enforce_admins": {"enabled": True},
            "allow_deletions": {"enabled": False},
            "allow_force_pushes": {"enabled": False},
            "required_linear_history": {"enabled": True},
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "required_approving_review_count": 1,
                "require_code_owner_reviews": False,
            },
            "required_status_checks": {
                "strict": True,
                "contexts": ["Intent Engineering / state"],
                "checks": [
                    {
                        "context": "Intent Engineering / state",
                        "app_id": GITHUB_ACTIONS_APP_ID,
                    }
                ],
            },
        },
    )
    tree_sha = "c" * 40
    anchor_sha = "d" * 40
    api = QueueApi(
        missing
        + missing
        + [
            _response(201, {"sha": tree_sha}),
            _response(201, {"sha": anchor_sha, "tree": {"sha": tree_sha}, "parents": []}),
            _response(201, {"ref": "refs/heads/intent-state", "object": {"sha": anchor_sha}}),
            _response(200, {"name": "intent-state", "commit": {"sha": anchor_sha}}),
        ]
        + _created_branch_inspection(anchor_sha)
        + [configured]
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    status = await client.inspect("acme/project")
    assert status.protection_compatible is False
    preview = client.protection_preview()

    with pytest.raises(GitHubTeamStateError):
        await client.configure_protection("sha256:" + "0" * 64)
    assert all(call[0] != "PUT" for call in api.calls)

    updated = await client.configure_protection(
        preview.digest, record_bootstrap=lambda _anchor, _tree: None
    )

    assert updated.protection_compatible is True
    assert api.calls[-1] == (
        "PUT",
        "/repos/acme/project/branches/intent-state/protection",
        {
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
                        "context": "Intent Engineering / state",
                        "app_id": GITHUB_ACTIONS_APP_ID,
                    }
                ],
                "contexts": [],
                "strict": True,
            },
            "restrictions": None,
        },
    )


@pytest.mark.anyio
async def test_inspect_reports_absent_state_branch_with_an_exact_bootstrap_base() -> None:
    """Catches new-team setup failing opaquely or inventing a branch starting point."""
    responses = _inspection()
    responses[2:] = [
        _response(404, {}),
        _response(
            200,
            {"name": "main", "protected": True, "commit": {"sha": "b" * 40}},
        ),
        _response(200, {"path": ".github/CODEOWNERS", "type": "file"}),
    ]
    client = GitHubTeamStateClient(
        QueueApi(responses), expected_account_id="123", expected_login="alice"
    )

    status = await client.inspect("acme/project")
    protection = client.protection_preview()

    assert status.branch_present is False
    assert status.default_branch_commit == "b" * 40
    assert protection.branch_creation_required is True
    assert protection.requires_change is True


@pytest.mark.anyio
async def test_confirmed_bootstrap_creates_an_empty_orphan_anchor_never_copies_main() -> None:
    """Catches setup mixing code history into the encrypted state branch."""
    missing = _inspection()
    missing[2:] = [
        _response(404, {}),
        _response(200, {"name": "main", "commit": {"sha": "b" * 40}}),
        _response(404, {}),
    ]
    configured = _response(
        200,
        {
            "enforce_admins": {"enabled": True},
            "allow_deletions": {"enabled": False},
            "allow_force_pushes": {"enabled": False},
            "required_linear_history": {"enabled": True},
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "required_approving_review_count": 1,
                "require_code_owner_reviews": False,
            },
            "required_status_checks": {
                "strict": True,
                "contexts": ["Intent Engineering / state"],
                "checks": [
                    {
                        "context": "Intent Engineering / state",
                        "app_id": GITHUB_ACTIONS_APP_ID,
                    }
                ],
            },
        },
    )
    tree_sha = "c" * 40
    anchor_sha = "d" * 40
    api = QueueApi(
        missing
        + missing
        + [
            _response(201, {"sha": tree_sha}),
            _response(201, {"sha": anchor_sha, "tree": {"sha": tree_sha}, "parents": []}),
            _response(
                201,
                {"ref": "refs/heads/intent-state", "object": {"sha": anchor_sha}},
            ),
            _response(
                200,
                {"name": "intent-state", "commit": {"sha": anchor_sha}},
            ),
        ]
        + _created_branch_inspection(anchor_sha)
        + [configured]
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    updated = await client.configure_protection(
        client.protection_preview().digest,
        record_bootstrap=lambda _anchor, _tree: None,
    )

    assert updated.branch_commit == anchor_sha
    assert updated.default_branch_commit is None
    tree_call = next(call for call in api.calls if call[1].endswith("/git/trees"))
    commit_call = next(call for call in api.calls if call[1].endswith("/git/commits"))
    ref_call = next(call for call in api.calls if call[1].endswith("/git/refs"))
    assert tree_call[2] == {"tree": []}
    assert commit_call[2]["parents"] == []  # type: ignore[index]
    assert ref_call[2] == {"ref": "refs/heads/intent-state", "sha": anchor_sha}
    assert ref_call[2] != {"ref": "refs/heads/intent-state", "sha": "b" * 40}


@pytest.mark.anyio
async def test_exact_receipted_orphan_recovers_after_lost_ref_creation_response() -> None:
    """Catches a successful ref create followed by a lost response stranding setup forever."""
    missing = _inspection()
    missing[2:] = [
        _response(404, {}),
        _response(200, {"name": "main", "commit": {"sha": "b" * 40}}),
        _response(404, {}),
    ]
    tree_sha = "c" * 40
    anchor_sha = "d" * 40
    receipt: list[tuple[str, str]] = []
    lost = QueueApi(
        missing
        + missing
        + [
            _response(201, {"sha": tree_sha}),
            _response(
                201,
                {"sha": anchor_sha, "tree": {"sha": tree_sha}, "parents": []},
            ),
        ]
    )
    first = GitHubTeamStateClient(lost, expected_account_id="123", expected_login="alice")
    await first.inspect("acme/project")

    with pytest.raises(GitHubTeamStateError):
        await first.configure_protection(
            first.protection_preview().digest,
            record_bootstrap=lambda anchor, tree: receipt.append((anchor, tree)),
        )

    assert receipt == [(anchor_sha, tree_sha)]
    configured = _response(200, _inspection()[3].payload)
    resumed_api = QueueApi(
        _created_branch_inspection(anchor_sha)
        + _created_branch_inspection(anchor_sha)
        + [
            _response(
                200,
                {"sha": anchor_sha, "tree": {"sha": tree_sha}, "parents": []},
            ),
            _response(200, {"sha": tree_sha, "tree": [], "truncated": False}),
            configured,
        ]
    )
    resumed = GitHubTeamStateClient(resumed_api, expected_account_id="123", expected_login="alice")
    await resumed.inspect("acme/project")

    updated = await resumed.configure_protection(
        resumed.protection_preview().digest,
        expected_bootstrap_anchor=anchor_sha,
    )

    assert updated.protection_compatible is True
    assert resumed_api.calls[-1][0] == "PUT"


@pytest.mark.anyio
async def test_pull_request_create_race_relists_and_reuses_only_the_exact_repository_pr() -> None:
    """Catches list-then-create races or a fork PR being accepted as the publication."""
    publication = _publication()
    exact = _pull(publication, 44)

    class RaceApi(QueueApi):
        async def get_pages(
            self, path: str, params: Mapping[str, str], etag: str | None = None
        ) -> PageResult:
            self.calls.append(("GET-PAGES", path, dict(params)))
            if sum(call[0] == "GET-PAGES" for call in self.calls) == 1:
                return PageResult(items=(), etag=None)
            return PageResult(items=(exact,), etag=None)

    api = RaceApi(
        _inspection()
        + _inspection()
        + [_publication_ref(publication)]
        + _inspection()
        + [_publication_ref(publication), _response(422, {"message": "exists"})]
        + _inspection()
        + [_publication_ref(publication)]
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    pull_request = await client.open_publication_pr(
        publication, expected_head_commit=PUBLICATION_COMMIT
    )

    assert pull_request.number == 44
    assert pull_request.created is False
    assert sum(call[0] == "POST" for call in api.calls) == 1


@pytest.mark.anyio
async def test_github_client_accepts_reviewed_get_404_without_mapping_it_to_an_error() -> None:
    """Catches real branch/CODEOWNERS absence being rejected before bootstrap preview."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    client = GitHubClient(
        GitHubCredentials(token=SecretStr("github-token"), source=CredentialSource.ENVIRONMENT),
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    try:
        response = await client.request_json_object(
            "GET",
            "/repos/acme/project/branches/intent-state",
            allowed_statuses=frozenset({200, 404}),
        )
        assert response.status_code == 404
        assert response.payload == {}
    finally:
        await client.aclose()


@pytest.mark.anyio
async def test_inspect_cancellation_closes_the_provider_client() -> None:
    """Catches cancellation leaving the authenticated HTTP client live."""

    class BlockingApi(QueueApi):
        async def request_json_object(self, *_: object, **__: object) -> GitHubJsonResponse:
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

    api = BlockingApi([])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    with anyio.move_on_after(0.01) as scope:
        await client.inspect("acme/project")

    assert scope.cancel_called is True
    assert api.closed is True


@pytest.mark.anyio
async def test_github_client_posts_one_bounded_object_and_maps_rate_errors_secret_safely() -> None:
    """Catches write retries, unvalidated JSON, or credentials entering provider failures."""
    token = "ghp_task6_write_secret_value"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/rate"):
            return httpx.Response(
                429,
                headers={"X-GitHub-Request-Id": token, "Retry-After": "1"},
                json={"message": token},
            )
        return httpx.Response(
            201,
            headers={"X-OAuth-Scopes": "repo"},
            json={"number": 7, "html_url": "https://github.com/acme/project/pull/7"},
        )

    client = GitHubClient(
        GitHubCredentials(token=SecretStr(token), source=CredentialSource.ENVIRONMENT),
        transport=httpx.MockTransport(handler),
        retry_policy=RetryPolicy(max_attempts=1),
    )
    try:
        response = await client.request_json_object(
            "POST",
            "/repos/acme/project/pulls",
            payload={"head": "intent-publication/digest", "base": "intent-state"},
            allowed_statuses=frozenset({201}),
        )
        assert response.payload["number"] == 7
        assert len(requests) == 1

        with pytest.raises(GitHubRateLimitError) as caught:
            await client.request_json_object(
                "GET",
                "/repos/acme/project/rate",
                allowed_statuses=frozenset({200}),
            )
        assert token not in str(caught.value)
        assert token not in repr(caught.value)
        assert len(requests) == 2
    finally:
        await client.aclose()
