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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import anyio
from pydantic import ConfigDict, Field, model_validator

from intent_engineering.cli.runtime import Runtime
from intent_engineering.control_plane.models import CredentialRecord, HumanDecisionPayload
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureFile
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
    from intent_engineering.team_state.github import (
        GitHubDefaultBranchTooling,
        GitHubProtectionPreview,
        GitHubTeamStateStatus,
    )
    from intent_engineering.team_state.local_trust import LocalTrustConfig, LocalTrustConfigV2
    from intent_engineering.team_state.models import CanonicalStateSnapshot, CiRecipientRecord
    from intent_engineering.team_state.publication import PublicationService, V1MigrationPreview
    from intent_engineering.team_state.restore import VerifiedReleaseV2, VerifiedV1Release
    from intent_engineering.team_state.setup import EnrollmentApprovalRequest, GitHubSetupBridge

_SESSION_FILE = "team-enrollment-session.json"
_APPROVAL_FILE = "team-enrollment-approval.json"
_SESSION_JOURNAL_FILE = "team-enrollment-session-transaction.json"
_PUBLICATION_FILE = "team-publication.json"
_RECEIPT_FILE = "team-enrollment-receipt.json"
_DISCARDED_PUBLICATION_STATE = b'{"discarded":true}'
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


def _write_owner_session(target: SecureFile, content: bytes) -> None:
    if len(content) > _MAX_SESSION_BYTES:
        raise ValueError("team enrollment unavailable")
    target.atomic_write(content, reject_target_races=True)
    os.chmod(target.name, 0o600, dir_fd=target.parent_fd, follow_symlinks=False)
    os.fsync(target.parent_fd)


def _recover_enrollment_session_state(runtime: Runtime) -> None:
    from intent_engineering.storage.transaction import LocalTransactionCoordinator

    session = runtime.workspace_directory.file(_SESSION_FILE)
    approval = runtime.workspace_directory.file(_APPROVAL_FILE)
    draft = runtime.workspace_directory.file(_PUBLICATION_FILE)
    receipt = runtime.workspace_directory.file(_RECEIPT_FILE)
    journal = runtime.workspace_directory.file(_SESSION_JOURNAL_FILE)
    coordinator: LocalTransactionCoordinator | None = None
    try:
        coordinator = LocalTransactionCoordinator(
            journal,
            {
                "session": session,
                "approval": approval,
                "draft": draft,
                "receipt": receipt,
            },
            legacy_target_sets=(frozenset({"session", "approval"}),),
            target_writers={"session": _write_owner_session},
        )
        coordinator.recover()
    finally:
        if coordinator is not None:
            coordinator.close()
        journal.close()
        receipt.close()
        draft.close()
        approval.close()
        session.close()


def _read_enrollment_request(runtime: Runtime, target: SecureFile) -> EnrollmentRequest | None:
    from intent_engineering.cli.team import discover_github_repository

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


def load_enrollment_request(runtime: Runtime) -> EnrollmentRequest | None:
    _recover_enrollment_session_state(runtime)
    target = runtime.workspace_directory.file(_SESSION_FILE)
    try:
        return _read_enrollment_request(runtime, target)
    finally:
        target.close()


def save_enrollment_request(runtime: Runtime, **fields: object) -> EnrollmentRequest:
    from intent_engineering.storage.transaction import LocalTransactionCoordinator

    request = EnrollmentRequest.model_validate({"session_id": secrets.token_hex(32), **fields})
    content = request.model_dump_json().encode()
    if len(content) > _MAX_SESSION_BYTES:
        raise ValueError("team enrollment unavailable")
    _recover_enrollment_session_state(runtime)
    cancellable = _enrollment_can_cancel(runtime, request)
    session = runtime.workspace_directory.file(_SESSION_FILE)
    approval = runtime.workspace_directory.file(_APPROVAL_FILE)
    draft = runtime.workspace_directory.file(_PUBLICATION_FILE)
    receipt = runtime.workspace_directory.file(_RECEIPT_FILE)
    journal = runtime.workspace_directory.file(_SESSION_JOURNAL_FILE)
    coordinator: LocalTransactionCoordinator | None = None
    try:
        coordinator = LocalTransactionCoordinator(
            journal,
            {
                "session": session,
                "approval": approval,
                "draft": draft,
                "receipt": receipt,
            },
            legacy_target_sets=(frozenset({"session", "approval"}),),
            target_writers={"session": _write_owner_session},
        )
        with coordinator.transaction(rollback_base_exceptions=True) as transaction:
            previous_content = transaction.read_optional_bounded(
                "session", max_bytes=_MAX_SESSION_BYTES
            )
            previous = _read_enrollment_request(runtime, session)
            if (previous is None) != (previous_content is None):
                raise ValueError("team enrollment unavailable")
            if previous is not None:
                if previous is not None and previous.model_dump(
                    exclude={"session_id", "created_invite"}
                ) == (request.model_dump(exclude={"session_id", "created_invite"})):
                    return previous
                if (
                    previous.created_invite is not None
                    and request.action == "approve-join"
                    and request.response is not None
                    and request.response.invite_id == previous.created_invite.invite_id
                    and cancellable
                    and transaction.read_optional("draft") in {None, _DISCARDED_PUBLICATION_STATE}
                    and transaction.read_optional("receipt") in {None, _DISCARDED_PUBLICATION_STATE}
                    and transaction.read_optional("approval") is None
                ):
                    transaction.write("session", content)
                else:
                    raise ValueError("team enrollment unavailable")
            elif transaction.read_optional("approval") is not None:
                raise ValueError("team enrollment unavailable")
            else:
                transaction.write("session", content)
        return request
    finally:
        if coordinator is not None:
            coordinator.close()
        journal.close()
        receipt.close()
        draft.close()
        approval.close()
        session.close()


def _enrollment_can_cancel(runtime: Runtime, request: EnrollmentRequest) -> bool:
    from intent_engineering.team_state.local_trust import LocalTrustConfigV2, LocalTrustProvider
    from intent_engineering.team_state.setup import _draft, _enrollment_receipt

    draft = _draft(runtime)
    pending = LocalTrustProvider(runtime.root).load_pending_join()
    active = LocalTrustProvider(runtime.root).load_versioned()
    return (
        draft is None
        and _enrollment_receipt(runtime) is None
        and pending is None
        and not (isinstance(active, LocalTrustConfigV2) and request.action == "join")
    )


def enrollment_status(runtime: Runtime) -> dict[str, object]:
    from intent_engineering.team_state.local_trust import (
        LocalTrustConfig,
        LocalTrustConfigV2,
        LocalTrustProvider,
    )
    from intent_engineering.team_state.publication import PreparedV1Migration
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
    if isinstance(active, LocalTrustConfig) and request.action == "invite":
        migration = None if draft is None else draft.publication()
        if draft is not None and type(migration) is not PreparedV1Migration:
            raise ValueError("team migration changed")
        state = (
            "publication_pending"
            if draft is not None and draft.pull_request_number is not None
            else "publication_recovery_required"
            if attempted
            else "migration_required"
        )
    elif request.created_invite is not None:
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
    elif attempted and not (isinstance(active, LocalTrustConfig) and request.action == "invite"):
        state = "publication_recovery_required"
    result: dict[str, object] = {
        "session_id": request.session_id,
        "action": request.action,
        "state": state,
        "project_id": request.project_id,
        "repository_id": request.repository_id,
        "can_cancel": (_enrollment_can_cancel(runtime, request)),
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
    request = load_enrollment_request(runtime)
    state = enrollment_status(runtime)
    if request is None or request.session_id != session_id or state.get("can_cancel") is not True:
        raise ValueError("team enrollment unavailable")
    _retire_enrollment_request(runtime, request)
    return {"state": "cancelled"}


def _read_approval_content(
    content: bytes | None, request: EnrollmentRequest
) -> EnrollmentApprovalRequest | None:
    from intent_engineering.team_state.models import MAX_BUNDLE_BYTES
    from intent_engineering.team_state.setup import EnrollmentApprovalRequest

    if content is None:
        return None
    if len(content) > MAX_BUNDLE_BYTES * 2 + 256 * 1024:
        raise ValueError("team enrollment unavailable")
    loads_strict_object(content.decode())
    approval = EnrollmentApprovalRequest.model_validate_json(content)
    if content != approval.canonical_bytes() or approval.preview.response != request.response:
        raise ValueError("team enrollment unavailable")
    return approval


def _retire_enrollment_request(runtime: Runtime, request: EnrollmentRequest) -> None:
    """Atomically remove one exact session and its exact matching approval metadata."""
    from intent_engineering.storage.transaction import LocalTransactionCoordinator
    from intent_engineering.team_state.models import MAX_BUNDLE_BYTES

    session = runtime.workspace_directory.file(_SESSION_FILE)
    approval = runtime.workspace_directory.file(_APPROVAL_FILE)
    draft = runtime.workspace_directory.file(_PUBLICATION_FILE)
    receipt = runtime.workspace_directory.file(_RECEIPT_FILE)
    journal = runtime.workspace_directory.file(_SESSION_JOURNAL_FILE)
    coordinator: LocalTransactionCoordinator | None = None
    try:
        coordinator = LocalTransactionCoordinator(
            journal,
            {
                "session": session,
                "approval": approval,
                "draft": draft,
                "receipt": receipt,
            },
            legacy_target_sets=(frozenset({"session", "approval"}),),
            target_writers={"session": _write_owner_session},
        )
        with coordinator.transaction(rollback_base_exceptions=True) as transaction:
            session_content = transaction.read_optional_bounded(
                "session", max_bytes=_MAX_SESSION_BYTES
            )
            if session_content != request.model_dump_json().encode():
                raise ValueError("team enrollment unavailable")
            approval_content = transaction.read_optional_bounded(
                "approval", max_bytes=MAX_BUNDLE_BYTES * 2 + 256 * 1024
            )
            _read_approval_content(approval_content, request)
            if transaction.read_optional("draft") not in {
                None,
                _DISCARDED_PUBLICATION_STATE,
            } or transaction.read_optional("receipt") not in {
                None,
                _DISCARDED_PUBLICATION_STATE,
            }:
                raise ValueError("team enrollment unavailable")
            transaction.delete("session")
            if approval_content is not None:
                transaction.delete("approval")
    finally:
        if coordinator is not None:
            coordinator.close()
        journal.close()
        receipt.close()
        draft.close()
        approval.close()
        session.close()


def _retire_closed_enrollment_request(
    runtime: Runtime,
    request: EnrollmentRequest,
    approval_request: EnrollmentApprovalRequest,
    draft_value: object,
    receipt_value: object,
) -> None:
    """Atomically retire the exact closed enrollment workflow after remote proof."""
    from intent_engineering.storage.transaction import LocalTransactionCoordinator
    from intent_engineering.team_state.setup import EncryptedPublicationDraft, EnrollmentReceiptV2

    if (
        type(draft_value) is not EncryptedPublicationDraft
        or type(receipt_value) is not EnrollmentReceiptV2
        or receipt_value.phase != "closed"
    ):
        raise ValueError("team enrollment requires reconciliation")
    session = runtime.workspace_directory.file(_SESSION_FILE)
    approval = runtime.workspace_directory.file(_APPROVAL_FILE)
    draft = runtime.workspace_directory.file(_PUBLICATION_FILE)
    receipt = runtime.workspace_directory.file(_RECEIPT_FILE)
    journal = runtime.workspace_directory.file(_SESSION_JOURNAL_FILE)
    coordinator: LocalTransactionCoordinator | None = None
    try:
        coordinator = LocalTransactionCoordinator(
            journal,
            {
                "session": session,
                "approval": approval,
                "draft": draft,
                "receipt": receipt,
            },
            legacy_target_sets=(frozenset({"session", "approval"}),),
            target_writers={"session": _write_owner_session},
        )
        with coordinator.transaction(rollback_base_exceptions=True) as transaction:
            if (
                transaction.read_optional("session") != request.model_dump_json().encode()
                or transaction.read_optional("approval") != approval_request.canonical_bytes()
                or transaction.read_optional("draft") != draft_value.model_dump_json().encode()
                or transaction.read_optional("receipt") != receipt_value.canonical_bytes()
            ):
                raise ValueError("team enrollment requires reconciliation")
            transaction.write("draft", _DISCARDED_PUBLICATION_STATE)
            transaction.write("receipt", _DISCARDED_PUBLICATION_STATE)
            transaction.delete("approval")
            transaction.delete("session")
    finally:
        if coordinator is not None:
            coordinator.close()
        journal.close()
        receipt.close()
        draft.close()
        approval.close()
        session.close()


def load_approval_request(
    runtime: Runtime, request: EnrollmentRequest
) -> EnrollmentApprovalRequest | None:
    from intent_engineering.team_state.models import MAX_BUNDLE_BYTES

    _recover_enrollment_session_state(runtime)
    target = runtime.workspace_directory.file(_APPROVAL_FILE)
    try:
        content = target.read_optional_nonblocking(max_bytes=MAX_BUNDLE_BYTES * 2 + 256 * 1024)
        return _read_approval_content(content, request)
    finally:
        target.close()


def save_approval_request(
    runtime: Runtime, request: EnrollmentRequest, approval: EnrollmentApprovalRequest
) -> None:
    from intent_engineering.team_state.models import MAX_BUNDLE_BYTES
    from intent_engineering.team_state.setup import EnrollmentApprovalRequest

    if (
        type(approval) is not EnrollmentApprovalRequest
        or approval.preview.response != request.response
    ):
        raise ValueError("team enrollment unavailable")
    encoded = approval.canonical_bytes()
    if len(encoded) > MAX_BUNDLE_BYTES * 2 + 256 * 1024:
        raise ValueError("team enrollment unavailable")
    _recover_enrollment_session_state(runtime)
    target = runtime.workspace_directory.file(_APPROVAL_FILE)
    try:
        with same_path_lock(target):
            previous = _read_approval_content(
                target.read_optional_nonblocking(max_bytes=MAX_BUNDLE_BYTES * 2 + 256 * 1024),
                request,
            )
            if previous is not None and previous != approval:
                raise ValueError("team enrollment unavailable")
            if previous is None:
                target.atomic_write(encoded, reject_target_races=True)
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
    credential: CredentialRecord


@dataclass(frozen=True)
class MigrationContext:
    current: VerifiedV1Release
    commit: str
    trust: LocalTrustConfig
    ci_recipient: CiRecipientRecord
    bridge: GitHubSetupBridge
    status: GitHubTeamStateStatus
    protection: GitHubProtectionPreview
    tooling: GitHubDefaultBranchTooling
    credential: CredentialRecord


def _migration_preflight_digest(context: MigrationContext) -> str:
    content = json.dumps(
        {
            "protection": context.protection.model_dump(mode="json"),
            "setup_preview_digest": context.bridge.request.preview.preview_digest,
            "status": context.status.model_dump(mode="json"),
            "tooling": context.tooling.model_dump(mode="json"),
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return "sha256:" + hashlib.sha256(content).hexdigest()


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
        self.publication_context: SponsorContext | None = None
        self.migration_context: MigrationContext | None = None
        self.migration_preview: V1MigrationPreview | None = None
        self.pre_counter: int | None = None
        self.guard = anyio.Lock()

    def close(self) -> None:
        self.proof = b""
        self.pending_payload = None
        self.publication_context = None
        self.migration_context = None
        self.migration_preview = None
        self.resources.close()

    async def _migration(self) -> MigrationContext:
        """Reconstruct the exact protected v1 state and local sponsor authority."""
        from intent_engineering.team_state.github import GitHubTeamStateClient
        from intent_engineering.team_state.keys import (
            KeyringRecipientKeyStore,
            LegacyRecipientDecryptor,
        )
        from intent_engineering.team_state.local_trust import LocalTrustConfig, LocalTrustProvider
        from intent_engineering.team_state.restore import (
            _read_local_marker,
            _refresh_state_ref,
            load_accepted_v1_release,
        )
        from intent_engineering.team_state.setup import (
            GitHubSetupBridge,
            github_api,
        )

        trust = LocalTrustProvider(self.runtime.root).load_versioned()
        marker = _read_local_marker(self.runtime.workspace_directory)
        if not isinstance(trust, LocalTrustConfig) or marker is None:
            raise ValueError("team migration unavailable")
        binding = trust.enrollment_binding()
        decryptor = LegacyRecipientDecryptor(
            binding,
            recipient_store=KeyringRecipientKeyStore(binding),
        )
        reader = _refresh_state_ref(trust.repository_id)
        try:
            commit = reader.commit()
            if commit != marker["ref_commit"]:
                raise ValueError("team migration changed")
            current = load_accepted_v1_release(
                reader, commit, trust, decryptor, self.service._clock()
            )
        finally:
            reader.close()
        bridge = GitHubSetupBridge(self.service)
        ci_recipient = bridge.request.preview.ci_recipient
        if ci_recipient is None:
            raise ValueError("team migration unavailable")
        api = github_api()
        try:
            client = GitHubTeamStateClient(
                api,
                expected_account_id=trust.recipient.github_account_id,
                expected_login=trust.recipient.github_login,
            )
            status = await client.inspect(bridge.repository)
            if (
                status.repository_id != trust.repository_id
                or status.branch_commit != commit
                or not status.protection_compatible
            ):
                raise ValueError("team migration changed")
            tooling = await client.verify_default_branch_tooling(
                codeowners=bridge.request.preview.codeowners_suggestion.encode(),
                workflow=bridge.request.preview.workflow_suggestion.encode(),
                check_workflow=bridge.request.preview.check_workflow_suggestion.encode(),
                runner_id=ci_recipient.runner_id,
            )
            protection = client.protection_preview()
            if protection.requires_change or protection.branch_creation_required:
                raise ValueError("team migration changed")
        finally:
            await api.aclose()
        matches = {
            credential.canonical_bytes(): (webauthn, credential)
            for webauthn in (self.service._webauthn, self.webauthn)
            for actor in {trust.recipient.actor, self.runtime.config.local_actor}
            for credential in webauthn.registered_credentials(actor, self.service._origin)
            if not credential.local_only
            and credential.project_id == trust.project_id
            and credential.actor == trust.recipient.actor
            and credential.credential_id == trust.recipient.webauthn_credential_id
            and credential.public_key == trust.recipient.webauthn_credential_public_key
            and credential.github_account_id == trust.recipient.github_account_id
            and credential.github_login == trust.recipient.github_login
        }
        if len(matches) != 1:
            raise ValueError("team migration unavailable")
        selected_webauthn, credential = next(iter(matches.values()))
        self.approval_webauthn = selected_webauthn
        return MigrationContext(
            current=current,
            commit=commit,
            trust=trust,
            ci_recipient=ci_recipient,
            bridge=bridge,
            status=status,
            protection=protection,
            tooling=tooling,
            credential=credential,
        )

    async def _sponsor(
        self, *, require_sponsor: bool = True, preflight: bool = True
    ) -> SponsorContext:
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
                    migration_reader.commit(),
                    legacy_trust,
                    migration_decryptor,
                    self.service._clock(),
                )
                activate_installed_migration(
                    self.runtime.root,
                    legacy_trust,
                    migrated,
                    merged_state_commit=migration_reader.commit(),
                    prior_state_commit=str(marker["ref_commit"]),
                )
                trust = trust_from_verified_migration(legacy_trust, migrated)
                marker = {**marker, "ref_commit": migrated.commit}
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
                default_branch_commit=tooling.commit,
                tooling_digest=_digest(tooling.model_dump(mode="json")),
            )
            if preflight:
                _, _, protection = await bridge._member_preflight(
                    api,
                    current,
                    trust.device_certificate_id,
                    require_sponsor=require_sponsor,
                )
            else:
                protection = client.protection_preview()
        finally:
            await api.aclose()
        certificate = next(
            item
            for item in parent.authority.device_certificates
            if item.certificate_id == trust.device_certificate_id
        )
        from intent_engineering.control_plane.models import credential_matches_digest

        matches = {
            credential.canonical_bytes(): (webauthn, credential)
            for webauthn in (self.service._webauthn, self.webauthn)
            for actor in {member.actor, self.runtime.config.local_actor}
            for credential in webauthn.registered_credentials(actor, self.service._origin)
            if credential_matches_digest(credential, certificate.claims.webauthn_credential_digest)
        }
        if len(matches) != 1:
            raise ValueError("team enrollment unavailable")
        selected_webauthn, selected_credential = next(iter(matches.values()))
        self.approval_webauthn = selected_webauthn
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
            decision_repository_id=selected_credential.repository_id,
            authority=lambda: authority,
            publisher=bridge.transport,
            device_signer=store,
            decision_actor=selected_credential.actor,
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
            selected_credential,
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
            self.publication_context = None
            self.migration_context = None
            self.migration_preview = None
            failure = _prepared_failure(error, "team enrollment unavailable")
            response = b""
            del error
            raise failure.with_traceback(None) from None
        finally:
            response = b""

    async def _publish_migration(
        self,
        context: MigrationContext,
        preview: V1MigrationPreview,
        decision: object,
    ) -> dict[str, object]:
        """Publish one freshly approved migration through the guarded state PR transport."""
        import sys

        from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
        from intent_engineering.team_state.github import GitHubTeamStateClient
        from intent_engineering.team_state.github_publication import GitHubApiPublisher
        from intent_engineering.team_state.publication import prepare_v1_migration
        from intent_engineering.team_state.setup import (
            _CloseOnceApi,
            _draft,
            _GuardedApi,
            github_api,
        )

        if type(decision) is not VerifiedHumanDecision:
            raise ValueError("team migration unavailable")
        from intent_engineering.control_plane.models import credential_identity_digest

        if (
            credential_identity_digest(decision.credential)
            != credential_identity_digest(context.credential)
            or decision.credential.sign_count < context.credential.sign_count
        ):
            raise ValueError("team migration changed")
        context = replace(context, credential=decision.credential)
        if preview.external_preflight_digest != _migration_preflight_digest(context):
            raise ValueError("team migration changed")
        prepared = prepare_v1_migration(
            current=context.current,
            legacy_trust=context.trust,
            ci_recipient=context.ci_recipient,
            preview=preview,
            sponsor_decision=decision,
            now=self.service._clock().replace(microsecond=0),
        )
        old = _draft(self.runtime)
        if old is not None and (old.publication() != prepared or old.anchor != context.commit):
            raise ValueError("team migration changed")
        _draft(
            self.runtime,
            prepared=prepared,
            anchor=context.commit,
            external_write_attempted=True,
            migration_preflight_digest=preview.external_preflight_digest,
            publication_commit=None if old is None else old.publication_commit,
            pull_request_number=None if old is None else old.pull_request_number,
            pull_request_url=None if old is None else old.pull_request_url,
        )
        api = _CloseOnceApi(github_api())
        try:

            async def require_live() -> None:
                fresh = await self._migration()
                if (
                    fresh.current != context.current
                    or fresh.commit != context.commit
                    or fresh.trust != context.trust
                    or fresh.ci_recipient != context.ci_recipient
                    or fresh.status != context.status
                    or fresh.protection != context.protection
                    or fresh.tooling != context.tooling
                    or fresh.credential != context.credential
                    or load_enrollment_request(self.runtime) != self.request
                    or self.service._clock() > decision.payload.expires_at
                ):
                    raise ValueError("team migration changed")

            guarded = _GuardedApi(api, require_live)
            client = GitHubTeamStateClient(
                guarded,
                expected_account_id=context.trust.recipient.github_account_id,
                expected_login=context.trust.recipient.github_login,
            )
            status = await client.inspect(context.bridge.repository)
            if status != context.status:
                raise ValueError("team migration changed")
            publisher = GitHubApiPublisher(guarded, client, status)
            commit = await publisher.publish(prepared, base_commit=context.commit)
            _draft(
                self.runtime,
                prepared=prepared,
                anchor=context.commit,
                external_write_attempted=True,
                publication_commit=commit,
            )
            pull_request = await client.open_publication_pr(prepared, expected_head_commit=commit)
            _draft(
                self.runtime,
                prepared=prepared,
                anchor=context.commit,
                external_write_attempted=True,
                publication_commit=commit,
                pull_request_number=pull_request.number,
                pull_request_url=pull_request.url,
            )
            return {
                **enrollment_status(self.runtime),
                "state": "publication_pending",
                "repository_id": pull_request.repository_id,
                "pull_request_url": pull_request.url,
            }
        finally:
            active_error = sys.exc_info()[1]
            with anyio.CancelScope(shield=True):
                try:
                    await api.aclose()
                except BaseException:
                    if active_error is None:
                        raise

    async def _reconcile_migration(self, *, restart_closed: bool) -> dict[str, object]:
        """Recover, finish, or explicitly retire one protected migration publication."""
        import sys

        from intent_engineering.team_state.github import GitHubTeamStateClient
        from intent_engineering.team_state.github_publication import GitHubApiPublisher
        from intent_engineering.team_state.local_trust import (
            LocalTrustConfig,
            LocalTrustConfigV2,
            LocalTrustProvider,
        )
        from intent_engineering.team_state.publication import PreparedV1Migration
        from intent_engineering.team_state.restore import verify_v1_migration
        from intent_engineering.team_state.setup import (
            GitHubSetupBridge,
            _CloseOnceApi,
            _draft,
            _GuardedApi,
            github_api,
        )

        draft = _draft(self.runtime)
        if draft is None or type(draft.publication()) is not PreparedV1Migration:
            raise ValueError("team migration unavailable")
        publication = cast(PreparedV1Migration, draft.publication())
        trust = LocalTrustProvider(self.runtime.root).load_versioned()
        if isinstance(trust, LocalTrustConfigV2):
            if restart_closed:
                raise ValueError("team migration requires reconciliation")
            activated = await self._sponsor(preflight=False)
            if (
                activated.current.state_commit == draft.anchor
                or activated.current.bundle_digest != publication.manifest.bundle_digest
                or activated.current.authority != publication.authority
            ):
                raise ValueError("team migration changed")
            _draft(
                self.runtime,
                prepared=publication,
                anchor=draft.anchor,
                publication_commit=draft.publication_commit,
                pull_request_number=draft.pull_request_number,
                pull_request_url=draft.pull_request_url,
                discard=True,
            )
            return {
                **enrollment_status(self.runtime),
                "state": "review_required",
                "repository_id": activated.current.authority.repository_id,
            }
        if not isinstance(trust, LocalTrustConfig):
            raise TypeError("team migration changed")
        bridge = GitHubSetupBridge(self.service)
        api = _CloseOnceApi(github_api())
        try:
            client = GitHubTeamStateClient(
                api,
                expected_account_id=trust.recipient.github_account_id,
                expected_login=trust.recipient.github_login,
            )
            status = await client.inspect(bridge.repository)
            if status.repository_id != trust.repository_id:
                raise ValueError("team migration changed")
            if status.branch_commit != draft.anchor:
                if (
                    restart_closed
                    or draft.publication_commit is None
                    or draft.pull_request_number is None
                ):
                    raise ValueError("team migration requires reconciliation")
                await client.confirm_publication_merge(
                    publication,
                    expected_head_commit=draft.publication_commit,
                    expected_base_commit=draft.anchor,
                    pull_request_number=draft.pull_request_number,
                )
                await self._sponsor()
                _draft(
                    self.runtime,
                    prepared=publication,
                    anchor=draft.anchor,
                    publication_commit=draft.publication_commit,
                    pull_request_number=draft.pull_request_number,
                    pull_request_url=draft.pull_request_url,
                    discard=True,
                )
                return {
                    **enrollment_status(self.runtime),
                    "state": "review_required",
                    "repository_id": status.repository_id,
                }
            if draft.publication_commit is None or draft.pull_request_number is None:
                if restart_closed:
                    raise ValueError("team migration requires reconciliation")
                context = await self._migration()
                if (
                    context.status != status
                    or context.commit != draft.anchor
                    or draft.migration_preflight_digest is None
                    or _migration_preflight_digest(context) != draft.migration_preflight_digest
                ):
                    raise ValueError("team migration changed")
                verify_v1_migration(
                    current=context.current,
                    manifest=publication.manifest,
                    envelope=publication.envelope,
                    root=publication.authority.root,
                    authority=publication.authority,
                    expected_ci_recipient=context.ci_recipient,
                    now=self.service._clock(),
                )

                async def require_live() -> None:
                    fresh = await self._migration()
                    if (
                        fresh.current != context.current
                        or fresh.commit != context.commit
                        or fresh.trust != context.trust
                        or fresh.ci_recipient != context.ci_recipient
                        or fresh.status != context.status
                        or fresh.protection != context.protection
                        or fresh.tooling != context.tooling
                        or fresh.credential != context.credential
                        or load_enrollment_request(self.runtime) != self.request
                    ):
                        raise ValueError("team migration changed")

                guarded = _GuardedApi(api, require_live)
                guarded_client = GitHubTeamStateClient(
                    guarded,
                    expected_account_id=trust.recipient.github_account_id,
                    expected_login=trust.recipient.github_login,
                )
                guarded_status = await guarded_client.inspect(context.bridge.repository)
                if guarded_status != status:
                    raise ValueError("team migration changed")
                commit = draft.publication_commit
                if commit is None:
                    publisher = GitHubApiPublisher(guarded, guarded_client, status)
                    commit = await publisher.publish(publication, base_commit=draft.anchor)
                    _draft(
                        self.runtime,
                        prepared=publication,
                        anchor=draft.anchor,
                        external_write_attempted=True,
                        publication_commit=commit,
                    )
                pull_request = await guarded_client.open_publication_pr(
                    publication, expected_head_commit=commit
                )
                _draft(
                    self.runtime,
                    prepared=publication,
                    anchor=draft.anchor,
                    external_write_attempted=True,
                    publication_commit=commit,
                    pull_request_number=pull_request.number,
                    pull_request_url=pull_request.url,
                )
                return {
                    **enrollment_status(self.runtime),
                    "state": "publication_pending",
                    "repository_id": pull_request.repository_id,
                    "pull_request_url": pull_request.url,
                }
            context = await self._migration()
            if context.status != status:
                raise ValueError("team migration changed")
            state = await client.publication_pull_request_state(
                publication,
                expected_head_commit=draft.publication_commit,
                expected_base_commit=draft.anchor,
                pull_request_number=draft.pull_request_number,
            )
            if state == "closed":
                if not restart_closed:
                    return {
                        **enrollment_status(self.runtime),
                        "state": "publication_closed",
                        "repository_id": status.repository_id,
                        "pull_request_url": draft.pull_request_url,
                    }
                _draft(
                    self.runtime,
                    prepared=publication,
                    anchor=draft.anchor,
                    publication_commit=draft.publication_commit,
                    pull_request_number=draft.pull_request_number,
                    pull_request_url=draft.pull_request_url,
                    discard=True,
                )
                return {
                    **enrollment_status(self.runtime),
                    "state": "migration_required",
                    "repository_id": status.repository_id,
                }
            if restart_closed:
                raise ValueError("team migration requires reconciliation")
            return {
                **enrollment_status(self.runtime),
                "state": "publication_pending",
                "repository_id": status.repository_id,
                "pull_request_url": draft.pull_request_url,
            }
        finally:
            active_error = sys.exc_info()[1]
            with anyio.CancelScope(shield=True):
                try:
                    await api.aclose()
                except BaseException:
                    if active_error is None:
                        raise

    async def _action(self, action: str, response: bytes) -> dict[str, object]:
        from intent_engineering.cli.team_enrollment import write_public_file
        from intent_engineering.team_state.enrollment import build_join_decision_payload
        from intent_engineering.team_state.local_trust import (
            LocalTrustConfig,
            LocalTrustConfigV2,
            LocalTrustProvider,
            PendingJoinTrustV2,
        )

        now = self.service._clock().replace(microsecond=0)
        active_trust = LocalTrustProvider(self.runtime.root).load_versioned()
        from intent_engineering.team_state.publication import PreparedV1Migration
        from intent_engineering.team_state.setup import _draft

        durable_migration = _draft(self.runtime)
        if (
            self.request.action == "invite"
            and durable_migration is not None
            and type(durable_migration.publication()) is PreparedV1Migration
        ):
            if action in {"reconcile", "migration-restart"}:
                return await self._reconcile_migration(restart_closed=action == "migration-restart")
            if not isinstance(active_trust, LocalTrustConfig):
                raise ValueError("team migration unavailable")
        if self.request.action == "invite" and isinstance(active_trust, LocalTrustConfig):
            if action in {"reconcile", "migration-restart"}:
                return await self._reconcile_migration(restart_closed=action == "migration-restart")
            if action == "migration-preview":
                from intent_engineering.team_state.publication import preview_v1_migration

                migration_context = await self._migration()
                migration_preview = preview_v1_migration(
                    current=migration_context.current,
                    legacy_trust=migration_context.trust,
                    ci_recipient=migration_context.ci_recipient,
                    sponsor_credential=migration_context.credential,
                    challenge="challenge:" + secrets.token_hex(32),
                    now=now,
                    external_preflight_digest=_migration_preflight_digest(migration_context),
                )
                self.pending_payload = migration_preview.payload
                self.migration_context = migration_context
                self.migration_preview = migration_preview
                return {
                    **enrollment_status(self.runtime),
                    "state": "migration_preview",
                    "preview": {
                        "authority": migration_preview.authority.model_dump(mode="json"),
                        "manifest": migration_preview.manifest.model_dump(mode="json"),
                        "legacy_parent_bundle_digest": migration_preview.legacy_parent_bundle_digest,
                        "root_key_id": migration_preview.root_key_id,
                    },
                    "payload": migration_preview.payload.model_dump(mode="json"),
                }
            if action == "options" and self.pending_payload is not None:
                return cast(
                    dict[str, object],
                    json.loads(
                        self.approval_webauthn.authentication_options(
                            self.pending_payload, self.service._origin, now
                        )
                    ),
                )
            if action == "verify":
                cached_migration_context = self.migration_context
                cached_migration_preview = self.migration_preview
                if (
                    self.pending_payload is None
                    or cached_migration_context is None
                    or cached_migration_preview is None
                ):
                    raise ValueError("team migration unavailable")
                decision = self.approval_webauthn.verify(
                    response, self.pending_payload, self.service._origin, now
                )
                migration_result = await self._publish_migration(
                    cached_migration_context, cached_migration_preview, decision
                )
                self.pending_payload = None
                self.migration_context = None
                self.migration_preview = None
                return migration_result
            raise ValueError("team migration unavailable")
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
            from intent_engineering.team_state.enrollment import build_sponsor_decision_payload
            from intent_engineering.team_state.setup import _draft, _enrollment_receipt

            context = await self._sponsor(preflight=action not in {"reconcile", "restart"})
            enrollment = context.enrollment
            if enrollment is None:
                raise ValueError("team enrollment unavailable")
            approval = load_approval_request(self.runtime, self.request)
            receipt = _enrollment_receipt(self.runtime)
            if action in {"reconcile", "restart"}:
                if approval is None or receipt is None:
                    raise ValueError("team enrollment unavailable")
                draft = _draft(self.runtime)
                result = await context.bridge.reconcile_member_approval(
                    request=approval,
                    current=context.current,
                    decision_repository_id=context.credential.repository_id,
                    decision_actor=context.credential.actor,
                    restart_closed=action == "restart",
                )
                if action == "restart" and result.state == "bootstrap_required":
                    _retire_closed_enrollment_request(
                        self.runtime,
                        self.request,
                        approval,
                        draft,
                        receipt,
                    )
                    return {"state": "cancelled"}
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
                credential = context.credential
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

            if action in {"publish-reconcile", "publish-restart"}:
                context = await self._sponsor(require_sponsor=False, preflight=False)
                publication_result = await context.bridge.reconcile_member_publication(
                    current=context.current,
                    certificate_id=context.trust.device_certificate_id,
                    restart_closed=action == "publish-restart",
                )
                return {**enrollment_status(self.runtime), **publication_result}
            if action == "publish-preview":
                context = await self._sponsor(require_sponsor=False)
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
                        or durable.pull_request_number is not None
                    ):
                        raise ValueError("team enrollment unavailable")
                    prepared = prepared_value
                    preview = context.publication.recover_device_preview(
                        prepared,
                        device_store=context.store,
                        now=now,
                    )
                self.pending_payload = preview.payload
                self.publication_context = context
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
            active_context = self.publication_context
            if self.pending_payload is None or active_context is None:
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
                        LocalTrustProvider(self.runtime.root).load_versioned()
                        != active_context.trust
                        or load_enrollment_request(self.runtime) != self.request
                    ):
                        raise ValueError("team enrollment changed")

                publication_result = await active_context.bridge.publish_member_state(
                    publication=active_context.publication,
                    decision=decision,
                    current=active_context.current,
                    certificate_id=active_context.trust.device_certificate_id,
                    protection=active_context.protection,
                    verify_local=verify_local,
                )
                self.pending_payload = None
                self.publication_context = None
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
            if existing_pending.phase == "response-ready":
                LocalTrustProvider(self.runtime.root).acknowledge_join_response(existing_pending)
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
            trust_provider = LocalTrustProvider(self.runtime.root)
            trust_provider.save_pending_join(pending)
            assert self.request.output is not None
            write_public_file(Path(self.request.output), joined)
            trust_provider.acknowledge_join_response(pending)
            self.proof = b""
            self.pending_payload = None
            return enrollment_status(self.runtime)
        raise ValueError("team enrollment unavailable")
