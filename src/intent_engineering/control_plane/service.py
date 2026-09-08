"""Repository-bound application service for authenticated human workflow decisions."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import cast

from intent_engineering.capture.mcp.profile_loader import (
    load_connector_config_bytes,
    load_strict_yaml_mapping_bytes,
)
from intent_engineering.cli.intent_workflow import (
    confirm_proposal,
    proposal_payload,
)
from intent_engineering.cli.runtime import Runtime
from intent_engineering.cli.writes import MutationPolicy, write_workflow
from intent_engineering.control_plane.models import (
    AttentionRoute,
    CredentialRecord,
    DecisionAction,
    DecisionSubject,
    DevStatus,
    HumanDecisionPayload,
)
from intent_engineering.control_plane.webauthn_service import (
    VerifiedHumanDecision,
    WebAuthnService,
    WebAuthnVerifier,
)
from intent_engineering.core.models import (
    EvidenceRecord,
    ProjectConfig,
    ReconciliationStatus,
    ResolutionAction,
    SourceRole,
)
from intent_engineering.core.policy.access import refs_allowed
from intent_engineering.intent_workflow.bootstrap import BootstrapService
from intent_engineering.intent_workflow.check import (
    SharedStateRestoreStatus,
    evidence_repository_id,
)
from intent_engineering.intent_workflow.clarification import (
    ClarificationCoordinator,
    ProposalConfirmationService,
    ProposalConfirmationStatus,
)
from intent_engineering.intent_workflow.clarification import _digest as clarification_digest
from intent_engineering.intent_workflow.conversation import (
    ConversationCapture,
    validate_conversation_record,
)
from intent_engineering.intent_workflow.dev_observer import (
    DevObserver,
    ObservationResult,
    TestRunResult,
)
from intent_engineering.intent_workflow.models import ClarificationIntentProposal, ProposalKind
from intent_engineering.intent_workflow.onboarding import (
    OnboardingRuntime,
    OnboardingState,
    inspect_onboarding,
)
from intent_engineering.mutations.approval import approve_plan
from intent_engineering.reconcile.local_resolution import LocalResolutionService
from intent_engineering.storage.executor import LocalChangeSetExecutor
from intent_engineering.storage.secure import SecureDirectory, SecureFile, SecureRead
from intent_engineering.storage.transaction import (
    LocalTransactionExtraReadPolicy,
    LocalTransactionSnapshot,
)
from intent_engineering.team_state.keys import (
    GitHubIdentity,
    GitHubIdentityVerifier,
    RecipientEnrollmentBinding,
    RecipientKeyStore,
    RecipientKeyStoreFactory,
    keyring_recipient_store,
    restore_recipient,
)
from intent_engineering.team_state.models import RecipientRecord as TeamRecipientRecord
from intent_engineering.team_state.publication import PublicationService

_MAX_AUTHORITY_FILE_BYTES = 1_048_576
_MAX_AUTHORITY_TOTAL_BYTES = 8_388_608
_MAX_AUTHORITY_FILES = 256
_MAX_AUTHORITY_DEPTH = 8
_DECISION_LIFETIME = timedelta(minutes=5)
_APPROVAL_LIFETIME = timedelta(minutes=15)
_MAX_PENDING_ANSWERS = 64
_MAX_VISIBLE_CLARIFICATION_SESSIONS = 64
_ANSWER_ID = re.compile(r"^answer:[0-9a-f]{64}$")
_TEAM_REPOSITORY_ID = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ControlPlaneError(ValueError):
    """The fixed public failure for an unavailable or stale control-plane operation."""

    def __init__(self) -> None:
        super().__init__("control plane unavailable")


@dataclass(frozen=True, slots=True)
class _PendingAnswer:
    session_id: str
    question_id: str
    answer: str
    actor: str
    answered_at: datetime
    acl: tuple[str, ...]
    evidence_id: str
    subject_digest: str
    result_digest: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _PendingTeamEnrollment:
    identity: GitHubIdentity
    actor: str
    issued_at: datetime
    expires_at: datetime
    authority_digest: str
    binding_digest: str
    challenge_id: str


@dataclass(slots=True)
class _Authority:
    files: dict[str, SecureFile]
    policies: dict[str, LocalTransactionExtraReadPolicy]
    preimages: dict[str, bytes | None]
    snapshot: LocalTransactionSnapshot
    config: ProjectConfig
    policy: MutationPolicy
    provider_principals: dict[str, frozenset[str]]
    membership_digest: str

    def close(self) -> None:
        for file in self.files.values():
            file.close()
        self.files.clear()
        self.policies.clear()
        self.preimages.clear()
        self.provider_principals.clear()


@dataclass(frozen=True, slots=True)
class _DecisionMaterial:
    subject: DecisionSubject
    subject_digest: str
    result_digest: str
    selected_node_ids: tuple[str, ...]
    preview: dict[str, object]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(value)).hexdigest()}"


def _repository_id(runtime: Runtime) -> str:
    device, inode = runtime.project_directory.identity
    material = _canonical_bytes(
        {
            "schema": "intent.local-repository.v1",
            "project_id": runtime.config.project_id,
            "directory_device": device,
            "directory_inode": inode,
        }
    )
    return f"repo:sha256:{hashlib.sha256(material).hexdigest()}"


def _membership_records(
    directory: SecureDirectory, *, read_content: bool
) -> tuple[tuple[PurePosixPath, SecureRead], ...]:
    return directory.walk_regular_files_bounded(
        ".yaml",
        max_files=_MAX_AUTHORITY_FILES,
        max_depth=_MAX_AUTHORITY_DEPTH,
        max_file_bytes=_MAX_AUTHORITY_FILE_BYTES,
        max_total_bytes=_MAX_AUTHORITY_TOTAL_BYTES,
        reject_symlinks=True,
        read_content=read_content,
    )


def _membership_digest(records: tuple[tuple[PurePosixPath, SecureRead], ...]) -> str:
    digest = hashlib.sha256(b"intent.connector-membership.v1\x00")
    for relative, source in records:
        encoded = _canonical_bytes(
            [relative.as_posix(), [list(identity) for identity in source.identities]]
        )
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"sha256:{digest.hexdigest()}"


def _parent_digest(content: Mapping[str, bytes | None], membership_digest: str) -> str:
    digest = hashlib.sha256(b"intent.control-plane-authority.v1\x00")
    for name in sorted(content):
        if name == "webauthn_challenges":
            continue
        encoded_name = name.encode("ascii")
        value = content[name]
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(b"\x00" if value is None else b"\x01")
        if value is not None:
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    digest.update(membership_digest.encode("ascii"))
    return f"sha256:{digest.hexdigest()}"


class ControlPlaneService:
    """Compose one held Runtime with exact WebAuthn-backed workflow decisions."""

    def __init__(
        self,
        runtime: Runtime,
        *,
        origin: str,
        clock: Callable[[], datetime] | None = None,
        challenge_source: Callable[[], bytes] | None = None,
        webauthn_verifier: WebAuthnVerifier | None = None,
        shared_state_status: SharedStateRestoreStatus = SharedStateRestoreStatus.NOT_REQUIRED,
        team_repository_id: str | None = None,
        github_identity_verifier: GitHubIdentityVerifier | None = None,
        recipient_key_store_factory: RecipientKeyStoreFactory = keyring_recipient_store,
        publication_service: PublicationService | None = None,
    ) -> None:
        if type(runtime) is not Runtime:
            raise ValueError("invalid control plane runtime")
        if type(shared_state_status) is not SharedStateRestoreStatus:
            raise ValueError("invalid shared-state status")
        if team_repository_id is not None and (
            type(team_repository_id) is not str
            or _TEAM_REPOSITORY_ID.fullmatch(team_repository_id) is None
        ):
            raise ValueError("invalid team repository")
        if not callable(recipient_key_store_factory):
            raise TypeError("invalid recipient key store factory")
        self._runtime = runtime
        self._origin = origin
        self._clock = clock or (lambda: datetime.now(UTC))
        self._challenge_source = challenge_source or (lambda: secrets.token_bytes(32))
        self.repository_id = _repository_id(runtime)
        self._config_file = runtime.workspace_directory.file("config.yaml")
        self._approvals_directory = runtime.workspace_directory.subdirectory("approvals")
        self._connectors_directory = runtime.workspace_directory.subdirectory("connectors")
        self._policy_file = self._approvals_directory.file("policy.yaml")
        self._plans_file = self._approvals_directory.file("plans.jsonl")
        self._pending_answers: dict[str, _PendingAnswer] = {}
        self._team_repository_id = team_repository_id
        if self._team_repository_id is None:
            from intent_engineering.team_state.local_trust import load_local_trust

            local_trust = load_local_trust(runtime.root)
            if local_trust is not None:
                self._team_repository_id = local_trust.repository_id
        self._github_identity_verifier = github_identity_verifier
        self._recipient_key_store_factory = recipient_key_store_factory
        if publication_service is not None and type(publication_service) is not PublicationService:
            raise TypeError("invalid publication service")
        self._publication_service = publication_service
        self._team_enrollment_guard = threading.RLock()
        self._team_enrollment_blocked = False
        self._pending_team_enrollment: _PendingTeamEnrollment | None = None
        self._team_recipient: TeamRecipientRecord | None = None
        self._team_key_store: RecipientKeyStore | None = None
        self._github_setup_bridge: object | None = None
        self._dev_observer: DevObserver | None = None
        self._observation_guard = threading.Lock()
        self._observation_state_guard = threading.Lock()
        self._observation_stop = threading.Event()
        self._observation_thread: threading.Thread | None = None
        self._latest_observation: ObservationResult | None = None
        self._shared_state_status = shared_state_status
        self._webauthn = WebAuthnService(
            project_id=runtime.config.project_id,
            repository_id=self.repository_id,
            expected_origin=origin,
            credentials=runtime.webauthn_credentials,
            challenges=runtime.webauthn_challenges,
            transactions=runtime.transactions,
            verifier=webauthn_verifier,
        )
        self._restore_team_recipient()

    @staticmethod
    def _recipient_matches_binding(
        recipient: TeamRecipientRecord,
        binding: RecipientEnrollmentBinding,
    ) -> bool:
        return (
            recipient.project_id == binding.project_id
            and recipient.repository_id == binding.repository_id
            and recipient.actor == binding.actor
            and recipient.github_account_id == binding.github_identity.account_id
            and recipient.github_login == binding.github_identity.login
            and recipient.webauthn_credential_id == binding.webauthn_credential_id
            and recipient.webauthn_credential_public_key == binding.webauthn_credential_public_key
            and recipient.enrolled_at == binding.enrolled_at
        )

    def _restore_team_recipient(self) -> None:
        """Recover one complete durable enrollment or block replacement fail-closed."""
        if self._team_repository_id is None:
            return
        authority: _Authority | None = None
        try:
            authority = self._authority()
            credentials = tuple(
                record
                for record in self._webauthn.registered_credentials(
                    authority.config.local_actor, self._origin
                )
                if not record.local_only
            )
            if not credentials:
                return
            self._team_enrollment_blocked = True
            if len(credentials) != 1:
                return
            credential = credentials[0]
            if credential.github_account_id is None or credential.github_login is None:
                return
            aliases = authority.policy.identities.get(credential.actor, frozenset())
            if (
                credential.actor != authority.config.local_actor
                or credential.actor not in aliases
                or f"github:{credential.github_login}" not in aliases
            ):
                return
            binding = RecipientEnrollmentBinding(
                project_id=credential.project_id,
                repository_id=self._team_repository_id,
                actor=credential.actor,
                github_identity=GitHubIdentity(
                    account_id=credential.github_account_id,
                    login=credential.github_login,
                ),
                webauthn_credential_id=credential.credential_id,
                webauthn_credential_public_key=credential.public_key,
                enrolled_at=credential.created_at,
            )
            key_store = self._recipient_key_store_factory(binding)
            recipient = restore_recipient(binding, key_store)
            if not self._recipient_matches_binding(recipient, binding):
                return
            self._team_key_store = key_store
            self._team_recipient = TeamRecipientRecord.model_validate(
                recipient.model_dump(mode="python")
            )
            self._team_enrollment_blocked = False
        except Exception:  # noqa: BLE001 - durable recovery is fail-closed
            self._team_enrollment_blocked = True
        finally:
            if authority is not None:
                authority.close()

    def _team_binding_digest(
        self,
        authority: _Authority,
        identity: GitHubIdentity,
        at: datetime,
    ) -> str:
        return _digest(
            {
                "schema": "intent.team-enrollment.v1",
                "project_id": authority.config.project_id,
                "repository_id": self._team_repository_id,
                "actor": authority.config.local_actor,
                "github_account_id": identity.account_id,
                "github_login": identity.login,
                "authority_digest": _parent_digest(
                    authority.snapshot.content,
                    authority.membership_digest,
                ),
                "issued_at": at.isoformat(),
                "generation": secrets.token_hex(32),
            }
        )

    def _revoke_team_pending(self, pending: _PendingTeamEnrollment, at: datetime) -> None:
        self._webauthn.revoke_registration(
            pending.challenge_id,
            pending.actor,
            self._origin,
            at,
            pending.binding_digest,
        )

    def _purge_expired_team_pending(self, at: datetime) -> None:
        pending = self._pending_team_enrollment
        if pending is None or pending.expires_at > at:
            return
        try:
            self._revoke_team_pending(pending, at)
        except Exception as error:  # noqa: BLE001 - expired challenge is already unusable
            error.__traceback__ = None
            error.__cause__ = None
            error.__context__ = None
        self._pending_team_enrollment = None

    def team_enrollment_status(self) -> dict[str, object]:
        """Return a credential-free projection of durable team enrollment."""
        try:
            with self._team_enrollment_guard:
                recipient = self._team_recipient
                if recipient is None:
                    return {
                        "schema_version": 1,
                        "status": "local_only",
                        "repository_id": self._team_repository_id,
                        "recipient_key_id": None,
                        "github_login": None,
                    }
                validated = TeamRecipientRecord.model_validate(recipient.model_dump(mode="python"))
                return {
                    "schema_version": 1,
                    "status": "enrolled",
                    "repository_id": validated.repository_id,
                    "recipient_key_id": validated.key_id,
                    "github_login": validated.github_login,
                }
        except Exception:  # noqa: BLE001 - fixed browser projection boundary
            raise ControlPlaneError() from None

    def team_publication_preview(self) -> dict[str, object]:
        """Return a secret-free projection of the exact pending team publication."""
        publication = self._publication_service
        if publication is None:
            raise ControlPlaneError() from None

        try:
            preview = publication.preview(now=self._now())
            return {
                "schema_version": 1,
                "preview": {
                    "snapshot_digest": preview.snapshot_digest,
                    "bundle_digest": preview.manifest.bundle_digest,
                    "bundle_size": preview.manifest.bundle_size,
                    "branch": preview.branch,
                    "parent_bundle_digest": preview.manifest.parent_bundle_digest,
                    "recipient_key_ids": list(preview.recipient_key_ids),
                    "created_at": preview.manifest.model_dump(mode="json")["created_at"],
                },
                "payload": preview.payload.model_dump(mode="json"),
            }
        except ControlPlaneError:
            raise
        except Exception:  # noqa: BLE001 - fixed local-browser boundary
            raise ControlPlaneError() from None

    def github_setup_status(self) -> dict[str, object]:
        """Read the bounded CLI request without acquiring provider credentials."""
        from intent_engineering.team_state.setup import GitHubSetupBridge, load_setup_request

        request = load_setup_request(self._runtime)
        if request is None:
            return {"state": "unconfigured"}
        if self._github_setup_bridge is None:
            self._github_setup_bridge = GitHubSetupBridge(self)
        return {
            "state": cast(GitHubSetupBridge, self._github_setup_bridge).status(),
            "repository_id": request.preview.repository_id,
            "preview_digest": request.preview.preview_digest,
        }

    async def github_setup_action(
        self, action: str, *, payload: HumanDecisionPayload | None = None, response: bytes = b""
    ) -> dict[str, object]:
        from intent_engineering.team_state.setup import GitHubSetupBridge

        if self._github_setup_bridge is None:
            self._github_setup_bridge = GitHubSetupBridge(self)
        bridge = cast(GitHubSetupBridge, self._github_setup_bridge)
        return await bridge.action(action, payload=payload, response=response)

    def _team_identity(self, proof: bytes, authority: _Authority) -> GitHubIdentity:
        verifier = self._github_identity_verifier
        if (
            verifier is None
            or self._team_repository_id is None
            or type(proof) is not bytes
            or not proof
            or len(proof) > 16 * 1024
        ):
            raise ValueError("team enrollment unavailable")
        verified = verifier.verify(proof)
        if type(verified) is not GitHubIdentity:
            raise ValueError("GitHub identity unavailable")
        identity = GitHubIdentity.model_validate(verified.model_dump(mode="python"))
        actor = authority.config.local_actor
        aliases = authority.policy.identities.get(actor, frozenset())
        if actor not in aliases or f"github:{identity.login}" not in aliases:
            raise ValueError("GitHub identity is not authorized for actor")
        return identity

    def _team_enrollment_options(self, identity_proof: bytes) -> bytes:
        """Verify one GitHub identity, then start its user-verifying credential ceremony."""
        authority: _Authority | None = None
        result: bytes | None = None
        signal: BaseException | None = None
        pending: _PendingTeamEnrollment | None = None
        try:
            at = self._now()
            self._purge_expired_team_pending(at)
            if (
                self._team_recipient is not None
                or self._team_enrollment_blocked
                or self._pending_team_enrollment is not None
            ):
                raise ValueError("team enrollment already exists")
            authority = self._authority()
            identity = self._team_identity(identity_proof, authority)
            if not self._authority_matches(authority):
                raise ValueError("team enrollment authority changed")
            binding_digest = self._team_binding_digest(authority, identity, at)
            result, challenge_id = self._webauthn.bound_registration_options(
                authority.config.local_actor,
                self._origin,
                at,
                binding_digest,
            )
            pending = _PendingTeamEnrollment(
                identity=identity,
                actor=authority.config.local_actor,
                issued_at=at,
                expires_at=at + _DECISION_LIFETIME,
                authority_digest=_parent_digest(
                    authority.snapshot.content,
                    authority.membership_digest,
                ),
                binding_digest=binding_digest,
                challenge_id=challenge_id,
            )
            if not self._authority_matches(authority):
                raise ValueError("team enrollment authority changed")
            self._pending_team_enrollment = pending
        except Exception as caught:  # noqa: BLE001 - fixed opaque enrollment boundary
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            result = None
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            identity_proof = b""
            if result is None and pending is not None:
                try:
                    self._revoke_team_pending(pending, self._now())
                except BaseException as cleanup_error:  # noqa: BLE001 - best-effort cleanup
                    cleanup_error.__traceback__ = None
                    cleanup_error.__cause__ = None
                    cleanup_error.__context__ = None
            pending = None
            if authority is not None:
                authority.close()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if result is None:
            raise ControlPlaneError() from None
        return result

    def team_enrollment_options(self, identity_proof: bytes) -> bytes:
        """Serialize enrollment generation with completion and cancellation."""
        with self._team_enrollment_guard:
            return self._team_enrollment_options(identity_proof)

    def _complete_team_enrollment(self, response: bytes) -> TeamRecipientRecord:
        """Complete WebAuthn registration and generate its bound recipient key atomically."""
        authority: _Authority | None = None
        pending = self._pending_team_enrollment
        registered: CredentialRecord | None = None
        recipient: TeamRecipientRecord | None = None
        key_store: RecipientKeyStore | None = None
        signal: BaseException | None = None
        failed = False
        try:
            if type(response) is not bytes or pending is None or self._team_recipient is not None:
                raise ValueError("team enrollment unavailable")
            authority = self._authority()
            at = self._now()
            if (
                pending.expires_at <= at
                or pending.actor != authority.config.local_actor
                or pending.authority_digest
                != _parent_digest(authority.snapshot.content, authority.membership_digest)
                or f"github:{pending.identity.login}"
                not in authority.policy.identities.get(pending.actor, frozenset())
            ):
                raise ValueError("team enrollment changed")
            with self._runtime.transactions.transaction(
                rollback_base_exceptions=True,
                extras=authority.files,
                extra_read_policies=authority.policies,
            ):
                snapshot = self._runtime.transactions.snapshot(
                    authority.files,
                    extra_read_policies=authority.policies,
                )
                if self._membership_now() != authority.membership_digest or any(
                    snapshot.content.get(name) != value
                    for name, value in authority.preimages.items()
                ):
                    raise ValueError("team enrollment authority changed")
                authority.snapshot = snapshot
                registered = self._webauthn.register(
                    response,
                    pending.actor,
                    self._origin,
                    at,
                    github_account_id=pending.identity.account_id,
                    github_login=pending.identity.login,
                    binding_digest=pending.binding_digest,
                    expected_challenge_id=pending.challenge_id,
                )
                if (
                    registered.local_only
                    or registered.github_account_id != pending.identity.account_id
                    or registered.github_login != pending.identity.login
                    or registered.actor != pending.actor
                    or registered.project_id != authority.config.project_id
                    or registered.repository_id != self.repository_id
                ):
                    raise ValueError("team credential changed")
                binding = RecipientEnrollmentBinding(
                    project_id=authority.config.project_id,
                    repository_id=cast(str, self._team_repository_id),
                    actor=pending.actor,
                    github_identity=pending.identity,
                    webauthn_credential_id=registered.credential_id,
                    webauthn_credential_public_key=registered.public_key,
                    enrolled_at=at,
                )
                key_store = self._recipient_key_store_factory(binding)
                recipient = key_store.generate(binding.project_id, binding.actor)
                if not self._recipient_matches_binding(recipient, binding):
                    raise ValueError("recipient binding changed")
                if self._membership_now() != authority.membership_digest:
                    raise ValueError("team enrollment authority changed")
            self._team_recipient = TeamRecipientRecord.model_validate(
                recipient.model_dump(mode="python")
            )
            self._team_key_store = key_store
            self._pending_team_enrollment = None
        except Exception as caught:  # noqa: BLE001 - fixed opaque enrollment boundary
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            response = b""
            registered = None
            if (failed or signal is not None) and key_store is not None and recipient is not None:
                try:
                    key_store.delete(recipient.key_id)
                except BaseException as cleanup_error:  # noqa: BLE001 - best-effort rollback
                    cleanup_error.__traceback__ = None
                    cleanup_error.__cause__ = None
                    cleanup_error.__context__ = None
            recipient = None
            key_store = None
            if authority is not None:
                authority.close()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if failed or self._team_recipient is None:
            raise ControlPlaneError() from None
        return TeamRecipientRecord.model_validate(self._team_recipient.model_dump(mode="python"))

    def complete_team_enrollment(self, response: bytes) -> TeamRecipientRecord:
        """Serialize enrollment completion with options and cancellation."""
        with self._team_enrollment_guard:
            return self._complete_team_enrollment(response)

    def cancel_team_enrollment(self) -> dict[str, object]:
        """Forget a pending identity without creating credential or recipient material."""
        try:
            with self._team_enrollment_guard:
                if self._team_recipient is not None:
                    return {"schema_version": 1, "status": "enrolled"}
                pending = self._pending_team_enrollment
                if pending is not None:
                    self._revoke_team_pending(pending, self._now())
                    self._pending_team_enrollment = None
                return {"schema_version": 1, "status": "cancelled"}
        except Exception:  # noqa: BLE001 - fixed cancellation boundary
            raise ControlPlaneError() from None

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("invalid control plane clock")
        return value.astimezone(UTC)

    def _nonce(self) -> str:
        value = self._challenge_source()
        if type(value) is not bytes or len(value) != 32:
            raise ValueError("invalid control plane challenge")
        return f"challenge:{value.hex()}"

    def _development_observer(self) -> DevObserver:
        loaded = ProjectConfig.model_validate(
            load_strict_yaml_mapping_bytes(
                self._config_file.read_bytes_nonblocking(max_bytes=_MAX_AUTHORITY_FILE_BYTES)
            )
        )
        if loaded != self._runtime.config:
            raise ValueError("control plane configuration changed")
        if self._dev_observer is None:
            self._dev_observer = DevObserver(
                self._runtime.root,
                self._runtime.config,
                repository_id=evidence_repository_id(self._runtime.config),
                principals=frozenset({self._runtime.config.local_actor}),
            )
        return self._dev_observer

    def observe_development(self) -> ObservationResult:
        """Return passive evidence candidates without graph or completion mutation."""
        if not self._observation_guard.acquire(blocking=False):
            raise ControlPlaneError() from None
        try:
            result = self._development_observer().poll(at=self._now())
            with self._observation_state_guard:
                self._latest_observation = result
            return result
        except Exception:  # noqa: BLE001 - fixed opaque observer boundary
            raise ControlPlaneError() from None
        finally:
            self._observation_guard.release()

    def development_observation(self) -> ObservationResult:
        """Return the latest detached passive result without initiating repository work."""
        with self._observation_state_guard:
            result = self._latest_observation
            if result is None:
                raise ControlPlaneError() from None
            return ObservationResult.model_validate_json(result.model_dump_json())

    def start_development_observation(self, *, interval_seconds: float = 2.0) -> None:
        """Start one bounded-cadence passive observer owned by this service."""
        if (
            type(interval_seconds) not in {int, float}
            or not 0.001 <= float(interval_seconds) <= 60.0
        ):
            raise ControlPlaneError() from None
        with self._observation_state_guard:
            if self._observation_thread is not None:
                return
            self._observation_stop.clear()

            def run() -> None:
                while not self._observation_stop.is_set():
                    try:
                        self.observe_development()
                    except Exception as error:  # noqa: BLE001 - passive failures are opaque
                        error.__traceback__ = None
                    if self._observation_stop.wait(float(interval_seconds)):
                        return

            thread = threading.Thread(
                target=run,
                name="intent-dev-observer",
                daemon=True,
            )
            self._observation_thread = thread
            thread.start()

    def _stop_development_observation(self) -> None:
        with self._observation_state_guard:
            thread = self._observation_thread
            self._observation_stop.set()
        if thread is not None:
            thread.join(timeout=30)
            if thread.is_alive():
                raise ControlPlaneError() from None
        with self._observation_state_guard:
            self._observation_thread = None

    async def run_reviewed_tests(self, command_id: str) -> TestRunResult:
        """Run one configured test action without acquiring human authority."""
        if not self._observation_guard.acquire(blocking=False):
            raise ControlPlaneError() from None
        try:
            return await self._development_observer().run_reviewed_tests(
                command_id,
                at=self._now(),
            )
        except Exception:  # noqa: BLE001 - cancellation remains a BaseException
            raise ControlPlaneError() from None
        finally:
            self._observation_guard.release()

    def _purge_pending_answers(self, now: datetime) -> None:
        expired = tuple(
            identifier
            for identifier, pending in self._pending_answers.items()
            if pending.expires_at <= now
        )
        for identifier in expired:
            self._pending_answers.pop(identifier, None)

    @staticmethod
    def _read_policies(
        files: Mapping[str, SecureFile],
    ) -> dict[str, LocalTransactionExtraReadPolicy]:
        return {
            name: LocalTransactionExtraReadPolicy(
                max_bytes=_MAX_AUTHORITY_FILE_BYTES,
                nonblocking_regular=True,
                aggregate_group=(
                    "authority_files"
                    if name.startswith(("authority_binding_", "authority_profile_"))
                    else None
                ),
                max_aggregate_bytes=(
                    _MAX_AUTHORITY_TOTAL_BYTES
                    if name.startswith(("authority_binding_", "authority_profile_"))
                    else None
                ),
            )
            for name in files
        }

    def _membership_now(self) -> str:
        return _membership_digest(
            _membership_records(self._connectors_directory, read_content=False)
        )

    def _authority(self) -> _Authority:
        files: dict[str, SecureFile] = {
            "authority_config": self._config_file.duplicate(),
            "authority_policy": self._policy_file.duplicate(),
            "authority_plans": self._plans_file.duplicate(),
        }
        records: tuple[tuple[PurePosixPath, SecureRead], ...] = ()
        profile_paths: set[str] = set()
        try:
            records = _membership_records(self._connectors_directory, read_content=True)
            membership_digest = _membership_digest(records)
            for index, (relative, source) in enumerate(records):
                files[f"authority_binding_{index}"] = self._connectors_directory.file(relative)
                profile_paths.add(load_connector_config_bytes(source.content).profile_path)
            for index, profile_path in enumerate(sorted(profile_paths)):
                files[f"authority_profile_{index}"] = self._runtime.project_directory.file(
                    profile_path
                )
            policies = self._read_policies(files)
            snapshot = self._runtime.transactions.snapshot(
                files,
                extra_read_policies=policies,
            )
            if self._membership_now() != membership_digest:
                raise ValueError("control plane authority changed")
            config, policy, provider_principals = ProposalConfirmationService._authority(snapshot)
            if config != self._runtime.config:
                raise ValueError("control plane configuration changed")
            preimages = {name: snapshot.content.get(name) for name in files}
            return _Authority(
                files=files,
                policies=policies,
                preimages=preimages,
                snapshot=snapshot,
                config=config,
                policy=policy,
                provider_principals=provider_principals,
                membership_digest=membership_digest,
            )
        except BaseException:
            for file in files.values():
                file.close()
            files.clear()
            raise
        finally:
            records = ()
            profile_paths.clear()

    def _authority_matches(self, authority: _Authority) -> bool:
        if self._membership_now() != authority.membership_digest:
            return False
        snapshot = self._runtime.transactions.snapshot(
            authority.files,
            extra_read_policies=authority.policies,
        )
        expected = {
            name: content
            for name, content in authority.snapshot.content.items()
            if name != "webauthn_challenges"
        }
        current = {
            name: content
            for name, content in snapshot.content.items()
            if name != "webauthn_challenges"
        }
        return current == expected and all(
            snapshot.content.get(name) == value for name, value in authority.preimages.items()
        )

    def _principals(self, authority: _Authority, actor: str) -> frozenset[str]:
        aliases = ProposalConfirmationService._aliases(
            actor,
            authority.policy,
            authority.provider_principals,
        )
        if actor not in aliases:
            raise ValueError("control plane actor unavailable")
        return frozenset({"agent:codex", *aliases})

    def status(self) -> dict[str, object]:
        """Return one detached local readiness projection without granting authority."""
        try:
            self._purge_pending_answers(self._now())
            onboarding = inspect_onboarding(cast(OnboardingRuntime, self._runtime))
            open_cases = tuple(
                case.id
                for case in self._runtime.cases()
                if case.status
                not in {
                    ReconciliationStatus.RESOLVED,
                    ReconciliationStatus.DEFERRED,
                    ReconciliationStatus.FALSE_POSITIVE,
                }
            )
            latest_sessions = {
                event.session.id: event.session
                for event in self._runtime.intent_proposals.clarification_events()
            }
            clarification_attention = any(
                session.status == "open"
                and bool(
                    {question.id for question in session.questions}
                    - {answer.question_id for answer in session.answers}
                )
                for session in latest_sessions.values()
            )
            shared_projection = {
                SharedStateRestoreStatus.INVALID: (
                    DevStatus.SHARED_STATE_INVALID,
                    AttentionRoute.TEAM_STATE,
                ),
                SharedStateRestoreStatus.UPGRADE_REQUIRED: (
                    DevStatus.UPGRADE_REQUIRED,
                    AttentionRoute.TEAM_STATE,
                ),
                SharedStateRestoreStatus.DIVERGED: (
                    DevStatus.HUMAN_ATTENTION_REQUIRED,
                    AttentionRoute.TEAM_STATE,
                ),
                SharedStateRestoreStatus.STALE: (
                    DevStatus.OFFLINE_STALE,
                    AttentionRoute.TEAM_STATE,
                ),
                SharedStateRestoreStatus.UNAVAILABLE: (
                    DevStatus.OFFLINE_STALE,
                    AttentionRoute.TEAM_STATE,
                ),
            }.get(self._shared_state_status)
            if shared_projection is not None:
                status, route = shared_projection
            elif onboarding.state is OnboardingState.REQUIRED:
                status, route = DevStatus.ONBOARDING_REQUIRED, AttentionRoute.ONBOARDING
            elif (
                onboarding.state is OnboardingState.REVIEW_REQUIRED
                or open_cases
                or clarification_attention
            ):
                status, route = DevStatus.HUMAN_ATTENTION_REQUIRED, AttentionRoute.INBOX
            else:
                status, route = (
                    (DevStatus.READY, AttentionRoute.HOME)
                    if self._shared_state_status is SharedStateRestoreStatus.VERIFIED
                    else (DevStatus.LOCAL_ONLY, AttentionRoute.HOME)
                )
            return {
                "schema_version": 1,
                "status": status.value,
                "attention_route": route.value,
                "project_id": self._runtime.config.project_id,
                "repository_id": self.repository_id,
                "graph_version": onboarding.graph_version,
                "pending_proposal_ids": list(onboarding.pending_proposal_ids),
                "open_case_ids": list(open_cases),
            }
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            raise ControlPlaneError() from None

    def _clarification_inbox(self, authority: _Authority) -> list[dict[str, object]]:
        actor = authority.config.local_actor
        principals = self._principals(authority, actor)
        records = tuple(self._runtime.evidence_store.list())
        evidence = {record.id: record for record in records}
        latest_sessions = {
            event.session.id: event.session
            for event in self._runtime.intent_proposals.clarification_events()
        }
        visible: list[dict[str, object]] = []
        for session_id in sorted(latest_sessions):
            if len(visible) >= _MAX_VISIBLE_CLARIFICATION_SESSIONS:
                break
            session = latest_sessions[session_id]
            if session.status != "open":
                continue
            answered = {answer.question_id for answer in session.answers}
            questions: list[dict[str, object]] = []
            for question in session.questions:
                if question.id in answered:
                    continue
                record = evidence.get(question.evidence_ref)
                if record is None or not refs_allowed(
                    (
                        session.request_evidence_ref,
                        session.classification_evidence_ref,
                        question.evidence_ref,
                    ),
                    records,
                    principals,
                ):
                    continue
                try:
                    prompt = validate_conversation_record(
                        record,
                        conversation_ref=session.conversation_ref,
                        role="agent",
                        author=question.author,
                        acl=record.acl,
                    )
                except Exception:  # noqa: BLE001, S112 - hidden and invalid are identical
                    continue
                if (
                    type(prompt) is not str
                    or clarification_digest(prompt) != question.prompt_digest
                ):
                    continue
                questions.append(
                    {
                        "id": question.id,
                        "prompt": prompt,
                        "required": question.required,
                    }
                )
            if questions:
                visible.append(
                    {
                        "id": session.id,
                        "task_id": session.task_id,
                        "questions": questions,
                    }
                )
        return visible

    def inbox(self) -> dict[str, object]:
        """Return one bounded ACL-filtered browser Inbox projection."""
        authority: _Authority | None = None
        try:
            self._purge_pending_answers(self._now())
            raw_status = self.status()
            visible_proposals: list[str] = []
            visible_cases: list[str] = []
            for identifier, destination in (
                *(
                    (item, visible_proposals)
                    for item in cast(list[str], raw_status["pending_proposal_ids"])
                ),
                *((item, visible_cases) for item in cast(list[str], raw_status["open_case_ids"])),
            ):
                try:
                    self.proposal_preview(identifier)
                except Exception:  # noqa: BLE001, S112 - hidden and unavailable are identical
                    continue
                destination.append(identifier)
            authority = self._authority()
            clarification_sessions = self._clarification_inbox(authority)
            if not self._authority_matches(authority):
                raise ValueError("inbox authority changed")
            return {
                "schema_version": 1,
                "pending_proposal_ids": visible_proposals,
                "open_case_ids": visible_cases,
                "clarification_sessions": clarification_sessions,
            }
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            raise ControlPlaneError() from None
        finally:
            if authority is not None:
                authority.close()

    def onboard_preview(self, prd: str) -> dict[str, object]:
        """Capture one bounded PRD through the existing held-runtime evidence path."""
        result: dict[str, object] | None = None
        signal: BaseException | None = None
        try:
            from intent_engineering.cli.intent_workflow import _capture_prd, _snapshot_config

            with self._runtime.transactions.transaction(rollback_base_exceptions=True):
                config, config_bytes = _snapshot_config(self._runtime)
                graph_version = self._runtime.graph_store.load().version
                proposal_bytes = self._runtime.intent_proposals.bytes()
                evidence_ref = _capture_prd(self._runtime, config, prd)
                if (
                    self._runtime.graph_store.load().version != graph_version
                    or self._runtime.intent_proposals.bytes() != proposal_bytes
                    or _snapshot_config(self._runtime) != (config, config_bytes)
                ):
                    raise ValueError("onboarding preview changed")
            result = {
                "schema_version": 1,
                "status": "agent_submission_required",
                "graph_version": graph_version,
                "evidence_ref": evidence_ref,
                "connector_id": "markdown",
                "scope": prd,
                "source_role": SourceRole.DECLARED_INTENT.value,
            }
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            result = None
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            prd = ""
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if result is None:
            raise ControlPlaneError() from None
        return result

    def _baseline_material(
        self,
        proposal_id: str,
        actor: str,
        at: datetime,
        selected: tuple[str, ...],
        authority: _Authority,
    ) -> _DecisionMaterial:
        if actor != authority.config.local_actor or actor not in authority.policy.contributors:
            raise ValueError("baseline actor unavailable")
        proposal = self._runtime.intent_proposals.get(proposal_id)
        if proposal.kind is not ProposalKind.BOOTSTRAP or isinstance(
            proposal, ClarificationIntentProposal
        ):
            raise ValueError("baseline proposal unavailable")
        preview = proposal_payload(self._runtime, authority.config, proposal_id)
        allowed = tuple(sorted(cast(list[str], preview["core_node_ids"])))
        if not selected:
            selected = allowed
        if tuple(sorted(set(selected))) != selected or not set(selected).issubset(set(allowed)):
            raise ValueError("baseline selection unavailable")
        changeset = BootstrapService._activation_changeset(proposal, selected, actor, at)
        body: dict[str, object] = {
            "kind": "baseline",
            "proposal": preview,
            "selected_node_ids": list(selected),
            "result_changeset_id": changeset.id,
            "result_changeset_digest": _digest(changeset.model_dump(mode="json")),
        }
        return _DecisionMaterial(
            DecisionSubject(kind="proposal", id=proposal.id),
            proposal.digest,
            cast(str, body["result_changeset_digest"]),
            selected,
            body,
        )

    def _clarification_material(
        self,
        proposal_id: str,
        actor: str,
        at: datetime,
        selected: tuple[str, ...],
        authority: _Authority,
    ) -> _DecisionMaterial:
        service = self._confirmation_service(authority)
        try:
            proposal = self._runtime.intent_proposals.get(proposal_id)
            if not isinstance(proposal, ClarificationIntentProposal):
                raise TypeError("clarification proposal unavailable")
            allowed = tuple(sorted(proposal.core_node_ids))
            if not selected:
                selected = allowed
            if (
                not selected
                or tuple(sorted(set(selected))) != selected
                or not set(selected).issubset(set(allowed))
            ):
                raise ValueError("proposal selection unavailable")
            authenticated = service.preview_confirmation(
                proposal_id,
                proposal_digest=proposal.digest,
                actor=actor,
                at=at,
                selected_node_ids=selected,
            )
            if authenticated.high_risk or authenticated.review_case is not None:
                raise ValueError("conflicting proposal requires a case decision")
            if authenticated.proposal != proposal or authenticated.selected_node_ids != selected:
                raise ValueError("proposal confirmation preview changed")
            changeset = authenticated.activation_changeset
        finally:
            service.close()
        body: dict[str, object] = {
            "kind": "clarification_proposal",
            "proposal": {
                **proposal.model_dump(mode="json"),
                "proposal_digest": proposal.digest,
            },
            "selected_node_ids": list(selected),
            "result_changeset_id": changeset.id,
            "result_changeset_digest": _digest(changeset.model_dump(mode="json")),
        }
        return _DecisionMaterial(
            DecisionSubject(kind="proposal", id=proposal.id),
            proposal.digest,
            cast(str, body["result_changeset_digest"]),
            selected,
            body,
        )

    def _case_material(
        self,
        case_id: str,
        actor: str,
        _at: datetime,
        selected: tuple[str, ...],
        authority: _Authority,
    ) -> _DecisionMaterial:
        if selected or actor not in authority.policy.approvers:
            raise ValueError("case authority unavailable")
        case = self._runtime.case_store.get(case_id)
        if case.status is not ReconciliationStatus.NEEDS_HUMAN:
            raise ValueError("case decision unavailable")
        evidence = tuple(self._runtime.evidence_store.get(ref) for ref in case.all_evidence_refs)
        aliases = self._principals(authority, actor)
        if not refs_allowed(case.all_evidence_refs, evidence, aliases):
            raise ValueError("case evidence unavailable")
        resolution = LocalResolutionService(
            self._runtime.graph_store,
            self._runtime.evidence_store,
            self._runtime.case_store,
            actor,
            transactions=self._runtime.transactions,
            principals=aliases,
        )
        action = ResolutionAction.UPDATE_REQUIREMENT
        changeset = resolution._canonical_changeset(
            case,
            self._runtime.graph_store.load().version,
            action,
        )
        approval_hash = resolution._approval_hash(
            case,
            self._runtime.graph_store.load().version,
            action,
            changeset,
        )
        body: dict[str, object] = {
            "kind": "reconciliation_case",
            "case": case.model_dump(mode="json"),
            "action": action.value,
            "approval_hash": approval_hash,
            "result_changeset_id": changeset.id,
            "result_changeset_digest": _digest(changeset.model_dump(mode="json")),
        }
        return _DecisionMaterial(
            DecisionSubject(kind="case", id=case.id),
            _digest(case.model_dump(mode="json")),
            cast(str, body["result_changeset_digest"]),
            (),
            body,
        )

    def _plan_material(
        self,
        plan_id: str,
        actor: str,
        at: datetime,
        selected: tuple[str, ...],
        authority: _Authority,
    ) -> _DecisionMaterial:
        if selected or actor not in authority.policy.approvers:
            raise ValueError("approval actor unavailable")
        workflow = write_workflow(self._runtime)
        try:
            if workflow.actor != actor or workflow.policy != authority.policy:
                raise ValueError("write authority changed")
            plan = workflow.preview(plan_id)
            configured = workflow._configuration_for(plan)
            approval = approve_plan(
                plan,
                configured.config.binding,
                actor=actor,
                now=at,
                expires_in=_APPROVAL_LIFETIME,
                confirmation=f"approve {plan.id}",
                interactive=True,
                authorized_approvers=authority.policy.approvers,
                identity_aliases={
                    principal: frozenset(aliases)
                    for principal, aliases in authority.policy.identities.items()
                },
            )
            body: dict[str, object] = {
                "kind": "external_write",
                "plan": plan.model_dump(mode="json"),
                "plan_hash": plan.canonical_hash,
                "result_approval_id": approval.id,
                "result_approval_digest": approval.canonical_hash,
            }
            return _DecisionMaterial(
                DecisionSubject(kind="write-plan", id=plan.id),
                plan.canonical_hash,
                approval.canonical_hash,
                (),
                body,
            )
        finally:
            workflow.close()

    def _answer_material(
        self,
        answer_id: str,
        actor: str,
        at: datetime,
        selected: tuple[str, ...],
        authority: _Authority,
    ) -> _DecisionMaterial:
        pending = self._pending_answers.get(answer_id)
        if pending is None or selected or pending.actor != actor or pending.answered_at != at:
            raise ValueError("answer preview unavailable")
        coordinator = self._clarification_coordinator(authority)
        record = coordinator.preview_answer(
            pending.session_id,
            actor=actor,
            question_id=pending.question_id,
            answer=pending.answer,
            answered_at=at,
            acl=pending.acl,
            principals=self._principals(authority, actor),
        )
        result_digest = _digest(record.model_dump(mode="json"))
        if (
            record.id != pending.evidence_id
            or pending.result_digest != result_digest
            or pending.subject_digest != clarification_digest(pending.answer)
        ):
            raise ValueError("answer preview changed")
        body: dict[str, object] = {
            "kind": "clarification_answer",
            "session_id": pending.session_id,
            "question_id": pending.question_id,
            "answer_digest": pending.subject_digest,
            "result_evidence_id": record.id,
            "result_evidence_digest": result_digest,
        }
        return _DecisionMaterial(
            DecisionSubject(kind="answer", id=answer_id),
            pending.subject_digest,
            result_digest,
            (),
            body,
        )

    def _material(
        self,
        action: DecisionAction,
        subject_id: str,
        actor: str,
        at: datetime,
        selected: tuple[str, ...],
        authority: _Authority,
    ) -> _DecisionMaterial:
        if action is DecisionAction.CONFIRM_BASELINE:
            return self._baseline_material(subject_id, actor, at, selected, authority)
        if action is DecisionAction.CONFIRM_PROPOSAL:
            return self._clarification_material(subject_id, actor, at, selected, authority)
        if action is DecisionAction.ANSWER_CLARIFICATION:
            return self._answer_material(subject_id, actor, at, selected, authority)
        if action is DecisionAction.RESOLVE_CONFLICT:
            return self._case_material(subject_id, actor, at, selected, authority)
        if action is DecisionAction.APPROVE_EXTERNAL_WRITE:
            return self._plan_material(subject_id, actor, at, selected, authority)
        raise ValueError("unsupported control plane decision")

    def _payload(
        self,
        action: DecisionAction,
        subject_id: str,
        authority: _Authority,
        *,
        selected: tuple[str, ...] = (),
        issued_at: datetime | None = None,
        challenge: str | None = None,
    ) -> tuple[HumanDecisionPayload, _DecisionMaterial]:
        at = self._now() if issued_at is None else issued_at
        material = self._material(
            action,
            subject_id,
            authority.config.local_actor,
            at,
            tuple(sorted(selected)),
            authority,
        )
        payload = HumanDecisionPayload(
            project_id=authority.config.project_id,
            repository_id=self.repository_id,
            actor=authority.config.local_actor,
            action=action,
            graph_version=self._runtime.graph_store.load().version,
            parent_bundle_digest=_parent_digest(
                authority.snapshot.content,
                authority.membership_digest,
            ),
            subject=material.subject,
            subject_digest=material.subject_digest,
            selected_node_ids=material.selected_node_ids,
            result_digest=material.result_digest,
            challenge=self._nonce() if challenge is None else challenge,
            issued_at=at,
            expires_at=at + _DECISION_LIFETIME,
        )
        return payload, material

    @staticmethod
    def _preview_result(
        payload: HumanDecisionPayload,
        material: _DecisionMaterial,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "preview_digest": _digest(material.preview),
            "preview": material.preview,
            "payload": payload.model_dump(mode="json"),
        }

    def proposal_preview(self, proposal_id: str) -> dict[str, object]:
        """Reconstruct a proposal, case, or write-plan preview from held descriptors."""
        authority: _Authority | None = None
        try:
            self._purge_pending_answers(self._now())
            authority = self._authority()
            if proposal_id.startswith("proposal:"):
                proposal = self._runtime.intent_proposals.get(proposal_id)
                action = (
                    DecisionAction.CONFIRM_PROPOSAL
                    if isinstance(proposal, ClarificationIntentProposal)
                    else DecisionAction.CONFIRM_BASELINE
                )
            elif proposal_id.startswith("case:"):
                action = DecisionAction.RESOLVE_CONFLICT
            elif proposal_id.startswith("write-plan:"):
                action = DecisionAction.APPROVE_EXTERNAL_WRITE
            else:
                raise ValueError("preview subject unavailable")
            payload, material = self._payload(action, proposal_id, authority)
            if not self._authority_matches(authority):
                raise ValueError("preview authority changed")
            return self._preview_result(payload, material)
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            raise ControlPlaneError() from None
        finally:
            proposal_id = ""
            if authority is not None:
                authority.close()

    def _clarification_coordinator(self, authority: _Authority) -> ClarificationCoordinator:
        return ClarificationCoordinator(
            graph_store=self._runtime.graph_store,
            evidence_store=self._runtime.evidence_store,
            proposal_store=self._runtime.intent_proposals,
            transactions=self._runtime.transactions,
            config=authority.config,
            capture=ConversationCapture(self._runtime.evidence_store),
            authority_files=authority.files,
            authority_preimages=authority.preimages,
            authority_read_policies=authority.policies,
            authority_membership_digest=authority.membership_digest,
            authority_membership_resolver=self._membership_now,
        )

    def answer_preview(self, session_id: str, question_id: str, answer: str) -> dict[str, object]:
        """Hold one private answer and return only its exact signed digest projection."""
        authority: _Authority | None = None
        result: dict[str, object] | None = None
        signal: BaseException | None = None
        answer_id: str | None = None
        coordinator: ClarificationCoordinator | None = None
        record: EvidenceRecord | None = None
        pending: _PendingAnswer | None = None
        existing: _PendingAnswer | None = None
        payload: HumanDecisionPayload | None = None
        material: _DecisionMaterial | None = None
        inserted = False
        try:
            authority = self._authority()
            at = self._now()
            self._purge_pending_answers(at)
            actor = authority.config.local_actor
            principals = self._principals(authority, actor)
            acl = tuple(sorted(principals))
            coordinator = self._clarification_coordinator(authority)
            record = coordinator.preview_answer(
                session_id,
                actor=actor,
                question_id=question_id,
                answer=answer,
                answered_at=at,
                acl=acl,
                principals=principals,
            )
            subject_digest = clarification_digest(answer)
            result_digest = _digest(record.model_dump(mode="json"))
            answer_id = f"answer:{_digest({'session': session_id, 'question': question_id, 'answer': subject_digest, 'at': at.isoformat()}).removeprefix('sha256:')}"
            pending = _PendingAnswer(
                session_id,
                question_id,
                answer,
                actor,
                at,
                acl,
                record.id,
                subject_digest,
                result_digest,
                at + _DECISION_LIFETIME,
            )
            existing = self._pending_answers.get(answer_id)
            if existing is not None and existing != pending:
                raise ValueError("answer preview collision")
            if existing is None:
                if len(self._pending_answers) >= _MAX_PENDING_ANSWERS:
                    raise ValueError("answer preview capacity unavailable")
                self._pending_answers[answer_id] = pending
                inserted = True
            payload, material = self._payload(
                DecisionAction.ANSWER_CLARIFICATION,
                answer_id,
                authority,
                issued_at=at,
            )
            if not self._authority_matches(authority):
                raise ValueError("answer authority changed")
            result = self._preview_result(payload, material)
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            result = None
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            session_id = question_id = answer = ""
            if result is None and inserted and answer_id is not None:
                self._pending_answers.pop(answer_id, None)
            answer_id = None
            coordinator = None
            record = None
            pending = None
            existing = None
            payload = None
            material = None
            inserted = False
            if authority is not None:
                authority.close()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if result is None:
            raise ControlPlaneError() from None
        return result

    def discard_answer_preview(self, answer_id: str) -> dict[str, object]:
        """Forget one private preview without creating or requiring human authority."""
        try:
            if type(answer_id) is not str or _ANSWER_ID.fullmatch(answer_id) is None:
                raise ValueError("invalid answer preview")
            self._purge_pending_answers(self._now())
            self._pending_answers.pop(answer_id, None)
            return {
                "schema_version": 1,
                "status": "discarded",
                "answer_id": answer_id,
            }
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            raise ControlPlaneError() from None
        finally:
            answer_id = ""

    def registration_options(self) -> bytes:
        """Issue enrollment options for the configured actor at server-owned time."""
        authority: _Authority | None = None
        snapshot: LocalTransactionSnapshot | None = None
        result: bytes | None = None
        signal: BaseException | None = None
        try:
            authority = self._authority()
            with self._runtime.transactions.transaction(
                rollback_base_exceptions=True,
                extras=authority.files,
                extra_read_policies=authority.policies,
            ):
                snapshot = self._runtime.transactions.snapshot(
                    authority.files,
                    extra_read_policies=authority.policies,
                )
                if self._membership_now() != authority.membership_digest or any(
                    snapshot.content.get(name) != value
                    for name, value in authority.preimages.items()
                ):
                    raise ValueError("registration authority changed")
                authority.snapshot = snapshot
                result = self._webauthn.registration_options(
                    authority.config.local_actor,
                    self._origin,
                    self._now(),
                )
                if self._membership_now() != authority.membership_digest:
                    raise ValueError("registration authority changed")
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            result = None
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            if authority is not None:
                authority.close()
            authority = None
            snapshot = None
        if signal is not None:
            result = None
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if result is None:
            raise ControlPlaneError() from None
        return result

    def register(self, response: bytes) -> CredentialRecord:
        """Enroll one credential for the configured actor at server-owned time."""
        authority: _Authority | None = None
        snapshot: LocalTransactionSnapshot | None = None
        result: CredentialRecord | None = None
        signal: BaseException | None = None
        try:
            if type(response) is not bytes:
                raise TypeError("invalid registration response")
            authority = self._authority()
            with self._runtime.transactions.transaction(
                rollback_base_exceptions=True,
                extras=authority.files,
                extra_read_policies=authority.policies,
            ):
                snapshot = self._runtime.transactions.snapshot(
                    authority.files,
                    extra_read_policies=authority.policies,
                )
                if self._membership_now() != authority.membership_digest or any(
                    snapshot.content.get(name) != value
                    for name, value in authority.preimages.items()
                ):
                    raise ValueError("registration authority changed")
                authority.snapshot = snapshot
                registered = self._webauthn.register(
                    response,
                    authority.config.local_actor,
                    self._origin,
                    self._now(),
                )
                if self._membership_now() != authority.membership_digest:
                    raise ValueError("registration authority changed")
                result = CredentialRecord.model_validate_json(registered.model_dump_json())
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            result = None
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            caught.__cause__ = None
            caught.__context__ = None
            signal = caught
        finally:
            response = b""
            if "registered" in locals():
                registered = cast(CredentialRecord, None)
            if authority is not None:
                authority.close()
            authority = None
            snapshot = None
        if signal is not None:
            result = None
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if result is None:
            raise ControlPlaneError() from None
        return result

    def _expected_payload(
        self,
        payload: HumanDecisionPayload,
        authority: _Authority,
    ) -> tuple[HumanDecisionPayload, _DecisionMaterial]:
        if payload.expires_at != payload.issued_at + _DECISION_LIFETIME:
            raise ValueError("decision lifetime changed")
        material = self._material(
            payload.action,
            payload.subject.id,
            payload.actor,
            payload.issued_at,
            payload.selected_node_ids,
            authority,
        )
        expected = HumanDecisionPayload(
            project_id=authority.config.project_id,
            repository_id=self.repository_id,
            actor=payload.actor,
            action=payload.action,
            graph_version=self._runtime.graph_store.load().version,
            parent_bundle_digest=_parent_digest(
                authority.snapshot.content,
                authority.membership_digest,
            ),
            subject=material.subject,
            subject_digest=material.subject_digest,
            selected_node_ids=material.selected_node_ids,
            result_digest=material.result_digest,
            challenge=payload.challenge,
            issued_at=payload.issued_at,
            expires_at=payload.expires_at,
        )
        if expected != payload:
            raise ValueError("decision preview changed")
        return expected, material

    def decision_options(self, payload: HumanDecisionPayload) -> bytes:
        """Issue WebAuthn options only for a freshly reconstructed exact preview."""
        authority: _Authority | None = None
        result: bytes | None = None
        signal: BaseException | None = None
        try:
            self._purge_pending_answers(self._now())
            if not isinstance(payload, HumanDecisionPayload):
                raise TypeError("invalid decision payload")
            payload = HumanDecisionPayload.model_validate_json(payload.model_dump_json())
            authority = self._authority()
            self._expected_payload(payload, authority)
            if not self._authority_matches(authority):
                raise ValueError("decision authority changed")
            result = self._webauthn.authentication_options(payload, self._origin, self._now())
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            result = None
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            payload = cast(HumanDecisionPayload, None)
            if authority is not None:
                authority.close()
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if result is None:
            raise ControlPlaneError() from None
        return result

    def _confirmation_service(self, authority: _Authority) -> ProposalConfirmationService:
        bindings = {
            name: file
            for name, file in authority.files.items()
            if name.startswith("authority_binding_")
        }
        names = {"authority_config", "authority_policy", *bindings}
        return ProposalConfirmationService(
            graph_store=self._runtime.graph_store,
            evidence_store=self._runtime.evidence_store,
            case_store=self._runtime.case_store,
            proposal_store=self._runtime.intent_proposals,
            changeset_executor=LocalChangeSetExecutor(
                self._runtime.graph_store,
                self._runtime.case_store,
                self._runtime.transactions,
            ),
            transactions=self._runtime.transactions,
            config_file=authority.files["authority_config"],
            policy_file=authority.files["authority_policy"],
            binding_files=bindings,
            authority_read_policies={name: authority.policies[name] for name in names},
            authority_preimages={name: authority.preimages[name] for name in names},
            authority_membership_digest=authority.membership_digest,
            authority_membership_resolver=self._membership_now,
        )

    def _apply_answer(
        self,
        payload: HumanDecisionPayload,
        material: _DecisionMaterial,
        authority: _Authority,
    ) -> dict[str, object]:
        pending = self._pending_answers.get(payload.subject.id)
        if pending is None:
            raise ValueError("answer unavailable")
        coordinator = self._clarification_coordinator(authority)
        session = coordinator.answer(
            pending.session_id,
            actor=payload.actor,
            question_id=pending.question_id,
            answer=pending.answer,
            answered_at=payload.issued_at,
            acl=pending.acl,
            principals=self._principals(authority, payload.actor),
        )
        self._runtime.transactions._fault("target:evidence")
        answer_record = next(
            (item for item in session.answers if item.evidence_ref == pending.evidence_id),
            None,
        )
        if (
            answer_record is None
            or answer_record.answer_digest != material.subject_digest
            or _digest(
                self._runtime.evidence_store.get(answer_record.evidence_ref).model_dump(mode="json")
            )
            != material.result_digest
        ):
            raise ValueError("answer result changed")
        return {
            "schema_version": 1,
            "status": session.status,
            "session_id": session.id,
            "evidence_ref": answer_record.evidence_ref,
        }

    def _apply_baseline(
        self,
        payload: HumanDecisionPayload,
        material: _DecisionMaterial,
    ) -> dict[str, object]:
        graph = confirm_proposal(
            self._runtime,
            proposal_id=payload.subject.id,
            proposal_digest=payload.subject_digest,
            selected_node_ids=payload.selected_node_ids,
            actor=payload.actor,
            at=payload.issued_at,
        )
        history = self._runtime.graph_store.history(payload.selected_node_ids[0])
        if not history or _digest(history[-1].model_dump(mode="json")) != material.result_digest:
            raise ValueError("baseline result changed")
        return {
            "schema_version": 1,
            "status": "activated",
            "proposal_id": payload.subject.id,
            "graph_version": graph.version,
            "changeset_id": history[-1].id,
        }

    def _apply_proposal(
        self,
        payload: HumanDecisionPayload,
        material: _DecisionMaterial,
        authority: _Authority,
    ) -> dict[str, object]:
        service = self._confirmation_service(authority)
        try:
            result = service.confirm(
                payload.subject.id,
                proposal_digest=payload.subject_digest,
                actor=payload.actor,
                at=payload.issued_at,
                selected_node_ids=payload.selected_node_ids,
            )
        finally:
            service.close()
        if result.status is not ProposalConfirmationStatus.APPLIED or result.decision_id is None:
            raise ValueError("proposal was not applied")
        decision = self._runtime.intent_proposals.decision_for(payload.subject.id)
        if decision is None:
            raise ValueError("proposal decision unavailable")
        history = self._runtime.graph_store.history(payload.selected_node_ids[0])
        if not history or _digest(history[-1].model_dump(mode="json")) != material.result_digest:
            raise ValueError("proposal result changed")
        return {
            "schema_version": 1,
            "status": result.status.value,
            "proposal_id": result.proposal_id,
            "graph_version": result.graph_version,
            "decision_id": result.decision_id,
            "changeset_id": history[-1].id,
        }

    def _apply_case(
        self,
        payload: HumanDecisionPayload,
        material: _DecisionMaterial,
        authority: _Authority,
    ) -> dict[str, object]:
        action = ResolutionAction.UPDATE_REQUIREMENT
        resolution = LocalResolutionService(
            self._runtime.graph_store,
            self._runtime.evidence_store,
            self._runtime.case_store,
            payload.actor,
            transactions=self._runtime.transactions,
            principals=self._principals(authority, payload.actor),
        )
        approval_hash = cast(str, material.preview["approval_hash"])
        case, changeset, pending = resolution.resolve(
            payload.subject.id,
            action,
            approve=approval_hash,
            at=payload.issued_at,
        )
        if (
            pending is not None
            or changeset is None
            or case.status is not ReconciliationStatus.RESOLVED
            or _digest(changeset.model_dump(mode="json")) != material.result_digest
        ):
            raise ValueError("case result changed")
        return {
            "schema_version": 1,
            "status": "resolved",
            "case_id": case.id,
            "action": action.value,
            "graph_version": self._runtime.graph_store.load().version,
            "changeset_id": changeset.id,
        }

    def _apply_plan(
        self,
        payload: HumanDecisionPayload,
        material: _DecisionMaterial,
        authority: _Authority,
    ) -> dict[str, object]:
        workflow = write_workflow(self._runtime)
        try:
            plan = workflow.preview(payload.subject.id)
            configured = workflow._configuration_for(plan)
            approval = approve_plan(
                plan,
                configured.config.binding,
                actor=payload.actor,
                now=payload.issued_at,
                expires_in=_APPROVAL_LIFETIME,
                confirmation=f"approve {plan.id}",
                interactive=True,
                authorized_approvers=authority.policy.approvers,
                identity_aliases={
                    principal: frozenset(aliases)
                    for principal, aliases in authority.policy.identities.items()
                },
            )
            if approval.canonical_hash != material.result_digest:
                raise ValueError("approval result changed")
            if not workflow.approvals.put(approval):
                raise ValueError("approval replay unavailable")
            self._runtime.transactions._fault("target:approvals")
            return {
                "schema_version": 1,
                "status": "approved",
                "plan_id": plan.id,
                "approval_id": approval.id,
                "plan_hash": approval.plan_hash,
            }
        finally:
            workflow.close()

    def _dispatch(
        self,
        payload: HumanDecisionPayload,
        material: _DecisionMaterial,
        authority: _Authority,
    ) -> dict[str, object]:
        if payload.action is DecisionAction.CONFIRM_BASELINE:
            return self._apply_baseline(payload, material)
        if payload.action is DecisionAction.ANSWER_CLARIFICATION:
            return self._apply_answer(payload, material, authority)
        if payload.action is DecisionAction.CONFIRM_PROPOSAL:
            return self._apply_proposal(payload, material, authority)
        if payload.action is DecisionAction.RESOLVE_CONFLICT:
            return self._apply_case(payload, material, authority)
        if payload.action is DecisionAction.APPROVE_EXTERNAL_WRITE:
            return self._apply_plan(payload, material, authority)
        raise ValueError("unsupported control plane decision")

    def _apply_decision(
        self,
        assertion: bytes,
        payload: HumanDecisionPayload,
    ) -> dict[str, object]:
        authority = self._authority()
        verified: VerifiedHumanDecision | None = None
        result: dict[str, object] | None = None
        try:
            with self._runtime.transactions.transaction(
                rollback_base_exceptions=True,
                extras=authority.files,
                extra_read_policies=authority.policies,
            ):
                snapshot = self._runtime.transactions.snapshot(
                    authority.files,
                    extra_read_policies=authority.policies,
                )
                if self._membership_now() != authority.membership_digest or any(
                    snapshot.content.get(name) != value
                    for name, value in authority.preimages.items()
                ):
                    raise ValueError("decision authority changed")
                authority.snapshot = snapshot
                expected, material = self._expected_payload(payload, authority)
                verified = self._webauthn.verify(
                    assertion,
                    expected,
                    self._origin,
                    self._now(),
                )
                if (
                    type(verified) is not VerifiedHumanDecision
                    or verified.payload != expected
                    or verified.credential.actor != expected.actor
                    or verified.credential.project_id != expected.project_id
                    or verified.credential.repository_id != expected.repository_id
                ):
                    raise ValueError("verified decision changed")
                result = self._dispatch(expected, material, authority)
                if self._membership_now() != authority.membership_digest:
                    raise ValueError("decision authority changed")
            if expected.action is DecisionAction.ANSWER_CLARIFICATION:
                self._pending_answers.pop(expected.subject.id, None)
            if result is None:
                raise ValueError("decision result unavailable")
            return result
        finally:
            assertion = b""
            payload = cast(HumanDecisionPayload, None)
            verified = None
            result = None
            authority.close()

    def apply_decision(
        self,
        assertion: bytes,
        payload: HumanDecisionPayload,
    ) -> dict[str, object]:
        """Verify and atomically apply one exact authoritative human decision."""
        result: dict[str, object] | None = None
        signal: BaseException | None = None
        failed = False
        try:
            self._purge_pending_answers(self._now())
            if type(assertion) is not bytes or not isinstance(payload, HumanDecisionPayload):
                raise TypeError("invalid decision assertion")
            detached = HumanDecisionPayload.model_validate_json(payload.model_dump_json())
            result = self._apply_decision(assertion, detached)
        except Exception:  # noqa: BLE001 - fixed opaque authority boundary
            failed = True
        except BaseException as caught:  # noqa: BLE001 - preserve cancellation identity
            caught.__traceback__ = None
            signal = caught
        finally:
            assertion = b""
            payload = cast(HumanDecisionPayload, None)
            if "detached" in locals():
                detached = cast(HumanDecisionPayload, None)
        if signal is not None:
            caught_signal = signal
            signal = None
            raise caught_signal.with_traceback(None)
        if failed or result is None:
            raise ControlPlaneError() from None
        return result

    def close(self) -> None:
        """Release descriptors owned by this service while leaving Runtime ownership intact."""
        self._stop_development_observation()
        if self._dev_observer is not None:
            self._dev_observer.close()
            self._dev_observer = None
        self._config_file.close()
        self._policy_file.close()
        self._plans_file.close()
        self._connectors_directory.close()
        self._approvals_directory.close()
        self._pending_answers.clear()
        self._pending_team_enrollment = None
        self._team_recipient = None
        self._team_key_store = None
        self._github_setup_bridge = None
        self._shared_state_status = SharedStateRestoreStatus.NOT_REQUIRED


__all__ = ["ControlPlaneError", "ControlPlaneService"]
