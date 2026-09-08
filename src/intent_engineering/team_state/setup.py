"""Repository-local, non-secret handoff for the trusted GitHub setup UI."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import anyio
from pydantic import ConfigDict, Field, model_validator

from intent_engineering.capture.github.auth import GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient
from intent_engineering.capture.github.models import PageResult
from intent_engineering.cli.team import GitHubEnablePreview
from intent_engineering.control_plane.models import (
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
)
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.team_state.ci import CiTrustConfig
from intent_engineering.team_state.github import (
    GitHubDefaultBranchBaseline,
    GitHubDefaultBranchTooling,
    GitHubJsonResponse,
    GitHubTeamStateApi,
    GitHubTeamStateClient,
    GitHubTeamStateError,
    GitHubTeamStateStatus,
)
from intent_engineering.team_state.keys import GitHubIdentity
from intent_engineering.team_state.models import (
    MAX_BUNDLE_BYTES,
    PreparedPublication,
    RecipientRecord,
    TeamStateManifest,
    canonical_manifest_bytes,
)
from intent_engineering.team_state.publication import PublicationAuthority, PublicationService
from intent_engineering.team_state.signing import SigningKeyStore

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime
    from intent_engineering.control_plane.service import ControlPlaneService
    from intent_engineering.team_state.suggestions import CodeSuggestionPreview


class GitHubSetupRequest(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    preview: GitHubEnablePreview


class EncryptedPublicationDraft(StrictModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    manifest: TeamStateManifest
    bundle: str = Field(max_length=MAX_BUNDLE_BYTES * 2)
    signatures: str = Field(max_length=1024 * 1024)
    anchor: str = Field(pattern=r"^[0-9a-f]{40}$")
    external_write_attempted: bool = False
    publication_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    pull_request_number: int | None = Field(default=None, gt=0)
    pull_request_url: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_pending_receipt(self) -> EncryptedPublicationDraft:
        if self.publication_commit is not None and not self.external_write_attempted:
            raise ValueError("invalid publication receipt")
        if self.publication_commit is None and (
            self.pull_request_number is not None or self.pull_request_url is not None
        ):
            raise ValueError("invalid publication receipt")
        if (self.pull_request_number is None) != (self.pull_request_url is None):
            raise ValueError("invalid publication receipt")
        if self.pull_request_number is not None:
            expected = (
                f"https://github.com/{self.manifest.repository_id.removeprefix('github.com/')}"
                f"/pull/{self.pull_request_number}"
            )
            if self.pull_request_url != expected:
                raise ValueError("invalid publication receipt")
        return self

    def publication(self) -> PreparedPublication:
        suffix = (
            f"{self.manifest.graph_version}-{self.manifest.bundle_digest.removeprefix('sha256:')}"
        )
        return PreparedPublication(
            repository_id=self.manifest.repository_id,
            branch=f"intent-publication/{self.manifest.bundle_digest.removeprefix('sha256:')}",
            manifest=self.manifest,
            manifest_bytes=canonical_manifest_bytes(self.manifest),
            bundle=base64.b64decode(self.bundle, validate=True),
            signatures=base64.b64decode(self.signatures, validate=True),
            bundle_path=f"bundles/{suffix}.intent",
            signature_path=f"signatures/{suffix}.json",
        )


class GitHubBootstrapReceipt(StrictModel):
    """Durable public receipt for one owned canonical empty orphan anchor."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    repository_id: str
    anchor: str = Field(pattern=r"^[0-9a-f]{40}$")
    tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    tooling: GitHubDefaultBranchTooling
    authority_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def _parse_publication_draft(content: bytes) -> tuple[EncryptedPublicationDraft, bytes, bool]:
    try:
        raw = json.loads(content)
    except (TypeError, ValueError):
        raise ValueError("GitHub publication draft changed") from None
    if type(raw) is not dict:
        raise ValueError("GitHub publication draft changed")
    legacy = "external_write_attempted" not in raw
    if legacy:
        raw["external_write_attempted"] = True
        migrated_input = json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        draft = EncryptedPublicationDraft.model_validate_json(migrated_input)
    else:
        draft = EncryptedPublicationDraft.model_validate_json(content)
    if legacy:
        if draft.model_dump_json(exclude={"external_write_attempted"}).encode() != content:
            raise ValueError("GitHub publication draft changed")
    elif draft.model_dump_json().encode() != content:
        raise ValueError("GitHub publication draft changed")
    return draft, draft.model_dump_json().encode(), legacy


def _bootstrap_receipt(
    runtime: Runtime,
    *,
    receipt: GitHubBootstrapReceipt | None = None,
    discard: bool = False,
) -> GitHubBootstrapReceipt | None:
    target = runtime.workspace_directory.file("team-bootstrap.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(max_bytes=65536)
            if receipt is None:
                if content is None:
                    return None
                existing = GitHubBootstrapReceipt.model_validate_json(content)
                if existing.model_dump_json().encode() != content:
                    raise ValueError("GitHub bootstrap receipt changed")
                return existing
            encoded = receipt.model_dump_json().encode()
            if len(encoded) > 65536:
                raise ValueError("GitHub bootstrap receipt unavailable")
            if content is not None and content != encoded:
                raise ValueError("GitHub bootstrap receipt changed")
            if discard:
                if content is not None:
                    target.unlink()
                return None
            if content is None:
                target.atomic_write(encoded, reject_target_races=True)
            return receipt
    finally:
        target.close()


def _draft(
    runtime: Runtime,
    *,
    prepared: PreparedPublication | None = None,
    anchor: str | None = None,
    publication_commit: str | None = None,
    pull_request_number: int | None = None,
    pull_request_url: str | None = None,
    external_write_attempted: bool | None = None,
    restart_closed: bool = False,
    discard: bool = False,
) -> EncryptedPublicationDraft | None:
    target = runtime.workspace_directory.file("team-publication.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(
                max_bytes=MAX_BUNDLE_BYTES * 2 + 2 * 1024 * 1024
            )
            if prepared is not None:
                existing: EncryptedPublicationDraft | None = None
                if content is not None:
                    existing, migrated, legacy = _parse_publication_draft(content)
                    if legacy:
                        target.atomic_write(migrated, reject_target_races=True)
                        content = migrated
                draft = EncryptedPublicationDraft(
                    manifest=prepared.manifest,
                    bundle=base64.b64encode(prepared.bundle).decode("ascii"),
                    signatures=base64.b64encode(prepared.signatures).decode("ascii"),
                    anchor=cast(str, anchor),
                    external_write_attempted=(
                        external_write_attempted
                        if external_write_attempted is not None
                        else (existing.external_write_attempted if existing is not None else False)
                    ),
                    publication_commit=publication_commit,
                    pull_request_number=pull_request_number,
                    pull_request_url=pull_request_url,
                )
                encoded = draft.model_dump_json().encode()
                if content is None:
                    if discard:
                        return None
                    target.atomic_write(encoded, reject_target_races=True)
                    return draft
                assert existing is not None
                if (
                    existing.manifest != draft.manifest
                    or existing.bundle != draft.bundle
                    or existing.signatures != draft.signatures
                    or existing.anchor != draft.anchor
                ):
                    raise ValueError("GitHub publication draft changed")
                if discard:
                    target.unlink()
                    return None
                if existing == draft:
                    return existing
                existing_rank = (
                    3
                    if existing.pull_request_number is not None
                    else 2
                    if existing.publication_commit is not None
                    else 1
                    if existing.external_write_attempted
                    else 0
                )
                draft_rank = (
                    3
                    if draft.pull_request_number is not None
                    else 2
                    if draft.publication_commit is not None
                    else 1
                    if draft.external_write_attempted
                    else 0
                )
                closed_restart = (
                    restart_closed
                    and existing_rank == 3
                    and draft_rank == 2
                    and existing.publication_commit == draft.publication_commit
                )
                if (draft_rank <= existing_rank and not closed_restart) or (
                    existing.publication_commit is not None
                    and existing.publication_commit != draft.publication_commit
                ):
                    raise ValueError("GitHub publication draft changed")
                target.atomic_write(encoded, reject_target_races=True)
                return draft
            if content is None:
                return None
            draft, migrated, legacy = _parse_publication_draft(content)
            if legacy:
                target.atomic_write(migrated, reject_target_races=True)
            return draft
    finally:
        target.close()


def save_setup_request(runtime: Runtime, preview: GitHubEnablePreview) -> None:
    request = GitHubSetupRequest(preview=preview)
    content = request.model_dump_json().encode("utf-8")
    if len(content) > 65536:
        raise ValueError("GitHub setup request unavailable")
    target = runtime.workspace_directory.file("team-setup.json")
    try:
        with same_path_lock(target):
            old = target.read_optional_nonblocking(max_bytes=65536)
            if old is not None and old != content:
                raise ValueError("GitHub setup request changed")
            if old is None:
                target.atomic_write(content, reject_target_races=True)
    finally:
        target.close()


def load_setup_request(runtime: Runtime) -> GitHubSetupRequest | None:
    target = runtime.workspace_directory.file("team-setup.json")
    try:
        content = target.read_optional_nonblocking(max_bytes=65536)
        if content is None:
            return None
        request = GitHubSetupRequest.model_validate_json(content)
        if (
            request.preview.project_id != runtime.config.project_id
            or request.preview.actor != runtime.config.local_actor
            or content != request.model_dump_json().encode()
            or request.preview.preview_digest
            != _digest(request.preview.model_dump(mode="json", exclude={"state", "preview_digest"}))
        ):
            raise ValueError("GitHub setup request changed")
        return request
    finally:
        target.close()


def _complete_setup_request(runtime: Runtime, request: GitHubSetupRequest) -> None:
    """Remove only the exact completed handoff; allow later explicit setup requests."""
    target = runtime.workspace_directory.file("team-setup.json")
    try:
        with same_path_lock(target):
            content = target.read_optional_nonblocking(max_bytes=65536)
            if content is None:
                return
            if content != request.model_dump_json().encode():
                raise ValueError("GitHub setup request changed")
            target.unlink()
    finally:
        target.close()


def github_api() -> GitHubTeamStateApi:
    """Resolve local credentials only within a confirmed UI operation."""
    return GitHubClient(GitHubCredentials.resolve(os.environ))


class _OneTimeIdentity:
    def __init__(self, identity: GitHubIdentity) -> None:
        self.identity: GitHubIdentity | None = identity

    def verify(self, proof: bytes) -> GitHubIdentity:
        identity, self.identity = self.identity, None
        if proof != b"local-github-inspection" or identity is None:
            raise ValueError("GitHub identity unavailable")
        return identity


class _PublicationTransport:
    def __init__(self) -> None:
        from intent_engineering.team_state.github_publication import GitHubApiPublisher

        self.publisher: GitHubApiPublisher | None = None
        self.published_commit: str | None = None

    def publish(self, publication: PreparedPublication, *, base_commit: str | None) -> None:
        publisher = self.publisher
        if publisher is None:
            raise ValueError("GitHub publication transport unavailable")

        async def run() -> str:
            return await publisher.publish(publication, base_commit=base_commit)

        self.published_commit = None
        self.published_commit = anyio.from_thread.run(run)


class _GuardedApi:
    """Recheck server-owned human and repository authority before every provider write."""

    def __init__(
        self, api: GitHubTeamStateApi, before_write: Callable[[], Awaitable[None]]
    ) -> None:
        self.api, self.before_write = api, before_write

    async def request_json_object(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> GitHubJsonResponse:
        if method != "GET":
            await self.before_write()
        return await self.api.request_json_object(
            method, path, params=params, payload=payload, allowed_statuses=allowed_statuses
        )

    async def get_pages(
        self, path: str, params: Mapping[str, str], etag: str | None = None
    ) -> PageResult:
        return await self.api.get_pages(path, params, etag)

    async def request_bytes(
        self,
        method: str,
        path: str,
        *,
        max_bytes: int,
        accept: str,
    ) -> bytes:
        return await self.api.request_bytes(
            method,
            path,
            max_bytes=max_bytes,
            accept=accept,
        )

    async def aclose(self) -> None:
        await self.api.aclose()


def _digest(value: object) -> str:
    content = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return "sha256:" + hashlib.sha256(content).hexdigest()


class GitHubSetupBridge:
    """Server-owned setup session; browser data never fabricates a verified decision."""

    def __init__(self, service: ControlPlaneService) -> None:
        request = load_setup_request(service._runtime)
        if request is None:
            raise ValueError("GitHub setup unavailable")
        self.service = service
        self.request = request
        self.repository = request.preview.repository_id.removeprefix("github.com/")
        self.identity: GitHubIdentity | None = None
        self.pending: HumanDecisionPayload | None = None
        self.reviewed: GitHubTeamStateStatus | None = None
        self.suggestions: CodeSuggestionPreview | None = None
        self.authority_digest: str | None = None
        self.protection_digest: str | None = None
        self.tooling: GitHubDefaultBranchTooling | None = None
        self.baseline: GitHubDefaultBranchBaseline | None = None
        self.setup_phase: str | None = None
        self.publication: PublicationService | None = None
        self.recipient_snapshot: RecipientRecord | None = None
        self.transport = _PublicationTransport()
        self.publication_restart_required = False
        self.guard = anyio.Lock()
        service._team_repository_id = request.preview.repository_id
        service._restore_team_recipient()

    def status(self) -> str:
        """Advisory local progress; each next action reinspects live provider authority."""
        if self.service._team_recipient is None:
            return "setup_required"
        draft = _draft(self.service._runtime)
        if draft is not None:
            if draft.manifest.repository_id != self.request.preview.repository_id:
                raise ValueError("GitHub publication draft changed")
            return (
                "publication_pending"
                if draft.pull_request_number is not None
                else "publication_recovery_required"
                if draft.external_write_attempted
                else "publication_draft"
            )
        try:
            SigningKeyStore(
                self.service._runtime.config.project_id,
                self.request.preview.repository_id,
                self.service._runtime.config.local_actor,
            ).public_keys()
        except ValueError:
            from intent_engineering.team_state.suggestions import preview_code_suggestions

            suggestions = preview_code_suggestions(
                self.service._runtime.root,
                self.request.preview.codeowners_suggestion,
                self.request.preview.workflow_suggestion,
            )
            installed = all(
                preimage is not None
                and base64.urlsafe_b64decode(preimage + "=" * (-len(preimage) % 4))
                == content.encode("utf-8")
                for preimage, content in (
                    (suggestions.codeowners_preimage, suggestions.codeowners_content),
                    (suggestions.workflow_preimage, suggestions.workflow_content),
                )
            )
            return "code_changes_staged" if installed else "enrolled"
        return "protection_configured"

    def _authority(self) -> str:
        from intent_engineering.control_plane.service import _parent_digest

        authority = self.service._authority()
        try:
            content = dict(authority.snapshot.content)
            content.pop("webauthn_credentials", None)
            return _parent_digest(content, authority.membership_digest)
        finally:
            authority.close()

    def _recipient(self, status: GitHubTeamStateStatus) -> RecipientRecord:
        self.service._restore_team_recipient()
        recipient = self.service._team_recipient
        if (
            recipient is None
            or self.service._team_enrollment_blocked
            or recipient.repository_id != status.repository_id
            or recipient.github_account_id != status.account_id
            or recipient.github_login != status.login
            or (self.recipient_snapshot is not None and recipient != self.recipient_snapshot)
        ):
            raise ValueError("GitHub enrollment changed")
        return recipient

    def _publication_authority(self) -> PublicationAuthority:
        reviewed = self.reviewed
        if reviewed is None or reviewed.branch_commit is None or not reviewed.protection_compatible:
            raise ValueError("GitHub publication authority unavailable")
        self._recipient(reviewed)
        recipient = self.service._team_recipient
        if recipient is None or self._authority() != self.authority_digest:
            raise ValueError("GitHub publication authority changed")
        signing = SigningKeyStore(recipient.project_id, recipient.repository_id, recipient.actor)
        ci = self.request.preview.ci_recipient
        if (
            ci is None
            or ci.project_id != recipient.project_id
            or ci.repository_id != recipient.repository_id
        ):
            raise ValueError("CI recipient unavailable")
        return PublicationAuthority(
            recipients=tuple(sorted((recipient, ci), key=lambda item: item.key_id)),
            signing_private_keys=signing.load_signing_keys(),
            remote_state=None,
            publication_base_commit=reviewed.branch_commit,
        )

    async def _anchor(self, api: GitHubTeamStateApi, status: GitHubTeamStateStatus) -> None:
        """An existing genesis base is trusted only as a verified orphan empty tree."""
        if status.branch_commit is None:
            return
        commit = await api.request_json_object(
            "GET", f"/repos/{self.repository}/git/commits/{status.branch_commit}"
        )
        tree = commit.payload.get("tree")
        if (
            commit.payload.get("sha") != status.branch_commit
            or commit.payload.get("parents") != []
            or type(tree) is not dict
        ):
            raise ValueError("GitHub bootstrap anchor changed")
        sha = tree.get("sha")
        if type(sha) is not str or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("GitHub bootstrap anchor changed")
        result = await api.request_json_object("GET", f"/repos/{self.repository}/git/trees/{sha}")
        if (
            result.payload.get("sha") != sha
            or result.payload.get("tree") != []
            or result.payload.get("truncated") is not False
        ):
            raise ValueError("GitHub bootstrap anchor changed")

    async def _finalize_merged_publication(
        self,
        client: GitHubTeamStateClient,
        status: GitHubTeamStateStatus,
    ) -> dict[str, object] | None:
        """Persist trust only after the exact reviewed PR commit becomes protected state."""
        draft = _draft(self.service._runtime)
        if draft is None or draft.publication_commit is None:
            return None
        if status.branch_commit == draft.anchor:
            if draft.pull_request_number is not None:
                state = await client.publication_pull_request_state(
                    draft.publication(),
                    expected_head_commit=draft.publication_commit,
                    expected_base_commit=draft.anchor,
                    pull_request_number=draft.pull_request_number,
                )
                self.publication_restart_required = state == "closed"
            return None
        if (
            status.branch_commit is None
            or draft.pull_request_number is None
            or draft.pull_request_url is None
        ):
            raise ValueError("GitHub publication merge changed")
        await client.confirm_publication_merge(
            draft.publication(),
            expected_head_commit=draft.publication_commit,
            expected_base_commit=draft.anchor,
            pull_request_number=draft.pull_request_number,
        )
        from intent_engineering.team_state.local_trust import save_local_trust

        save_local_trust(
            self.service._runtime,
            self._recipient(status),
            SigningKeyStore(
                self.service._runtime.config.project_id,
                status.repository_id,
                self.service._runtime.config.local_actor,
            ).public_keys(),
        )
        _draft(
            self.service._runtime,
            prepared=draft.publication(),
            anchor=draft.anchor,
            publication_commit=draft.publication_commit,
            pull_request_number=draft.pull_request_number,
            pull_request_url=draft.pull_request_url,
            discard=True,
        )
        _complete_setup_request(self.service._runtime, self.request)
        self.service._github_setup_bridge = None
        return {
            "state": "published",
            "pull_request_url": draft.pull_request_url,
            "repository_id": status.repository_id,
        }

    async def action(
        self, action: str, *, payload: HumanDecisionPayload | None = None, response: bytes = b""
    ) -> dict[str, object]:
        signal: BaseException | None = None
        try:
            async with self.guard:
                return await self._action(action, payload=payload, response=response)
        except BaseException as caught:  # noqa: BLE001 - scrub external frames, preserve cancellation
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = (
                caught
                if not isinstance(caught, Exception)
                else ValueError("GitHub setup unavailable")
            )
            self.publication = None
            self.transport.publisher = None
        finally:
            response = b""
            payload = None
        assert signal is not None
        raise signal.with_traceback(None) from None

    async def _action(
        self, action: str, *, payload: HumanDecisionPayload | None, response: bytes
    ) -> dict[str, object]:
        from intent_engineering.team_state.suggestions import (
            preview_code_suggestions,
            stage_code_suggestions,
        )

        service = self.service
        if load_setup_request(service._runtime) != self.request:
            raise ValueError("GitHub setup request changed")
        if action == "cancel":
            draft = _draft(service._runtime)
            bootstrap = _bootstrap_receipt(service._runtime)
            if (
                draft is not None
                and (
                    draft.external_write_attempted
                    or draft.publication_commit is not None
                    or draft.pull_request_number is not None
                    or draft.pull_request_url is not None
                )
            ) or bootstrap is not None:
                raise ValueError(
                    "GitHub provider state must be reconciled before setup can restart"
                )
            self.pending = None
            self.publication = None
            self.transport.publisher = None
            service.cancel_team_enrollment()
            if draft is not None:
                if draft.manifest.repository_id != self.request.preview.repository_id:
                    raise ValueError("GitHub publication draft changed")
                _draft(
                    service._runtime,
                    prepared=draft.publication(),
                    anchor=draft.anchor,
                    discard=True,
                )
            _complete_setup_request(service._runtime, self.request)
            service._github_setup_bridge = None
            return {"state": "cancelled"}
        api: GitHubTeamStateApi | None = None
        client: GitHubTeamStateClient | None = None
        decision: VerifiedHumanDecision | None = None

        async def require_write_authority() -> None:
            if client is None or decision is None or api is None:
                raise ValueError("GitHub write authority unavailable")
            now = service._now()
            if (
                not decision.payload.issued_at
                <= decision.verified_at
                <= now
                <= decision.payload.expires_at
                or self._authority() != self.authority_digest
            ):
                raise ValueError("GitHub write authority changed")
            reviewed = client._reviewed
            if reviewed is None or await client._inspect(self.repository) != reviewed:
                raise ValueError("GitHub repository authority changed")
            self._recipient(reviewed)
            if (
                preview_code_suggestions(
                    service._runtime.root,
                    self.request.preview.codeowners_suggestion,
                    self.request.preview.workflow_suggestion,
                )
                != self.suggestions
            ):
                raise ValueError("GitHub code suggestions changed")
            await self._anchor(api, reviewed)
            if self.baseline is None:
                raise ValueError("GitHub default-branch baseline unavailable")
            live_baseline = await client.verify_default_branch_baseline()
            if live_baseline != self.baseline:
                raise ValueError("GitHub default-branch baseline changed")
            if self.setup_phase in {"protection", "publication"}:
                if self.tooling is None:
                    raise ValueError("GitHub default-branch tooling unavailable")
                live_tooling = await client.verify_default_branch_tooling(
                    codeowners=self.request.preview.codeowners_suggestion.encode("utf-8"),
                    workflow=self.request.preview.workflow_suggestion.encode("utf-8"),
                    runner_id=self.request.preview.ci_recipient.runner_id
                    if self.request.preview.ci_recipient is not None
                    else "",
                )
                if live_tooling != self.tooling:
                    raise ValueError("GitHub default-branch tooling changed")
            if (
                self._authority() != self.authority_digest
                or load_setup_request(service._runtime) != self.request
            ):
                raise ValueError("GitHub local authority changed")

        try:
            api = _GuardedApi(github_api(), require_write_authority)
            user = await api.request_json_object("GET", "/user")
            identity = GitHubIdentity.model_validate(
                {"account_id": str(user.payload.get("id")), "login": user.payload.get("login")}
            )
            if self.identity is not None and identity != self.identity:
                raise ValueError("GitHub identity changed")
            self.identity = identity
            client = GitHubTeamStateClient(
                api, expected_account_id=identity.account_id, expected_login=identity.login
            )
            status = await client.inspect(self.repository)
            if status.repository_id != self.request.preview.repository_id:
                raise ValueError("GitHub repository changed")
            if self.request.preview.code_owner != f"@{status.login}":
                raise ValueError(
                    "GitHub code owner unavailable; configure exactly one github:<login> alias "
                    "for the local actor"
                )
            completed = await self._finalize_merged_publication(client, status)
            if completed is not None:
                return completed
            if action == "inspect":
                draft = _draft(service._runtime)
                return {
                    "state": (
                        "identity_verified"
                        if service._team_recipient is None
                        else "publication_restart_required"
                        if self.publication_restart_required
                        else self.status()
                    ),
                    "repository_id": status.repository_id,
                    "github_account_id": status.account_id,
                    "github_login": status.login,
                    "enrollment": service.team_enrollment_status(),
                    "pull_request_url": draft.pull_request_url if draft is not None else None,
                }
            if action == "enroll":
                verifier = _OneTimeIdentity(identity)
                service._github_identity_verifier = verifier
                try:
                    options = service.team_enrollment_options(b"local-github-inspection")
                    return cast(dict[str, object], json.loads(options))
                finally:
                    verifier.identity = None
                    service._github_identity_verifier = None
            self._recipient(status)
            suggestions = preview_code_suggestions(
                service._runtime.root,
                self.request.preview.codeowners_suggestion,
                self.request.preview.workflow_suggestion,
            )
            if action == "publication-preview":
                baseline = await client.verify_default_branch_baseline()
                publication_tooling = await client.verify_default_branch_tooling(
                    codeowners=self.request.preview.codeowners_suggestion.encode("utf-8"),
                    workflow=self.request.preview.workflow_suggestion.encode("utf-8"),
                    runner_id=self.request.preview.ci_recipient.runner_id
                    if self.request.preview.ci_recipient is not None
                    else "",
                )
                await self._anchor(api, status)
                ci_recipient = self.request.preview.ci_recipient
                if ci_recipient is None:
                    raise ValueError("CI recipient unavailable")
                if not status.protection_compatible or status.branch_commit is None:
                    raise ValueError("GitHub protection required")
                self.reviewed, self.authority_digest = status, self._authority()
                self.recipient_snapshot = service._team_recipient
                self.suggestions = suggestions
                self.baseline = baseline
                self.tooling = publication_tooling
                self.setup_phase = "publication"
                self.publication = PublicationService(
                    service._runtime,
                    repository_id=status.repository_id,
                    decision_repository_id=service.repository_id,
                    authority=self._publication_authority,
                    publisher=self.transport,
                    challenge_source=service._challenge_source,
                )
                draft = _draft(service._runtime)
                if draft is None:
                    preview = self.publication.preview(now=service._now())
                    _draft(
                        service._runtime,
                        prepared=self.publication.pending_publication(),
                        anchor=status.branch_commit,
                    )
                else:
                    recipient = service._team_recipient
                    key_store = service._team_key_store
                    if (
                        draft.anchor != status.branch_commit
                        or recipient is None
                        or key_store is None
                    ):
                        raise ValueError("GitHub publication draft changed")
                    preview = self.publication.recover_preview(
                        draft.publication(),
                        recipient_private_key=key_store.private_key(recipient.key_id),
                        now=service._now(),
                    )
                self.pending = preview.payload
                signing = SigningKeyStore(
                    service._runtime.config.project_id,
                    status.repository_id,
                    service._runtime.config.local_actor,
                )
                return {
                    "payload": preview.payload.model_dump(mode="json"),
                    "preview": {
                        "bundle_digest": preview.manifest.bundle_digest,
                        "parent_bundle_digest": preview.manifest.parent_bundle_digest,
                        "branch": preview.branch,
                        "recipient_key_ids": list(preview.recipient_key_ids),
                        "manifest": preview.manifest.model_dump(mode="json"),
                        "signing_public_keys": {
                            key: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
                            for key, value in signing.public_keys().items()
                        },
                        "ci_trust": CiTrustConfig(
                            recipient=ci_recipient,
                            signing_public_keys={
                                key: base64.b64encode(value).decode("ascii")
                                for key, value in signing.public_keys().items()
                            },
                        ).model_dump(mode="json"),
                    },
                }
            if action == "protection-preview":
                try:
                    baseline = await client.verify_default_branch_baseline()
                except GitHubTeamStateError:
                    return {
                        "state": "default_branch_prerequisite",
                        "repository_id": status.repository_id,
                        "guidance": (
                            "Protect the default branch with enforced administrators, stale-review "
                            "dismissal, at least one human approval, no bypass allowances, and no "
                            "force pushes or deletions before staging setup files."
                        ),
                    }
                installed = True
                for existing, suggested in (
                    (suggestions.codeowners_preimage, suggestions.codeowners_content),
                    (suggestions.workflow_preimage, suggestions.workflow_content),
                ):
                    current = (
                        None
                        if existing is None
                        else base64.urlsafe_b64decode(existing + "=" * (-len(existing) % 4))
                    )
                    if current is not None and current != suggested.encode("utf-8"):
                        raise ValueError("GitHub code suggestions conflict")
                    installed = installed and current == suggested.encode("utf-8")
                tooling: GitHubDefaultBranchTooling | None = None
                if installed:
                    try:
                        tooling = await client.verify_default_branch_tooling(
                            codeowners=self.request.preview.codeowners_suggestion.encode("utf-8"),
                            workflow=self.request.preview.workflow_suggestion.encode("utf-8"),
                            runner_id=self.request.preview.ci_recipient.runner_id
                            if self.request.preview.ci_recipient is not None
                            else "",
                        )
                    except GitHubTeamStateError:
                        return {
                            "state": "code_changes_staged",
                            "repository_id": status.repository_id,
                            "guidance": (
                                "Commit and merge the exact staged CODEOWNERS and validator workflow "
                                "to the protected default branch, restrict the intent-state runner "
                                "group to that workflow, then preview protection again."
                            ),
                        }
                authority = self._authority()
                bootstrap = _bootstrap_receipt(service._runtime)
                if bootstrap is not None:
                    if (
                        bootstrap.repository_id != status.repository_id
                        or bootstrap.tooling != tooling
                        or bootstrap.authority_digest != authority
                        or (status.branch_present and status.branch_commit != bootstrap.anchor)
                    ):
                        raise ValueError("GitHub bootstrap receipt changed")
                    if status.branch_present:
                        await self._anchor(api, status)
                protection = client.protection_preview() if tooling is not None else None
                self.recipient_snapshot = service._team_recipient
                subject = _digest(
                    {
                        "phase": "protection" if tooling is not None else "code_changes",
                        "protection": (
                            None if protection is None else protection.model_dump(mode="json")
                        ),
                        "status": status.model_dump(mode="json"),
                        "suggestions": suggestions.model_dump(mode="json"),
                        "tooling": None if tooling is None else tooling.model_dump(mode="json"),
                        "authority": authority,
                        "baseline": baseline.model_dump(mode="json"),
                        "recipient": service._team_recipient.model_dump(mode="json")
                        if service._team_recipient
                        else None,
                    }
                )
                now = service._now()
                self.pending = HumanDecisionPayload(
                    project_id=service._runtime.config.project_id,
                    repository_id=service.repository_id,
                    actor=service._runtime.config.local_actor,
                    action=DecisionAction.APPROVE_EXTERNAL_WRITE,
                    graph_version=service._runtime.graph_store.load().version,
                    parent_bundle_digest="sha256:" + "0" * 64,
                    subject=DecisionSubject(
                        kind="github_setup", id="github_setup:" + subject.removeprefix("sha256:")
                    ),
                    subject_digest=subject,
                    result_digest=subject,
                    challenge=service._nonce(),
                    issued_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
                self.reviewed, self.suggestions, self.authority_digest = (
                    status,
                    suggestions,
                    authority,
                )
                self.protection_digest = None if protection is None else protection.digest
                self.tooling = tooling
                self.baseline = baseline
                self.setup_phase = "protection" if tooling is not None else "code_changes"
                return {
                    "payload": self.pending.model_dump(mode="json"),
                    "preview": {
                        "phase": self.setup_phase,
                        "protection": (
                            None if protection is None else protection.model_dump(mode="json")
                        ),
                        "suggestions": suggestions.model_dump(mode="json"),
                        "tooling": None if tooling is None else tooling.model_dump(mode="json"),
                        "github_account_id": status.account_id,
                        "github_login": status.login,
                    },
                }
            if (
                action not in {"options", "verify"}
                or payload is None
                or self.pending is None
                or payload != self.pending
            ):
                raise ValueError("GitHub setup decision changed")
            if status != self.reviewed or self._authority() != self.authority_digest:
                raise ValueError("GitHub setup authority changed")
            if suggestions != self.suggestions:
                raise ValueError("GitHub code suggestions changed")
            if action == "options":
                return cast(
                    dict[str, object],
                    json.loads(
                        service._webauthn.authentication_options(
                            payload, service._origin, service._now()
                        )
                    ),
                )
            decision = service._webauthn.verify(response, payload, service._origin, service._now())
            recipient = service._team_recipient
            if (
                recipient is None
                or decision.credential.local_only
                or decision.credential.credential_id != recipient.webauthn_credential_id
                or decision.credential.public_key != recipient.webauthn_credential_public_key
                or decision.credential.github_account_id != status.account_id
                or decision.credential.github_login != status.login
            ):
                raise ValueError("GitHub decision credential changed")
            self.pending = None
            if self._authority() != self.authority_digest:
                raise ValueError("GitHub setup authority changed")
            if payload.action is DecisionAction.PUBLISH_STATE:
                from intent_engineering.team_state.github_publication import GitHubApiPublisher

                publication = self.publication
                if publication is None:
                    raise ValueError("GitHub publication unavailable")
                pending_draft = _draft(service._runtime)
                if pending_draft is None or pending_draft.anchor != status.branch_commit:
                    raise ValueError("GitHub publication draft changed")
                if self.publication_restart_required:
                    if (
                        pending_draft.publication_commit is None
                        or pending_draft.pull_request_number is None
                        or await client.publication_pull_request_state(
                            pending_draft.publication(),
                            expected_head_commit=pending_draft.publication_commit,
                            expected_base_commit=pending_draft.anchor,
                            pull_request_number=pending_draft.pull_request_number,
                        )
                        != "closed"
                    ):
                        raise ValueError("GitHub publication restart changed")
                    _draft(
                        service._runtime,
                        prepared=pending_draft.publication(),
                        anchor=pending_draft.anchor,
                        external_write_attempted=True,
                        publication_commit=pending_draft.publication_commit,
                        restart_closed=True,
                    )
                    pending_draft = cast(EncryptedPublicationDraft, _draft(service._runtime))
                _draft(
                    service._runtime,
                    prepared=pending_draft.publication(),
                    anchor=pending_draft.anchor,
                    external_write_attempted=True,
                    publication_commit=pending_draft.publication_commit,
                    pull_request_number=pending_draft.pull_request_number,
                    pull_request_url=pending_draft.pull_request_url,
                )
                self.transport.publisher = GitHubApiPublisher(api, client, status)
                try:
                    prepared = await anyio.to_thread.run_sync(
                        lambda: publication.prepare(decision, now=service._now())
                    )
                finally:
                    self.transport.publisher = None
                self.publication = None
                if self.transport.published_commit is None:
                    raise ValueError("GitHub publication commit unavailable")
                _draft(
                    service._runtime,
                    prepared=prepared,
                    anchor=status.branch_commit,
                    external_write_attempted=True,
                    publication_commit=self.transport.published_commit,
                )
                pr = await client.open_publication_pr(
                    prepared, expected_head_commit=self.transport.published_commit
                )
                _draft(
                    service._runtime,
                    prepared=prepared,
                    anchor=status.branch_commit,
                    external_write_attempted=True,
                    publication_commit=self.transport.published_commit,
                    pull_request_number=pr.number,
                    pull_request_url=pr.url,
                )
                return {
                    "state": "publication_pending",
                    "pull_request_url": pr.url,
                    "repository_id": pr.repository_id,
                }
            if self.setup_phase == "code_changes":
                await require_write_authority()
                stage_code_suggestions(service._runtime.root, suggestions)
                self.suggestions = preview_code_suggestions(
                    service._runtime.root,
                    self.request.preview.codeowners_suggestion,
                    self.request.preview.workflow_suggestion,
                )
                return {
                    "state": "code_changes_staged",
                    "repository_id": status.repository_id,
                    "guidance": (
                        "Commit and merge the staged CODEOWNERS and validator workflow to the "
                        "protected default branch, then preview branch protection again."
                    ),
                }
            if self.setup_phase != "protection" or self.protection_digest is None:
                raise ValueError("GitHub setup phase changed")
            tooling = self.tooling
            if tooling is None or self.authority_digest is None:
                raise ValueError("GitHub bootstrap authority unavailable")
            bootstrap = _bootstrap_receipt(service._runtime)

            def record_bootstrap(anchor: str, tree: str) -> None:
                _bootstrap_receipt(
                    service._runtime,
                    receipt=GitHubBootstrapReceipt(
                        repository_id=status.repository_id,
                        anchor=anchor,
                        tree=tree,
                        tooling=tooling,
                        authority_digest=cast(str, self.authority_digest),
                    ),
                )

            updated = await client.configure_protection(
                self.protection_digest,
                record_bootstrap=record_bootstrap,
                expected_bootstrap_anchor=(None if bootstrap is None else bootstrap.anchor),
            )
            await self._anchor(api, updated)
            await require_write_authority()
            completed_bootstrap = _bootstrap_receipt(service._runtime)
            if completed_bootstrap is not None:
                _bootstrap_receipt(
                    service._runtime,
                    receipt=completed_bootstrap,
                    discard=True,
                )
            SigningKeyStore(
                service._runtime.config.project_id,
                status.repository_id,
                service._runtime.config.local_actor,
            ).signing_keys()
            return {
                "state": "protection_configured",
                "repository_id": updated.repository_id,
                "anchor": updated.branch_commit,
            }
        finally:
            response = b""
            if api is not None:
                failure = sys.exc_info()[1]
                with anyio.CancelScope(shield=True):
                    try:
                        await api.aclose()
                    except BaseException:
                        if failure is None:
                            raise
