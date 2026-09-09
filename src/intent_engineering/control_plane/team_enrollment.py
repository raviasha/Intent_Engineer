"""Repository-bound public handoff and opaque local enrollment sessions.

Cryptographic enrollment, publication, and restore remain owned by team-state services.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import anyio
from pydantic import ConfigDict, Field, model_validator

from intent_engineering.cli.runtime import Runtime
from intent_engineering.control_plane.models import HumanDecisionPayload
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.team_state.enrollment import (
    JoinResponseV2,
    TeamEnrollmentService,
    TeamInviteV2,
    VerifiedRemoteStateV2,
)
from intent_engineering.team_state.keys import (
    DeviceEnrollmentBinding,
    DeviceKeyStore,
    DevicePublicMaterial,
    GitHubIdentity,
)

if TYPE_CHECKING:
    from intent_engineering.control_plane.service import ControlPlaneService
    from intent_engineering.team_state.github import GitHubProtectionPreview
    from intent_engineering.team_state.local_trust import LocalTrustConfigV2
    from intent_engineering.team_state.models import CanonicalStateSnapshot
    from intent_engineering.team_state.publication import PublicationService
    from intent_engineering.team_state.restore import VerifiedReleaseV2
    from intent_engineering.team_state.setup import EnrollmentApprovalRequest, GitHubSetupBridge

_SESSION_FILE = "team-enrollment-session.json"
_MAX_SESSION_BYTES = 128 * 1024


class EnrollmentRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    session_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str
    repository_id: str
    action: Literal["invite", "join", "approve-join"]
    identity: GitHubIdentity | None = None
    invite: TeamInviteV2 | None = None
    response: JoinResponseV2 | None = None
    output: str | None = None
    created_invite: TeamInviteV2 | None = None

    @model_validator(mode="after")
    def require_action_inputs(self) -> EnrollmentRequest:
        if self.action == "invite":
            valid = self.identity is not None and self.invite is None and self.response is None
        elif self.action == "join":
            valid = self.identity is None and self.invite is not None and self.response is None
        else:
            valid = self.identity is None and self.invite is None and self.response is not None
        if not valid or (self.output is None) != (self.action == "approve-join"):
            raise ValueError("team enrollment unavailable")
        if self.created_invite is not None and (
            self.action != "invite"
            or self.identity is None
            or self.created_invite.project_id != self.project_id
            or self.created_invite.repository_id != self.repository_id
            or str(self.created_invite.intended_github_account_id) != self.identity.account_id
            or self.created_invite.intended_github_login != self.identity.login
        ):
            raise ValueError("team enrollment unavailable")
        for value in (self.invite, self.response):
            if value is not None and (
                value.project_id != self.project_id or value.repository_id != self.repository_id
            ):
                raise ValueError("team enrollment unavailable")
        return self


def load_enrollment_request(runtime: Runtime) -> EnrollmentRequest | None:
    from intent_engineering.cli.team import discover_github_repository

    target = runtime.workspace_directory.file(_SESSION_FILE)
    try:
        content = target.read_optional_nonblocking(max_bytes=_MAX_SESSION_BYTES)
        if content is None:
            return None
        metadata = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("team enrollment unavailable")
        loads_strict_object(content.decode())
        request = EnrollmentRequest.model_validate_json(content)
        if (
            content != request.model_dump_json().encode()
            or request.project_id != runtime.config.project_id
            or request.repository_id != "github.com/" + discover_github_repository(runtime.root)
        ):
            raise ValueError("team enrollment unavailable")
        return request
    finally:
        target.close()


def save_enrollment_request(runtime: Runtime, **fields: object) -> EnrollmentRequest:
    request = EnrollmentRequest.model_validate({"session_id": secrets.token_hex(32), **fields})
    target = runtime.workspace_directory.file(_SESSION_FILE)
    try:
        with same_path_lock(target):
            if target.exists():
                previous = load_enrollment_request(runtime)
                if previous is not None and previous.model_dump(
                    exclude={"session_id", "created_invite"}
                ) == (request.model_dump(exclude={"session_id", "created_invite"})):
                    return previous
                if (
                    previous is not None
                    and previous.created_invite is not None
                    and request.action == "approve-join"
                    and request.response is not None
                    and request.response.invite_id == previous.created_invite.invite_id
                    and enrollment_status(runtime).get("can_cancel") is True
                ):
                    target.atomic_write(
                        request.model_dump_json().encode(), reject_target_races=True
                    )
                    os.chmod(target.name, 0o600, dir_fd=target.parent_fd, follow_symlinks=False)
                    return request
                raise ValueError("team enrollment unavailable")
            content = request.model_dump_json().encode()
            if len(content) > _MAX_SESSION_BYTES:
                raise ValueError("team enrollment unavailable")
            descriptor = os.open(
                target.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=target.parent_fd,
            )
            try:
                pending = memoryview(content)
                while pending:
                    count = os.write(descriptor, pending)
                    if count <= 0:
                        raise ValueError("team enrollment unavailable")
                    pending = pending[count:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(target.parent_fd)
            return request
    finally:
        target.close()


def enrollment_status(runtime: Runtime) -> dict[str, object]:
    from intent_engineering.team_state.local_trust import LocalTrustConfigV2, LocalTrustProvider
    from intent_engineering.team_state.setup import _draft, _enrollment_receipt

    request = load_enrollment_request(runtime)
    if request is None:
        return {"state": "unconfigured"}
    draft = _draft(runtime)
    receipt = _enrollment_receipt(runtime)
    trust_provider = LocalTrustProvider(runtime.root)
    pending = trust_provider.load_pending_join()
    active = trust_provider.load_versioned()
    attempted = draft is not None and draft.external_write_attempted
    state = "review_required"
    if request.created_invite is not None:
        state = "invitation-ready"
    if pending is not None and request.action == "join":
        state = pending.phase
    elif isinstance(active, LocalTrustConfigV2) and request.action == "join":
        state = (
            "publication_pending"
            if draft is not None and draft.pull_request_number is not None
            else "publication_recovery_required"
            if attempted
            else "publication_draft"
            if draft is not None
            else "member-active"
        )
    elif receipt is not None:
        state = receipt.phase
    elif attempted:
        state = "publication_recovery_required"
    result: dict[str, object] = {
        "session_id": request.session_id,
        "action": request.action,
        "state": state,
        "project_id": request.project_id,
        "repository_id": request.repository_id,
        "can_cancel": (
            not attempted
            and receipt is None
            and pending is None
            and not (isinstance(active, LocalTrustConfigV2) and request.action == "join")
        ),
    }
    if request.identity is not None:
        result["identity"] = request.identity.model_dump(mode="json")
    if request.invite is not None:
        result["invite"] = request.invite.model_dump(mode="json")
    if request.response is not None:
        response = request.response
        result["identity"] = {
            "account_id": str(response.github_account_id),
            "login": response.github_login,
        }
        result["recipient_key_id"] = response.recipient_key_id
        result["signature_id"] = response.signature_id
    approval = load_approval_request(runtime, request)
    if approval is not None:
        result["preview"] = {
            "approval_request_digest": approval.digest(),
            "authority_before_digest": approval.preview.authority_before_digest,
            "authority_after_digest": approval.preview.authority_after_digest,
            "authority_after": approval.preview.authority_after.model_dump(mode="json"),
            "certificate": approval.preview.certificate.model_dump(mode="json"),
            "base_state_commit": approval.preview.base_state_commit,
            "base_bundle_digest": approval.preview.base_bundle_digest,
            "default_branch_commit": approval.preview.default_branch_commit,
            "tooling_digest": approval.preview.tooling_digest,
            "github_preflight": approval.github_preflight.model_dump(mode="json"),
            "manifest": approval.publication_plan.manifest.model_dump(mode="json"),
        }
    return result


def cancel_enrollment(runtime: Runtime, session_id: str) -> dict[str, object]:
    target = runtime.workspace_directory.file(_SESSION_FILE)
    try:
        with same_path_lock(target):
            state = enrollment_status(runtime)
            if state.get("session_id") != session_id or state.get("can_cancel") is not True:
                raise ValueError("team enrollment unavailable")
            target.unlink()
            return {"state": "cancelled"}
    finally:
        target.close()


def load_approval_request(
    runtime: Runtime, request: EnrollmentRequest
) -> EnrollmentApprovalRequest | None:
    from intent_engineering.team_state.models import MAX_BUNDLE_BYTES
    from intent_engineering.team_state.setup import EnrollmentApprovalRequest

    target = runtime.workspace_directory.file("team-enrollment-approval.json")
    try:
        content = target.read_optional_nonblocking(max_bytes=MAX_BUNDLE_BYTES * 2 + 256 * 1024)
        if content is None:
            return None
        loads_strict_object(content.decode())
        approval = EnrollmentApprovalRequest.model_validate_json(content)
        if content != approval.canonical_bytes() or approval.preview.response != request.response:
            raise ValueError("team enrollment unavailable")
        return approval
    finally:
        target.close()


def save_approval_request(
    runtime: Runtime, request: EnrollmentRequest, approval: EnrollmentApprovalRequest
) -> None:
    from intent_engineering.team_state.setup import EnrollmentApprovalRequest

    if (
        type(approval) is not EnrollmentApprovalRequest
        or approval.preview.response != request.response
    ):
        raise ValueError("team enrollment unavailable")
    target = runtime.workspace_directory.file("team-enrollment-approval.json")
    try:
        with same_path_lock(target):
            previous = load_approval_request(runtime, request)
            if previous is not None and previous != approval:
                raise ValueError("team enrollment unavailable")
            if previous is None:
                target.atomic_write(approval.canonical_bytes(), reject_target_races=True)
    finally:
        target.close()


def local_identity_proof() -> bytes:
    from intent_engineering.capture.github.auth import GitHubCredentials

    return GitHubCredentials.resolve(os.environ).token.get_secret_value().encode("ascii")


class _GitHubIdentityVerifier:
    def _read(self, proof: bytes, path: str) -> GitHubIdentity:
        import httpx

        content = bytearray()
        try:
            if not proof or len(proof) > 16 * 1024:
                raise ValueError("team enrollment unavailable")
            with (
                httpx.Client(timeout=20, follow_redirects=False) as client,
                client.stream(
                    "GET",
                    "https://api.github.com" + path,
                    headers={
                        "Authorization": "Bearer " + proof.decode("ascii"),
                        "Accept": "application/vnd.github+json",
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    raise ValueError("team enrollment unavailable")
                for part in response.iter_bytes():
                    content.extend(part)
                    if len(content) > 64 * 1024:
                        raise ValueError("team enrollment unavailable")
            body = loads_strict_object(bytes(content).decode())
            if type(body.get("id")) is not int or type(body.get("login")) is not str:
                raise ValueError("team enrollment unavailable")
            return GitHubIdentity(account_id=str(body["id"]), login=cast(str, body["login"]))
        finally:
            proof = b""
            content.clear()

    def verify(self, proof: bytes) -> GitHubIdentity:
        return self._read(proof, "/user")

    def lookup(self, account_id: int) -> GitHubIdentity:
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("team enrollment unavailable")
        return self._read(local_identity_proof(), f"/user/{account_id}")


def identity_verifier() -> _GitHubIdentityVerifier:
    return _GitHubIdentityVerifier()


def device_store(binding: DeviceEnrollmentBinding) -> DeviceKeyStore:
    from intent_engineering.team_state.keys import KeyringDeviceKeyStore

    return KeyringDeviceKeyStore(binding)


@dataclass(frozen=True)
class SponsorContext:
    enrollment: TeamEnrollmentService | None
    current: VerifiedRemoteStateV2
    parent: VerifiedReleaseV2
    snapshot: CanonicalStateSnapshot
    bridge: GitHubSetupBridge
    publication: PublicationService
    trust: LocalTrustConfigV2
    store: DeviceKeyStore
    protection: GitHubProtectionPreview


class MembershipSession:
    """Compose server-owned ceremonies; browsers hold only an opaque session identifier."""

    def __init__(self, service: ControlPlaneService) -> None:
        from intent_engineering.control_plane.webauthn_service import WebAuthnService
        from intent_engineering.control_plane.webauthn_store import (
            WebAuthnChallengeStore,
            WebAuthnCredentialStore,
        )
        from intent_engineering.storage.transaction import LocalTransactionCoordinator
        from intent_engineering.team_state.enrollment import _decision_repository_id

        self.service = service
        self.runtime = service._runtime
        request = load_enrollment_request(self.runtime)
        if request is None:
            raise ValueError("team enrollment unavailable")
        self.request = request
        self.resources = ExitStack()
        targets = {}
        for key, name in (
            ("webauthn_credentials", "team-webauthn-credentials.jsonl"),
            ("webauthn_challenges", "team-webauthn-challenges.jsonl"),
        ):
            target = self.runtime.workspace_directory.file(name)
            self.resources.callback(target.close)
            targets[key] = target
        journal = self.runtime.workspace_directory.file("team-webauthn-transaction.json")
        self.resources.callback(journal.close)
        transactions = LocalTransactionCoordinator(journal, targets)
        self.resources.callback(transactions.close)
        credentials = WebAuthnCredentialStore(
            targets["webauthn_credentials"], transactions=transactions
        )
        challenges = WebAuthnChallengeStore(
            targets["webauthn_challenges"], transactions=transactions
        )
        self.resources.callback(credentials.close)
        self.resources.callback(challenges.close)
        self.webauthn = WebAuthnService(
            project_id=self.request.project_id,
            repository_id=_decision_repository_id(self.request.repository_id),
            expected_origin=service._origin,
            credentials=credentials,
            challenges=challenges,
            transactions=transactions,
            verifier=service._webauthn._verifier,
        )
        self.pending_registration: str | None = None
        self.pending_payload: HumanDecisionPayload | None = None
        self.approval_webauthn = self.webauthn
        self.proof = b""
        self.identity: GitHubIdentity | None = None
        self.enrollment: TeamEnrollmentService | None = None
        self.pre_counter: int | None = None
        self.guard = anyio.Lock()

    def close(self) -> None:
        self.proof = b""
        self.pending_payload = None
        self.resources.close()

    async def _sponsor(self, *, require_sponsor: bool = True) -> SponsorContext:
        from intent_engineering.team_state.authority import derive_recipient_key_id
        from intent_engineering.team_state.github import GitHubTeamStateClient
        from intent_engineering.team_state.keys import (
            DeviceBundleDecryptor,
            KeyringRecipientKeyStore,
            MigrationRecipientDecryptor,
        )
        from intent_engineering.team_state.local_trust import (
            LocalTrustConfig,
            LocalTrustConfigV2,
            LocalTrustProvider,
            activate_installed_migration,
            migrated_device_store,
            trust_from_verified_migration,
        )
        from intent_engineering.team_state.publication import (
            PublicationService,
            authority_from_verified_state,
        )
        from intent_engineering.team_state.restore import (
            TrustedSigningKey,
            _read_local_marker,
            _refresh_state_ref,
            load_accepted_device_release,
            load_installed_device_migration,
        )
        from intent_engineering.team_state.setup import (
            GitHubSetupBridge,
            GitHubSetupRequest,
            _digest,
            _enrollment_context,
            github_api,
            load_setup_request,
        )
        from intent_engineering.team_state.signing import (
            KeyringTeamRootKeyStore,
            RootEnrollmentBinding,
            SigningKeyStore,
        )

        trust = LocalTrustProvider(self.runtime.root).load_versioned()
        marker = _read_local_marker(self.runtime.workspace_directory)
        if marker is None:
            raise ValueError("team enrollment unavailable")
        if isinstance(trust, LocalTrustConfig):
            legacy_trust = trust
            legacy_binding = legacy_trust.enrollment_binding()
            recipient_id = derive_recipient_key_id(
                trust.project_id, trust.repository_id, trust.recipient.public_key
            )
            migration_decryptor = MigrationRecipientDecryptor(
                legacy_binding,
                recipient_id,
                recipient_store=KeyringRecipientKeyStore(legacy_binding),
            )
            migration_reader = _refresh_state_ref(trust.repository_id)
            try:
                migrated = load_installed_device_migration(
                    migration_reader,
                    str(marker["ref_commit"]),
                    legacy_trust,
                    migration_decryptor,
                    self.service._clock(),
                )
                activate_installed_migration(
                    self.runtime.root,
                    legacy_trust,
                    migrated,
                    merged_state_commit=migration_reader.commit(),
                )
                trust = trust_from_verified_migration(legacy_trust, migrated)
            finally:
                migration_reader.close()
        if not isinstance(trust, LocalTrustConfigV2):
            raise TypeError("team enrollment unavailable")
        store: DeviceKeyStore
        decryptor: DeviceBundleDecryptor
        if trust.migration_recipient_binding is not None:
            legacy = trust.migration_recipient_binding
            decryptor = MigrationRecipientDecryptor(
                legacy, trust.recipient_key_id, recipient_store=KeyringRecipientKeyStore(legacy)
            )
        elif trust.device_binding is not None:
            store = device_store(trust.device_binding)
            decryptor = store
        else:
            raise ValueError("team enrollment unavailable")
        reader = _refresh_state_ref(trust.repository_id)
        try:
            legacy_keys: tuple[TrustedSigningKey, ...] = ()
            if trust.migration_recipient_binding is not None:
                legacy_keys = tuple(
                    TrustedSigningKey(k, v)
                    for k, v in sorted(
                        SigningKeyStore(
                            trust.project_id,
                            trust.repository_id,
                            trust.migration_recipient_binding.actor,
                        )
                        .public_keys()
                        .items()
                    )
                )
            parent = load_accepted_device_release(
                reader,
                str(marker["ref_commit"]),
                trust,
                decryptor,
                legacy_keys,
                self.service._clock(),
            )
        finally:
            reader.close()
        authority = authority_from_verified_state(parent, trust)
        if trust.migration_recipient_binding is not None:
            store = migrated_device_store(trust, parent)
        member = next(m for m in parent.authority.members if m.member_id == trust.member_id)
        if require_sponsor and member.role != "sponsor":
            raise ValueError("team enrollment unavailable")
        if load_setup_request(self.runtime) is None and _enrollment_context(self.runtime) is None:
            from intent_engineering.cli.team import build_github_enable_preview

            preview = build_github_enable_preview(
                self.runtime.root,
                trust.repository_id.removeprefix("github.com/"),
                ci_recipient=parent.authority.ci_recipient,
            )
            bridge = GitHubSetupBridge(
                self.service,
                request=GitHubSetupRequest(preview=preview),
            )
        else:
            bridge = GitHubSetupBridge(self.service)
        api = github_api()
        try:
            client = GitHubTeamStateClient(
                api,
                expected_account_id=str(member.github_account_id),
                expected_login=member.github_login,
            )
            status = await client.inspect(bridge.repository)
            if status.default_branch_commit is None:
                raise ValueError("team enrollment unavailable")
            tooling = await client.verify_default_branch_tooling(
                codeowners=bridge.request.preview.codeowners_suggestion.encode(),
                workflow=bridge.request.preview.workflow_suggestion.encode(),
                check_workflow=bridge.request.preview.check_workflow_suggestion.encode(),
                runner_id=parent.authority.ci_recipient.runner_id,
            )
            current = VerifiedRemoteStateV2(
                authority=parent.authority,
                state_commit=parent.commit,
                bundle_digest=parent.manifest.bundle_digest,
                default_branch=status.default_branch,
                default_branch_commit=status.default_branch_commit,
                tooling_digest=_digest(tooling.model_dump(mode="json")),
            )
            _, _, protection = await bridge._member_preflight(
                api,
                current,
                trust.device_certificate_id,
                require_sponsor=require_sponsor,
            )
        finally:
            await api.aclose()
        enrollment: TeamEnrollmentService | None = None
        if require_sponsor:
            root = trust.root
            root_store = KeyringTeamRootKeyStore(
                RootEnrollmentBinding(
                    project_id=root.project_id,
                    repository_id=root.repository_id,
                    authority_epoch=root.authority_epoch,
                    created_at=root.created_at,
                    predecessor_root_key_id=root.predecessor_root_key_id,
                )
            )
            if root_store.root_trust() != root:
                raise ValueError("team enrollment unavailable")
            from intent_engineering.team_state.enrollment import (
                FileEnrollmentReplayStateStore,
                KeyringEnrollmentChallengeKeyStore,
            )

            verifier = identity_verifier()
            enrollment = TeamEnrollmentService(
                device_store=store,
                root_store=root_store,
                sponsor_certificate_id=trust.device_certificate_id,
                webauthn_verifier=self.service._webauthn._verifier,
                expected_origin=self.service._origin,
                expected_rp_id="localhost",
                github_identity_verifier=verifier,
                identity_lookup=verifier,
                replay_store=FileEnrollmentReplayStateStore(
                    self.runtime.root / ".intent" / "team-invites",
                    trust.project_id,
                    trust.repository_id,
                ),
                challenge_store=KeyringEnrollmentChallengeKeyStore(
                    trust.project_id, trust.repository_id
                ),
            )
        publication = PublicationService(
            self.runtime,
            repository_id=trust.repository_id,
            decision_repository_id=(
                self.webauthn._repository_id
                if self.request.action == "join"
                else self.service._webauthn._repository_id
            ),
            authority=lambda: authority,
            publisher=bridge.transport,
            device_signer=store,
        )
        snapshot, _ = publication._capture()
        return SponsorContext(
            enrollment,
            current,
            parent,
            snapshot,
            bridge,
            publication,
            trust,
            store,
            protection,
        )

    def _join(self) -> tuple[TeamInviteV2, GitHubIdentity, DevicePublicMaterial]:
        from intent_engineering.team_state.enrollment import TeamEnrollmentService
        from intent_engineering.team_state.keys import DeviceEnrollmentBinding

        invite = self.request.invite
        if invite is None:
            raise ValueError("team enrollment unavailable")
        proof = local_identity_proof()
        identity = identity_verifier().verify(proof)
        if (
            identity.account_id != str(invite.intended_github_account_id)
            or identity.login != invite.intended_github_login
        ):
            raise ValueError("team enrollment unavailable")
        if self.identity is not None and (identity != self.identity or proof != self.proof):
            raise ValueError("team enrollment changed")
        binding = DeviceEnrollmentBinding(
            project_id=invite.project_id,
            repository_id=invite.repository_id,
            actor="github:" + identity.account_id,
            github_account_id=int(identity.account_id),
            github_login=identity.login,
            device_id="device:" + self.request.session_id[:32],
        )
        self.enrollment = TeamEnrollmentService(device_store=device_store(binding))
        material = self.enrollment.device_public_material(invite=invite, local_identity=identity)
        self.identity = identity
        self.proof = proof
        return invite, identity, material

    async def action(
        self, action: str, session_id: str, *, response: bytes = b""
    ) -> dict[str, object]:
        from intent_engineering.team_state.enrollment import _prepared_failure

        try:
            async with self.guard:
                if (
                    load_enrollment_request(self.runtime) != self.request
                    or session_id != self.request.session_id
                ):
                    raise ValueError("team enrollment unavailable")
                if action == "cancel":
                    result = cancel_enrollment(self.runtime, session_id)
                    self.proof = b""
                    self.pending_payload = None
                    return result
                return await self._action(action, response)
        except BaseException as error:  # noqa: BLE001 - fixed secret-bearing ceremony boundary
            self.proof = b""
            self.pending_payload = None
            failure = _prepared_failure(error, "team enrollment unavailable")
            response = b""
            del error
            raise failure.with_traceback(None) from None
        finally:
            response = b""

    async def _action(self, action: str, response: bytes) -> dict[str, object]:
        from intent_engineering.cli.team_enrollment import write_public_file
        from intent_engineering.team_state.enrollment import build_join_decision_payload
        from intent_engineering.team_state.local_trust import (
            LocalTrustConfigV2,
            LocalTrustProvider,
            PendingJoinTrustV2,
        )

        now = self.service._clock().replace(microsecond=0)
        if self.request.action == "invite":
            if (
                action != "create-invite"
                or self.request.identity is None
                or self.request.output is None
            ):
                raise ValueError("team enrollment unavailable")
            invite = self.request.created_invite
            if invite is None:
                context = await self._sponsor()
                enrollment = context.enrollment
                if enrollment is None:
                    raise ValueError("team enrollment unavailable")
                invite = enrollment.create_invite(
                    state=context.current, intended_identity=self.request.identity, now=now
                )
                target = self.runtime.workspace_directory.file(_SESSION_FILE)
                try:
                    with same_path_lock(target):
                        if load_enrollment_request(self.runtime) != self.request:
                            raise ValueError("team enrollment unavailable")
                        self.request = self.request.model_copy(update={"created_invite": invite})
                        target.atomic_write(
                            self.request.model_dump_json().encode(), reject_target_races=True
                        )
                        os.chmod(target.name, 0o600, dir_fd=target.parent_fd, follow_symlinks=False)
                finally:
                    target.close()
            assert self.request.output is not None
            output = Path(self.request.output)
            from intent_engineering.cli.team_enrollment import read_invite

            if output.exists():
                if read_invite(output) != invite:
                    raise ValueError("team enrollment unavailable")
            else:
                write_public_file(output, invite)
            return enrollment_status(self.runtime)
        if self.request.action == "approve-join":
            from intent_engineering.control_plane.models import credential_matches_digest
            from intent_engineering.team_state.enrollment import build_sponsor_decision_payload
            from intent_engineering.team_state.setup import _enrollment_receipt

            context = await self._sponsor()
            enrollment = context.enrollment
            if enrollment is None:
                raise ValueError("team enrollment unavailable")
            approval = load_approval_request(self.runtime, self.request)
            receipt = _enrollment_receipt(self.runtime)
            if action in {"reconcile", "restart"}:
                if approval is None or receipt is None:
                    raise ValueError("team enrollment unavailable")
                result = await context.bridge.reconcile_member_approval(
                    request=approval, current=context.current, restart_closed=action == "restart"
                )
                if action == "restart" and result.state == "bootstrap_required":
                    return cancel_enrollment(self.runtime, self.request.session_id)
                return enrollment_status(self.runtime)
            if receipt is not None or self.request.response is None:
                raise ValueError("team enrollment unavailable")
            if action == "preview":
                if approval is None:
                    invite = enrollment._replay_store.get_invite(self.request.response.invite_id)
                    if invite is None:
                        raise ValueError("team enrollment unavailable")
                    approval = await context.bridge.preview_member_approval(
                        enrollment=enrollment,
                        invite=invite,
                        response=self.request.response,
                        current=context.current,
                        parent=context.parent,
                        snapshot=context.snapshot,
                        now=now,
                    )
                    save_approval_request(self.runtime, self.request, approval)
                if not enrollment._state_matches_invite(context.current, approval.preview.invite):
                    raise ValueError("team enrollment changed")
                sponsor = next(
                    m
                    for m in context.current.authority.members
                    if m.member_id == approval.preview.invite.sponsor_member_id
                )
                digest = (
                    approval.preview.invite.sponsor_certificate.claims.webauthn_credential_digest
                )
                matches = [
                    (webauthn, credential)
                    for webauthn in (self.service._webauthn, self.webauthn)
                    for credential in webauthn.registered_credentials(
                        sponsor.actor, self.service._origin
                    )
                    if credential_matches_digest(credential, digest)
                ]
                if len(matches) != 1:
                    raise ValueError("team enrollment unavailable")
                self.approval_webauthn, credential = matches[0]
                self.pre_counter = credential.sign_count
                self.pending_payload = build_sponsor_decision_payload(
                    preview=approval.preview,
                    credential=credential,
                    challenge=secrets.token_bytes(32),
                    now=now,
                    approval_request_digest=approval.digest(),
                )
                return {**enrollment_status(self.runtime), "state": "preview_ready"}
            if approval is None or self.pending_payload is None or self.pre_counter is None:
                raise ValueError("team enrollment unavailable")
            if action == "options":
                return cast(
                    dict[str, object],
                    json.loads(
                        self.approval_webauthn.authentication_options(
                            self.pending_payload, self.service._origin, now
                        )
                    ),
                )
            if action == "verify":
                decision = self.approval_webauthn.verify(
                    response, self.pending_payload, self.service._origin, now
                )
                await context.bridge.approve_member(
                    request=approval,
                    enrollment=enrollment,
                    sponsor_decision=decision,
                    sponsor_pre_assertion_sign_count=self.pre_counter,
                    sponsor_assertion=response,
                    current=context.current,
                    parent=context.parent,
                    snapshot=context.snapshot,
                    now=now,
                )
                self.pending_payload = None
                return enrollment_status(self.runtime)
            raise ValueError("team enrollment unavailable")
        trust_provider = LocalTrustProvider(self.runtime.root)
        existing_pending = trust_provider.load_pending_join()
        active = trust_provider.load_versioned()
        if (
            self.request.action == "join"
            and existing_pending is None
            and isinstance(active, LocalTrustConfigV2)
            and action.startswith("publish-")
        ):
            from intent_engineering.team_state.publication import (
                PreparedPublicationV2,
                PublicationPreviewV2,
            )
            from intent_engineering.team_state.setup import _draft

            context = await self._sponsor(require_sponsor=False)
            if action == "publish-preview":
                durable = _draft(self.runtime)
                if durable is None:
                    preview = context.publication.preview(now=now)
                    prepared_value: object = context.publication.pending_publication()
                    if (
                        type(preview) is not PublicationPreviewV2
                        or type(prepared_value) is not PreparedPublicationV2
                    ):
                        raise ValueError("team enrollment unavailable")
                    prepared = prepared_value
                    _draft(
                        self.runtime,
                        prepared=prepared,
                        anchor=context.current.state_commit,
                    )
                else:
                    prepared_value = durable.publication()
                    if (
                        type(prepared_value) is not PreparedPublicationV2
                        or durable.anchor != context.current.state_commit
                        or durable.external_write_attempted
                    ):
                        raise ValueError("team enrollment unavailable")
                    prepared = prepared_value
                    preview = context.publication.recover_device_preview(
                        prepared,
                        device_store=context.store,
                        now=now,
                    )
                self.pending_payload = preview.payload
                self.approval_webauthn = self.webauthn
                return {
                    **enrollment_status(self.runtime),
                    "state": "publication_preview",
                    "preview": {
                        "bundle_digest": preview.manifest.bundle_digest,
                        "parent_bundle_digest": preview.manifest.parent_bundle_digest,
                        "branch": preview.branch,
                        "recipient_key_ids": list(preview.recipient_key_ids),
                        "manifest": preview.manifest.model_dump(mode="json"),
                    },
                    "payload": preview.payload.model_dump(mode="json"),
                }
            if self.pending_payload is None:
                raise ValueError("team enrollment unavailable")
            if action == "publish-options":
                return cast(
                    dict[str, object],
                    json.loads(
                        self.approval_webauthn.authentication_options(
                            self.pending_payload, self.service._origin, now
                        )
                    ),
                )
            if action == "publish-verify":
                decision = self.approval_webauthn.verify(
                    response, self.pending_payload, self.service._origin, now
                )

                def verify_local() -> None:
                    if (
                        LocalTrustProvider(self.runtime.root).load_versioned() != context.trust
                        or load_enrollment_request(self.runtime) != self.request
                    ):
                        raise ValueError("team enrollment changed")

                publication_result = await context.bridge.publish_member_state(
                    publication=context.publication,
                    decision=decision,
                    current=context.current,
                    certificate_id=context.trust.device_certificate_id,
                    protection=context.protection,
                    verify_local=verify_local,
                )
                self.pending_payload = None
                return publication_result
            raise ValueError("team enrollment unavailable")
        if existing_pending is not None:
            if (
                action != "reconcile"
                or existing_pending.invite != self.request.invite
                or self.request.output is None
            ):
                raise ValueError("team enrollment unavailable")
            from intent_engineering.cli.team_enrollment import read_response

            output = Path(self.request.output)
            if output.exists():
                if read_response(output) != existing_pending.response:
                    raise ValueError("team enrollment unavailable")
            else:
                write_public_file(output, existing_pending.response)
            return enrollment_status(self.runtime)
        invite, identity, material = self._join()
        actor = "github:" + identity.account_id
        if action == "register-options":
            options, challenge_id = self.webauthn.bound_registration_options(
                actor,
                self.service._origin,
                now,
                "sha256:" + hashlib.sha256(invite.model_dump_json().encode()).hexdigest(),
            )
            self.pending_registration = challenge_id
            return cast(dict[str, object], json.loads(options))
        if action == "register-verify":
            if self.pending_registration is None:
                raise ValueError("team enrollment unavailable")
            self.webauthn.register(
                response,
                actor,
                self.service._origin,
                now,
                github_account_id=identity.account_id,
                github_login=identity.login,
                binding_digest="sha256:"
                + hashlib.sha256(invite.model_dump_json().encode()).hexdigest(),
                expected_challenge_id=self.pending_registration,
            )
            self.pending_registration = None
            return {"state": "review_required", "session_id": self.request.session_id}
        if action == "preview":
            credentials = self.webauthn._current_credentials(actor)
            if len(credentials) != 1:
                raise ValueError("team enrollment unavailable")
            credential = credentials[0]
            self.pre_counter = credential.sign_count
            self.pending_payload = build_join_decision_payload(
                invite=invite,
                identity=identity,
                material=material,
                credential=credential,
                identity_proof=self.proof,
                pre_assertion_sign_count=self.pre_counter,
                challenge=secrets.token_bytes(32),
                now=now,
            )
            return {
                **enrollment_status(self.runtime),
                "state": "preview_ready",
                "identity": identity.model_dump(mode="json"),
                "recipient_key_id": material.recipient_key_id,
                "signature_id": material.signature_id,
                "root_key_id": invite.root.root_key_id,
                "role": "member",
                "preview_digest": self.pending_payload.subject_digest,
            }
        payload = self.pending_payload
        if payload is None or self.pre_counter is None:
            raise ValueError("team enrollment unavailable")
        if action == "options":
            return cast(
                dict[str, object],
                json.loads(
                    self.webauthn.authentication_options(payload, self.service._origin, now)
                ),
            )
        if action == "verify":
            assert self.enrollment is not None
            decision = self.webauthn.verify(response, payload, self.service._origin, now)
            joined = self.enrollment.create_join_response(
                invite=invite,
                local_identity=identity,
                decision=decision,
                identity_proof=self.proof,
                pre_assertion_sign_count=self.pre_counter,
                webauthn_assertion=response,
                now=now,
            )
            pending = PendingJoinTrustV2(
                phase="response-ready",
                invite=invite,
                response=joined,
                local_recipient_key_id=material.recipient_key_id,
                local_signature_id=material.signature_id,
                expected_root_key_id=invite.root.root_key_id,
                expected_authority_before_digest=invite.authority_digest,
                external_write_attempted=False,
            )
            LocalTrustProvider(self.runtime.root).save_pending_join(pending)
            assert self.request.output is not None
            write_public_file(Path(self.request.output), joined)
            self.proof = b""
            self.pending_payload = None
            return enrollment_status(self.runtime)
        raise ValueError("team enrollment unavailable")
