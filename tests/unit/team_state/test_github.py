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
STATE_COMMIT = "a" * 40


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
                "commit": {"sha": "a" * 40},
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
                "required_status_checks": {"strict": True, "contexts": contexts},
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
    ("permissions", "accepted"),
    [
        (
            {"admin": False, "maintain": True, "pull": True, "push": True, "triage": True},
            True,
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
    updated = await client.configure_protection(preview.digest)

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
    incompatible = _inspection(protection_contexts=["another-check"])
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

    updated = await client.configure_protection(preview.digest)

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
