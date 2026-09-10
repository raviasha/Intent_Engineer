"""Reviewed preparation of encrypted team-state publication branches."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from intent_engineering.capture.mcp.profile_loader import load_strict_yaml_mapping_bytes
from intent_engineering.cli.runtime import Runtime
from intent_engineering.cli.writes import MutationPolicy
from intent_engineering.control_plane.models import (
    CredentialRecord,
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
    credential_identity_digest,
    credential_matches_digest,
)
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.core.models import ProjectConfig
from intent_engineering.mutations.models import ApprovalRecord, WritePlan
from intent_engineering.storage.jsonl.approval_store import parse_immutable_records
from intent_engineering.storage.secure import SecureFile, configured_graph_relative
from intent_engineering.team_state.archive import build_archive, build_archive_v2
from intent_engineering.team_state.authority import (
    authority_digest,
    canonical_authority_attestation_preimage,
    canonical_authority_bytes,
    canonical_certificate_signing_preimage,
    canonical_state_signature_preimage,
    derive_member_id,
    issue_device_certificate,
    verify_v2_envelope,
)
from intent_engineering.team_state.crypto import (
    AuthenticatedBundleContextV2,
    EncryptedBundle,
    _encrypt_bundle_for_public_keys,
    canonical_authenticated_context_bytes,
    canonical_encrypted_bundle_bytes,
    decrypt_bundle,
)
from intent_engineering.team_state.keys import (
    DeviceBundleDecryptor,
    DeviceEnrollmentBinding,
    KeyringDeviceKeyStore,
)
from intent_engineering.team_state.local_trust import LocalTrustConfig, LocalTrustConfigV2
from intent_engineering.team_state.models import (
    CANONICAL_STATE_PATHS,
    AuthorityAttestationV2,
    CanonicalStateFile,
    CanonicalStateSnapshot,
    CertifiedStateSignatureV2,
    CiRecipientRecord,
    DeviceCertificateClaimsV2,
    DeviceSignerCertificateV2,
    EncryptionRecipient,
    MemberRecordV2,
    PreparedPublication,
    RecipientRecord,
    RemoteStateSnapshot,
    StateSignature,
    StateSignatureEnvelopeV2,
    TeamAuthorityPolicyV2,
    TeamAuthorityRegistryV2,
    TeamStateManifest,
    TeamStateManifestV2,
    V1MigrationBinding,
    V1MigrationProof,
    canonical_manifest_bytes,
    validate_encryption_recipient,
)
from intent_engineering.team_state.restore import (
    StateSignatureEnvelope,
    VerifiedReleaseV2,
    VerifiedV1Release,
    _git_executable_token,
    _manifest_aad,
    _validate_v2_snapshot,
    seal_state_payload,
    verify_v1_migration,
)
from intent_engineering.team_state.signing import (
    ExistingRecipientDeviceSigner,
    KeyringExistingRecipientDeviceSigner,
    KeyringTeamRootKeyStore,
    RootEnrollmentBinding,
    SigningKeyStore,
    TeamRootKeyStore,
    canonical_v1_migration_preimage,
)
from intent_engineering.validation import validate_canonical_snapshot

_GENESIS_PARENT = "sha256:" + "0" * 64
_DECISION_LIFETIME = timedelta(minutes=5)
_TARGET_PATHS = {
    "approvals": "approvals/approvals.jsonl",
    "receipts": "approvals/receipts.jsonl",
    "evidence": "evidence/evidence.jsonl",
    "graph": "graph.yaml",
    "history": "history/changesets.jsonl",
    "intent_proposals": "history/intent-proposals.jsonl",
    "cases": "reconciliation/cases.jsonl",
}
_EXTRA_PATHS = {
    "publication_config": "config.yaml",
    "publication_plans": "approvals/plans.jsonl",
    "publication_policy": "approvals/policy.yaml",
}
_MAX_GIT_OUTPUT_BYTES = 64 * 1024
_GIT_TIMEOUT_SECONDS = 10.0
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_GIT_EXECUTABLE = Path("/usr/bin/git")
_GIT_SUPERVISOR = (
    "import os,subprocess,sys,time;"
    "fd=int(sys.argv[1]);"
    "child=subprocess.Popen(sys.argv[2:],stdin=subprocess.DEVNULL);"
    "code=child.wait();"
    "os.write(fd,(str(code)+'\\n').encode('ascii'));"
    "os.close(fd);"
    "time.sleep(3600)"
)


@dataclass(frozen=True, slots=True)
class PublicationAuthority:
    recipients: tuple[EncryptionRecipient, ...]
    signing_private_keys: Mapping[str, bytes]
    remote_state: RemoteStateSnapshot | None
    publication_base_commit: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationAuthorityV2:
    """Registry-derived authority for an ordinary v2 release."""

    registry: TeamAuthorityRegistryV2
    local_member_id: str
    local_device_certificate_id: str
    remote_state: VerifiedReleaseV2
    publication_base_commit: str


def authority_from_verified_state(
    restored: VerifiedReleaseV2,
    local_trust: LocalTrustConfigV2,
) -> PublicationAuthorityV2:
    """Derive effective recipients and signer identity from one verified parent."""
    try:
        if type(restored) is not VerifiedReleaseV2 or type(local_trust) is not LocalTrustConfigV2:
            raise ValueError("invalid version two publication authority")
        registry = TeamAuthorityRegistryV2.model_validate(
            restored.authority.model_dump(mode="python")
        )
        trust = LocalTrustConfigV2.model_validate(local_trust.model_dump(mode="python"))
        digest = authority_digest(registry)
        certificates = {item.certificate_id: item for item in registry.device_certificates}
        certificate = certificates.get(trust.device_certificate_id)
        members = {item.member_id: item for item in registry.members}
        member = members.get(trust.member_id)
        revoked = {item.certificate_id for item in registry.revocations}
        if (
            restored.manifest.authority_digest != digest
            or restored.manifest.recipient_key_ids != registry.active_recipient_key_ids()
            or trust.project_id != registry.project_id
            or trust.repository_id != registry.repository_id
            or trust.root != registry.root
            or trust.accepted_authority_digest != digest
            or trust.accepted_authority_sequence != registry.sequence
            or trust.accepted_bundle_digest != restored.manifest.bundle_digest
            or certificate is None
            or member is None
            or member.status != "active"
            or certificate.certificate_id in revoked
            or certificate.claims.member_id != trust.member_id
            or certificate.claims.recipient_key_id != trust.recipient_key_id
            or certificate.claims.signature_id != trust.signature_id
            or certificate.claims.issued_at > restored.manifest.created_at + timedelta(minutes=5)
            or certificate.claims.expires_at < restored.manifest.created_at - timedelta(minutes=5)
        ):
            raise ValueError("invalid version two publication authority")
        return PublicationAuthorityV2(
            registry=registry,
            local_member_id=trust.member_id,
            local_device_certificate_id=trust.device_certificate_id,
            remote_state=restored,
            publication_base_commit=restored.commit,
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError("version two publication authority unavailable") from None


class LegacyMigrationSigner(Protocol):
    """Existing v1 authority used only for the one-time bridge signature."""

    def public_keys(self) -> Mapping[str, bytes]: ...

    def sign(self, signature_id: str, preimage: bytes) -> bytes: ...


@dataclass(frozen=True, slots=True)
class MigrationKeyAuthorities:
    root_store: TeamRootKeyStore
    device_signer: ExistingRecipientDeviceSigner
    legacy_signer: LegacyMigrationSigner


@dataclass(frozen=True, slots=True)
class PreparedV1Migration:
    """Exact v2 artifacts for the first dual-verifiable state publication."""

    repository_id: str
    branch: str
    manifest: TeamStateManifestV2
    manifest_bytes: bytes
    bundle: bytes
    envelope: StateSignatureEnvelopeV2
    signatures: bytes
    bundle_path: str
    signature_path: str
    authority: TeamAuthorityRegistryV2

    def __post_init__(self) -> None:
        digest_hex = self.manifest.bundle_digest.removeprefix("sha256:")
        release = f"{self.manifest.graph_version}-{digest_hex}"
        if (
            self.repository_id != self.manifest.repository_id
            or self.branch != f"intent-publication/{digest_hex}"
            or self.manifest_bytes != canonical_manifest_bytes(self.manifest)
            or self.signatures != self.envelope.canonical_bytes()
            or len(self.bundle) != self.manifest.bundle_size
            or "sha256:" + hashlib.sha256(self.bundle).hexdigest() != self.manifest.bundle_digest
            or self.bundle_path != f"bundles/{release}.intent"
            or self.signature_path != f"signatures/{release}.json"
            or authority_digest(self.authority) != self.manifest.authority_digest
        ):
            raise ValueError("prepared migration artifacts are not exactly bound")


@dataclass(frozen=True, slots=True)
class PreparedPublicationV2:
    """Exact ordinary v2 artifacts derived from the verified parent registry."""

    repository_id: str
    branch: str
    manifest: TeamStateManifestV2
    manifest_bytes: bytes
    bundle: bytes
    envelope: StateSignatureEnvelopeV2
    signatures: bytes
    bundle_path: str
    signature_path: str
    authority: TeamAuthorityRegistryV2

    def __post_init__(self) -> None:
        digest_hex = self.manifest.bundle_digest.removeprefix("sha256:")
        release = f"{self.manifest.graph_version}-{digest_hex}"
        if (
            self.repository_id != self.manifest.repository_id
            or self.branch != f"intent-publication/{digest_hex}"
            or self.manifest_bytes != canonical_manifest_bytes(self.manifest)
            or self.signatures != self.envelope.canonical_bytes()
            or len(self.bundle) != self.manifest.bundle_size
            or "sha256:" + hashlib.sha256(self.bundle).hexdigest() != self.manifest.bundle_digest
            or self.bundle_path != f"bundles/{release}.intent"
            or self.signature_path != f"signatures/{release}.json"
            or authority_digest(self.authority) != self.manifest.authority_digest
            or self.manifest.recipient_key_ids != self.authority.active_recipient_key_ids()
            or self.manifest.migration is not None
            or self.envelope.manifest_digest
            != "sha256:" + hashlib.sha256(self.manifest_bytes).hexdigest()
            or self.envelope.bundle_digest != self.manifest.bundle_digest
            or self.envelope.authority_digest != self.manifest.authority_digest
            or len(self.envelope.certificates) != 1
            or self.envelope.certificates[0] not in self.authority.device_certificates
            or self.envelope.authority_attestation is not None
            or self.envelope.migration_proof is not None
        ):
            raise ValueError("prepared version two artifacts are not exactly bound")


class V2DeviceSigner(Protocol):
    """Non-exporting local device signing boundary."""

    def sign(self, signature_id: str, preimage: bytes) -> bytes: ...


def _prepare_v2_publication(
    *,
    snapshot: CanonicalStateSnapshot,
    authority: PublicationAuthorityV2,
    device_signer: V2DeviceSigner,
    now: datetime,
) -> PreparedPublicationV2:
    """Prepare an ordinary release without caller-controlled policy or root authority."""
    signature = b""
    try:
        if (
            type(snapshot) is not CanonicalStateSnapshot
            or type(authority) is not PublicationAuthorityV2
            or type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or now.microsecond != 0
        ):
            raise ValueError("version two publication input changed")
        snapshot = CanonicalStateSnapshot.model_validate(snapshot.model_dump(mode="python"))
        registry = TeamAuthorityRegistryV2.model_validate(
            authority.registry.model_dump(mode="python")
        )
        parent = authority.remote_state
        if (
            type(parent) is not VerifiedReleaseV2
            or authority.publication_base_commit != parent.commit
            or parent.authority != registry
            or snapshot.project_id != registry.project_id
            or snapshot.repository_id != registry.repository_id
            or snapshot.graph_version < parent.manifest.graph_version
        ):
            raise ValueError("version two publication parent changed")
        certificates = {item.certificate_id: item for item in registry.device_certificates}
        certificate = certificates.get(authority.local_device_certificate_id)
        member = {item.member_id: item for item in registry.members}.get(authority.local_member_id)
        if (
            certificate is None
            or member is None
            or member.status != "active"
            or certificate.claims.member_id != member.member_id
            or certificate.certificate_id in {item.certificate_id for item in registry.revocations}
            or certificate.claims.issued_at > now + timedelta(minutes=5)
            or certificate.claims.expires_at < now - timedelta(minutes=5)
        ):
            raise ValueError("version two publication signer unavailable")
        recipient_ids = registry.active_recipient_key_ids()
        recipient_public_keys = {
            item.claims.recipient_key_id: _decode_public_key(item.claims.recipient_public_key)
            for item in registry.device_certificates
            if item.claims.recipient_key_id in recipient_ids
        }
        recipient_public_keys[registry.ci_recipient.key_id] = _decode_public_key(
            registry.ci_recipient.public_key
        )
        recipient_public_keys = dict(sorted(recipient_public_keys.items()))
        if tuple(recipient_public_keys) != recipient_ids:
            raise ValueError("version two publication recipients changed")
        archive = build_archive_v2(snapshot, registry)
        aad = _v2_authenticated_aad(
            project_id=snapshot.project_id,
            repository_id=snapshot.repository_id,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent.manifest.bundle_digest,
            recipient_key_ids=recipient_ids,
            authority=registry,
            created_at=now.astimezone(UTC),
        )
        bundle = canonical_encrypted_bundle_bytes(
            _encrypt_bundle_for_public_keys(archive, recipient_public_keys, aad)
        )
        manifest = TeamStateManifestV2(
            project_id=snapshot.project_id,
            repository_id=snapshot.repository_id,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent.manifest.bundle_digest,
            bundle_digest="sha256:" + hashlib.sha256(bundle).hexdigest(),
            bundle_size=len(bundle),
            recipient_key_ids=recipient_ids,
            authority_digest=authority_digest(registry),
            authority_epoch=registry.authority_epoch,
            root_key_id=registry.root.root_key_id,
            created_at=now.astimezone(UTC),
        )
        _validate_v2_snapshot(snapshot, manifest)
        signature = device_signer.sign(
            certificate.claims.signature_id,
            canonical_state_signature_preimage(manifest),
        )
        if type(signature) is not bytes or len(signature) != 64:
            raise ValueError("version two publication signature unavailable")
        envelope = StateSignatureEnvelopeV2(
            manifest_digest="sha256:" + hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
            bundle_digest=manifest.bundle_digest,
            authority_digest=manifest.authority_digest,
            certificates=(certificate,),
            signatures=(
                CertifiedStateSignatureV2(
                    certificate_id=certificate.certificate_id,
                    signature_id=certificate.claims.signature_id,
                    signature=base64.urlsafe_b64encode(signature).rstrip(b"=").decode(),
                ),
            ),
        )
        verify_v2_envelope(manifest, envelope, registry.root, registry, now)
        digest_hex = manifest.bundle_digest.removeprefix("sha256:")
        release = f"{manifest.graph_version}-{digest_hex}"
        return PreparedPublicationV2(
            repository_id=snapshot.repository_id,
            branch=f"intent-publication/{digest_hex}",
            manifest=manifest,
            manifest_bytes=manifest.canonical_bytes(),
            bundle=bundle,
            envelope=envelope,
            signatures=envelope.canonical_bytes(),
            bundle_path=f"bundles/{release}.intent",
            signature_path=f"signatures/{release}.json",
            authority=registry,
        )
    finally:
        signature = b""
        device_signer = None  # type: ignore[assignment]


def prepare_v2_publication(
    *,
    snapshot: CanonicalStateSnapshot,
    authority: PublicationAuthorityV2,
    device_signer: V2DeviceSigner,
    now: datetime,
) -> PreparedPublicationV2:
    """Public fixed-error and cancellation-safe v2 preparation boundary."""
    result: PreparedPublicationV2 | None = None
    failure: BaseException | None = None
    try:
        result = _prepare_v2_publication(
            snapshot=snapshot,
            authority=authority,
            device_signer=device_signer,
            now=now,
        )
    except BaseException as error:  # noqa: BLE001 - scrub and preserve cancellation
        error_traceback = error.__traceback__
        if error_traceback is not None:
            traceback.clear_frames(error_traceback)
        error_traceback = None
        error.args = ()
        error.__dict__.clear()
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        failure = (
            error
            if not isinstance(error, Exception)
            else ValueError("version two publication preparation failed")
        )
    finally:
        snapshot = None  # type: ignore[assignment]
        authority = None  # type: ignore[assignment]
        device_signer = None  # type: ignore[assignment]
        now = None  # type: ignore[assignment]
    if failure is not None:
        detached = failure
        failure = None
        raise detached.with_traceback(None) from None
    assert result is not None
    return result


@dataclass(frozen=True, slots=True)
class V1MigrationPreview:
    """Canonical public draft that one exact WebAuthn decision may authorize."""

    project_id: str
    repository_id: str
    graph_version: int
    legacy_parent_bundle_digest: str
    legacy_manifest_digest: str
    legacy_signature_ids: tuple[str, ...]
    ci_recipient_key_id: str
    ci_recipient_digest: str
    root_key_id: str
    root_digest: str
    device_signature_id: str
    device_digest: str
    device_certificate_id: str
    authority_digest: str
    archive_digest: str
    authenticated_context_digest: str
    bundle_digest: str
    result_digest: str
    subject: DecisionSubject
    subject_digest: str
    payload: HumanDecisionPayload
    manifest: TeamStateManifestV2
    bundle: bytes
    authority: TeamAuthorityRegistryV2
    certificate: DeviceSignerCertificateV2

    def __post_init__(self) -> None:
        if (
            type(self.payload) is not HumanDecisionPayload
            or type(self.manifest) is not TeamStateManifestV2
            or type(self.authority) is not TeamAuthorityRegistryV2
            or type(self.certificate) is not DeviceSignerCertificateV2
            or type(self.bundle) is not bytes
            or self.payload.action is not DecisionAction.PUBLISH_STATE
            or self.payload.project_id != self.project_id
            or self.payload.graph_version != self.graph_version
            or self.payload.parent_bundle_digest != self.legacy_parent_bundle_digest
            or self.payload.subject != self.subject
            or self.payload.subject_digest != self.subject_digest
            or self.payload.result_digest != self.result_digest
            or self.manifest.project_id != self.project_id
            or self.manifest.repository_id != self.repository_id
            or self.manifest.graph_version != self.graph_version
            or self.manifest.parent_bundle_digest != self.legacy_parent_bundle_digest
            or self.manifest.bundle_digest != self.bundle_digest
            or self.result_digest != self.bundle_digest
            or self.manifest.authority_digest != self.authority_digest
            or self.manifest.root_key_id != self.root_key_id
            or self.authority.root.root_key_id != self.root_key_id
            or self.authority.ci_recipient.key_id != self.ci_recipient_key_id
            or self.certificate.certificate_id != self.device_certificate_id
            or self.certificate.claims.signature_id != self.device_signature_id
            or self.certificate not in self.authority.device_certificates
            or authority_digest(self.authority) != self.authority_digest
            or "sha256:" + hashlib.sha256(self.bundle).hexdigest() != self.bundle_digest
            or len(self.bundle) != self.manifest.bundle_size
        ):
            raise ValueError("migration preview binding changed")
        parsed_bundle = EncryptedBundle.model_validate_json(self.bundle)
        if (
            tuple(item.recipient_key_id for item in parsed_bundle.wrapped_keys)
            != self.manifest.recipient_key_ids
        ):
            raise ValueError("migration preview binding changed")

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "archive_digest": self.archive_digest,
                "authenticated_context_digest": self.authenticated_context_digest,
                "authority_digest": self.authority_digest,
                "bundle_digest": self.bundle_digest,
                "ci_recipient_digest": self.ci_recipient_digest,
                "ci_recipient_key_id": self.ci_recipient_key_id,
                "device_certificate_id": self.device_certificate_id,
                "device_digest": self.device_digest,
                "device_signature_id": self.device_signature_id,
                "graph_version": self.graph_version,
                "legacy_manifest_digest": self.legacy_manifest_digest,
                "legacy_parent_bundle_digest": self.legacy_parent_bundle_digest,
                "legacy_signature_ids": list(self.legacy_signature_ids),
                "manifest_digest": "sha256:"
                + hashlib.sha256(self.manifest.canonical_bytes()).hexdigest(),
                "payload": self.payload.model_dump(mode="json"),
                "project_id": self.project_id,
                "repository_id": self.repository_id,
                "result_digest": self.result_digest,
                "root_digest": self.root_digest,
                "root_key_id": self.root_key_id,
                "schema": "intent.v1-migration-preview.v2",
                "subject": self.subject.model_dump(mode="json"),
                "subject_digest": self.subject_digest,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


class _LegacySigningAdapter:
    def __init__(self, store: SigningKeyStore) -> None:
        self._store = store

    def public_keys(self) -> Mapping[str, bytes]:
        return self._store.public_keys()

    def sign(self, signature_id: str, preimage: bytes) -> bytes:
        private = b""
        values: dict[str, bytes] = {}
        try:
            values = dict(self._store.load_signing_keys())
            private = values[signature_id]
            return Ed25519PrivateKey.from_private_bytes(private).sign(preimage)
        finally:
            private = b""
            values.clear()


@dataclass(frozen=True, slots=True)
class PublicationPreview:
    """Credential-free projection of the exact encrypted release awaiting review."""

    payload: HumanDecisionPayload
    manifest: TeamStateManifest
    snapshot_digest: str
    recipient_key_ids: tuple[str, ...]
    branch: str


@dataclass(frozen=True, slots=True)
class PublicationPreviewV2:
    """Credential-free projection of one registry-preserving v2 release."""

    payload: HumanDecisionPayload
    manifest: TeamStateManifestV2
    snapshot_digest: str
    authority_digest: str
    authority_sequence: int
    recipient_key_ids: tuple[str, ...]
    local_device_certificate_id: str
    branch: str


class PublicationPublisher(Protocol):
    def publish(self, publication: PreparedPublication, *, base_commit: str | None) -> None: ...


class PublicationCleanupError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes


class TemporaryWorktreePublisher:
    """Push exact publication artifacts from one safely owned temporary worktree."""

    def __init__(
        self,
        repository: Path,
        *,
        temp_root: Path | None = None,
        transport: Path | None = None,
        allow_local_transport: bool = False,
    ) -> None:
        self._repository = Path(os.path.abspath(repository))
        self._temp_root = Path(
            os.path.abspath(temp_root if temp_root is not None else tempfile.gettempdir())
        )
        try:
            repository_real = self._repository.resolve(strict=True)
            root_real = self._temp_root.resolve(strict=True)
            metadata = os.lstat(root_real)
        except (FileNotFoundError, OSError) as error:
            raise ValueError("invalid temporary publication root") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or root_real == repository_real
            or repository_real in root_real.parents
        ):
            raise ValueError("invalid temporary publication root")
        self._repository = repository_real
        self._temp_root = root_real
        if transport is not None and not allow_local_transport:
            raise ValueError("local publication transport is unavailable")
        self._transport = None if transport is None else str(Path(os.path.abspath(transport)))
        self._allow_local_transport = allow_local_transport

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            "GIT_AUTHOR_NAME": "Intent Engineering",
            "GIT_AUTHOR_EMAIL": "intent-state@localhost",
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_COMMITTER_NAME": "Intent Engineering",
            "GIT_COMMITTER_EMAIL": "intent-state@localhost",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "SSH_ASKPASS": "/usr/bin/false",
        }

    @staticmethod
    def _trusted_transport(repository_id: str) -> str:
        matched = re.fullmatch(
            r"github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
            r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99})",
            repository_id,
        )
        if matched is None or matched.group(2) in {".", ".."} or matched.group(2).endswith(".git"):
            raise ValueError("publication branch unavailable")
        owner, repository = matched.groups()
        return f"https://github.com/{owner}/{repository}.git"

    @staticmethod
    def _stop_git(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (PermissionError, ProcessLookupError):
            return
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    def _git(
        self,
        cwd: Path,
        *arguments: str,
        check: bool = True,
        allow_file: bool = False,
    ) -> _GitResult:
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        status_read = -1
        status_write = -1
        status = bytearray()
        output = bytearray()
        output_size = 0
        token: tuple[int, int, int, int, str] | None = None
        try:
            token = _git_executable_token()
            status_read, status_write = os.pipe()
            os.set_inheritable(status_read, False)
            os.set_inheritable(status_write, True)
            argv = (
                sys.executable,
                "-I",
                "-S",
                "-c",
                _GIT_SUPERVISOR,
                str(status_write),
                str(_GIT_EXECUTABLE),
                "--no-pager",
                "--no-replace-objects",
                "-c",
                "core.askPass=",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.sshCommand=/usr/bin/false",
                "-c",
                "credential.helper=",
                "-c",
                "credential.interactive=never",
                "-c",
                "gc.auto=0",
                "-c",
                "http.extraHeader=",
                "-c",
                "http.proxy=",
                "-c",
                "https.proxy=",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.ext.allow=never",
                "-c",
                f"protocol.file.allow={'always' if allow_file else 'never'}",
                "-c",
                "protocol.git.allow=never",
                "-c",
                "protocol.ssh.allow=never",
                "-c",
                "protocol.https.allow=always",
                "-c",
                "submodule.recurse=false",
                "-C",
                str(cwd),
                *arguments,
            )
            process = subprocess.Popen(
                argv,
                cwd="/",
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                pass_fds=(status_write,),
                start_new_session=True,
                shell=False,
                bufsize=0,
            )
            assert process.stdout is not None and process.stderr is not None
            os.close(status_write)
            status_write = -1
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            selector.register(status_read, selectors.EVENT_READ)
            deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not (events := selector.select(remaining)):
                    raise TimeoutError("publication Git unavailable")
                for key, _mask in events:
                    file_object = key.fileobj
                    descriptor = (
                        file_object if isinstance(file_object, int) else file_object.fileno()
                    )
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif descriptor == status_read:
                        status.extend(chunk)
                        if len(status) > 16:
                            raise ValueError("publication Git unavailable")
                        if b"\n" in status:
                            self._stop_git(process)
                    else:
                        output_size += len(chunk)
                        if output_size > _MAX_GIT_OUTPUT_BYTES:
                            raise ValueError("publication Git unavailable")
                        if descriptor == process.stdout.fileno():
                            output.extend(chunk)
            try:
                returncode = int(bytes(status).strip())
            except ValueError as error:
                raise ValueError("publication Git unavailable") from error
            if check and returncode != 0:
                raise subprocess.CalledProcessError(returncode, argv)
            if _git_executable_token() != token:
                raise ValueError("publication Git unavailable")
            return _GitResult(returncode=returncode, stdout=bytes(output))
        finally:
            selector.close()
            status.clear()
            output.clear()
            if status_write >= 0:
                os.close(status_write)
            if status_read >= 0:
                os.close(status_read)
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                if process.poll() is None:
                    self._stop_git(process)

    def _cleanup(self, owner: Path, owner_identity: tuple[int, int]) -> None:
        current = os.lstat(owner)
        if (current.st_dev, current.st_ino) != owner_identity or not stat.S_ISDIR(current.st_mode):
            raise PublicationCleanupError("publication cleanup refused")
        for child in tuple(owner.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        try:
            os.rmdir(owner)
        except OSError as error:
            raise PublicationCleanupError("publication cleanup refused") from error

    def publish(
        self,
        publication: PreparedPublication | PreparedPublicationV2,
        *,
        base_commit: str | None,
    ) -> None:
        if type(publication) is PreparedPublication:
            publication = PreparedPublication.model_validate(publication.model_dump(mode="python"))
        elif type(publication) is PreparedPublicationV2:
            publication = PreparedPublicationV2(
                repository_id=publication.repository_id,
                branch=publication.branch,
                manifest=TeamStateManifestV2.model_validate(
                    publication.manifest.model_dump(mode="python")
                ),
                manifest_bytes=publication.manifest_bytes,
                bundle=publication.bundle,
                envelope=StateSignatureEnvelopeV2.model_validate(
                    publication.envelope.model_dump(mode="python")
                ),
                signatures=publication.signatures,
                bundle_path=publication.bundle_path,
                signature_path=publication.signature_path,
                authority=TeamAuthorityRegistryV2.model_validate(
                    publication.authority.model_dump(mode="python")
                ),
            )
        else:
            raise ValueError("publication artifacts unavailable")
        owner = Path(tempfile.mkdtemp(prefix="intent-publication-", dir=self._temp_root))
        owner_stat = os.lstat(owner)
        owner_identity = (owner_stat.st_dev, owner_stat.st_ino)
        checkout = owner / "checkout"
        git_directory = owner / "release.git"
        failure: BaseException | None = None
        try:
            checkout.mkdir()
            self._git(
                owner,
                "init",
                "--quiet",
                "--separate-git-dir",
                str(git_directory),
                str(checkout),
            )
            transport = self._transport or self._trusted_transport(publication.repository_id)
            existing = self._git(
                checkout,
                "ls-remote",
                "--exit-code",
                "--heads",
                transport,
                f"refs/heads/{publication.branch}",
                check=False,
                allow_file=self._allow_local_transport,
            )
            if existing.returncode == 0:
                raise ValueError("publication branch unavailable")
            if existing.returncode not in {2}:
                raise ValueError("publication branch unavailable")
            self._git(checkout, "symbolic-ref", "HEAD", "refs/heads/build")
            if base_commit is not None:
                if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", base_commit) is None:
                    raise ValueError("publication branch unavailable")
                self._git(
                    checkout,
                    "fetch",
                    "--force",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "--no-write-fetch-head",
                    "--quiet",
                    transport,
                    "refs/heads/intent-state:refs/remotes/publication/intent-state",
                    allow_file=self._allow_local_transport,
                )
                resolved_parent = self._git(
                    checkout,
                    "rev-parse",
                    "--verify",
                    "refs/remotes/publication/intent-state^{commit}",
                ).stdout.strip()
                if resolved_parent != base_commit.encode("ascii"):
                    raise ValueError("publication branch unavailable")
                self._git(checkout, "update-ref", "refs/heads/build", base_commit)
            self._git(checkout, "read-tree", "--empty")
            artifacts = {
                "manifest.json": publication.manifest_bytes,
                publication.bundle_path: publication.bundle,
                publication.signature_path: publication.signatures,
            }
            for relative, content in artifacts.items():
                target = checkout / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            self._git(checkout, "add", "--", *artifacts)
            self._git(
                checkout,
                "commit",
                "--quiet",
                "-m",
                f"Publish intent state {publication.manifest.bundle_digest}",
            )
            if base_commit is None:
                state_ref = self._git(
                    checkout,
                    "ls-remote",
                    "--exit-code",
                    "--heads",
                    transport,
                    "refs/heads/intent-state",
                    check=False,
                    allow_file=self._allow_local_transport,
                )
                if state_ref.returncode != 2:
                    raise ValueError("publication branch unavailable")
            self._git(
                checkout,
                "push",
                "--porcelain",
                transport,
                f"refs/heads/build:refs/heads/{publication.branch}",
                allow_file=self._allow_local_transport,
            )
        except BaseException as error:  # noqa: BLE001 - cancellation identity is preserved
            failure = error
        cleanup_failure: BaseException | None = None
        try:
            self._cleanup(owner, owner_identity)
        except BaseException as error:  # noqa: BLE001 - fail closed on owned-path ambiguity
            cleanup_failure = error
        if failure is not None:
            if isinstance(failure, Exception):
                raise ValueError("publication branch unavailable") from None
            raise failure.with_traceback(None)
        if cleanup_failure is not None:
            if isinstance(cleanup_failure, PublicationCleanupError):
                raise cleanup_failure
            raise PublicationCleanupError("publication cleanup refused") from None


def _decode_public_key(value: str) -> bytes:
    if type(value) is not str or not value or "=" in value:
        raise ValueError("migration public key unavailable")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() != value:
        raise ValueError("migration public key unavailable")
    return decoded


def _default_migration_authorities(
    legacy_trust: LocalTrustConfig,
    root_binding: RootEnrollmentBinding,
    device_binding: DeviceEnrollmentBinding,
) -> MigrationKeyAuthorities:
    return MigrationKeyAuthorities(
        root_store=KeyringTeamRootKeyStore(root_binding),
        device_signer=KeyringExistingRecipientDeviceSigner(device_binding),
        legacy_signer=_LegacySigningAdapter(
            SigningKeyStore(
                legacy_trust.project_id,
                legacy_trust.repository_id,
                legacy_trust.recipient.actor,
            )
        ),
    )


def _v2_authenticated_aad(
    *,
    project_id: str,
    repository_id: str,
    graph_version: int,
    parent_bundle_digest: str,
    recipient_key_ids: tuple[str, ...],
    authority: TeamAuthorityRegistryV2,
    created_at: datetime,
) -> bytes:
    """Return final AAD bound to the exact registry used by the archive."""
    return canonical_authenticated_context_bytes(
        AuthenticatedBundleContextV2(
            project_id=project_id,
            repository_id=repository_id,
            graph_version=graph_version,
            parent_bundle_digest=parent_bundle_digest,
            recipient_key_ids=recipient_key_ids,
            authority_digest=authority_digest(authority),
            authority_epoch=authority.authority_epoch,
            authority_sequence=authority.sequence,
            root_key_id=authority.root.root_key_id,
            created_at=created_at,
        )
    )


def preview_v1_migration(
    *,
    current: VerifiedV1Release,
    legacy_trust: LocalTrustConfig,
    ci_recipient: CiRecipientRecord,
    sponsor_credential: CredentialRecord,
    challenge: str,
    now: datetime,
    authorities: MigrationKeyAuthorities | None = None,
) -> V1MigrationPreview:
    """Create the exact public migration draft that requires WebAuthn approval."""
    failure: BaseException | None = None
    recipient_public = signing_public = b""
    try:
        if type(current) is not VerifiedV1Release or current.snapshot is None:
            raise ValueError("verified version one snapshot unavailable")
        legacy_trust = LocalTrustConfig.model_validate(legacy_trust.model_dump(mode="python"))
        ci_recipient = CiRecipientRecord.model_validate(ci_recipient.model_dump(mode="python"))
        credential = CredentialRecord.model_validate(sponsor_credential.model_dump(mode="python"))
        if (
            credential.project_id != legacy_trust.project_id
            or credential.local_only
            or credential.github_account_id is None
            or credential.github_login is None
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or now.microsecond != 0
            or current.manifest.project_id != legacy_trust.project_id
            or current.manifest.repository_id != legacy_trust.repository_id
            or current.snapshot.project_id != legacy_trust.project_id
            or current.snapshot.repository_id != legacy_trust.repository_id
            or ci_recipient.project_id != legacy_trust.project_id
            or ci_recipient.repository_id != legacy_trust.repository_id
        ):
            raise ValueError("migration decision changed")
        expected_legacy = {item.signature_id: item.public_key for item in current.signing_keys}
        if {
            item.signature_id: item.public_key for item in legacy_trust.trusted_signing_keys()
        } != expected_legacy:
            raise ValueError("legacy signer trust changed")
        account_id = int(credential.github_account_id)
        actor = f"github:{account_id}"
        device_id = (
            "device:"
            + hashlib.sha256(
                b"intent.v1-migration-device.v2\0"
                + credential.credential_id.encode()
                + b"\0"
                + legacy_trust.repository_id.encode()
            ).hexdigest()[:32]
        )
        root_binding = RootEnrollmentBinding(
            project_id=legacy_trust.project_id,
            repository_id=legacy_trust.repository_id,
            authority_epoch=1,
            created_at=now.astimezone(UTC),
        )
        device_binding = DeviceEnrollmentBinding(
            project_id=legacy_trust.project_id,
            repository_id=legacy_trust.repository_id,
            actor=actor,
            github_account_id=account_id,
            github_login=credential.github_login,
            device_id=device_id,
        )
        selected = authorities or _default_migration_authorities(
            legacy_trust, root_binding, device_binding
        )
        root = selected.root_store.create(root_binding)
        recipient_public = _decode_public_key(legacy_trust.recipient.public_key)
        material = selected.device_signer.create_for_existing_recipient(
            device_binding, recipient_public
        )
        if material.recipient_public_key != recipient_public:
            raise ValueError("migration recipient changed")
        signing_public = material.signing_public_key
        if _decode_public_key(root.root_public_key) == signing_public:
            raise ValueError("migration authorities are not distinct")
        member_id = derive_member_id(
            legacy_trust.project_id, legacy_trust.repository_id, account_id
        )
        claims = DeviceCertificateClaimsV2(
            project_id=legacy_trust.project_id,
            repository_id=legacy_trust.repository_id,
            authority_epoch=1,
            member_id=member_id,
            device_id=material.device_id,
            github_account_id=account_id,
            github_login=credential.github_login,
            recipient_key_id=material.recipient_key_id,
            recipient_public_key=base64.urlsafe_b64encode(recipient_public).rstrip(b"=").decode(),
            signature_id=material.signature_id,
            signing_public_key=base64.urlsafe_b64encode(signing_public).rstrip(b"=").decode(),
            webauthn_credential_digest=credential_identity_digest(credential),
            serial=1,
            issued_at=now.astimezone(UTC),
            expires_at=now.astimezone(UTC) + timedelta(days=366),
        )
        certificate = issue_device_certificate(claims, selected.root_store)
        member = MemberRecordV2(
            member_id=member_id,
            actor=actor,
            github_account_id=account_id,
            github_login=credential.github_login,
            role="sponsor",
            status="active",
            device_certificate_ids=(certificate.certificate_id,),
            enrolled_at=now.astimezone(UTC),
        )
        authority = TeamAuthorityRegistryV2(
            project_id=legacy_trust.project_id,
            repository_id=legacy_trust.repository_id,
            authority_epoch=1,
            sequence=1,
            root=root,
            policy=TeamAuthorityPolicyV2(),
            members=(member,),
            device_certificates=(certificate,),
            revocations=(),
            ci_recipient=ci_recipient,
            previous_authority_digest=None,
        )
        archive = build_archive_v2(current.snapshot, authority)
        recipient_ids = authority.active_recipient_key_ids()
        aad = _v2_authenticated_aad(
            project_id=legacy_trust.project_id,
            repository_id=legacy_trust.repository_id,
            graph_version=current.snapshot.graph_version,
            parent_bundle_digest=current.manifest.bundle_digest,
            recipient_key_ids=recipient_ids,
            authority=authority,
            created_at=now.astimezone(UTC),
        )
        bundle = canonical_encrypted_bundle_bytes(
            _encrypt_bundle_for_public_keys(
                archive,
                dict(
                    sorted(
                        {
                            material.recipient_key_id: recipient_public,
                            ci_recipient.key_id: _decode_public_key(ci_recipient.public_key),
                        }.items()
                    )
                ),
                aad,
            )
        )
        prior_manifest_digest = "sha256:" + hashlib.sha256(current.manifest_bytes).hexdigest()
        migration = V1MigrationBinding(
            prior_manifest_digest=prior_manifest_digest,
            legacy_signature_ids=current.manifest.required_signature_ids,
        )
        manifest = TeamStateManifestV2(
            project_id=legacy_trust.project_id,
            repository_id=legacy_trust.repository_id,
            graph_version=current.snapshot.graph_version,
            parent_bundle_digest=current.manifest.bundle_digest,
            bundle_digest="sha256:" + hashlib.sha256(bundle).hexdigest(),
            bundle_size=len(bundle),
            recipient_key_ids=recipient_ids,
            authority_digest=authority_digest(authority),
            authority_epoch=1,
            root_key_id=root.root_key_id,
            created_at=now.astimezone(UTC),
            migration=migration,
        )
        root_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    root.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        device_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "device_id": material.device_id,
                        "recipient_key_id": material.recipient_key_id,
                        "recipient_public_key": base64.urlsafe_b64encode(
                            material.recipient_public_key
                        )
                        .rstrip(b"=")
                        .decode(),
                        "signature_id": material.signature_id,
                        "signing_public_key": base64.urlsafe_b64encode(material.signing_public_key)
                        .rstrip(b"=")
                        .decode(),
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        ci_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    ci_recipient.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
        aad_digest = "sha256:" + hashlib.sha256(aad).hexdigest()
        subject_content = json.dumps(
            {
                "archive_digest": archive_digest,
                "authenticated_context_digest": aad_digest,
                "authority_digest": manifest.authority_digest,
                "bundle_digest": manifest.bundle_digest,
                "ci_recipient_digest": ci_digest,
                "device_certificate_id": certificate.certificate_id,
                "device_digest": device_digest,
                "graph_version": manifest.graph_version,
                "legacy_manifest_digest": prior_manifest_digest,
                "legacy_parent_bundle_digest": current.manifest.bundle_digest,
                "legacy_signature_ids": list(current.manifest.required_signature_ids),
                "project_id": legacy_trust.project_id,
                "repository_id": legacy_trust.repository_id,
                "root_digest": root_digest,
                "schema": "intent.v1-migration-subject.v2",
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        subject_digest = "sha256:" + hashlib.sha256(subject_content).hexdigest()
        subject = DecisionSubject(
            kind="publication",
            id=f"publication:v1-migration:{subject_digest.removeprefix('sha256:')}",
        )
        payload = HumanDecisionPayload(
            project_id=legacy_trust.project_id,
            repository_id=credential.repository_id,
            actor=credential.actor,
            action=DecisionAction.PUBLISH_STATE,
            graph_version=manifest.graph_version,
            parent_bundle_digest=current.manifest.bundle_digest,
            subject=subject,
            subject_digest=subject_digest,
            result_digest=manifest.bundle_digest,
            challenge=challenge,
            issued_at=now.astimezone(UTC),
            expires_at=now.astimezone(UTC) + _DECISION_LIFETIME,
        )
        return V1MigrationPreview(
            project_id=legacy_trust.project_id,
            repository_id=legacy_trust.repository_id,
            graph_version=manifest.graph_version,
            legacy_parent_bundle_digest=current.manifest.bundle_digest,
            legacy_manifest_digest=prior_manifest_digest,
            legacy_signature_ids=current.manifest.required_signature_ids,
            ci_recipient_key_id=ci_recipient.key_id,
            ci_recipient_digest=ci_digest,
            root_key_id=root.root_key_id,
            root_digest=root_digest,
            device_signature_id=material.signature_id,
            device_digest=device_digest,
            device_certificate_id=certificate.certificate_id,
            authority_digest=manifest.authority_digest,
            archive_digest=archive_digest,
            authenticated_context_digest=aad_digest,
            bundle_digest=manifest.bundle_digest,
            result_digest=manifest.bundle_digest,
            subject=subject,
            subject_digest=subject_digest,
            payload=payload,
            manifest=manifest,
            bundle=bundle,
            authority=authority,
            certificate=certificate,
        )
    except BaseException as error:  # noqa: BLE001 - fixed migration preview boundary
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        error.args = ()
        failure = (
            error
            if not isinstance(error, Exception)
            else ValueError("version one migration preview failed")
        )
    finally:
        recipient_public = signing_public = b""
    assert failure is not None
    raise failure.with_traceback(None)


def prepare_v1_migration(
    *,
    current: VerifiedV1Release,
    legacy_trust: LocalTrustConfig,
    ci_recipient: CiRecipientRecord,
    preview: V1MigrationPreview,
    sponsor_decision: VerifiedHumanDecision,
    now: datetime,
    authorities: MigrationKeyAuthorities | None = None,
) -> PreparedV1Migration:
    """Revalidate and sign only the exact migration draft approved by WebAuthn."""
    failure: BaseException | None = None
    recipient_public = signing_public = legacy_preimage = b""
    try:
        if (
            type(current) is not VerifiedV1Release
            or current.snapshot is None
            or type(preview) is not V1MigrationPreview
            or type(sponsor_decision) is not VerifiedHumanDecision
        ):
            raise ValueError("migration decision unavailable")
        legacy_trust = LocalTrustConfig.model_validate(legacy_trust.model_dump(mode="python"))
        ci_recipient = CiRecipientRecord.model_validate(ci_recipient.model_dump(mode="python"))
        credential = sponsor_decision.credential
        payload = sponsor_decision.payload
        decided_at = sponsor_decision.verified_at
        expected_legacy = {item.signature_id: item.public_key for item in current.signing_keys}
        if (
            payload != preview.payload
            or payload.action is not DecisionAction.PUBLISH_STATE
            or payload.project_id != preview.project_id
            or payload.repository_id != credential.repository_id
            or payload.actor != credential.actor
            or payload.graph_version != preview.graph_version
            or payload.parent_bundle_digest != preview.legacy_parent_bundle_digest
            or payload.subject != preview.subject
            or payload.subject_digest != preview.subject_digest
            or payload.result_digest != preview.result_digest
            or decided_at.tzinfo is None
            or decided_at.utcoffset() != timedelta(0)
            or decided_at.microsecond != 0
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or now.microsecond != 0
            or decided_at < payload.issued_at
            or decided_at > payload.expires_at
            or now < decided_at
            or now > payload.expires_at
            or credential.local_only
            or credential.project_id != preview.project_id
            or credential.github_account_id is None
            or credential.github_login is None
            or current.manifest.project_id != preview.project_id
            or current.manifest.repository_id != preview.repository_id
            or current.manifest.graph_version != preview.graph_version
            or current.manifest.bundle_digest != preview.legacy_parent_bundle_digest
            or "sha256:" + hashlib.sha256(current.manifest_bytes).hexdigest()
            != preview.legacy_manifest_digest
            or current.manifest.required_signature_ids != preview.legacy_signature_ids
            or current.snapshot.project_id != preview.project_id
            or current.snapshot.repository_id != preview.repository_id
            or current.snapshot.graph_version != preview.graph_version
            or legacy_trust.project_id != preview.project_id
            or legacy_trust.repository_id != preview.repository_id
            or ci_recipient.project_id != preview.project_id
            or ci_recipient.repository_id != preview.repository_id
            or ci_recipient.key_id != preview.ci_recipient_key_id
            or {item.signature_id: item.public_key for item in legacy_trust.trusted_signing_keys()}
            != expected_legacy
        ):
            raise ValueError("migration decision changed")
        account_id = int(credential.github_account_id)
        actor = f"github:{account_id}"
        device_id = (
            "device:"
            + hashlib.sha256(
                b"intent.v1-migration-device.v2\0"
                + credential.credential_id.encode()
                + b"\0"
                + preview.repository_id.encode()
            ).hexdigest()[:32]
        )
        root_binding = RootEnrollmentBinding(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            authority_epoch=1,
            created_at=payload.issued_at,
        )
        device_binding = DeviceEnrollmentBinding(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            actor=actor,
            github_account_id=account_id,
            github_login=credential.github_login,
            device_id=device_id,
        )
        selected = authorities or _default_migration_authorities(
            legacy_trust, root_binding, device_binding
        )
        if dict(selected.legacy_signer.public_keys()) != expected_legacy:
            raise ValueError("legacy signer authority changed")
        root = selected.root_store.root_trust()
        recipient_public = _decode_public_key(legacy_trust.recipient.public_key)
        material = selected.device_signer.create_for_existing_recipient(
            device_binding, recipient_public
        )
        signing_public = material.signing_public_key
        if _decode_public_key(root.root_public_key) == signing_public:
            raise ValueError("migration authorities are not distinct")
        member_id = derive_member_id(preview.project_id, preview.repository_id, account_id)
        claims = DeviceCertificateClaimsV2(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            authority_epoch=1,
            member_id=member_id,
            device_id=material.device_id,
            github_account_id=account_id,
            github_login=credential.github_login,
            recipient_key_id=material.recipient_key_id,
            recipient_public_key=base64.urlsafe_b64encode(recipient_public).rstrip(b"=").decode(),
            signature_id=material.signature_id,
            signing_public_key=base64.urlsafe_b64encode(signing_public).rstrip(b"=").decode(),
            webauthn_credential_digest=credential_identity_digest(credential),
            serial=1,
            issued_at=payload.issued_at,
            expires_at=payload.issued_at + timedelta(days=366),
        )
        certificate = preview.certificate
        if claims != certificate.claims or certificate.root_key_id != root.root_key_id:
            raise ValueError("migration preview changed")
        certificate_signature = base64.urlsafe_b64decode(
            certificate.root_signature + "=" * (-len(certificate.root_signature) % 4)
        )
        if (
            len(certificate_signature) != 64
            or base64.urlsafe_b64encode(certificate_signature).rstrip(b"=").decode()
            != certificate.root_signature
        ):
            raise ValueError("migration preview changed")
        Ed25519PublicKey.from_public_bytes(_decode_public_key(root.root_public_key)).verify(
            certificate_signature,
            canonical_certificate_signing_preimage(claims),
        )
        authority = TeamAuthorityRegistryV2(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            authority_epoch=1,
            sequence=1,
            root=root,
            policy=TeamAuthorityPolicyV2(),
            members=(
                MemberRecordV2(
                    member_id=member_id,
                    actor=actor,
                    github_account_id=account_id,
                    github_login=credential.github_login,
                    role="sponsor",
                    status="active",
                    device_certificate_ids=(certificate.certificate_id,),
                    enrolled_at=payload.issued_at,
                ),
            ),
            device_certificates=(certificate,),
            revocations=(),
            ci_recipient=ci_recipient,
            previous_authority_digest=None,
        )
        archive = build_archive_v2(current.snapshot, authority)
        aad = _v2_authenticated_aad(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            graph_version=preview.graph_version,
            parent_bundle_digest=preview.legacy_parent_bundle_digest,
            recipient_key_ids=authority.active_recipient_key_ids(),
            authority=authority,
            created_at=payload.issued_at,
        )
        parsed_bundle = EncryptedBundle.model_validate_json(preview.bundle)
        migration = V1MigrationBinding(
            prior_manifest_digest=preview.legacy_manifest_digest,
            legacy_signature_ids=preview.legacy_signature_ids,
        )
        manifest = TeamStateManifestV2(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            graph_version=preview.graph_version,
            parent_bundle_digest=preview.legacy_parent_bundle_digest,
            bundle_digest="sha256:" + hashlib.sha256(preview.bundle).hexdigest(),
            bundle_size=len(preview.bundle),
            recipient_key_ids=authority.active_recipient_key_ids(),
            authority_digest=authority_digest(authority),
            authority_epoch=1,
            root_key_id=root.root_key_id,
            created_at=payload.issued_at,
            migration=migration,
        )
        ci_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    ci_recipient.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        root_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    root.model_dump(mode="json"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        device_digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "device_id": material.device_id,
                        "recipient_key_id": material.recipient_key_id,
                        "recipient_public_key": base64.urlsafe_b64encode(
                            material.recipient_public_key
                        )
                        .rstrip(b"=")
                        .decode(),
                        "signature_id": material.signature_id,
                        "signing_public_key": base64.urlsafe_b64encode(material.signing_public_key)
                        .rstrip(b"=")
                        .decode(),
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        subject_content = json.dumps(
            {
                "archive_digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                "authenticated_context_digest": "sha256:" + hashlib.sha256(aad).hexdigest(),
                "authority_digest": manifest.authority_digest,
                "bundle_digest": manifest.bundle_digest,
                "ci_recipient_digest": ci_digest,
                "device_certificate_id": certificate.certificate_id,
                "device_digest": device_digest,
                "graph_version": manifest.graph_version,
                "legacy_manifest_digest": preview.legacy_manifest_digest,
                "legacy_parent_bundle_digest": preview.legacy_parent_bundle_digest,
                "legacy_signature_ids": list(preview.legacy_signature_ids),
                "project_id": preview.project_id,
                "repository_id": preview.repository_id,
                "root_digest": root_digest,
                "schema": "intent.v1-migration-subject.v2",
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        expected_subject_digest = "sha256:" + hashlib.sha256(subject_content).hexdigest()
        expected_subject = DecisionSubject(
            kind="publication",
            id=f"publication:v1-migration:{expected_subject_digest.removeprefix('sha256:')}",
        )
        if (
            root != preview.authority.root
            or certificate != preview.certificate
            or authority != preview.authority
            or manifest != preview.manifest
            or ci_digest != preview.ci_recipient_digest
            or root_digest != preview.root_digest
            or device_digest != preview.device_digest
            or "sha256:" + hashlib.sha256(archive).hexdigest() != preview.archive_digest
            or "sha256:" + hashlib.sha256(aad).hexdigest() != preview.authenticated_context_digest
            or expected_subject_digest != preview.subject_digest
            or expected_subject != preview.subject
            or tuple(item.recipient_key_id for item in parsed_bundle.wrapped_keys)
            != manifest.recipient_key_ids
        ):
            raise ValueError("migration preview changed")
        if issue_device_certificate(claims, selected.root_store) != certificate:
            raise ValueError("migration preview changed")
        attestation = AuthorityAttestationV2(
            project_id=preview.project_id,
            repository_id=preview.repository_id,
            authority_epoch=1,
            previous_authority_digest=None,
            authority_digest=manifest.authority_digest,
            parent_bundle_digest=preview.legacy_parent_bundle_digest,
            operation="v1-migration",
            subject_digest=preview.subject_digest,
            sponsor_member_id=member_id,
            sponsor_device_certificate_id=certificate.certificate_id,
            sponsor_decision_digest="sha256:"
            + hashlib.sha256(payload.canonical_bytes()).hexdigest(),
            decided_at=decided_at,
            root_key_id=root.root_key_id,
            root_signature=base64.urlsafe_b64encode(b"\0" * 64).rstrip(b"=").decode(),
        )
        attestation = attestation.model_copy(
            update={
                "root_signature": base64.urlsafe_b64encode(
                    selected.root_store.sign(
                        root.root_key_id,
                        canonical_authority_attestation_preimage(attestation),
                    )
                )
                .rstrip(b"=")
                .decode()
            }
        )
        state_signature = selected.device_signer.sign(
            material.signature_id, canonical_state_signature_preimage(manifest)
        )
        legacy_preimage = canonical_v1_migration_preimage(current, manifest)
        legacy_signatures = tuple(
            StateSignature(
                signature_id=signature_id,
                signature=base64.urlsafe_b64encode(
                    selected.legacy_signer.sign(signature_id, legacy_preimage)
                )
                .rstrip(b"=")
                .decode(),
            )
            for signature_id in preview.legacy_signature_ids
        )
        envelope = StateSignatureEnvelopeV2(
            manifest_digest="sha256:" + hashlib.sha256(manifest.canonical_bytes()).hexdigest(),
            bundle_digest=manifest.bundle_digest,
            authority_digest=manifest.authority_digest,
            certificates=(certificate,),
            signatures=(
                CertifiedStateSignatureV2(
                    certificate_id=certificate.certificate_id,
                    signature_id=material.signature_id,
                    signature=base64.urlsafe_b64encode(state_signature).rstrip(b"=").decode(),
                ),
            ),
            authority_attestation=attestation,
            migration_proof=V1MigrationProof(
                prior_manifest_digest=preview.legacy_manifest_digest,
                legacy_signatures=legacy_signatures,
            ),
        )
        verify_v1_migration(
            current=current,
            manifest=manifest,
            envelope=envelope,
            root=root,
            authority=authority,
            expected_ci_recipient=ci_recipient,
            now=decided_at,
        )
        digest_hex = manifest.bundle_digest.removeprefix("sha256:")
        release = f"{manifest.graph_version}-{digest_hex}"
        return PreparedV1Migration(
            repository_id=preview.repository_id,
            branch=f"intent-publication/{digest_hex}",
            manifest=manifest,
            manifest_bytes=manifest.canonical_bytes(),
            bundle=preview.bundle,
            envelope=envelope,
            signatures=envelope.canonical_bytes(),
            bundle_path=f"bundles/{release}.intent",
            signature_path=f"signatures/{release}.json",
            authority=authority,
        )
    except BaseException as error:  # noqa: BLE001 - fixed migration preparation boundary
        error.__traceback__ = None
        error.__cause__ = None
        error.__context__ = None
        error.args = ()
        failure = (
            error
            if not isinstance(error, Exception)
            else ValueError("version one migration preparation failed")
        )
    finally:
        recipient_public = signing_public = legacy_preimage = b""
    assert failure is not None
    raise failure.with_traceback(None)


class PublicationService:
    def __init__(
        self,
        runtime: Runtime,
        *,
        repository_id: str,
        decision_repository_id: str,
        authority: Callable[[], PublicationAuthority | PublicationAuthorityV2],
        publisher: PublicationPublisher,
        device_signer: V2DeviceSigner | None = None,
        decision_actor: str | None = None,
        challenge_source: Callable[[], bytes] = lambda: secrets.token_bytes(32),
    ) -> None:
        self._runtime = runtime
        self._repository_id = repository_id
        self._decision_repository_id = decision_repository_id
        self._authority = authority
        self._publisher = publisher
        self._device_signer = device_signer
        if decision_actor is not None and (type(decision_actor) is not str or not decision_actor):
            raise ValueError("publication decision actor unavailable")
        self._decision_actor = decision_actor
        self._challenge_source = challenge_source
        self._publication_base_commit: str | None = None
        self._pending: (
            tuple[
                PublicationPreview | PublicationPreviewV2,
                PreparedPublication | PreparedPublicationV2,
                bytes,
                bytes,
                str | None,
            ]
            | None
        ) = None

    def bind_publication_base_commit(self, commit: str) -> None:
        """Bind the next reviewed release to the exact state-branch anchor."""
        if (
            self._pending is not None
            or type(commit) is not str
            or _COMMIT.fullmatch(commit) is None
        ):
            raise ValueError("publication parent binding changed")
        self._publication_base_commit = commit

    def _current_authority(self) -> PublicationAuthority | PublicationAuthorityV2:
        authority = self._authority()
        if self._publication_base_commit is None:
            return authority
        if type(authority) is PublicationAuthorityV2:
            if authority.publication_base_commit != self._publication_base_commit:
                raise ValueError("publication parent binding changed")
            return authority
        if type(authority) is not PublicationAuthority:
            raise ValueError("publication authority unavailable")
        if (
            authority.publication_base_commit is not None
            and authority.publication_base_commit != self._publication_base_commit
        ):
            raise ValueError("publication parent binding changed")
        return PublicationAuthority(
            recipients=authority.recipients,
            signing_private_keys=authority.signing_private_keys,
            remote_state=authority.remote_state,
            publication_base_commit=self._publication_base_commit,
        )

    @staticmethod
    def _digest(content: bytes) -> str:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def _capture(self) -> tuple[CanonicalStateSnapshot, bytes]:
        extras: dict[str, SecureFile] = {}
        try:
            for name, path in _EXTRA_PATHS.items():
                extras[name] = self._runtime.workspace_directory.file(path)
            captured = self._runtime.transactions.snapshot(extras)
            files: dict[str, bytes] = {}
            for name, path in _TARGET_PATHS.items():
                content = captured.content.get(name)
                if content is not None and type(content) is not bytes:
                    raise ValueError("canonical publication state unavailable")
                files[path] = b"" if content is None else content
            for name, path in _EXTRA_PATHS.items():
                content = captured.content.get(name)
                if content is not None and type(content) is not bytes:
                    raise ValueError("canonical publication state unavailable")
                files[path] = b"" if content is None else content
            validation = validate_canonical_snapshot(
                {
                    "config": files["config.yaml"],
                    "graph": files["graph.yaml"],
                    "history": files["history/changesets.jsonl"],
                    "cases": files["reconciliation/cases.jsonl"],
                    "evidence": files["evidence/evidence.jsonl"],
                    "receipts": files["approvals/receipts.jsonl"],
                    "checkpoints": None,
                }
            )
            if not validation.valid or validation.graph_version is None:
                raise ValueError("canonical publication state is invalid")
            config = ProjectConfig.model_validate_json(
                json.dumps(load_strict_yaml_mapping_bytes(files["config.yaml"]))
            )
            if (
                config != self._runtime.config
                or config.project_id != self._runtime.config.project_id
                or str(configured_graph_relative(config.graph_path)) != "graph.yaml"
            ):
                raise ValueError("publication project identity changed")
            parse_immutable_records(files["approvals/approvals.jsonl"], ApprovalRecord)
            parse_immutable_records(files["approvals/plans.jsonl"], WritePlan)
            if files["approvals/policy.yaml"]:
                MutationPolicy.model_validate(
                    load_strict_yaml_mapping_bytes(files["approvals/policy.yaml"])
                )
            snapshot = CanonicalStateSnapshot(
                project_id=config.project_id,
                repository_id=self._repository_id,
                graph_version=validation.graph_version,
                files=tuple(
                    CanonicalStateFile(path=path, content=files[path])
                    for path in CANONICAL_STATE_PATHS
                ),
            )
            archive = build_archive(snapshot)
            return snapshot, archive
        finally:
            for extra in extras.values():
                extra.close()

    @staticmethod
    def _authority_material(
        authority: PublicationAuthority,
        *,
        project_id: str,
        repository_id: str,
    ) -> tuple[tuple[EncryptionRecipient, ...], dict[str, bytes], str | None, str | None, bytes]:
        if type(authority) is not PublicationAuthority:
            raise ValueError("publication authority unavailable")
        recipients = tuple(validate_encryption_recipient(item) for item in authority.recipients)
        key_ids = tuple(item.key_id for item in recipients)
        if not recipients or key_ids != tuple(sorted(key_ids)) or len(key_ids) != len(set(key_ids)):
            raise ValueError("publication recipients are invalid")
        if any(
            item.project_id != recipients[0].project_id
            or item.repository_id != recipients[0].repository_id
            for item in recipients
        ):
            raise ValueError("publication recipients are invalid")
        signing = dict(authority.signing_private_keys)
        if not signing or any(
            type(key) is not str or type(value) is not bytes for key, value in signing.items()
        ):
            raise ValueError("publication signing authority unavailable")
        signer_public = {
            key: Ed25519PrivateKey.from_private_bytes(value).public_key().public_bytes_raw()
            for key, value in signing.items()
        }
        remote = authority.remote_state
        publication_base_commit = authority.publication_base_commit
        if (
            publication_base_commit is not None
            and _COMMIT.fullmatch(publication_base_commit) is None
        ):
            raise ValueError("publication parent binding changed")
        if remote is not None:
            remote = RemoteStateSnapshot.model_validate(remote.model_dump(mode="python"))
            if (
                remote.repository_id != repository_id
                or remote.manifest.project_id != project_id
                or remote.manifest.repository_id != repository_id
            ):
                raise ValueError("publication parent binding changed")
            if publication_base_commit is not None and publication_base_commit != remote.commit:
                raise ValueError("publication parent binding changed")
            publication_base_commit = remote.commit
        canonical = json.dumps(
            {
                "recipients": [item.model_dump(mode="json") for item in recipients],
                "signers": {
                    key: base64.urlsafe_b64encode(value).decode("ascii")
                    for key, value in sorted(signer_public.items())
                },
                "remote": None if remote is None else remote.model_dump(mode="json"),
                "publication_base_commit": publication_base_commit,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return (
            recipients,
            signing,
            None if remote is None else remote.manifest.bundle_digest,
            publication_base_commit,
            canonical,
        )

    def _v2_signer(self, authority: PublicationAuthorityV2) -> V2DeviceSigner:
        if self._device_signer is not None:
            return self._device_signer
        certificate = {
            item.certificate_id: item for item in authority.registry.device_certificates
        }.get(authority.local_device_certificate_id)
        member = {item.member_id: item for item in authority.registry.members}.get(
            authority.local_member_id
        )
        if certificate is None or member is None:
            raise ValueError("version two publication signer unavailable")
        return KeyringDeviceKeyStore(
            DeviceEnrollmentBinding(
                project_id=authority.registry.project_id,
                repository_id=authority.registry.repository_id,
                actor=member.actor,
                github_account_id=member.github_account_id,
                github_login=member.github_login,
                device_id=certificate.claims.device_id,
            )
        )

    def _preview_v2(
        self,
        snapshot: CanonicalStateSnapshot,
        authority: PublicationAuthorityV2,
        *,
        now: datetime,
    ) -> PublicationPreviewV2:
        prepared = prepare_v2_publication(
            snapshot=snapshot,
            authority=authority,
            device_signer=self._v2_signer(authority),
            now=now,
        )
        archive = build_archive_v2(snapshot, authority.registry)
        authority_bytes = canonical_authority_bytes(authority.registry)
        challenge = self._challenge_source()
        if type(challenge) is not bytes or len(challenge) < 16:
            raise ValueError("publication challenge unavailable")
        parent_digest = prepared.manifest.parent_bundle_digest
        if parent_digest is None:
            raise ValueError("publication parent binding changed")
        snapshot_digest = self._digest(archive)
        member = next(
            m for m in authority.registry.members if m.member_id == authority.local_member_id
        )
        payload = HumanDecisionPayload(
            project_id=snapshot.project_id,
            repository_id=self._decision_repository_id,
            actor=self._decision_actor or member.actor,
            action=DecisionAction.PUBLISH_STATE,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent_digest,
            subject=DecisionSubject(
                kind="publication",
                id=f"publication:{snapshot_digest.removeprefix('sha256:')}",
            ),
            subject_digest=self._digest(authority_bytes + archive),
            result_digest=prepared.manifest.bundle_digest,
            challenge=f"challenge:{hashlib.sha256(challenge).hexdigest()}",
            issued_at=now.astimezone(UTC),
            expires_at=now.astimezone(UTC) + _DECISION_LIFETIME,
        )
        preview = PublicationPreviewV2(
            payload=payload,
            manifest=prepared.manifest,
            snapshot_digest=snapshot_digest,
            authority_digest=prepared.manifest.authority_digest,
            authority_sequence=authority.registry.sequence,
            recipient_key_ids=prepared.manifest.recipient_key_ids,
            local_device_certificate_id=authority.local_device_certificate_id,
            branch=prepared.branch,
        )
        self._pending = (
            preview,
            prepared,
            archive,
            authority_bytes,
            authority.publication_base_commit,
        )
        return preview

    def preview(self, *, now: datetime) -> PublicationPreview | PublicationPreviewV2:
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("publication time must be UTC")
        snapshot, archive = self._capture()
        current_authority = self._current_authority()
        if type(current_authority) is PublicationAuthorityV2:
            return self._preview_v2(snapshot, current_authority, now=now.astimezone(UTC))
        if type(current_authority) is not PublicationAuthority:
            raise ValueError("publication authority unavailable")
        recipients, signing, parent_digest, parent_commit, authority_bytes = (
            self._authority_material(
                current_authority,
                project_id=snapshot.project_id,
                repository_id=snapshot.repository_id,
            )
        )
        if any(
            item.project_id != snapshot.project_id or item.repository_id != snapshot.repository_id
            for item in recipients
        ):
            raise ValueError("publication recipient binding changed")
        recipient_keys = {
            item.key_id: base64.urlsafe_b64decode(
                item.public_key + "=" * (-len(item.public_key) % 4)
            )
            for item in recipients
        }
        artifacts = seal_state_payload(
            archive,
            project_id=snapshot.project_id,
            repository_id=snapshot.repository_id,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent_digest,
            created_at=now.astimezone(UTC),
            recipient_public_keys=recipient_keys,
            signing_private_keys=signing,
        )
        manifest = TeamStateManifest.model_validate_json(artifacts.manifest)
        digest_hex = manifest.bundle_digest.removeprefix("sha256:")
        prepared = PreparedPublication(
            repository_id=snapshot.repository_id,
            branch=f"intent-publication/{digest_hex}",
            manifest=manifest,
            manifest_bytes=artifacts.manifest,
            bundle=artifacts.bundle,
            signatures=artifacts.signatures,
            bundle_path=artifacts.bundle_path,
            signature_path=artifacts.signature_path,
        )
        challenge = self._challenge_source()
        if type(challenge) is not bytes or len(challenge) < 16:
            raise ValueError("publication challenge unavailable")
        snapshot_digest = self._digest(archive)
        payload = HumanDecisionPayload(
            project_id=snapshot.project_id,
            repository_id=self._decision_repository_id,
            actor=self._runtime.config.local_actor,
            action=DecisionAction.PUBLISH_STATE,
            graph_version=snapshot.graph_version,
            parent_bundle_digest=parent_digest or _GENESIS_PARENT,
            subject=DecisionSubject(
                kind="publication",
                id=f"publication:{snapshot_digest.removeprefix('sha256:')}",
            ),
            subject_digest=self._digest(authority_bytes + archive),
            result_digest=manifest.bundle_digest,
            challenge=f"challenge:{hashlib.sha256(challenge).hexdigest()}",
            issued_at=now.astimezone(UTC),
            expires_at=now.astimezone(UTC) + _DECISION_LIFETIME,
        )
        preview = PublicationPreview(
            payload=payload,
            manifest=manifest,
            snapshot_digest=snapshot_digest,
            recipient_key_ids=manifest.recipient_key_ids,
            branch=prepared.branch,
        )
        self._pending = (preview, prepared, archive, authority_bytes, parent_commit)
        return preview

    def pending_publication(self) -> PreparedPublication:
        """Return only the encrypted draft; it carries no human authority."""
        if self._pending is None:
            raise ValueError("publication draft unavailable")
        # Task 7 broadens the transport/receipt model; retain its v1 static seam meanwhile.
        return cast(PreparedPublication, self._pending[1])

    def recover_device_preview(
        self, prepared: PreparedPublicationV2, *, device_store: DeviceBundleDecryptor, now: datetime
    ) -> PublicationPreviewV2:
        """Reauthorize the same durable v2 artifacts without exporting a recipient key."""
        try:
            current = self.preview(now=now)
            authority = self._current_authority()
            pending = self._pending
            if (
                type(prepared) is not PreparedPublicationV2
                or type(current) is not PublicationPreviewV2
                or type(authority) is not PublicationAuthorityV2
                or pending is None
            ):
                raise ValueError("publication draft changed")
            _, _, archive, authority_bytes, parent_commit = pending
            manifest = prepared.manifest
            if (
                prepared.authority != authority.registry
                or manifest.parent_bundle_digest != current.manifest.parent_bundle_digest
                or manifest.graph_version != current.manifest.graph_version
                or manifest.created_at > now
            ):
                raise ValueError("publication draft changed")
            verify_v2_envelope(
                manifest, prepared.envelope, authority.registry.root, authority.registry, now
            )
            certificate = next(
                c
                for c in authority.registry.device_certificates
                if c.certificate_id == authority.local_device_certificate_id
            )
            aad = canonical_authenticated_context_bytes(
                AuthenticatedBundleContextV2(
                    project_id=manifest.project_id,
                    repository_id=manifest.repository_id,
                    graph_version=manifest.graph_version,
                    parent_bundle_digest=cast(str, manifest.parent_bundle_digest),
                    recipient_key_ids=manifest.recipient_key_ids,
                    authority_digest=manifest.authority_digest,
                    authority_epoch=manifest.authority_epoch,
                    authority_sequence=authority.registry.sequence,
                    root_key_id=manifest.root_key_id,
                    created_at=manifest.created_at,
                )
            )
            if (
                device_store.decrypt_bundle(
                    certificate.claims.recipient_key_id, prepared.bundle, aad
                )
                != archive
            ):
                raise ValueError("publication draft changed")
            payload = current.payload.model_copy(update={"result_digest": manifest.bundle_digest})
            preview = replace(current, payload=payload, manifest=manifest, branch=prepared.branch)
            self._pending = (preview, prepared, archive, authority_bytes, parent_commit)
            return preview
        except BaseException:
            self._pending = None
            raise

    def recover_preview(
        self,
        prepared: PreparedPublication,
        *,
        recipient_private_key: bytes,
        now: datetime,
    ) -> PublicationPreview:
        """Reauthenticate an encrypted draft against current state and issue fresh authority."""
        plaintext = b""
        signing: dict[str, bytes] = {}
        try:
            prepared = PreparedPublication.model_validate(prepared.model_dump(mode="python"))
            current = self.preview(now=now)
            if type(current) is not PublicationPreview:
                raise ValueError("publication draft changed")
            pending = self._pending
            assert pending is not None
            _, _, archive, authority_bytes, parent_commit = pending
            manifest = prepared.manifest
            if (
                manifest.project_id != current.manifest.project_id
                or manifest.repository_id != current.manifest.repository_id
                or manifest.graph_version != current.manifest.graph_version
                or manifest.parent_bundle_digest != current.manifest.parent_bundle_digest
                or manifest.recipient_key_ids != current.manifest.recipient_key_ids
                or manifest.required_signature_ids != current.manifest.required_signature_ids
                or manifest.created_at > now
            ):
                raise ValueError("publication draft changed")
            envelope = StateSignatureEnvelope.model_validate_json(prepared.signatures)
            signed = json.dumps(
                {
                    "schema_version": 1,
                    "manifest_digest": self._digest(prepared.manifest_bytes),
                    "bundle_digest": manifest.bundle_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if (
                envelope.manifest_digest != self._digest(prepared.manifest_bytes)
                or envelope.bundle_digest != manifest.bundle_digest
                or tuple(item.signature_id for item in envelope.signatures)
                != manifest.required_signature_ids
            ):
                raise ValueError("publication draft signatures changed")
            current_authority = self._current_authority()
            if type(current_authority) is not PublicationAuthority:
                raise ValueError("publication authority changed")
            _, signing, _, _, _ = self._authority_material(
                current_authority,
                project_id=manifest.project_id,
                repository_id=manifest.repository_id,
            )
            for signature in envelope.signatures:
                Ed25519PrivateKey.from_private_bytes(
                    signing[signature.signature_id]
                ).public_key().verify(
                    base64.urlsafe_b64decode(
                        signature.signature + "=" * (-len(signature.signature) % 4)
                    ),
                    signed,
                )
            aad = _manifest_aad(
                project_id=manifest.project_id,
                repository_id=manifest.repository_id,
                graph_version=manifest.graph_version,
                parent_bundle_digest=manifest.parent_bundle_digest,
                created_at=manifest.created_at,
                recipient_key_ids=manifest.recipient_key_ids,
                required_signature_ids=manifest.required_signature_ids,
            )
            plaintext = decrypt_bundle(
                EncryptedBundle.model_validate_json(prepared.bundle), recipient_private_key, aad
            )
            if plaintext != archive:
                raise ValueError("publication draft snapshot changed")
            payload = HumanDecisionPayload.model_validate_json(
                current.payload.model_copy(
                    update={"result_digest": manifest.bundle_digest}
                ).model_dump_json()
            )
            preview = PublicationPreview(
                payload=payload,
                manifest=manifest,
                snapshot_digest=current.snapshot_digest,
                recipient_key_ids=manifest.recipient_key_ids,
                branch=prepared.branch,
            )
            self._pending = (preview, prepared, archive, authority_bytes, parent_commit)
            return preview
        except BaseException:
            self._pending = None
            raise
        finally:
            plaintext = b""
            recipient_private_key = b""
            signing.clear()

    def prepare(
        self,
        decision: VerifiedHumanDecision,
        *,
        now: datetime,
    ) -> PreparedPublication:
        pending = self._pending
        if pending is None or type(decision) is not VerifiedHumanDecision:
            raise ValueError("publication decision unavailable")
        preview, prepared, expected_archive, expected_authority, parent_commit = pending
        if type(preview) is PublicationPreviewV2 and type(prepared) is PreparedPublicationV2:
            credential = decision.credential
            if (
                decision.payload != preview.payload
                or decision.verified_at < preview.payload.issued_at
                or decision.verified_at > preview.payload.expires_at
                or now < decision.verified_at
                or now > preview.payload.expires_at
                or credential.local_only
            ):
                raise ValueError("publication decision changed")
            snapshot, _archive_v1 = self._capture()
            current = self._current_authority()
            if type(current) is not PublicationAuthorityV2:
                raise ValueError("publication authority changed")
            certificate = {
                item.certificate_id: item for item in current.registry.device_certificates
            }.get(current.local_device_certificate_id)
            member = next(
                (m for m in current.registry.members if m.member_id == current.local_member_id),
                None,
            )
            if (
                certificate is None
                or member is None
                or member.status != "active"
                or certificate.claims.member_id != member.member_id
                or credential.actor != preview.payload.actor
                or decision.payload.actor != preview.payload.actor
                or credential.project_id != snapshot.project_id
                or credential.repository_id != self._decision_repository_id
                or credential.github_account_id != str(certificate.claims.github_account_id)
                or credential.github_login != certificate.claims.github_login
                or not credential_matches_digest(
                    credential, certificate.claims.webauthn_credential_digest
                )
                or build_archive_v2(snapshot, current.registry) != expected_archive
                or canonical_authority_bytes(current.registry) != expected_authority
                or current.publication_base_commit != parent_commit
                or current.remote_state.manifest.bundle_digest
                != prepared.manifest.parent_bundle_digest
                or snapshot.graph_version != prepared.manifest.graph_version
            ):
                raise ValueError("publication state changed")
            self._publisher.publish(cast(PreparedPublication, prepared), base_commit=parent_commit)
            self._pending = None
            self._publication_base_commit = None
            return cast(PreparedPublication, prepared)
        if type(preview) is not PublicationPreview or type(prepared) is not PreparedPublication:
            raise ValueError("publication draft unavailable")
        credential = decision.credential
        if (
            decision.payload != preview.payload
            or decision.verified_at < preview.payload.issued_at
            or decision.verified_at > preview.payload.expires_at
            or now < decision.verified_at
            or now > preview.payload.expires_at
            or credential.local_only
            or credential.project_id != preview.payload.project_id
            or credential.repository_id != preview.payload.repository_id
            or credential.actor != preview.payload.actor
        ):
            raise ValueError("publication decision changed")
        snapshot, archive = self._capture()
        current_authority = self._current_authority()
        if type(current_authority) is not PublicationAuthority:
            raise ValueError("publication authority changed")
        recipients, _signing, current_parent, current_commit, authority_bytes = (
            self._authority_material(
                current_authority,
                project_id=snapshot.project_id,
                repository_id=snapshot.repository_id,
            )
        )
        exact_recipients = tuple(
            item
            for item in recipients
            if isinstance(item, RecipientRecord)
            and item.project_id == credential.project_id
            and item.repository_id == self._repository_id
            and item.actor == credential.actor
            and item.webauthn_credential_id == credential.credential_id
            and item.webauthn_credential_public_key == credential.public_key
            and item.github_account_id == credential.github_account_id
            and item.github_login == credential.github_login
        )
        if len(exact_recipients) != 1:
            raise ValueError("publication decision changed")
        if (
            archive != expected_archive
            or authority_bytes != expected_authority
            or current_parent != prepared.manifest.parent_bundle_digest
            or current_commit != parent_commit
            or snapshot.graph_version != prepared.manifest.graph_version
        ):
            raise ValueError("publication state changed")
        self._publisher.publish(prepared, base_commit=parent_commit)
        self._pending = None
        self._publication_base_commit = None
        return prepared
