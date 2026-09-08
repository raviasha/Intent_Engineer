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


class QueueApi:
    def __init__(
        self, responses: list[GitHubJsonResponse], pages: PageResult | None = None
    ) -> None:
        self.responses = responses
        self.pages = pages or PageResult(items=(), etag=None)
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
        return self.pages

    async def aclose(self) -> None:
        self.closed = True


def _response(
    status: int, payload: Mapping[str, object], *, scopes: str = "repo"
) -> GitHubJsonResponse:
    return GitHubJsonResponse(
        status_code=status,
        payload=payload,
        headers={"X-OAuth-Scopes": scopes},
    )


def _inspection(*, protection_contexts: list[str] | None = None) -> list[GitHubJsonResponse]:
    contexts = protection_contexts or ["Intent Engineering / state"]
    return [
        _response(
            200,
            {
                "id": 77,
                "full_name": "acme/project",
                "private": True,
                "default_branch": "main",
            },
        ),
        _response(200, {"id": 123, "login": "alice"}),
        _response(
            200,
            {
                "name": "intent-state",
                "protected": True,
                "commit": {"sha": "a" * 40},
            },
        ),
        _response(
            200,
            {
                "enforce_admins": {"enabled": True},
                "required_pull_request_reviews": {
                    "dismiss_stale_reviews": True,
                    "required_approving_review_count": 1,
                    "require_code_owner_reviews": True,
                },
                "required_status_checks": {"strict": True, "contexts": contexts},
            },
        ),
        _response(200, {"path": ".github/CODEOWNERS", "type": "file"}),
    ]


def _publication() -> PreparedPublication:
    bundle = b"encrypted"
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
    assert status.scopes == ("repo",)
    assert status.branch == "intent-state"
    assert status.branch_commit == "a" * 40
    assert status.protection_compatible is True
    assert status.codeowners_present is True


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
        {"id": 77, "full_name": "acme/project", "private": True, "default_branch": "main"},
        scopes="read:user",
    )
    with pytest.raises(GitHubTeamStateError):
        await GitHubTeamStateClient(
            QueueApi(missing_scope), expected_account_id="123", expected_login="alice"
        ).inspect("acme/project")

    changed = QueueApi(_inspection(protection_contexts=["another-check"]))
    changed_client = GitHubTeamStateClient(
        changed, expected_account_id="123", expected_login="alice"
    )
    changed_status = await changed_client.inspect("acme/project")
    assert changed_status.protection_compatible is False
    assert changed_client.protection_preview().requires_change is True

    public = _inspection()
    public[0] = _response(
        200,
        {"id": 77, "full_name": "acme/project", "private": False, "default_branch": "main"},
        scopes="public_repo",
    )
    with pytest.raises(GitHubTeamStateError):
        await GitHubTeamStateClient(
            QueueApi(public), expected_account_id="123", expected_login="alice"
        ).inspect("acme/project")


@pytest.mark.anyio
async def test_open_publication_pr_rechecks_status_and_reuses_one_exact_open_pr() -> None:
    """Catches duplicate PR creation or stale repository/protection authorization."""
    publication = _publication()
    existing = {
        "number": 42,
        "html_url": "https://github.com/acme/project/pull/42",
        "head": {"ref": publication.branch, "repo": {"full_name": "acme/project"}},
        "base": {"ref": "intent-state", "repo": {"full_name": "acme/project"}},
    }
    api = QueueApi(_inspection() + _inspection(), PageResult(items=(existing,), etag=None))
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    pull_request = await client.open_publication_pr(publication)

    assert pull_request.number == 42
    assert pull_request.created is False
    assert pull_request.url == "https://github.com/acme/project/pull/42"
    assert all(call[0] != "POST" for call in api.calls)


@pytest.mark.anyio
async def test_open_publication_pr_posts_one_exact_payload_and_closes_on_cancellation() -> None:
    """Catches an unbound PR target or cancellation leaking the owned provider client."""
    publication = _publication()
    created = _response(
        201,
        {"number": 43, "html_url": "https://github.com/acme/project/pull/43"},
    )
    api = QueueApi(_inspection() + _inspection() + [created])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    pull_request = await client.open_publication_pr(publication)

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
        await client.open_publication_pr(_publication())

    assert all(call[0] != "POST" for call in api.calls)


@pytest.mark.anyio
async def test_branch_protection_requires_its_exact_second_preview_digest() -> None:
    """Catches branch protection mutation under stale or single-stage consent."""
    missing = _inspection(protection_contexts=["another-check"])
    configured = _response(
        200,
        {
            "enforce_admins": {"enabled": True},
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "required_approving_review_count": 1,
                "require_code_owner_reviews": True,
            },
            "required_status_checks": {
                "strict": True,
                "contexts": ["Intent Engineering / state"],
            },
        },
    )
    api = QueueApi(missing + missing + [configured])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    status = await client.inspect("acme/project")
    assert status.protection_compatible is False
    preview = client.protection_preview()

    with pytest.raises(GitHubTeamStateError):
        await client.configure_protection("sha256:" + "0" * 64)
    assert all(call[0] != "PUT" for call in api.calls)

    updated = await client.configure_protection(preview.digest)

    assert updated.protection_compatible is True
    assert api.calls[-1] == (
        "PUT",
        "/repos/acme/project/branches/intent-state/protection",
        {
            "enforce_admins": True,
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "require_code_owner_reviews": True,
                "required_approving_review_count": 1,
            },
            "required_status_checks": {
                "contexts": ["Intent Engineering / state"],
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
            "required_pull_request_reviews": {
                "dismiss_stale_reviews": True,
                "required_approving_review_count": 1,
                "require_code_owner_reviews": True,
            },
            "required_status_checks": {
                "strict": True,
                "contexts": ["Intent Engineering / state"],
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
            configured,
        ]
    )
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    updated = await client.configure_protection(client.protection_preview().digest)

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
async def test_pull_request_create_race_relists_and_reuses_only_the_exact_repository_pr() -> None:
    """Catches list-then-create races or a fork PR being accepted as the publication."""
    publication = _publication()
    exact = {
        "number": 44,
        "html_url": "https://github.com/acme/project/pull/44",
        "head": {
            "ref": publication.branch,
            "repo": {"full_name": "acme/project"},
        },
        "base": {"ref": "intent-state", "repo": {"full_name": "acme/project"}},
    }

    class RaceApi(QueueApi):
        async def get_pages(
            self, path: str, params: Mapping[str, str], etag: str | None = None
        ) -> PageResult:
            self.calls.append(("GET-PAGES", path, dict(params)))
            if sum(call[0] == "GET-PAGES" for call in self.calls) == 1:
                return PageResult(items=(), etag=None)
            return PageResult(items=(exact,), etag=None)

    api = RaceApi(_inspection() + _inspection() + [_response(422, {"message": "exists"})])
    client = GitHubTeamStateClient(api, expected_account_id="123", expected_login="alice")
    await client.inspect("acme/project")

    pull_request = await client.open_publication_pr(publication)

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
