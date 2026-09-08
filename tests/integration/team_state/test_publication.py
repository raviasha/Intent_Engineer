"""Reviewed deterministic team-state publication preparation."""

from __future__ import annotations

import base64
import subprocess
import time
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
