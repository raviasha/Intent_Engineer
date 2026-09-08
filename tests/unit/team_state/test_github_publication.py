"""Authenticated GitHub Git Data publication without credential-bearing Git subprocesses."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime

import anyio
import pytest

from intent_engineering.capture.github.models import PageResult
from intent_engineering.team_state.github import (
    GitHubJsonResponse,
    GitHubTeamStateStatus,
)
from intent_engineering.team_state.github_publication import (
    GitHubApiPublisher,
    GitHubPublicationError,
)
from intent_engineering.team_state.models import (
    PreparedPublication,
    TeamStateManifest,
    canonical_manifest_bytes,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
ANCHOR = "a" * 40


def _publication() -> PreparedPublication:
    bundle = b"encrypted bundle"
    bundle_digest = f"sha256:{hashlib.sha256(bundle).hexdigest()}"
    manifest = TeamStateManifest(
        project_id="project",
        repository_id="github.com/acme/project",
        graph_version=1,
        parent_bundle_digest=None,
        bundle_digest=bundle_digest,
        bundle_size=len(bundle),
        recipient_key_ids=("recipient:alice",),
        required_signature_ids=("signer:release",),
        created_at=NOW,
    )
    suffix = f"1-{bundle_digest.removeprefix('sha256:')}"
    return PreparedPublication(
        repository_id=manifest.repository_id,
        branch=f"intent-publication/{bundle_digest.removeprefix('sha256:')}",
        manifest=manifest,
        manifest_bytes=canonical_manifest_bytes(manifest),
        bundle=bundle,
        signatures=b'{"signatures":[]}',
        bundle_path=f"bundles/{suffix}.intent",
        signature_path=f"signatures/{suffix}.json",
    )


def _status(*, branch_commit: str = ANCHOR) -> GitHubTeamStateStatus:
    return GitHubTeamStateStatus(
        repository_id="github.com/acme/project",
        repository_node_id="77",
        account_id="123",
        login="alice",
        scopes=("repo",),
        private=True,
        default_branch="main",
        branch_commit=branch_commit,
        branch_present=True,
        protection_compatible=True,
        codeowners_present=True,
    )


class InspectClient:
    def __init__(self, statuses: list[GitHubTeamStateStatus]) -> None:
        self.statuses = statuses
        self.calls = 0

    async def inspect(self, repository: str) -> GitHubTeamStateStatus:
        assert repository == "acme/project"
        self.calls += 1
        return self.statuses.pop(0) if self.statuses else _status()


class RecordingApi:
    def __init__(self, responses: list[GitHubJsonResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Mapping[str, object] | None]] = []

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
        raise AssertionError("publisher must not paginate")

    async def aclose(self) -> None:
        raise AssertionError("publisher does not own the shared API client")


def _response(status: int, payload: Mapping[str, object]) -> GitHubJsonResponse:
    return GitHubJsonResponse(status_code=status, payload=payload, headers={})


def _success_responses(
    publication: PreparedPublication, *, created_tree_recursive: bool = False
) -> list[GitHubJsonResponse]:
    paths = ("manifest.json", publication.bundle_path, publication.signature_path)
    blobs = ("b" * 40, "c" * 40, "d" * 40)
    created_tree = (
        [
            {"path": path, "mode": "100644", "type": "blob", "sha": sha}
            for path, sha in zip(paths, blobs, strict=True)
        ]
        if created_tree_recursive
        else [
            {"path": "bundles", "mode": "040000", "type": "tree", "sha": "1" * 40},
            {
                "path": "manifest.json",
                "mode": "100644",
                "type": "blob",
                "sha": blobs[0],
            },
            {
                "path": "signatures",
                "mode": "040000",
                "type": "tree",
                "sha": "2" * 40,
            },
        ]
    )
    return [
        *(_response(201, {"sha": sha}) for sha in blobs),
        _response(
            201,
            {
                "sha": "e" * 40,
                "tree": created_tree,
                "truncated": False,
            },
        ),
        _response(
            200,
            {
                "sha": "e" * 40,
                "tree": [
                    {"path": "bundles", "mode": "040000", "type": "tree", "sha": "1" * 40},
                    {"path": paths[1], "mode": "100644", "type": "blob", "sha": blobs[1]},
                    {"path": paths[0], "mode": "100644", "type": "blob", "sha": blobs[0]},
                    {
                        "path": "signatures",
                        "mode": "040000",
                        "type": "tree",
                        "sha": "2" * 40,
                    },
                    {"path": paths[2], "mode": "100644", "type": "blob", "sha": blobs[2]},
                ],
                "truncated": False,
            },
        ),
        _response(
            201,
            {
                "sha": "f" * 40,
                "tree": {"sha": "e" * 40},
                "parents": [{"sha": ANCHOR}],
            },
        ),
        _response(
            201,
            {
                "ref": f"refs/heads/{publication.branch}",
                "object": {"sha": "f" * 40, "type": "commit"},
            },
        ),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize("created_tree_recursive", [False, True])
async def test_publisher_creates_only_exact_encrypted_publication_objects_and_ref(
    created_tree_recursive: bool,
) -> None:
    """Catches code/plaintext paths, state-ref updates, or writes without live reinspection."""
    publication = _publication()
    api = RecordingApi(
        _success_responses(publication, created_tree_recursive=created_tree_recursive)
    )
    inspector = InspectClient([_status()] * 6)

    await GitHubApiPublisher(api, inspector, _status()).publish(
        publication,
        base_commit=ANCHOR,
    )

    assert inspector.calls == 6
    assert [call[:2] for call in api.calls] == [
        ("POST", "/repos/acme/project/git/blobs"),
        ("POST", "/repos/acme/project/git/blobs"),
        ("POST", "/repos/acme/project/git/blobs"),
        ("POST", "/repos/acme/project/git/trees"),
        ("GET", "/repos/acme/project/git/trees/" + "e" * 40),
        ("POST", "/repos/acme/project/git/commits"),
        ("POST", "/repos/acme/project/git/refs"),
    ]
    expected_content = (
        publication.manifest_bytes,
        publication.bundle,
        publication.signatures,
    )
    for call, content in zip(api.calls[:3], expected_content, strict=True):
        assert call[2] == {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64",
        }
    assert api.calls[3][2] == {
        "tree": [
            {
                "mode": "100644",
                "path": "manifest.json",
                "sha": "b" * 40,
                "type": "blob",
            },
            {
                "mode": "100644",
                "path": publication.bundle_path,
                "sha": "c" * 40,
                "type": "blob",
            },
            {
                "mode": "100644",
                "path": publication.signature_path,
                "sha": "d" * 40,
                "type": "blob",
            },
        ]
    }
    assert api.calls[4][2] == {"recursive": "1"}
    assert api.calls[5][2] == {
        "author": {
            "date": "2026-09-08T12:00:00Z",
            "email": "intent-state@localhost",
            "name": "Intent Engineering",
        },
        "committer": {
            "date": "2026-09-08T12:00:00Z",
            "email": "intent-state@localhost",
            "name": "Intent Engineering",
        },
        "message": f"Publish intent state {publication.manifest.bundle_digest}",
        "parents": [ANCHOR],
        "tree": "e" * 40,
    }
    assert api.calls[6][2] == {
        "ref": f"refs/heads/{publication.branch}",
        "sha": "f" * 40,
    }
    assert all(
        path != "/repos/acme/project/git/refs/heads/intent-state" for _, path, _ in api.calls
    )


@pytest.mark.anyio
async def test_publisher_rejects_stale_or_unprotected_base_before_any_write() -> None:
    """Catches a publication descending from a changed or unprotected state branch."""
    publication = _publication()
    api = RecordingApi([])
    stale = _status(branch_commit="9" * 40)

    with pytest.raises(GitHubPublicationError):
        await GitHubApiPublisher(api, InspectClient([stale]), _status()).publish(
            publication,
            base_commit=ANCHOR,
        )

    assert api.calls == []


@pytest.mark.anyio
async def test_publisher_recovers_only_the_exact_ref_after_a_creation_race() -> None:
    """Catches 422 being accepted without proving the existing ref names the prepared commit."""
    publication = _publication()
    responses = _success_responses(publication)
    responses[-1] = _response(422, {})
    responses.append(
        _response(
            200,
            {
                "ref": f"refs/heads/{publication.branch}",
                "object": {"sha": "f" * 40, "type": "commit"},
            },
        )
    )
    api = RecordingApi(responses)

    await GitHubApiPublisher(api, InspectClient([_status()] * 7), _status()).publish(
        publication,
        base_commit=ANCHOR,
    )

    assert api.calls[-1][:2] == (
        "GET",
        f"/repos/acme/project/git/ref/heads/{publication.branch}",
    )


@pytest.mark.anyio
async def test_publisher_preserves_cancellation_and_never_creates_a_ref_after_it() -> None:
    """Catches cancellation being replaced by an ordinary error or publication continuing."""
    publication = _publication()

    class BlockingApi(RecordingApi):
        async def request_json_object(self, *args: object, **kwargs: object) -> GitHubJsonResponse:
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

    api = BlockingApi([])
    with anyio.move_on_after(0.01) as scope:
        await GitHubApiPublisher(api, InspectClient([_status()]), _status()).publish(
            publication,
            base_commit=ANCHOR,
        )

    assert scope.cancel_called is True
    assert api.calls == []
