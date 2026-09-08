"""Authenticated GitHub Git Data publication for private repositories."""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import Protocol

import anyio

from intent_engineering.team_state.github import (
    GitHubTeamStateApi,
    GitHubTeamStateStatus,
)
from intent_engineering.team_state.models import PreparedPublication

_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_REPOSITORY_ID = re.compile(
    r"github\.com/((?!-)(?!.*--)[a-z0-9-]{1,39}(?<!-)/[a-z0-9][a-z0-9._-]{0,99})\Z"
)


class GitHubPublicationError(ValueError):
    """One fixed failure that retains no provider or credential material."""

    def __init__(self) -> None:
        super().__init__("GitHub publication unavailable")


class GitHubStatusInspector(Protocol):
    async def inspect(self, repository: str) -> GitHubTeamStateStatus: ...


def _sha(value: object) -> str:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise GitHubPublicationError()
    return value


class GitHubApiPublisher:
    """Create one encrypted publication ref without exposing Git credentials to Git."""

    def __init__(
        self,
        api: GitHubTeamStateApi,
        inspector: GitHubStatusInspector,
        reviewed_status: GitHubTeamStateStatus,
    ) -> None:
        if type(reviewed_status) is not GitHubTeamStateStatus:
            raise GitHubPublicationError()
        reviewed = GitHubTeamStateStatus.model_validate(reviewed_status.model_dump(mode="python"))
        matched = _REPOSITORY_ID.fullmatch(reviewed.repository_id)
        if (
            matched is None
            or not reviewed.branch_present
            or not reviewed.protection_compatible
            or reviewed.branch_commit is None
        ):
            raise GitHubPublicationError()
        self._api = api
        self._inspector = inspector
        self._reviewed = reviewed
        self._repository = matched.group(1)

    async def _require_live(self, base_commit: str) -> None:
        current = await self._inspector.inspect(self._repository)
        if type(current) is not GitHubTeamStateStatus:
            raise GitHubPublicationError()
        current = GitHubTeamStateStatus.model_validate(current.model_dump(mode="python"))
        if (
            current != self._reviewed
            or current.branch_commit != base_commit
            or not current.branch_present
            or not current.protection_compatible
        ):
            raise GitHubPublicationError()

    @staticmethod
    def _validate_created_tree(
        payload: Mapping[str, object],
        expected: tuple[tuple[str, str], ...],
    ) -> str:
        tree_sha = _sha(payload.get("sha"))
        entries = payload.get("tree")
        top: dict[str, tuple[str, str | None]] = {}
        for path, blob_sha in expected:
            name, separator, _remainder = path.partition("/")
            candidate = ("tree", None) if separator else ("blob", blob_sha)
            if name in top and top[name] != candidate:
                raise GitHubPublicationError()
            top[name] = candidate
        leaves = {path: ("blob", blob_sha) for path, blob_sha in expected}
        if type(entries) is not list:
            raise GitHubPublicationError()
        observed: dict[str, tuple[str, str, str]] = {}
        for entry in entries:
            if (
                not isinstance(entry, Mapping)
                or type(entry.get("path")) is not str
                or type(entry.get("type")) is not str
                or type(entry.get("mode")) is not str
            ):
                raise GitHubPublicationError()
            path = entry["path"]
            if path in observed:
                raise GitHubPublicationError()
            observed[path] = (entry["type"], entry["mode"], _sha(entry.get("sha")))

        def matches(specification: Mapping[str, tuple[str, str | None]]) -> bool:
            if set(observed) != set(specification):
                return False
            for path, (kind, expected_sha) in specification.items():
                actual_kind, actual_mode, actual_sha = observed[path]
                if (
                    actual_kind != kind
                    or actual_mode != ("040000" if kind == "tree" else "100644")
                    or (expected_sha is not None and actual_sha != expected_sha)
                ):
                    return False
            return True

        if not (matches(top) or matches(leaves)) or payload.get("truncated") is not False:
            raise GitHubPublicationError()
        return tree_sha

    @staticmethod
    def _validate_recursive_tree(
        payload: Mapping[str, object],
        tree_sha: str,
        expected: tuple[tuple[str, str], ...],
    ) -> None:
        if payload.get("sha") != tree_sha or payload.get("truncated") is not False:
            raise GitHubPublicationError()
        required: dict[str, tuple[str, str | None]] = {
            path: ("blob", blob_sha) for path, blob_sha in expected
        }
        for path, _blob_sha in expected:
            parts = path.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                required.setdefault("/".join(parts[:depth]), ("tree", None))
        entries = payload.get("tree")
        if type(entries) is not list or len(entries) != len(required):
            raise GitHubPublicationError()
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, Mapping) or type(entry.get("path")) is not str:
                raise GitHubPublicationError()
            path = entry["path"]
            expected_entry = required.get(path)
            if expected_entry is None or path in seen:
                raise GitHubPublicationError()
            kind, expected_sha = expected_entry
            if (
                entry.get("type") != kind
                or entry.get("mode") != ("040000" if kind == "tree" else "100644")
                or _sha(entry.get("sha"))
                != (entry.get("sha") if expected_sha is None else expected_sha)
            ):
                raise GitHubPublicationError()
            seen.add(path)
        if seen != set(required):
            raise GitHubPublicationError()

    @staticmethod
    def _validate_commit(payload: Mapping[str, object], tree_sha: str, parent: str) -> str:
        commit_sha = _sha(payload.get("sha"))
        tree = payload.get("tree")
        parents = payload.get("parents")
        if (
            not isinstance(tree, Mapping)
            or tree.get("sha") != tree_sha
            or type(parents) is not list
            or len(parents) != 1
            or not isinstance(parents[0], Mapping)
            or parents[0].get("sha") != parent
        ):
            raise GitHubPublicationError()
        return commit_sha

    @staticmethod
    def _validate_ref(payload: Mapping[str, object], ref: str, commit_sha: str) -> None:
        target = payload.get("object")
        if (
            payload.get("ref") != ref
            or not isinstance(target, Mapping)
            or target.get("type") != "commit"
            or target.get("sha") != commit_sha
        ):
            raise GitHubPublicationError()

    async def publish(
        self,
        publication: PreparedPublication,
        *,
        base_commit: str | None,
    ) -> str:
        """Create an exact publication branch; never update the protected state ref."""
        if type(publication) is not PreparedPublication or base_commit is None:
            raise GitHubPublicationError()
        publication = PreparedPublication.model_validate(publication.model_dump(mode="python"))
        if (
            publication.repository_id != self._reviewed.repository_id
            or _COMMIT.fullmatch(base_commit) is None
            or base_commit != self._reviewed.branch_commit
        ):
            raise GitHubPublicationError()
        cancelled_class = anyio.get_cancelled_exc_class()
        try:
            artifacts = (
                ("manifest.json", publication.manifest_bytes),
                (publication.bundle_path, publication.bundle),
                (publication.signature_path, publication.signatures),
            )
            blobs: list[tuple[str, str]] = []
            for path, content in artifacts:
                await self._require_live(base_commit)
                response = await self._api.request_json_object(
                    "POST",
                    f"/repos/{self._repository}/git/blobs",
                    payload={
                        "content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64",
                    },
                    allowed_statuses=frozenset({201}),
                )
                blobs.append((path, _sha(response.payload.get("sha"))))

            tree_entries = [
                {"mode": "100644", "path": path, "sha": sha, "type": "blob"} for path, sha in blobs
            ]
            await self._require_live(base_commit)
            tree_response = await self._api.request_json_object(
                "POST",
                f"/repos/{self._repository}/git/trees",
                payload={"tree": tree_entries},
                allowed_statuses=frozenset({201}),
            )
            expected_tree = tuple(blobs)
            tree_sha = self._validate_created_tree(tree_response.payload, expected_tree)
            recursive_tree = await self._api.request_json_object(
                "GET",
                f"/repos/{self._repository}/git/trees/{tree_sha}",
                params={"recursive": "1"},
            )
            self._validate_recursive_tree(recursive_tree.payload, tree_sha, expected_tree)

            identity = {
                "date": publication.manifest.model_dump(mode="json")["created_at"],
                "email": "intent-state@localhost",
                "name": "Intent Engineering",
            }
            await self._require_live(base_commit)
            commit_response = await self._api.request_json_object(
                "POST",
                f"/repos/{self._repository}/git/commits",
                payload={
                    "author": identity,
                    "committer": identity,
                    "message": f"Publish intent state {publication.manifest.bundle_digest}",
                    "parents": [base_commit],
                    "tree": tree_sha,
                },
                allowed_statuses=frozenset({201}),
            )
            commit_sha = self._validate_commit(commit_response.payload, tree_sha, base_commit)

            ref = f"refs/heads/{publication.branch}"
            await self._require_live(base_commit)
            ref_response = await self._api.request_json_object(
                "POST",
                f"/repos/{self._repository}/git/refs",
                payload={"ref": ref, "sha": commit_sha},
                allowed_statuses=frozenset({201, 422}),
            )
            if ref_response.status_code == 201:
                self._validate_ref(ref_response.payload, ref, commit_sha)
                return commit_sha
            await self._require_live(base_commit)
            existing = await self._api.request_json_object(
                "GET",
                f"/repos/{self._repository}/git/ref/heads/{publication.branch}",
            )
            self._validate_ref(existing.payload, ref, commit_sha)
            return commit_sha
        except cancelled_class:
            raise
        except GitHubPublicationError:
            raise
        except Exception as error:  # noqa: BLE001 - replace the provider boundary
            error.__traceback__ = None
            raise GitHubPublicationError() from None
        finally:
            if "blobs" in locals():
                blobs.clear()


__all__ = ["GitHubApiPublisher", "GitHubPublicationError", "GitHubStatusInspector"]
