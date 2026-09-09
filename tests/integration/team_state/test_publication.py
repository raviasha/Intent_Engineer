"""Reviewed deterministic team-state publication preparation."""

from __future__ import annotations

import base64
import subprocess
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.control_plane.models import CredentialRecord, DecisionAction
from intent_engineering.control_plane.service import ControlPlaneService
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.team_state import publication as publication_module
from intent_engineering.team_state.models import (
    CanonicalStateFile,
    CanonicalStateSnapshot,
    CiRecipientRecord,
    PreparedPublication,
    RecipientRecord,
    RemoteStateSnapshot,
    TeamStateManifest,
)
from intent_engineering.team_state.publication import (
    PublicationAuthority,
    PublicationCleanupError,
    PublicationService,
    TemporaryWorktreePublisher,
)
from tests.helpers.shared_state import (
    REPOSITORY_ID,
    artifacts,
    canonical_files,
    git,
    install_state_ref,
    keys,
    ready_project,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
LOCAL_REPOSITORY_ID = "repo:sha256:" + "1" * 64


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _recipient(private_key: X25519PrivateKey) -> RecipientRecord:
    return RecipientRecord(
        key_id="recipient:alice",
        project_id="project",
        repository_id=REPOSITORY_ID,
        actor="local",
        github_account_id="123",
        github_login="alice",
        public_key=_b64(private_key.public_key().public_bytes_raw()),
        webauthn_credential_id=_b64(b"credential"),
        webauthn_credential_public_key=_b64(b"public-credential-key"),
        enrolled_at=NOW,
    )


class RecordingPublisher:
    def __init__(self) -> None:
        self.publications: list[PreparedPublication] = []

    def publish(self, publication: PreparedPublication, *, base_commit: str | None) -> None:
        assert base_commit is None
        self.publications.append(publication)


def _credential() -> CredentialRecord:
    return CredentialRecord(
        id="credential:alice",
        project_id="project",
        repository_id=LOCAL_REPOSITORY_ID,
        actor="local",
        credential_id=_b64(b"credential"),
        public_key=_b64(b"public-credential-key"),
        sign_count=1,
        created_at=NOW,
        local_only=False,
        github_account_id="123",
        github_login="alice",
    )


def _service(
    root: Path, authority: PublicationAuthority, publisher: object
) -> tuple[object, PublicationService]:
    runtime = load_runtime(root)
    return runtime, PublicationService(
        runtime,
        repository_id=REPOSITORY_ID,
        decision_repository_id=LOCAL_REPOSITORY_ID,
        authority=lambda: authority,
        publisher=publisher,  # type: ignore[arg-type]
        challenge_source=lambda: b"p" * 32,
    )


def test_v2_publication_authority_is_derived_from_verified_parent_and_local_trust() -> None:
    """Catches callers replacing registry recipient or signer policy."""
    from intent_engineering.team_state.authority import authority_digest
    from intent_engineering.team_state.local_trust import LocalTrustConfigV2
    from intent_engineering.team_state.publication import (
        PublicationAuthorityV2,
        authority_from_verified_state,
    )
    from intent_engineering.team_state.restore import VerifiedReleaseV2
    from tests.unit.team_state.test_authority import (
        NOW as AUTHORITY_NOW,
    )
    from tests.unit.team_state.test_authority import (
        _real_certificate,
        _RootStore,
        _verified_authority,
    )

    root_store = _RootStore(b"r" * 32)
    certificate, _private = _real_certificate(root_store, signing_private=b"s" * 32)
    registry = _verified_authority(root_store, (certificate,), roles=("sponsor",))
    manifest = publication_module.TeamStateManifestV2(
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=2,
        parent_bundle_digest="sha256:" + "1" * 64,
        bundle_digest="sha256:" + "2" * 64,
        bundle_size=100,
        recipient_key_ids=registry.active_recipient_key_ids(),
        authority_digest=authority_digest(registry),
        authority_epoch=registry.authority_epoch,
        root_key_id=registry.root.root_key_id,
        created_at=AUTHORITY_NOW,
    )
    restored = VerifiedReleaseV2(
        manifest=manifest,
        manifest_bytes=manifest.canonical_bytes(),
        authority=registry,
        commit="a" * 40,
    )
    trust = LocalTrustConfigV2(
        project_id="project",
        repository_id=REPOSITORY_ID,
        root=registry.root,
        member_id=certificate.claims.member_id,
        device_certificate_id=certificate.certificate_id,
        recipient_key_id=certificate.claims.recipient_key_id,
        signature_id=certificate.claims.signature_id,
        accepted_authority_digest=authority_digest(registry),
        accepted_authority_sequence=registry.sequence,
        accepted_bundle_digest=manifest.bundle_digest,
    )

    result = authority_from_verified_state(restored, trust)

    assert type(result) is PublicationAuthorityV2
    assert result.registry == registry
    assert result.local_member_id == certificate.claims.member_id
    assert result.local_device_certificate_id == certificate.certificate_id
    assert result.remote_state is restored
    assert result.publication_base_commit == "a" * 40
    stale = trust.model_copy(update={"accepted_authority_sequence": registry.sequence + 1})
    with pytest.raises(ValueError, match="version two publication authority unavailable"):
        authority_from_verified_state(restored, stale)


def test_prepare_v2_publication_uses_exact_registry_recipients_and_local_certificate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches omitted/extra recipients, caller signer policy, or root-key use."""
    from intent_engineering.team_state import restore as restore_module
    from intent_engineering.team_state.authority import (
        authority_digest,
        derive_recipient_key_id,
        issue_device_certificate,
    )
    from intent_engineering.team_state.local_trust import LocalTrustConfigV2
    from intent_engineering.team_state.models import DeviceRevocationV2, RestoredSnapshotV2
    from intent_engineering.team_state.publication import (
        authority_from_verified_state,
        prepare_v2_publication,
    )
    from intent_engineering.team_state.restore import VerifiedReleaseV2, verify_v2_release
    from tests.unit.team_state.test_authority import (
        NOW as AUTHORITY_NOW,
    )
    from tests.unit.team_state.test_authority import (
        _real_certificate,
        _RootStore,
        _verified_authority,
    )

    source = tmp_path / "project"
    source.mkdir()
    ready_project(source)
    files = canonical_files(source)
    snapshot = CanonicalStateSnapshot(
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=1,
        files=tuple(CanonicalStateFile(path=path, content=files[path]) for path in sorted(files)),
    )
    root_store = _RootStore(b"r" * 32)
    a_certificate, _a_private = _real_certificate(
        root_store, signing_private=b"a" * 32, serial=1, device_digit="1"
    )
    b_certificate, b_private = _real_certificate(
        root_store,
        signing_private=b"b" * 32,
        account_id=5678,
        login="bob-dev",
        serial=2,
        device_digit="2",
    )
    a_recipient = X25519PrivateKey.from_private_bytes(b"A" * 32)
    b_recipient = X25519PrivateKey.from_private_bytes(b"B" * 32)
    ci_recipient = X25519PrivateKey.from_private_bytes(b"C" * 32)

    def bind_recipient(certificate, private):
        public = _b64(private.public_key().public_bytes_raw())
        values = certificate.claims.model_dump(mode="python")
        values.update(
            recipient_public_key=public,
            recipient_key_id=derive_recipient_key_id("project", REPOSITORY_ID, public),
        )
        return issue_device_certificate(
            publication_module.DeviceCertificateClaimsV2(**values), root_store
        )

    a_certificate = bind_recipient(a_certificate, a_recipient)
    b_certificate = bind_recipient(b_certificate, b_recipient)
    registry = _verified_authority(
        root_store, (a_certificate, b_certificate), roles=("sponsor", "member")
    )
    ci = CiRecipientRecord(
        project_id="project",
        repository_id=REPOSITORY_ID,
        runner_id="intent-state",
        public_key=_b64(ci_recipient.public_key().public_bytes_raw()),
    )
    registry = publication_module.TeamAuthorityRegistryV2(
        **{**registry.model_dump(mode="python"), "ci_recipient": ci}
    )
    parent = publication_module.TeamStateManifestV2(
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=1,
        parent_bundle_digest="sha256:" + "1" * 64,
        bundle_digest="sha256:" + "2" * 64,
        bundle_size=100,
        recipient_key_ids=registry.active_recipient_key_ids(),
        authority_digest=authority_digest(registry),
        authority_epoch=registry.authority_epoch,
        root_key_id=registry.root.root_key_id,
        created_at=AUTHORITY_NOW,
    )
    restored = VerifiedReleaseV2(
        manifest=parent,
        manifest_bytes=parent.canonical_bytes(),
        authority=registry,
        commit="a" * 40,
    )
    trust = LocalTrustConfigV2(
        project_id="project",
        repository_id=REPOSITORY_ID,
        root=registry.root,
        member_id=b_certificate.claims.member_id,
        device_certificate_id=b_certificate.certificate_id,
        recipient_key_id=b_certificate.claims.recipient_key_id,
        signature_id=b_certificate.claims.signature_id,
        accepted_authority_digest=authority_digest(registry),
        accepted_authority_sequence=registry.sequence,
        accepted_bundle_digest=parent.bundle_digest,
    )

    class DeviceSigner:
        def sign(self, signature_id: str, preimage: bytes) -> bytes:
            assert signature_id == b_certificate.claims.signature_id
            return Ed25519PrivateKey.from_private_bytes(b_private).sign(preimage)

    prepared = prepare_v2_publication(
        snapshot=snapshot,
        authority=authority_from_verified_state(restored, trust),
        device_signer=DeviceSigner(),
        now=AUTHORITY_NOW,
    )

    assert prepared.manifest.recipient_key_ids == registry.active_recipient_key_ids()
    assert prepared.manifest.authority_digest == authority_digest(registry)
    assert prepared.authority == registry
    assert prepared.envelope.certificates == (b_certificate,)
    assert prepared.manifest.parent_bundle_digest == parent.bundle_digest
    for key_id, private in (
        (a_certificate.claims.recipient_key_id, a_recipient),
        (b_certificate.claims.recipient_key_id, b_recipient),
        (ci.key_id, ci_recipient),
    ):
        verified = verify_v2_release(
            manifest_bytes=prepared.manifest_bytes,
            bundle_bytes=prepared.bundle,
            envelope_bytes=prepared.signatures,
            parent=restored,
            recipient_key_id=key_id,
            recipient_private_key=private.private_bytes_raw(),
            commit="b" * 40,
            now=AUTHORITY_NOW,
        )
        assert verified.authority == registry
        assert verified.snapshot == snapshot

    import inspect

    assert tuple(inspect.signature(prepare_v2_publication).parameters) == (
        "snapshot",
        "authority",
        "device_signer",
        "now",
    )
    for caller_policy in (
        {"recipients": ()},
        {"signing_private_keys": {}},
    ):
        with pytest.raises(TypeError):
            prepare_v2_publication(
                **{
                    "snapshot": snapshot,
                    "authority": authority_from_verified_state(restored, trust),
                    "device_signer": DeviceSigner(),
                    "now": AUTHORITY_NOW,
                    **caller_policy,
                }
            )

    wrong_parent_manifest = parent.model_copy(update={"bundle_digest": "sha256:" + "3" * 64})
    wrong_parent = VerifiedReleaseV2(
        manifest=wrong_parent_manifest,
        manifest_bytes=wrong_parent_manifest.canonical_bytes(),
        authority=registry,
        commit="a" * 40,
    )
    with pytest.raises(ValueError, match="version two release verification failed"):
        verify_v2_release(
            manifest_bytes=prepared.manifest_bytes,
            bundle_bytes=prepared.bundle,
            envelope_bytes=prepared.signatures,
            parent=wrong_parent,
            recipient_key_id=ci.key_id,
            recipient_private_key=ci_recipient.private_bytes_raw(),
            commit="b" * 40,
            now=AUTHORITY_NOW,
        )

    changed_ci_private = X25519PrivateKey.from_private_bytes(b"D" * 32)
    changed_ci = CiRecipientRecord(
        project_id="project",
        repository_id=REPOSITORY_ID,
        runner_id=ci.runner_id,
        public_key=_b64(changed_ci_private.public_key().public_bytes_raw()),
    )
    changed_ci_registry = publication_module.TeamAuthorityRegistryV2(
        **{**registry.model_dump(mode="python"), "ci_recipient": changed_ci}
    )
    changed_ci_manifest = parent.model_copy(
        update={
            "recipient_key_ids": changed_ci_registry.active_recipient_key_ids(),
            "authority_digest": authority_digest(changed_ci_registry),
        }
    )
    changed_ci_parent = VerifiedReleaseV2(
        manifest=changed_ci_manifest,
        manifest_bytes=changed_ci_manifest.canonical_bytes(),
        authority=changed_ci_registry,
        commit="a" * 40,
    )
    with pytest.raises(ValueError, match="version two release verification failed"):
        verify_v2_release(
            manifest_bytes=prepared.manifest_bytes,
            bundle_bytes=prepared.bundle,
            envelope_bytes=prepared.signatures,
            parent=changed_ci_parent,
            recipient_key_id=a_certificate.claims.recipient_key_id,
            recipient_private_key=a_recipient.private_bytes_raw(),
            commit="b" * 40,
            now=AUTHORITY_NOW,
        )

    revoked_member = next(
        item for item in registry.members if item.member_id == b_certificate.claims.member_id
    ).model_copy(update={"status": "revoked", "revoked_at": AUTHORITY_NOW})
    unchanged_members = tuple(
        revoked_member if item.member_id == revoked_member.member_id else item
        for item in registry.members
    )
    revoked_registry = publication_module.TeamAuthorityRegistryV2(
        **{
            **registry.model_dump(mode="python"),
            "sequence": registry.sequence + 1,
            "previous_authority_digest": authority_digest(registry),
            "members": unchanged_members,
            "revocations": (
                DeviceRevocationV2(
                    certificate_id=b_certificate.certificate_id,
                    revoked_at=AUTHORITY_NOW,
                    reason="member-removed",
                    sponsor_member_id=a_certificate.claims.member_id,
                ),
            ),
        }
    )
    revoked_manifest = parent.model_copy(
        update={
            "recipient_key_ids": revoked_registry.active_recipient_key_ids(),
            "authority_digest": authority_digest(revoked_registry),
        }
    )
    revoked_parent = VerifiedReleaseV2(
        manifest=revoked_manifest,
        manifest_bytes=revoked_manifest.canonical_bytes(),
        authority=revoked_registry,
        commit="a" * 40,
    )
    revoked_trust = trust.model_copy(
        update={
            "accepted_authority_digest": authority_digest(revoked_registry),
            "accepted_authority_sequence": revoked_registry.sequence,
        }
    )
    with pytest.raises(ValueError, match="version two publication authority unavailable"):
        authority_from_verified_state(revoked_parent, revoked_trust)

    malformed = CanonicalStateSnapshot(
        project_id=snapshot.project_id,
        repository_id=snapshot.repository_id,
        graph_version=snapshot.graph_version,
        files=tuple(
            item.model_copy(update={"content": b"not: [valid"})
            if item.path == "graph.yaml"
            else item
            for item in snapshot.files
        ),
    )
    monkeypatch.setattr(
        restore_module,
        "validate_archive_v2",
        lambda _content: RestoredSnapshotV2(snapshot=malformed, authority=registry),
    )
    with pytest.raises(ValueError, match="version two release verification failed"):
        verify_v2_release(
            manifest_bytes=prepared.manifest_bytes,
            bundle_bytes=prepared.bundle,
            envelope_bytes=prepared.signatures,
            parent=restored,
            recipient_key_id=ci.key_id,
            recipient_private_key=ci_recipient.private_bytes_raw(),
            commit="b" * 40,
            now=AUTHORITY_NOW,
        )

    monkeypatch.setattr(
        restore_module,
        "validate_archive_v2",
        lambda _content: RestoredSnapshotV2(
            snapshot=snapshot,
            authority=changed_ci_registry,
        ),
    )
    with pytest.raises(ValueError, match="version two release verification failed"):
        verify_v2_release(
            manifest_bytes=prepared.manifest_bytes,
            bundle_bytes=prepared.bundle,
            envelope_bytes=prepared.signatures,
            parent=restored,
            recipient_key_id=ci.key_id,
            recipient_private_key=ci_recipient.private_bytes_raw(),
            commit="b" * 40,
            now=AUTHORITY_NOW,
        )
    monkeypatch.undo()

    from dataclasses import replace
    from datetime import timedelta

    with pytest.raises(ValueError, match="version two publication preparation failed"):
        prepare_v2_publication(
            snapshot=snapshot,
            authority=authority_from_verified_state(restored, trust),
            device_signer=DeviceSigner(),
            now=b_certificate.claims.expires_at + timedelta(minutes=6),
        )
    forged_registry = publication_module.TeamAuthorityRegistryV2(
        **{
            **registry.model_dump(mode="python"),
            "sequence": registry.sequence + 1,
            "previous_authority_digest": authority_digest(registry),
        }
    )
    forged = replace(authority_from_verified_state(restored, trust), registry=forged_registry)
    with pytest.raises(ValueError, match="version two publication preparation failed"):
        prepare_v2_publication(
            snapshot=snapshot,
            authority=forged,
            device_signer=DeviceSigner(),
            now=AUTHORITY_NOW,
        )

    class Cancelled(BaseException):
        pass

    marker = "PRIVATE-V2-PUBLICATION-CANCEL-7219"
    cancellation = Cancelled(marker)
    cancellation.private = marker
    cancellation.__cause__ = RuntimeError(marker)
    retained_tracebacks = []

    class CancellingSigner:
        secret = marker

        def sign(self, _signature_id: str, _preimage: bytes) -> bytes:
            _secret = marker
            try:
                raise cancellation
            except BaseException as caught:
                retained_tracebacks.append(caught.__traceback__)
                raise

    with pytest.raises(Cancelled) as caught:
        prepare_v2_publication(
            snapshot=snapshot,
            authority=authority_from_verified_state(restored, trust),
            device_signer=CancellingSigner(),
            now=AUTHORITY_NOW,
        )
    assert caught.value is cancellation
    assert caught.value.args == ()
    assert caught.value.__dict__ == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert len(retained_tracebacks) == 1
    for frame, _line in traceback.walk_tb(retained_tracebacks[0]):
        assert marker not in repr(frame.f_locals)
    for frame, _line in traceback.walk_tb(caught.value.__traceback__):
        if frame.f_globals.get("__name__") != "intent_engineering.team_state.publication":
            continue
        assert frame.f_locals.get("snapshot") is None
        assert frame.f_locals.get("authority") is None
        assert frame.f_locals.get("device_signer") is None
        assert marker not in repr(frame.f_locals)


def test_prepare_v1_migration_is_dual_signed_and_does_not_advance_local_trust(
    tmp_path: Path,
) -> None:
    """Catches an in-place v1 trust edit or first-v2 release lacking either authority."""
    import hashlib

    from intent_engineering.team_state.authority import (
        canonical_state_signature_preimage,
        derive_recipient_key_id,
        derive_signature_id,
    )
    from intent_engineering.team_state.keys import _key_id
    from intent_engineering.team_state.local_trust import LocalTrustConfig, LocalTrustConfigV2
    from intent_engineering.team_state.models import TeamStateManifest, canonical_manifest_bytes
    from intent_engineering.team_state.publication import (
        MigrationKeyAuthorities,
        authority_from_verified_state,
        prepare_v1_migration,
        prepare_v2_publication,
        preview_v1_migration,
    )
    from intent_engineering.team_state.restore import (
        TrustedSigningKey,
        VerifiedV1Release,
        verify_v1_migration,
        verify_v2_migration_release,
        verify_v2_release,
    )
    from tests.unit.team_state.test_authority import _RootStore

    source = tmp_path / "project"
    source.mkdir()
    ready_project(source)
    files = canonical_files(source)
    snapshot = CanonicalStateSnapshot(
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=1,
        files=tuple(CanonicalStateFile(path=path, content=files[path]) for path in sorted(files)),
    )
    legacy_private = Ed25519PrivateKey.from_private_bytes(b"l" * 32)
    legacy_public = legacy_private.public_key().public_bytes_raw()
    recipient_private = X25519PrivateKey.from_private_bytes(b"a" * 32)
    recipient = _recipient(recipient_private)
    temporary_trust = LocalTrustConfig.model_construct(
        project_id="project",
        repository_id=REPOSITORY_ID,
        recipient_key_id=recipient.key_id,
        recipient=recipient,
        signing_public_keys={"signer:legacy": _b64(legacy_public) + "="},
    )
    recipient = recipient.model_copy(
        update={"key_id": _key_id(temporary_trust.enrollment_binding())}
    )
    legacy_trust = LocalTrustConfig(
        project_id="project",
        repository_id=REPOSITORY_ID,
        recipient_key_id=recipient.key_id,
        recipient=recipient,
        signing_public_keys={"signer:legacy": _b64(legacy_public) + "="},
    )
    legacy_manifest = TeamStateManifest(
        project_id="project",
        repository_id=REPOSITORY_ID,
        graph_version=1,
        bundle_digest="sha256:" + "a" * 64,
        bundle_size=100,
        recipient_key_ids=(recipient.key_id,),
        required_signature_ids=("signer:legacy",),
        created_at=NOW,
    )
    current = VerifiedV1Release(
        manifest=legacy_manifest,
        manifest_bytes=canonical_manifest_bytes(legacy_manifest),
        signing_keys=(TrustedSigningKey("signer:legacy", legacy_public),),
        snapshot=snapshot,
    )
    ci_private = X25519PrivateKey.from_private_bytes(b"c" * 32)
    ci = CiRecipientRecord(
        project_id="project",
        repository_id=REPOSITORY_ID,
        runner_id="intent-runner",
        public_key=_b64(ci_private.public_key().public_bytes_raw()),
    )
    credential = _credential()
    root_store = _RootStore(b"r" * 32)
    root_signatures: list[bytes] = []
    root_sign = root_store.sign

    def record_root_signature(root_key_id: str, preimage: bytes) -> bytes:
        root_signatures.append(preimage)
        return root_sign(root_key_id, preimage)

    root_store.sign = record_root_signature  # type: ignore[method-assign]
    device_private = Ed25519PrivateKey.from_private_bytes(b"s" * 32)
    device_signatures: list[bytes] = []
    legacy_signatures: list[bytes] = []

    class DeviceSigner:
        def create_for_existing_recipient(self, binding, recipient_public_key):
            from intent_engineering.team_state.keys import DevicePublicMaterial

            signing_public = device_private.public_key().public_bytes_raw()
            return DevicePublicMaterial(
                device_id=binding.device_id,
                recipient_key_id=derive_recipient_key_id(
                    binding.project_id, binding.repository_id, _b64(recipient_public_key)
                ),
                recipient_public_key=recipient_public_key,
                signature_id=derive_signature_id(
                    binding.project_id, binding.repository_id, _b64(signing_public)
                ),
                signing_public_key=signing_public,
            )

        def sign(self, signature_id, preimage):
            assert signature_id == derive_signature_id(
                "project", REPOSITORY_ID, _b64(device_private.public_key().public_bytes_raw())
            )
            device_signatures.append(preimage)
            return device_private.sign(preimage)

    class LegacySigner:
        def public_keys(self):
            return {"signer:legacy": legacy_public}

        def sign(self, signature_id, preimage):
            assert signature_id == "signer:legacy"
            legacy_signatures.append(preimage)
            return legacy_private.sign(preimage)

    authorities = MigrationKeyAuthorities(
        root_store=root_store,
        device_signer=DeviceSigner(),
        legacy_signer=LegacySigner(),
    )
    before = legacy_trust.model_dump(mode="python")
    preview = preview_v1_migration(
        current=current,
        legacy_trust=legacy_trust,
        ci_recipient=ci,
        sponsor_credential=credential,
        challenge="challenge:" + "f" * 64,
        now=NOW,
        authorities=authorities,
    )
    assert preview.project_id == "project"
    assert preview.repository_id == REPOSITORY_ID
    assert preview.graph_version == snapshot.graph_version
    assert preview.legacy_parent_bundle_digest == legacy_manifest.bundle_digest
    assert preview.legacy_signature_ids == ("signer:legacy",)
    assert preview.ci_recipient_key_id == ci.key_id
    assert preview.root_key_id == preview.authority.root.root_key_id
    assert preview.device_signature_id == preview.certificate.claims.signature_id
    assert preview.device_certificate_id == preview.certificate.certificate_id
    assert preview.authority_digest == preview.manifest.authority_digest
    assert preview.bundle_digest == preview.manifest.bundle_digest
    assert preview.result_digest == preview.payload.result_digest
    assert preview.subject == preview.payload.subject
    assert preview.subject_digest == preview.payload.subject_digest
    assert preview.canonical_bytes() == preview.canonical_bytes()
    assert len(root_signatures) == 1
    wrong_action = preview.payload.model_copy(
        update={"action": DecisionAction.APPROVE_EXTERNAL_WRITE}
    )
    with pytest.raises(ValueError, match="version one migration preparation failed"):
        prepare_v1_migration(
            current=current,
            legacy_trust=legacy_trust,
            ci_recipient=ci,
            preview=preview,
            sponsor_decision=VerifiedHumanDecision(wrong_action, credential, NOW),
            now=NOW,
            authorities=authorities,
        )
    assert device_signatures == []
    assert legacy_signatures == []
    assert len(root_signatures) == 1
    from dataclasses import replace

    from intent_engineering.control_plane.models import DecisionSubject

    forged_subject_digest = "sha256:" + "9" * 64
    forged_subject = DecisionSubject(
        kind="publication",
        id="publication:v1-migration:" + "9" * 64,
    )
    forged_preview = replace(
        preview,
        subject=forged_subject,
        subject_digest=forged_subject_digest,
        payload=preview.payload.model_copy(
            update={
                "subject": forged_subject,
                "subject_digest": forged_subject_digest,
            }
        ),
    )
    with pytest.raises(ValueError, match="version one migration preparation failed"):
        prepare_v1_migration(
            current=current,
            legacy_trust=legacy_trust,
            ci_recipient=ci,
            preview=forged_preview,
            sponsor_decision=VerifiedHumanDecision(forged_preview.payload, credential, NOW),
            now=NOW,
            authorities=authorities,
        )
    assert device_signatures == []
    assert legacy_signatures == []
    assert len(root_signatures) == 1
    decision = VerifiedHumanDecision(preview.payload, credential, NOW)
    prepared = prepare_v1_migration(
        current=current,
        legacy_trust=legacy_trust,
        ci_recipient=ci,
        preview=preview,
        sponsor_decision=decision,
        now=NOW,
        authorities=authorities,
    )
    assert len(device_signatures) == 1
    assert len(legacy_signatures) == 1
    assert len(root_signatures) == 3

    from datetime import timedelta

    with pytest.raises(ValueError, match="version one migration preparation failed"):
        prepare_v1_migration(
            current=current,
            legacy_trust=legacy_trust,
            ci_recipient=ci,
            preview=preview,
            sponsor_decision=decision,
            now=preview.payload.expires_at + timedelta(seconds=1),
            authorities=authorities,
        )
    assert len(root_signatures) == 3
    assert len(device_signatures) == 1
    assert len(legacy_signatures) == 1

    assert legacy_trust.model_dump(mode="python") == before
    assert prepared.manifest.schema_version == 2
    assert prepared.manifest.parent_bundle_digest == legacy_manifest.bundle_digest
    assert prepared.manifest.migration.legacy_signature_ids == ("signer:legacy",)
    assert (
        prepared.envelope.signatures[0].signature
        != prepared.envelope.migration_proof.legacy_signatures[0].signature
    )
    assert prepared.envelope.signatures[0].signature == _b64(
        device_private.sign(canonical_state_signature_preimage(prepared.manifest))
    )
    verified = verify_v1_migration(
        current=current,
        manifest=prepared.manifest,
        envelope=prepared.envelope,
        root=prepared.authority.root,
        authority=prepared.authority,
        expected_ci_recipient=ci,
        now=NOW,
    )
    assert verified.authority_digest == prepared.manifest.authority_digest
    assert hashlib.sha256(
        prepared.bundle
    ).hexdigest() == prepared.manifest.bundle_digest.removeprefix("sha256:")
    migrated = verify_v2_migration_release(
        manifest_bytes=prepared.manifest_bytes,
        bundle_bytes=prepared.bundle,
        envelope_bytes=prepared.signatures,
        current=current,
        recipient_key_id=ci.key_id,
        recipient_private_key=ci_private.private_bytes_raw(),
        commit="c" * 40,
        now=NOW,
    )
    assert migrated.authority == prepared.authority
    assert migrated.snapshot == snapshot

    v2_trust = LocalTrustConfigV2(
        project_id="project",
        repository_id=REPOSITORY_ID,
        root=migrated.authority.root,
        member_id=preview.certificate.claims.member_id,
        device_certificate_id=preview.certificate.certificate_id,
        recipient_key_id=preview.certificate.claims.recipient_key_id,
        signature_id=preview.certificate.claims.signature_id,
        accepted_authority_digest=migrated.manifest.authority_digest,
        accepted_authority_sequence=migrated.authority.sequence,
        accepted_bundle_digest=migrated.manifest.bundle_digest,
    )
    ordinary = prepare_v2_publication(
        snapshot=snapshot,
        authority=authority_from_verified_state(migrated, v2_trust),
        device_signer=DeviceSigner(),
        now=NOW,
    )
    assert ordinary.authority == migrated.authority
    assert ordinary.manifest.authority_digest == migrated.manifest.authority_digest
    assert ordinary.manifest.recipient_key_ids == migrated.manifest.recipient_key_ids
    assert ordinary.manifest.parent_bundle_digest == migrated.manifest.bundle_digest
    accepted_by_ci = verify_v2_release(
        manifest_bytes=ordinary.manifest_bytes,
        bundle_bytes=ordinary.bundle,
        envelope_bytes=ordinary.signatures,
        parent=migrated,
        recipient_key_id=ci.key_id,
        recipient_private_key=ci_private.private_bytes_raw(),
        commit="d" * 40,
        now=NOW,
    )
    accepted_by_device = verify_v2_release(
        manifest_bytes=ordinary.manifest_bytes,
        bundle_bytes=ordinary.bundle,
        envelope_bytes=ordinary.signatures,
        parent=migrated,
        recipient_key_id=preview.certificate.claims.recipient_key_id,
        recipient_private_key=recipient_private.private_bytes_raw(),
        commit="d" * 40,
        now=NOW,
    )
    assert accepted_by_ci.snapshot == snapshot
    assert accepted_by_device.snapshot == snapshot

    with pytest.raises(ValueError, match="version one migration preview failed"):
        preview_v1_migration(
            current=current,
            legacy_trust=legacy_trust,
            ci_recipient=ci,
            sponsor_credential=credential,
            challenge="challenge:" + "1" * 64,
            now=NOW,
            authorities=MigrationKeyAuthorities(
                root_store=_RootStore(b"s" * 32),
                device_signer=DeviceSigner(),
                legacy_signer=LegacySigner(),
            ),
        )


def _prepared(root: Path) -> PreparedPublication:
    authority = PublicationAuthority(
        recipients=(_recipient(X25519PrivateKey.generate()),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    recorder = RecordingPublisher()
    runtime, service = _service(root, authority, recorder)
    try:
        preview = service.preview(now=NOW)
        return service.prepare(VerifiedHumanDecision(preview.payload, _credential(), NOW), now=NOW)
    finally:
        runtime.close()  # type: ignore[union-attr]


def test_machine_recipient_is_bound_to_human_authorized_publication(tmp_path: Path) -> None:
    """A machine may decrypt but cannot replace the enrolled human's WebAuthn binding."""
    from intent_engineering.team_state.models import CiRecipientRecord

    tmp_path = tmp_path / "project"
    tmp_path.mkdir()
    ready_project(tmp_path)
    machine = CiRecipientRecord(
        project_id="project",
        repository_id=REPOSITORY_ID,
        runner_id="release-01",
        public_key=_b64(X25519PrivateKey.generate().public_key().public_bytes_raw()),
    )
    signer = {"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()}
    for recipients, allowed in (
        ((machine,), False),
        ((_recipient(X25519PrivateKey.generate()), machine), True),
    ):
        authority = PublicationAuthority(
            recipients=recipients, signing_private_keys=signer, remote_state=None
        )
        publisher = RecordingPublisher()
        runtime, service = _service(tmp_path, authority, publisher)
        try:
            preview = service.preview(now=NOW)
            decision = VerifiedHumanDecision(preview.payload, _credential(), NOW)
            if allowed:
                prepared = service.prepare(decision, now=NOW)
                assert machine.key_id in prepared.manifest.recipient_key_ids
            else:
                with pytest.raises(ValueError, match="publication decision changed"):
                    service.prepare(decision, now=NOW)
                assert not publisher.publications
        finally:
            runtime.close()


def test_preview_is_stable_for_one_snapshot_and_prepare_requires_its_exact_decision(
    tmp_path: Path,
) -> None:
    """Catches publication from a different snapshot or WebAuthn result than the reviewed preview."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    runtime = load_runtime(root)
    recipient_key = X25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    authority = PublicationAuthority(
        recipients=(_recipient(recipient_key),),
        signing_private_keys={"signer:release": signer.private_bytes_raw()},
        remote_state=None,
    )
    publisher = RecordingPublisher()
    service = PublicationService(
        runtime,
        repository_id=REPOSITORY_ID,
        decision_repository_id=LOCAL_REPOSITORY_ID,
        authority=lambda: authority,
        publisher=publisher,
        challenge_source=lambda: b"p" * 32,
    )
    try:
        preview = service.preview(now=NOW)

        assert preview.payload.action is DecisionAction.PUBLISH_STATE
        assert preview.payload.result_digest == preview.manifest.bundle_digest
        assert preview.payload.parent_bundle_digest == "sha256:" + "0" * 64
        assert preview.payload.graph_version == 1
        assert preview.recipient_key_ids == ("recipient:alice",)

        forged = preview.payload.model_copy(update={"result_digest": "sha256:" + "9" * 64})
        with pytest.raises(ValueError, match="publication decision changed"):
            service.prepare(
                VerifiedHumanDecision(forged, _credential(), NOW),
                now=NOW,
            )
        assert publisher.publications == []

        prepared = service.prepare(
            VerifiedHumanDecision(preview.payload, _credential(), NOW),
            now=NOW,
        )

        assert prepared.manifest == preview.manifest
        assert publisher.publications == [prepared]
    finally:
        runtime.close()


def test_genesis_publication_binds_an_orphan_state_branch_anchor_and_rejects_drift(
    tmp_path: Path,
) -> None:
    """Catches a bootstrap anchor being copied from code or changed after WebAuthn review."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    anchor = "a" * 40
    current = PublicationAuthority(
        recipients=(_recipient(X25519PrivateKey.generate()),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
        publication_base_commit=anchor,
    )

    class AnchorPublisher:
        def __init__(self) -> None:
            self.base_commit: str | None = None

        def publish(self, publication: PreparedPublication, *, base_commit: str | None) -> None:
            assert publication.manifest.parent_bundle_digest is None
            self.base_commit = base_commit

    publisher = AnchorPublisher()
    runtime = load_runtime(root)
    service = PublicationService(
        runtime,
        repository_id=REPOSITORY_ID,
        decision_repository_id=LOCAL_REPOSITORY_ID,
        authority=lambda: current,
        publisher=publisher,
        challenge_source=lambda: b"p" * 32,
    )
    try:
        preview = service.preview(now=NOW)
        decision = VerifiedHumanDecision(preview.payload, _credential(), NOW)
        current = PublicationAuthority(
            recipients=current.recipients,
            signing_private_keys=current.signing_private_keys,
            remote_state=None,
            publication_base_commit="b" * 40,
        )
        with pytest.raises(ValueError, match="publication state changed"):
            service.prepare(decision, now=NOW)

        current = current.__class__(
            recipients=current.recipients,
            signing_private_keys=current.signing_private_keys,
            remote_state=None,
            publication_base_commit=anchor,
        )
        preview = service.preview(now=NOW)
        service.prepare(VerifiedHumanDecision(preview.payload, _credential(), NOW), now=NOW)
        assert publisher.base_commit == anchor
    finally:
        runtime.close()


def test_equal_plaintext_has_a_stable_snapshot_digest_but_fresh_ciphertext(tmp_path: Path) -> None:
    """Catches randomized encryption contaminating the deterministic reviewed state identity."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    recipient_key = X25519PrivateKey.generate()
    authority = PublicationAuthority(
        recipients=(_recipient(recipient_key),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    runtime, service = _service(root, authority, RecordingPublisher())
    try:
        first = service.preview(now=NOW)
        second = service.preview(now=NOW)
    finally:
        runtime.close()  # type: ignore[union-attr]

    assert first.snapshot_digest == second.snapshot_digest
    assert first.payload.subject_digest == second.payload.subject_digest
    assert first.manifest.bundle_digest != second.manifest.bundle_digest
    assert first.branch != second.branch


def test_prepare_rejects_local_or_recipient_drift_before_publication(tmp_path: Path) -> None:
    """Catches an exact preview authorizing changed canonical state or a changed recipient set."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    first_key = X25519PrivateKey.generate()
    authority = PublicationAuthority(
        recipients=(_recipient(first_key),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    publisher = RecordingPublisher()
    runtime, service = _service(root, authority, publisher)
    try:
        preview = service.preview(now=NOW)
        config = root / ".intent/config.yaml"
        config.write_bytes(config.read_bytes() + b"# reviewed input drift\n")
        with pytest.raises(ValueError, match="publication state changed"):
            service.prepare(VerifiedHumanDecision(preview.payload, _credential(), NOW), now=NOW)
        assert publisher.publications == []
    finally:
        runtime.close()  # type: ignore[union-attr]

    config.write_bytes(config.read_bytes().removesuffix(b"# reviewed input drift\n"))
    runtime = load_runtime(root)
    current = {"value": authority}
    service = PublicationService(
        runtime,
        repository_id=REPOSITORY_ID,
        decision_repository_id=LOCAL_REPOSITORY_ID,
        authority=lambda: current["value"],
        publisher=publisher,
        challenge_source=lambda: b"q" * 32,
    )
    try:
        preview = service.preview(now=NOW)
        second = _recipient(X25519PrivateKey.generate()).model_copy(
            update={"key_id": "recipient:bob", "github_account_id": "456", "github_login": "bob"}
        )
        current["value"] = PublicationAuthority(
            recipients=(authority.recipients[0], second),
            signing_private_keys=authority.signing_private_keys,
            remote_state=None,
        )
        with pytest.raises(ValueError, match="publication state changed"):
            service.prepare(VerifiedHumanDecision(preview.payload, _credential(), NOW), now=NOW)
        assert publisher.publications == []
    finally:
        runtime.close()


def test_temporary_worktree_pushes_only_a_publication_branch_and_cleans_up(tmp_path: Path) -> None:
    """Catches publication checking out/updating intent-state or leaking its temporary worktree."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--quiet")
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    git(root, "remote", "add", "origin", str(remote))
    (root / "README.md").write_text("code\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "code")
    ready_project(root)
    temp_root = tmp_path / "publication-worktrees"
    temp_root.mkdir()
    authority = PublicationAuthority(
        recipients=(_recipient(X25519PrivateKey.generate()),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    publisher = TemporaryWorktreePublisher(
        root, temp_root=temp_root, transport=remote, allow_local_transport=True
    )
    runtime, service = _service(root, authority, publisher)
    try:
        preview = service.preview(now=NOW)
        prepared = service.prepare(
            VerifiedHumanDecision(preview.payload, _credential(), NOW), now=NOW
        )
    finally:
        runtime.close()  # type: ignore[union-attr]

    publication_ref = f"refs/heads/{prepared.branch}"
    assert git(remote, "show-ref", "--verify", publication_ref)
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "show-ref", "--verify", "refs/heads/intent-state")
    tree = set(git(remote, "ls-tree", "-r", "--name-only", publication_ref).decode().splitlines())
    assert tree == {"manifest.json", prepared.bundle_path, prepared.signature_path}
    assert list(temp_root.iterdir()) == []

    with pytest.raises(ValueError, match="publication branch unavailable"):
        publisher.publish(prepared, base_commit=None)
    assert list(temp_root.iterdir()) == []


def test_temporary_worktree_rejects_an_in_repository_root(
    tmp_path: Path,
) -> None:
    """Catches publication artifacts being staged inside the developer's repository."""
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "README.md").write_text("code\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "code")
    with pytest.raises(ValueError, match="temporary publication root"):
        TemporaryWorktreePublisher(root, temp_root=root / "unsafe")


def test_prepare_rejects_a_changed_remote_parent_before_push(tmp_path: Path) -> None:
    """Catches a concurrent intent-state merge being overwritten by a stale reviewed preview."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    recipient = X25519PrivateKey.generate()
    signer = Ed25519PrivateKey.generate()
    first = artifacts(canonical_files(root), recipient, signer)
    first_manifest = TeamStateManifest.model_validate_json(first.manifest)
    current = {
        "value": PublicationAuthority(
            recipients=(_recipient(X25519PrivateKey.generate()),),
            signing_private_keys={
                "signer:release": Ed25519PrivateKey.generate().private_bytes_raw()
            },
            remote_state=RemoteStateSnapshot(
                repository_id=REPOSITORY_ID,
                commit="a" * 40,
                manifest=first_manifest,
                manifest_bytes=first.manifest,
            ),
        )
    }
    publisher = RecordingPublisher()
    runtime = load_runtime(root)
    service = PublicationService(
        runtime,
        repository_id=REPOSITORY_ID,
        decision_repository_id=LOCAL_REPOSITORY_ID,
        authority=lambda: current["value"],
        publisher=publisher,
        challenge_source=lambda: b"r" * 32,
    )
    try:
        preview = service.preview(now=NOW)
        current["value"] = PublicationAuthority(
            recipients=current["value"].recipients,
            signing_private_keys=current["value"].signing_private_keys,
            remote_state=current["value"].remote_state.model_copy(  # type: ignore[union-attr]
                update={"commit": "b" * 40}
            ),
        )
        with pytest.raises(ValueError, match="publication state changed"):
            service.prepare(VerifiedHumanDecision(preview.payload, _credential(), NOW), now=NOW)
    finally:
        runtime.close()
    assert publisher.publications == []


def test_failed_push_and_cancellation_clean_the_owned_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches failures retaining staged publication artifacts or translating cancellation."""
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "README.md").write_text("code\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "code")
    ready_project(root)
    prepared = _prepared(root)
    temp_root = tmp_path / "publication-worktrees"
    temp_root.mkdir()
    publisher = TemporaryWorktreePublisher(
        root, temp_root=temp_root, transport=tmp_path / "missing.git", allow_local_transport=True
    )

    with pytest.raises(ValueError, match="publication branch unavailable"):
        publisher.publish(prepared, base_commit=None)
    assert list(temp_root.iterdir()) == []

    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--quiet")
    git(root, "remote", "add", "origin", str(remote))
    publisher = TemporaryWorktreePublisher(
        root, temp_root=temp_root, transport=remote, allow_local_transport=True
    )
    original_git = publisher._git

    class Cancelled(BaseException):
        pass

    def cancel_push(cwd: Path, *arguments: str, check: bool = True, allow_file: bool = False):
        if "push" in arguments:
            raise Cancelled()
        return original_git(cwd, *arguments, check=check, allow_file=allow_file)

    monkeypatch.setattr(publisher, "_git", cancel_push)
    with pytest.raises(Cancelled):
        publisher.publish(prepared, base_commit=None)
    assert list(temp_root.iterdir()) == []


def test_cleanup_refuses_to_claim_success_when_owned_directory_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches cleanup ambiguity being reported as a successful publication."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--quiet")
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    git(root, "remote", "add", "origin", str(remote))
    (root / "README.md").write_text("code\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "code")
    ready_project(root)
    prepared = _prepared(root)
    temp_root = tmp_path / "publication-worktrees"
    temp_root.mkdir()
    publisher = TemporaryWorktreePublisher(
        root, temp_root=temp_root, transport=remote, allow_local_transport=True
    )
    original_rmdir = __import__("os").rmdir

    def refuse(path: object, **kwargs: object) -> None:
        if Path(path).parent == temp_root:
            raise OSError("refused")
        original_rmdir(path, **kwargs)

    monkeypatch.setattr("intent_engineering.team_state.publication.os.rmdir", refuse)
    with pytest.raises(PublicationCleanupError, match="publication cleanup refused"):
        publisher.publish(prepared, base_commit=None)
    leftovers = list(temp_root.iterdir())
    assert len(leftovers) == 1 and list(leftovers[0].iterdir()) == []
    original_rmdir(leftovers[0])


def test_control_plane_team_state_projects_the_exact_pending_publication(tmp_path: Path) -> None:
    """Catches the Team state view inventing publication metadata outside the core service."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    authority = PublicationAuthority(
        recipients=(_recipient(X25519PrivateKey.generate()),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    runtime = load_runtime(root)
    publication = PublicationService(
        runtime,
        repository_id=REPOSITORY_ID,
        decision_repository_id=LOCAL_REPOSITORY_ID,
        authority=lambda: authority,
        publisher=RecordingPublisher(),
        challenge_source=lambda: b"s" * 32,
    )
    control = ControlPlaneService(
        runtime,
        origin="http://localhost:8765",
        clock=lambda: NOW,
        publication_service=publication,
    )
    try:
        projected = control.team_publication_preview()
    finally:
        control.close()
        runtime.close()

    assert projected["schema_version"] == 1
    assert projected["preview"]["branch"].startswith("intent-publication/")
    assert projected["preview"]["bundle_digest"] == projected["payload"]["result_digest"]
    assert projected["preview"]["recipient_key_ids"] == ["recipient:alice"]
    assert "bundle" not in projected["preview"]


def test_prepare_requires_one_recipient_to_match_the_complete_verified_identity(
    tmp_path: Path,
) -> None:
    """Catches credential and GitHub fields being independently mixed across recipients."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    alice = _recipient(X25519PrivateKey.generate()).model_copy(
        update={"github_account_id": "999", "github_login": "mallory"}
    )
    bob = _recipient(X25519PrivateKey.generate()).model_copy(
        update={
            "key_id": "recipient:bob",
            "webauthn_credential_id": _b64(b"other-credential"),
            "webauthn_credential_public_key": _b64(b"other-public-key-value"),
        }
    )
    authority = PublicationAuthority(
        recipients=(alice, bob),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    publisher = RecordingPublisher()
    runtime, service = _service(root, authority, publisher)
    try:
        preview = service.preview(now=NOW)
        with pytest.raises(ValueError, match="publication decision changed"):
            service.prepare(VerifiedHumanDecision(preview.payload, _credential(), NOW), now=NOW)
    finally:
        runtime.close()  # type: ignore[union-attr]
    assert publisher.publications == []


def test_preview_rejects_duplicate_recipient_key_ids_before_encryption(tmp_path: Path) -> None:
    """Catches duplicate reviewed recipients collapsing silently in the encryption mapping."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    recipient = _recipient(X25519PrivateKey.generate())
    authority = PublicationAuthority(
        recipients=(recipient, recipient),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    runtime, service = _service(root, authority, RecordingPublisher())
    try:
        with pytest.raises(ValueError, match="publication recipients are invalid"):
            service.preview(now=NOW)
    finally:
        runtime.close()  # type: ignore[union-attr]


@pytest.mark.parametrize("relative", ("config.yaml", "approvals/policy.yaml"))
def test_preview_rejects_duplicate_keys_in_publication_yaml(tmp_path: Path, relative: str) -> None:
    """Catches permissive YAML parsing changing reviewed config or publication policy."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    target = root / ".intent" / relative
    if target.exists():
        content = target.read_bytes()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (
            b"schema_version: 1\n"
            b"contributors: [local]\n"
            b"approvers: [local]\n"
            b"executors: [local]\n"
            b"identities:\n  local: [local]\n"
        )
    duplicate = next(line for line in content.splitlines() if line and not line.startswith(b" "))
    target.write_bytes(content + b"\n" + duplicate + b"\n")
    authority = PublicationAuthority(
        recipients=(_recipient(X25519PrivateKey.generate()),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=None,
    )
    runtime, service = _service(root, authority, RecordingPublisher())
    try:
        with pytest.raises(ValueError):
            service.preview(now=NOW)
    finally:
        runtime.close()  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("project_id", "repository_id"),
    (("other", REPOSITORY_ID), ("project", "github.com/acme/other")),
)
def test_preview_rejects_a_remote_parent_bound_to_another_project_or_repository(
    tmp_path: Path, project_id: str, repository_id: str
) -> None:
    """Catches a valid foreign state manifest being accepted as this publication's parent."""
    root = tmp_path / "project"
    root.mkdir()
    ready_project(root)
    recipient, signer, _trust = keys(project_id=project_id)
    release = artifacts(
        canonical_files(root), recipient, signer, project_id=project_id, repository_id=repository_id
    )
    manifest = TeamStateManifest.model_validate_json(release.manifest)
    remote = RemoteStateSnapshot(
        repository_id=repository_id,
        commit="a" * 40,
        manifest=manifest,
        manifest_bytes=release.manifest,
    )
    authority = PublicationAuthority(
        recipients=(_recipient(X25519PrivateKey.generate()),),
        signing_private_keys={"signer:release": Ed25519PrivateKey.generate().private_bytes_raw()},
        remote_state=remote,
    )
    runtime, service = _service(root, authority, RecordingPublisher())
    try:
        with pytest.raises(ValueError, match="publication parent binding changed"):
            service.preview(now=NOW)
    finally:
        runtime.close()  # type: ignore[union-attr]


def test_non_genesis_publication_replaces_the_tree_but_preserves_the_exact_parent(
    tmp_path: Path,
) -> None:
    """Catches historical state artifacts accumulating in the latest publication tree."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--quiet")
    state_source = tmp_path / "state-source"
    state_source.mkdir()
    git(state_source, "init", "--quiet")
    git(state_source, "remote", "add", "origin", str(remote))
    (state_source / "README.md").write_text("state source\n", encoding="utf-8")
    git(state_source, "add", "README.md")
    git(state_source, "commit", "--quiet", "-m", "state source")
    ready_project(state_source)
    recipient, signer, _trust = keys()
    old = artifacts(canonical_files(state_source), recipient, signer)
    parent = install_state_ref(state_source, old)
    git(state_source, "push", "--quiet", "origin", f"{parent}:refs/heads/intent-state")
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "README.md").write_text("code\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "code")
    ready_project(root)
    with pytest.raises(subprocess.CalledProcessError):
        git(root, "cat-file", "-e", f"{parent}^{{commit}}")
    prepared = _prepared(root)
    temp_root = tmp_path / "publication-worktrees"
    temp_root.mkdir()
    publisher = TemporaryWorktreePublisher(
        root, temp_root=temp_root, transport=remote, allow_local_transport=True
    )
    wrong_parent = git(root, "rev-parse", "HEAD").decode().strip()

    with pytest.raises(ValueError, match="publication branch unavailable"):
        publisher.publish(prepared, base_commit=wrong_parent)
    assert list(temp_root.iterdir()) == []

    publisher.publish(prepared, base_commit=parent)

    publication_ref = f"refs/heads/{prepared.branch}"
    commit = git(remote, "rev-parse", publication_ref).decode().strip()
    parents = git(remote, "show", "-s", "--format=%P", commit).decode().strip().split()
    assert parents == [parent]
    tree = set(git(remote, "ls-tree", "-r", "--name-only", commit).decode().splitlines())
    assert tree == {"manifest.json", prepared.bundle_path, prepared.signature_path}


def test_genesis_publication_rejects_intent_state_that_appeared_after_preview(
    tmp_path: Path,
) -> None:
    """Catches a stale genesis preview overwriting a newly established shared-state lineage."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--quiet")
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "README.md").write_text("code\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "code")
    ready_project(root)
    prepared = _prepared(root)
    state_commit = git(root, "rev-parse", "HEAD").decode().strip()
    git(root, "push", "--quiet", str(remote), f"{state_commit}:refs/heads/intent-state")
    temp_root = tmp_path / "publication-worktrees"
    temp_root.mkdir()
    publisher = TemporaryWorktreePublisher(
        root, temp_root=temp_root, transport=remote, allow_local_transport=True
    )

    with pytest.raises(ValueError, match="publication branch unavailable"):
        publisher.publish(prepared, base_commit=None)

    publication_ref = f"refs/heads/{prepared.branch}"
    with pytest.raises(subprocess.CalledProcessError):
        git(remote, "show-ref", "--verify", publication_ref)
    assert list(temp_root.iterdir()) == []


def test_git_execution_bounds_output_and_blocks_hostile_local_transport_rewrites(
    tmp_path: Path,
) -> None:
    """Catches unbounded Git output or repository config selecting an executable transport."""
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "README.md").write_text("code\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "--quiet", "-m", "code")
    ready_project(root)
    prepared = _prepared(root)
    marker = tmp_path / "hostile-transport-ran"
    helper = tmp_path / "hostile-helper"
    helper.write_text(f"#!/bin/sh\ntouch '{marker}'\nprintf '%70000s' x\n", encoding="utf-8")
    helper.chmod(0o700)
    git(root, "config", f"url.ext::{helper}.insteadOf", "https://github.com/")
    git(root, "remote", "add", "origin", "https://github.com/acme/project.git")
    temp_root = tmp_path / "publication-worktrees"
    temp_root.mkdir()
    publisher = TemporaryWorktreePublisher(root, temp_root=temp_root)

    with pytest.raises(ValueError, match="publication branch unavailable"):
        publisher.publish(prepared, base_commit=None)

    assert not marker.exists()
    assert list(temp_root.iterdir()) == []


def test_bounded_git_runner_kills_its_process_group_when_output_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches oversized output returning early while a Git descendant keeps running."""
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--quiet")
    marker = tmp_path / "descendant-survived"
    fake_git = tmp_path / "fake-git"
    fake_git.write_text(
        f"#!/bin/sh\n(sleep 0.4; /usr/bin/touch '{marker}') &\nprintf '%70000s' x\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    monkeypatch.setattr(publication_module, "_GIT_EXECUTABLE", fake_git)
    monkeypatch.setattr(
        publication_module,
        "_git_executable_token",
        lambda: (1, 2, 3, 4, "a" * 64),
    )
    temp_root = tmp_path / "publication-worktrees"
    temp_root.mkdir()
    publisher = TemporaryWorktreePublisher(root, temp_root=temp_root)

    with pytest.raises(ValueError, match="publication Git unavailable"):
        publisher._git(root, "status")
    time.sleep(0.6)

    assert not marker.exists()
