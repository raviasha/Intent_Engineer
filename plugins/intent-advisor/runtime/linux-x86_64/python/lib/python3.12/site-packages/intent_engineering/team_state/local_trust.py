"""Bounded public team enrollment for reconstructing local OS-keyring trust."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from pydantic import ConfigDict, Field, field_validator, model_validator

from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.core.models import ProjectConfig
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.jsonl.strict import loads_strict_object
from intent_engineering.storage.secure import SecureDirectory, SecureFile
from intent_engineering.storage.transaction import LocalTransaction, LocalTransactionCoordinator
from intent_engineering.team_state.enrollment import JoinResponseV2, TeamInviteV2
from intent_engineering.team_state.keys import (
    DeviceEnrollmentBinding,
    GitHubIdentity,
    KeyringRecipientKeyStore,
    MigratedDeviceKeyStore,
    RecipientEnrollmentBinding,
    RecipientKeyStore,
    _key_id,
)
from intent_engineering.team_state.models import (
    CANONICAL_STATE_PATHS,
    RecipientRecord,
    TeamRootTrustV2,
)
from intent_engineering.team_state.restore import (
    TRUST_ENVIRONMENT_VARIABLE,
    EnvironmentTrustProvider,
    RecipientKeyStoreTrustProvider,
    SharedStateTrust,
    TrustedSigningKey,
    TrustProvider,
)

if TYPE_CHECKING:
    from intent_engineering.cli.runtime import Runtime
    from intent_engineering.team_state.restore import VerifiedReleaseV2
    from intent_engineering.team_state.signing import ExistingRecipientDeviceSigner

MAX_LOCAL_TRUST_BYTES = 32 * 1024
MAX_PENDING_JOIN_BYTES = 128 * 1024
_FILENAME = "team-trust.json"
_PENDING_FILENAME = "team-join-pending.json"
_ACTIVATION_JOURNAL_FILENAME = "join-activation.json"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_ROOT_ID = re.compile(r"^root:sha256:[0-9a-f]{64}$")
_MEMBER_ID = re.compile(r"^member:sha256:[0-9a-f]{64}$")
_CERTIFICATE_ID = re.compile(r"^certificate:sha256:[0-9a-f]{64}$")
_RECIPIENT_ID = re.compile(r"^recipient:sha256:[0-9a-f]{64}$")
_SIGNATURE_ID = re.compile(r"^signer:sha256:[0-9a-f]{64}$")


class LocalTrustError(ValueError):
    """Fixed public failure without provider or document details."""

    def __init__(self) -> None:
        super().__init__("local team trust unavailable")


def _failure(caught: BaseException) -> NoReturn:
    caught_traceback = caught.__traceback__
    if caught_traceback is not None:
        traceback.clear_frames(caught_traceback)
    caught_traceback = None
    caught.args = ()
    caught.__dict__.clear()
    caught.__traceback__ = None
    caught.__cause__ = None
    caught.__context__ = None
    if isinstance(caught, Exception):
        raise LocalTrustError() from None
    raise caught.with_traceback(None) from None


class LocalTrustConfig(StrictModel):
    """Public reviewed identity and keys; no private key is serializable here."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    project_id: str
    repository_id: str
    recipient_key_id: str
    recipient: RecipientRecord
    signing_public_keys: dict[str, str] = Field(min_length=1, max_length=64)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid local trust version")
        return value

    def enrollment_binding(self) -> RecipientEnrollmentBinding:
        recipient = self.recipient
        return RecipientEnrollmentBinding(
            project_id=recipient.project_id,
            repository_id=recipient.repository_id,
            actor=recipient.actor,
            github_identity=GitHubIdentity(
                account_id=recipient.github_account_id,
                login=recipient.github_login,
            ),
            webauthn_credential_id=recipient.webauthn_credential_id,
            webauthn_credential_public_key=recipient.webauthn_credential_public_key,
            enrolled_at=recipient.enrolled_at,
        )

    def trusted_signing_keys(self) -> tuple[TrustedSigningKey, ...]:
        keys = []
        for signature_id, encoded in sorted(self.signing_public_keys.items()):
            if len(encoded) != 44:
                raise ValueError("invalid public signing key")
            public = base64.b64decode(encoded, validate=True)
            if base64.b64encode(public).decode("ascii") != encoded:
                raise ValueError("invalid public signing key")
            keys.append(TrustedSigningKey(signature_id, public))
        return tuple(keys)

    @model_validator(mode="after")
    def validate_binding(self) -> LocalTrustConfig:
        recipient = RecipientRecord.model_validate(self.recipient.model_dump(mode="python"))
        binding = self.enrollment_binding()
        if (
            self.project_id != recipient.project_id
            or self.repository_id != recipient.repository_id
            or self.recipient_key_id != recipient.key_id
            or self.recipient_key_id != _key_id(binding)
        ):
            raise ValueError("invalid local trust binding")
        self.trusted_signing_keys()
        return self


class LocalTrustConfigV2(StrictModel):
    """Public stable-root trust activated only with an installed v2 release."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    project_id: str
    repository_id: str
    root: TeamRootTrustV2
    member_id: str = Field(pattern=_MEMBER_ID.pattern)
    device_certificate_id: str = Field(pattern=_CERTIFICATE_ID.pattern)
    recipient_key_id: str = Field(pattern=_RECIPIENT_ID.pattern)
    signature_id: str = Field(pattern=_SIGNATURE_ID.pattern)
    accepted_authority_digest: str = Field(pattern=_SHA256.pattern)
    accepted_authority_sequence: int = Field(ge=1, le=2**63 - 1)
    accepted_bundle_digest: str = Field(pattern=_SHA256.pattern)
    device_binding: DeviceEnrollmentBinding | None = None
    migration_recipient_binding: RecipientEnrollmentBinding | None = None

    @field_validator("schema_version", "accepted_authority_sequence", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid version two local trust integer")
        return value

    @model_validator(mode="after")
    def require_scope(self) -> LocalTrustConfigV2:
        if type(self.root) is not TeamRootTrustV2:
            raise ValueError("invalid version two root trust")
        root = self.root
        if root.project_id != self.project_id or root.repository_id != self.repository_id:
            raise ValueError("version two local trust scope changed")
        if self.device_binding is not None and (
            self.device_binding.project_id != self.project_id
            or self.device_binding.repository_id != self.repository_id
        ):
            raise ValueError("version two device binding changed")
        legacy = self.migration_recipient_binding
        if legacy is not None:
            from intent_engineering.team_state.authority import derive_member_id

            if (
                self.device_binding is not None
                or legacy.project_id != self.project_id
                or legacy.repository_id != self.repository_id
                or derive_member_id(
                    self.project_id, self.repository_id, int(legacy.github_identity.account_id)
                )
                != self.member_id
            ):
                raise ValueError("version two migration binding changed")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self)


def migrated_device_store(
    trust: LocalTrustConfigV2,
    release: VerifiedReleaseV2,
    *,
    recipient_store: RecipientKeyStore | None = None,
    signer: ExistingRecipientDeviceSigner | None = None,
) -> MigratedDeviceKeyStore:
    """Derive signing authority only from the authenticated active registry."""
    from intent_engineering.team_state.keys import DevicePublicMaterial
    from intent_engineering.team_state.publication import authority_from_verified_state
    from intent_engineering.team_state.signing import KeyringExistingRecipientDeviceSigner

    authority = authority_from_verified_state(release, trust)
    legacy = trust.migration_recipient_binding
    if legacy is None:
        raise LocalTrustError()
    member = next(m for m in authority.registry.members if m.member_id == trust.member_id)
    certificate = next(
        c
        for c in authority.registry.device_certificates
        if c.certificate_id == trust.device_certificate_id
    )
    claims = certificate.claims
    binding = DeviceEnrollmentBinding(
        project_id=trust.project_id,
        repository_id=trust.repository_id,
        actor=member.actor,
        github_account_id=member.github_account_id,
        github_login=member.github_login,
        device_id=claims.device_id,
    )
    material = DevicePublicMaterial(
        claims.device_id,
        claims.recipient_key_id,
        base64.urlsafe_b64decode(
            claims.recipient_public_key + "=" * (-len(claims.recipient_public_key) % 4)
        ),
        claims.signature_id,
        base64.urlsafe_b64decode(
            claims.signing_public_key + "=" * (-len(claims.signing_public_key) % 4)
        ),
    )
    return MigratedDeviceKeyStore(
        binding,
        legacy,
        material,
        recipient_store=recipient_store or KeyringRecipientKeyStore(legacy),
        signer=signer or KeyringExistingRecipientDeviceSigner(binding),
    )


def trust_from_verified_migration(
    legacy: LocalTrustConfig, release: VerifiedReleaseV2
) -> LocalTrustConfigV2:
    """Preserve the exact legacy keyring binding after verification, never key bytes."""
    from intent_engineering.team_state.authority import derive_member_id
    from intent_engineering.team_state.restore import VerifiedReleaseV2

    if (
        type(legacy) is not LocalTrustConfig
        or type(release) is not VerifiedReleaseV2
        or release.manifest.migration is None
    ):
        raise LocalTrustError()
    identity = legacy.enrollment_binding().github_identity
    member_id = derive_member_id(legacy.project_id, legacy.repository_id, int(identity.account_id))
    members = [m for m in release.authority.members if m.member_id == member_id]
    certificates = [
        c for c in release.authority.device_certificates if c.claims.member_id == member_id
    ]
    if len(members) != 1 or len(certificates) != 1:
        raise LocalTrustError()
    member, certificate = members[0], certificates[0]
    if (
        release.manifest.project_id != legacy.project_id
        or release.manifest.repository_id != legacy.repository_id
        or member.actor != f"github:{identity.account_id}"
        or member.github_login != identity.login
        or member.status != "active"
        or certificate.certificate_id not in member.device_certificate_ids
        or certificate.claims.recipient_public_key != legacy.recipient.public_key
    ):
        raise LocalTrustError()
    return LocalTrustConfigV2(
        project_id=legacy.project_id,
        repository_id=legacy.repository_id,
        root=release.authority.root,
        member_id=member_id,
        device_certificate_id=certificate.certificate_id,
        recipient_key_id=certificate.claims.recipient_key_id,
        signature_id=certificate.claims.signature_id,
        accepted_authority_digest=release.manifest.authority_digest,
        accepted_authority_sequence=release.authority.sequence,
        accepted_bundle_digest=release.manifest.bundle_digest,
        migration_recipient_binding=legacy.enrollment_binding(),
    )


@contextmanager
def _migration_activation_transaction(
    root: Path,
) -> Iterator[tuple[LocalTransactionCoordinator, dict[str, str]]]:
    with ExitStack() as stack:
        workspace = SecureDirectory.open(root / ".intent")
        stack.callback(workspace.close)
        _require_owner_directory(workspace, harden=True)
        paths = {path: f"state_{i}" for i, path in enumerate(CANONICAL_STATE_PATHS)}
        paths.update(
            {
                "cache/shared-state.json": "shared_state",
                "team-trust.json": "team_trust",
                "team-join-pending.json": "pending_join_trust",
            }
        )
        targets = {name: workspace.file(path) for path, name in paths.items()}
        for target in targets.values():
            stack.callback(target.close)
        journal = workspace.file("team-migration-activation.json")
        stack.callback(journal.close)
        coordinator = LocalTransactionCoordinator(
            journal, targets, max_recovery_bytes=24 * 1024 * 1024
        )
        stack.callback(coordinator.close)
        try:
            yield coordinator, paths
        finally:
            if targets["team_trust"].exists():
                _harden_owner_target(targets["team_trust"])


def activate_installed_migration(
    root: Path,
    legacy: LocalTrustConfig,
    release: VerifiedReleaseV2,
    *,
    merged_state_commit: str,
    prior_state_commit: str | None = None,
) -> None:
    """Atomically convert public trust only after the exact verified migration is installed."""
    try:
        if release.commit != merged_state_commit or release.snapshot is None:
            raise LocalTrustError()
        trust = trust_from_verified_migration(legacy, release)
        expected = {f.path: f.content for f in release.snapshot.files}
        marker = json.dumps(
            {
                "schema_version": 1,
                "bundle_digest": release.manifest.bundle_digest,
                "graph_version": release.manifest.graph_version,
                "ref_commit": release.commit,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        prior_marker = (
            None
            if prior_state_commit is None or release.manifest.parent_bundle_digest is None
            else json.dumps(
                {
                    "schema_version": 1,
                    "bundle_digest": release.manifest.parent_bundle_digest,
                    "graph_version": release.manifest.graph_version,
                    "ref_commit": prior_state_commit,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        with (
            _migration_activation_transaction(root) as (coordinator, paths),
            coordinator.transaction() as transaction,
        ):
            for path, content in expected.items():
                if (transaction.read_optional(paths[path]) or b"") != content:
                    raise LocalTrustError()
            current_marker = transaction.read_optional("shared_state")
            if current_marker == prior_marker:
                transaction.write("shared_state", marker)
            elif current_marker != marker:
                raise LocalTrustError()
            if transaction.read_optional("pending_join_trust") not in {None, b""}:
                raise LocalTrustError()
            before = transaction.read_optional("team_trust")
            if before == trust.canonical_bytes():
                return
            if before is None:
                raise LocalTrustError()
            loads_strict_object(before.decode("utf-8"))
            if LocalTrustConfig.model_validate_json(before) != legacy:
                raise LocalTrustError()
            transaction.write("team_trust", trust.canonical_bytes())
    except BaseException as error:  # noqa: BLE001 - fixed public activation boundary
        _failure(error)


class PendingJoinTrustV2(StrictModel):
    """Public-only local join receipt; never sufficient publication authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    phase: Literal["response-ready", "awaiting-merge"]
    invite: TeamInviteV2
    response: JoinResponseV2
    local_recipient_key_id: str = Field(pattern=_RECIPIENT_ID.pattern)
    local_signature_id: str = Field(pattern=_SIGNATURE_ID.pattern)
    expected_root_key_id: str = Field(pattern=_ROOT_ID.pattern)
    expected_authority_before_digest: str = Field(pattern=_SHA256.pattern)
    external_write_attempted: bool

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid pending join version")
        return value

    @field_validator("external_write_attempted", mode="before")
    @classmethod
    def require_boolean(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("invalid pending join write marker")
        return value

    @model_validator(mode="after")
    def require_public_bindings(self) -> PendingJoinTrustV2:
        if type(self.invite) is not TeamInviteV2 or type(self.response) is not JoinResponseV2:
            raise ValueError("invalid pending join receipt")
        invite = self.invite
        response = self.response
        if (
            response.invite_id != invite.invite_id
            or response.project_id != invite.project_id
            or response.repository_id != invite.repository_id
            or self.local_recipient_key_id != response.recipient_key_id
            or self.local_signature_id != response.signature_id
            or self.expected_root_key_id != invite.root.root_key_id
            or self.expected_authority_before_digest != invite.authority_digest
            or (self.phase == "response-ready" and self.external_write_attempted)
        ):
            raise ValueError("pending join binding changed")
        return self

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self)


class StateInstallTransaction:
    """One journaled mutation spanning canonical state and public trust activation."""

    def __init__(
        self,
        journal: SecureFile,
        state_targets: Mapping[str, SecureFile],
        install_state: Callable[[LocalTransaction], None],
        *,
        fault_hook: Callable[[str], None] | None = None,
        verify_installed: Callable[[], None] | None = None,
        install_scope: Callable[[], AbstractContextManager[object]] | None = None,
        recovery_merges: Mapping[
            str, Callable[[bytes | None, bytes | None, bytes | None], bytes | None]
        ]
        | None = None,
        target_writers: Mapping[str, Callable[[SecureFile, bytes], None]] | None = None,
    ) -> None:
        if not state_targets or any(
            name in {"team_trust", "pending_join_trust"} for name in state_targets
        ):
            raise ValueError("invalid state install targets")
        self._journal = journal.duplicate()
        self._state_targets = {name: target.duplicate() for name, target in state_targets.items()}
        self._install_state = install_state
        self._fault_hook = fault_hook
        self._verify_installed = verify_installed
        self._install_scope = install_scope or nullcontext
        self._recovery_merges = recovery_merges
        self._target_writers = target_writers

    def close(self) -> None:
        self._journal.close()
        for target in self._state_targets.values():
            target.close()

    def matches_journal(self, target: SecureFile) -> bool:
        """Confirm restore and provider share the one canonical activation journal."""
        return self._journal.lock_key == target.lock_key

    def _coordinator(
        self,
        pending_target: SecureFile,
        active_target: SecureFile,
    ) -> LocalTransactionCoordinator:
        targets = {
            **self._state_targets,
            "pending_join_trust": pending_target,
            "team_trust": active_target,
        }
        scope = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {name: str(target.path) for name, target in sorted(targets.items())},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        return LocalTransactionCoordinator(
            self._journal,
            {
                **self._state_targets,
                "pending_join_trust": pending_target,
                "team_trust": active_target,
            },
            fault_hook=self._fault_hook,
            recovery_scope=scope,
            max_recovery_bytes=24 * 1024 * 1024,
            recovery_merges=self._recovery_merges,
            target_writers=self._target_writers,
        )

    def recover_with_trust(
        self,
        *,
        pending_target: SecureFile,
        active_target: SecureFile,
    ) -> None:
        """Recover an interrupted commit before strict trust metadata is parsed."""
        coordinator = self._coordinator(pending_target, active_target)
        try:
            recovered = coordinator.snapshot(target_names=()).recovered
            if recovered:
                _harden_owner_target(active_target)
                _harden_owner_target(pending_target)
        finally:
            coordinator.close()

    def install_with_trust(
        self,
        *,
        pending_target: SecureFile,
        active_target: SecureFile,
        pending_preimage: bytes | None,
        trust_content: bytes,
        active_preimage: bytes | None = None,
    ) -> None:
        coordinator = self._coordinator(pending_target, active_target)
        try:
            with (
                coordinator.coordinated(),
                self._install_scope(),
                coordinator.transaction(rollback_base_exceptions=True) as transaction,
            ):
                if transaction.read_optional("pending_join_trust") != pending_preimage:
                    raise ValueError("pending join trust changed")
                active = transaction.read_optional("team_trust")
                if (active_preimage is not None and active != active_preimage) or (
                    active_preimage is None and active not in {None, trust_content}
                ):
                    raise ValueError("active local trust changed")
                self._install_state(transaction)
                transaction.write("team_trust", trust_content)
                os.chmod(
                    active_target.name,
                    0o600,
                    dir_fd=active_target.parent_fd,
                    follow_symlinks=False,
                )
                transaction.write("pending_join_trust", b"")
                os.chmod(
                    pending_target.name,
                    0o600,
                    dir_fd=pending_target.parent_fd,
                    follow_symlinks=False,
                )
                if self._verify_installed is not None:
                    self._verify_installed()
        finally:
            try:
                _harden_owner_target(active_target)
                _harden_owner_target(pending_target)
            finally:
                coordinator.close()


def _canonical_bytes(value: StrictModel) -> bytes:
    document = value.model_dump(mode="json")
    if isinstance(value, LocalTrustConfigV2) and value.device_binding is None:
        document.pop("device_binding")
    if isinstance(value, LocalTrustConfigV2) and value.migration_recipient_binding is None:
        document.pop("migration_recipient_binding")
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


@contextmanager
def _locked_files(*targets: SecureFile) -> Iterator[None]:
    with ExitStack() as stack:
        for target in sorted(targets, key=lambda item: item.lock_key):
            stack.enter_context(same_path_lock(target))
        yield


def _read_locked_descriptor(
    target: SecureFile,
    *,
    max_bytes: int,
) -> tuple[bytes | None, os.stat_result | None]:
    descriptor = -1
    with same_path_lock(target):
        try:
            try:
                descriptor = os.open(
                    target.name,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=target.parent_fd,
                )
            except FileNotFoundError:
                return None, None
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("unsafe local trust file")
            retained = bytearray()
            while chunk := os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(retained))):
                retained.extend(chunk)
                if len(retained) > max_bytes:
                    raise ValueError("local trust file oversized")
            after = os.fstat(descriptor)
            path_metadata = os.stat(
                target.name,
                dir_fd=target.parent_fd,
                follow_symlinks=False,
            )
            if _security_metadata(before) != _security_metadata(after) or _security_metadata(
                after
            ) != _security_metadata(path_metadata):
                raise ValueError("local trust file changed")
            return bytes(retained), after
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _security_metadata(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _require_owner_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ValueError("unsafe local trust file")


def _read(target: SecureFile) -> LocalTrustConfig | None:
    content = target.read_optional_nonblocking(max_bytes=MAX_LOCAL_TRUST_BYTES)
    if content is None:
        return None
    loads_strict_object(content.decode("utf-8"))
    return LocalTrustConfig.model_validate_json(content)


def _read_versioned(
    directory: SecureDirectory,
    target: SecureFile,
) -> LocalTrustConfig | LocalTrustConfigV2 | None:
    content, metadata = _read_locked_descriptor(target, max_bytes=MAX_LOCAL_TRUST_BYTES)
    if content is None or content == b"":
        return None
    assert metadata is not None
    loaded = loads_strict_object(content.decode("utf-8"))
    version = loaded.get("schema_version")
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("invalid local trust version")
    if version == 1:
        return LocalTrustConfig.model_validate_json(content)
    _require_owner_directory(directory, harden=False)
    _require_owner_metadata(metadata)
    parsed = LocalTrustConfigV2.model_validate_json(content)
    if content != _canonical_bytes(parsed):
        raise ValueError("noncanonical local trust")
    return parsed


def _read_pending(directory: SecureDirectory, target: SecureFile) -> PendingJoinTrustV2 | None:
    content, metadata = _read_locked_descriptor(target, max_bytes=MAX_PENDING_JOIN_BYTES)
    if content is None or content == b"":
        return None
    _require_owner_directory(directory, harden=False)
    assert metadata is not None
    _require_owner_metadata(metadata)
    loads_strict_object(content.decode("utf-8"))
    parsed = PendingJoinTrustV2.model_validate_json(content)
    if content != parsed.canonical_bytes():
        raise ValueError("noncanonical pending join trust")
    return parsed


def _require_owner_directory(directory: SecureDirectory, *, harden: bool | None) -> None:
    metadata = os.fstat(directory.descriptor)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("unsafe local trust directory")
    if harden is None:
        return
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        if not harden:
            raise ValueError("unsafe local trust directory")
        os.fchmod(directory.descriptor, 0o700)
        os.fsync(directory.descriptor)
        metadata = os.fstat(directory.descriptor)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("unsafe local trust directory")


def _require_owner_file(directory: SecureDirectory, name: str) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory.descriptor,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ValueError("unsafe local trust file")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _harden_owner_target(target: SecureFile) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            target.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=target.parent_fd,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise ValueError("unsafe local trust file")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    except FileNotFoundError:
        return
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_workspace(root: Path, *, harden: bool | None) -> tuple[SecureDirectory, SecureDirectory]:
    project = SecureDirectory.open(root)
    try:
        workspace = project.subdirectory(".intent")
    except BaseException:
        project.close()
        raise
    try:
        _require_owner_directory(workspace, harden=harden)
    except BaseException:
        workspace.close()
        project.close()
        raise
    return project, workspace


def _check_project(directory: SecureDirectory, project_id: str) -> None:
    target = directory.file("config.yaml")
    try:
        content = target.read_optional_nonblocking(max_bytes=64 * 1024)
        if content is not None:
            config = ProjectConfig.model_validate_json(
                json.dumps(load_strict_yaml_mapping_bytes(content), allow_nan=False)
            )
            if config.project_id != project_id:
                raise ValueError("invalid local trust project")
    finally:
        target.close()


def save_local_trust(
    runtime: Runtime,
    recipient: RecipientRecord,
    signing_public_keys: Mapping[str, bytes],
) -> None:
    """Save an exact reviewed public binding once; incompatible writes fail closed."""
    caught: BaseException
    try:
        if any(type(key) is not bytes or len(key) != 32 for key in signing_public_keys.values()):
            raise ValueError("invalid signing keys")
        config = LocalTrustConfig(
            project_id=runtime.config.project_id,
            repository_id=recipient.repository_id,
            recipient_key_id=recipient.key_id,
            recipient=recipient,
            signing_public_keys={
                name: base64.b64encode(public).decode("ascii")
                for name, public in sorted(signing_public_keys.items())
            },
        )
        content = config.model_dump_json().encode("utf-8")
        if len(content) > MAX_LOCAL_TRUST_BYTES:
            raise ValueError("invalid local trust size")
        target = runtime.workspace_directory.file(_FILENAME)
        try:
            with same_path_lock(target):
                _check_project(runtime.workspace_directory, config.project_id)
                old = _read(target)
                if old is not None and old != config:
                    raise ValueError("local trust changed")
                if old is None:
                    target.atomic_write(content, reject_target_races=True)
        finally:
            target.close()
        return
    except BaseException as error:  # noqa: BLE001 - fixed public/cancellation boundary
        caught = error
    _failure(caught)


def load_local_trust(root: Path) -> LocalTrustConfig | None:
    """Read public trust without opening a canonical runtime or creating paths."""
    caught: BaseException
    try:
        project = SecureDirectory.open(root)
        try:
            try:
                os.stat(".intent", dir_fd=project.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return None
            workspace = project.subdirectory(".intent")
            try:
                target = workspace.file(_FILENAME)
                try:
                    config = _read(target)
                    if config is not None:
                        _check_project(workspace, config.project_id)
                    return config
                finally:
                    target.close()
            finally:
                workspace.close()
        finally:
            project.close()
    except BaseException as error:  # noqa: BLE001 - fixed public/cancellation boundary
        caught = error
    _failure(caught)


class LocalTrustProvider:
    """Reconstruct the production recipient store from durable public enrollment."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def requires_activation_recovery(self) -> bool:
        """Check the fixed journal name before parsing potentially interrupted trust."""
        try:
            project, workspace = _open_workspace(self._root, harden=None)
        except FileNotFoundError:
            return False
        try:
            target = workspace.file(_ACTIVATION_JOURNAL_FILENAME)
            try:
                return target.exists()
            finally:
                target.close()
        finally:
            workspace.close()
            project.close()

    def load_versioned(self) -> LocalTrustConfig | LocalTrustConfigV2 | None:
        """Read exact public trust without using it as secret-bearing authority."""
        caught: BaseException
        try:
            project, workspace = _open_workspace(self._root, harden=None)
            try:
                migration_journal = workspace.file("team-migration-activation.json")
                try:
                    if migration_journal.exists():
                        with _migration_activation_transaction(self._root) as (coordinator, _paths):
                            coordinator.snapshot(target_names=())
                finally:
                    migration_journal.close()
                target = workspace.file(_FILENAME)
                journal = workspace.file(_ACTIVATION_JOURNAL_FILENAME)
                try:
                    with _locked_files(journal, target):
                        if journal.exists():
                            raise ValueError("join activation recovery required")
                        config = _read_versioned(workspace, target)
                        if config is not None:
                            _check_project(workspace, config.project_id)
                        return config
                finally:
                    journal.close()
                    target.close()
            finally:
                workspace.close()
                project.close()
        except FileNotFoundError:
            return None
        except BaseException as error:  # noqa: BLE001
            caught = error
        _failure(caught)

    def load_pending_join(self) -> PendingJoinTrustV2 | None:
        """Read a public join receipt; its presence never activates device authority."""
        caught: BaseException
        try:
            project, workspace = _open_workspace(self._root, harden=None)
            try:
                target = workspace.file(_PENDING_FILENAME)
                try:
                    pending = _read_pending(workspace, target)
                    if pending is not None:
                        _check_project(workspace, pending.invite.project_id)
                    return pending
                finally:
                    target.close()
            finally:
                workspace.close()
                project.close()
        except FileNotFoundError:
            return None
        except BaseException as error:  # noqa: BLE001
            caught = error
        _failure(caught)

    def save_pending_join(self, receipt: PendingJoinTrustV2) -> None:
        """Persist one canonical public receipt without replacing a different ceremony."""
        caught: BaseException
        try:
            receipt = PendingJoinTrustV2.model_validate_json(receipt.canonical_bytes())
            content = receipt.canonical_bytes()
            if len(content) > MAX_PENDING_JOIN_BYTES:
                raise ValueError("pending join trust oversized")
            project, workspace = _open_workspace(self._root, harden=True)
            try:
                _check_project(workspace, receipt.invite.project_id)
                target = workspace.file(_PENDING_FILENAME)
                try:
                    with same_path_lock(target):
                        old = _read_pending(workspace, target)
                        if old is not None and old != receipt:
                            raise ValueError("pending join trust changed")
                        if old is None:
                            target.atomic_write(
                                content,
                                reject_target_races=True,
                                mode=0o600,
                            )
                            _require_owner_file(workspace, target.name)
                finally:
                    target.close()
            finally:
                workspace.close()
                project.close()
            return
        except BaseException as error:  # noqa: BLE001
            caught = error
        _failure(caught)

    def acknowledge_join_response(self, preimage: PendingJoinTrustV2) -> PendingJoinTrustV2:
        """CAS an exact successfully exported response into its merge-wait state."""
        caught: BaseException
        try:
            preimage = PendingJoinTrustV2.model_validate_json(preimage.canonical_bytes())
            if preimage.phase != "response-ready" or preimage.external_write_attempted:
                raise ValueError("pending join response changed")
            acknowledged = preimage.model_copy(
                update={"phase": "awaiting-merge", "external_write_attempted": True}
            )
            project, workspace = _open_workspace(self._root, harden=True)
            try:
                _check_project(workspace, preimage.invite.project_id)
                target = workspace.file(_PENDING_FILENAME)
                try:
                    with same_path_lock(target):
                        if _read_pending(workspace, target) != preimage:
                            raise ValueError("pending join response changed")
                        target.atomic_write(
                            acknowledged.canonical_bytes(),
                            reject_target_races=True,
                            mode=0o600,
                        )
                        _require_owner_file(workspace, target.name)
                finally:
                    target.close()
            finally:
                workspace.close()
                project.close()
            return acknowledged
        except BaseException as error:  # noqa: BLE001 - fixed public trust boundary
            caught = error
        _failure(caught)

    def advance_member(
        self,
        *,
        preimage: LocalTrustConfigV2,
        trust: LocalTrustConfigV2,
        install: StateInstallTransaction,
    ) -> None:
        """CAS an already active member's public pins in the same state transaction."""
        try:
            if (
                trust.model_copy(
                    update={
                        "accepted_bundle_digest": preimage.accepted_bundle_digest,
                        "accepted_authority_digest": preimage.accepted_authority_digest,
                        "accepted_authority_sequence": preimage.accepted_authority_sequence,
                    }
                )
                != preimage
                or trust.accepted_authority_sequence < preimage.accepted_authority_sequence
            ):
                raise ValueError("active member binding changed")
            project, workspace = _open_workspace(self._root, harden=False)
            try:
                with ExitStack() as stack:
                    pending = workspace.file(_PENDING_FILENAME)
                    active = workspace.file(_FILENAME)
                    journal = workspace.file(_ACTIVATION_JOURNAL_FILENAME)
                    for target in (pending, active, journal):
                        stack.callback(target.close)
                    if not install.matches_journal(journal):
                        raise ValueError("member journal changed")
                    install.recover_with_trust(pending_target=pending, active_target=active)
                    if (
                        _read_pending(workspace, pending) is not None
                        or _read_versioned(workspace, active) != preimage
                    ):
                        raise ValueError("active member changed")
                    install.install_with_trust(
                        pending_target=pending,
                        active_target=active,
                        pending_preimage=pending.read_optional_nonblocking(
                            max_bytes=MAX_PENDING_JOIN_BYTES
                        ),
                        active_preimage=preimage.canonical_bytes(),
                        trust_content=trust.canonical_bytes(),
                    )
            finally:
                workspace.close()
                project.close()
            return
        except BaseException as error:  # noqa: BLE001 - fixed public trust boundary
            caught = error
        _failure(caught)

    def activate_join(
        self,
        *,
        pending_preimage: PendingJoinTrustV2,
        trust: LocalTrustConfigV2,
        install: StateInstallTransaction,
    ) -> None:
        """Delegate the exact state/trust commit to one restore-owned transaction."""
        caught: BaseException
        try:
            pending_preimage = PendingJoinTrustV2.model_validate_json(
                pending_preimage.canonical_bytes()
            )
            trust = LocalTrustConfigV2.model_validate_json(trust.canonical_bytes())
            if (
                pending_preimage.phase != "awaiting-merge"
                or pending_preimage.expected_root_key_id != trust.root.root_key_id
                or pending_preimage.invite.project_id != trust.project_id
                or pending_preimage.invite.repository_id != trust.repository_id
                or pending_preimage.response.proposed_member.member_id != trust.member_id
                or pending_preimage.local_recipient_key_id != trust.recipient_key_id
                or pending_preimage.local_signature_id != trust.signature_id
            ):
                raise ValueError("join activation binding changed")
            project, workspace = _open_workspace(self._root, harden=False)
            try:
                pending_target = workspace.file(_PENDING_FILENAME)
                active_target = workspace.file(_FILENAME)
                journal_target = workspace.file(_ACTIVATION_JOURNAL_FILENAME)
                try:
                    if not install.matches_journal(journal_target):
                        raise ValueError("join activation journal changed")
                    install.recover_with_trust(
                        pending_target=pending_target,
                        active_target=active_target,
                    )
                    current = _read_pending(workspace, pending_target)
                    existing = _read_versioned(workspace, active_target)
                    if current is None and existing == trust:
                        return
                    if current != pending_preimage:
                        raise ValueError("pending join trust changed")
                    if existing is not None and existing != trust:
                        raise ValueError("active local trust changed")
                    install.install_with_trust(
                        pending_target=pending_target,
                        active_target=active_target,
                        pending_preimage=pending_preimage.canonical_bytes(),
                        trust_content=trust.canonical_bytes(),
                    )
                    if _read_versioned(workspace, active_target) != trust:
                        raise ValueError("join activation unavailable")
                    if _read_pending(workspace, pending_target) is not None:
                        raise ValueError("join activation incomplete")
                finally:
                    journal_target.close()
                    pending_target.close()
                    active_target.close()
            finally:
                workspace.close()
                project.close()
            return
        except BaseException as error:  # noqa: BLE001
            caught = error
        _failure(caught)

    def load(self) -> SharedStateTrust | None:
        caught: BaseException
        trust = None
        try:
            config = self.load_versioned()
            if config is None:
                return None
            if isinstance(config, LocalTrustConfigV2):
                return None
            provider = RecipientKeyStoreTrustProvider(
                project_id=config.project_id,
                repository_id=config.repository_id,
                recipient_key_id=config.recipient_key_id,
                signing_keys=config.trusted_signing_keys(),
                key_store=KeyringRecipientKeyStore(config.enrollment_binding()),
            )
            trust = provider.load()
            if trust is not None:
                public = (
                    X25519PrivateKey.from_private_bytes(
                        trust.recipient_private_key,
                    )
                    .public_key()
                    .public_bytes_raw()
                )
                if (
                    base64.urlsafe_b64encode(public).rstrip(b"=").decode()
                    != config.recipient.public_key
                ):
                    raise ValueError("local recipient key changed")
            return trust
        except BaseException as error:  # noqa: BLE001 - scrub provider traceback
            caught = error
        trust = None
        _failure(caught)


def local_or_environment_trust(
    root: Path,
    environment: Mapping[str, str] | None = None,
) -> TrustProvider:
    """Use explicit environment trust when present, otherwise enrolled local trust."""
    selected = os.environ if environment is None else environment
    if TRUST_ENVIRONMENT_VARIABLE in selected:
        return EnvironmentTrustProvider(selected)
    return LocalTrustProvider(root)


__all__ = [
    "MAX_PENDING_JOIN_BYTES",
    "LocalTrustConfig",
    "LocalTrustConfigV2",
    "LocalTrustError",
    "LocalTrustProvider",
    "PendingJoinTrustV2",
    "StateInstallTransaction",
    "load_local_trust",
    "local_or_environment_trust",
    "save_local_trust",
]
