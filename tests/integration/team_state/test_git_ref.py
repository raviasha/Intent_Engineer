"""Public Git-ref and enrolled-key restore compatibility seams."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from intent_engineering.cli.runtime import load_runtime
from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
from intent_engineering.team_state import restore as restore_module
from intent_engineering.team_state.archive import build_archive
from intent_engineering.team_state.git_ref import GitRefReader
from intent_engineering.team_state.keys import RecipientKeyStoreError
from intent_engineering.team_state.models import (
    STATE_REF,
    CanonicalStateFile,
    CanonicalStateSnapshot,
)
from intent_engineering.team_state.restore import (
    RecipientKeyStoreTrustProvider,
    StaticTrustProvider,
    TeamStateRestorer,
    TeamStateRestoreRuntime,
)
from tests.helpers.shared_state import (
    NOW,
    REPOSITORY_ID,
    artifacts,
    canonical_files,
    init_repository,
    install_state_ref,
    keys,
    ready_project,
)


def _approved_source(tmp_path: Path) -> Path:
    source = tmp_path / "source" / "project"
    source.mkdir(parents=True)
    ready_project(source)
    return source


class _RecipientStore:
    def __init__(self, key_id: str, private_key: bytes | None) -> None:
        self.key_id = key_id
        self.private = private_key
        self.requested: list[str] = []

    def generate(self, project_id: str, actor: str) -> object:
        raise AssertionError("restore must never generate recipient keys")

    def private_key(self, key_id: str) -> bytes:
        self.requested.append(key_id)
        if key_id != self.key_id or self.private is None:
            raise RecipientKeyStoreError()
        return self.private

    def delete(self, key_id: str) -> None:
        raise AssertionError("restore must never delete recipient keys")


def test_public_git_reader_returns_canonical_manifest_and_pins_followup_blobs(
    tmp_path: Path,
) -> None:
    """Catches follow-up blob reads drifting to a ref moved after manifest review."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, _trust = keys()
    first = artifacts(canonical_files(source), recipient, signer)
    first_commit = install_state_ref(target, first)
    first_manifest = json.loads(first.manifest)
    first_bundle_path = (
        f"bundles/{first_manifest['graph_version']}-"
        f"{first_manifest['bundle_digest'].removeprefix('sha256:')}.intent"
    )
    reader = GitRefReader(target)

    remote = reader.fetch_manifest("origin", STATE_REF)
    second = artifacts(
        canonical_files(source),
        recipient,
        signer,
        graph_version=2,
        parent_bundle_digest=first_manifest["bundle_digest"],
    )
    install_state_ref(target, second, parent=first_commit)

    assert remote.commit == first_commit
    assert remote.manifest_bytes == first.manifest
    assert reader.read_blob(STATE_REF, first_bundle_path) == first.bundle
    reader.close()


@pytest.mark.parametrize(
    ("remote", "ref"),
    (("upstream", STATE_REF), ("origin", "refs/heads/main"), ("--upload-pack=evil", STATE_REF)),
)
def test_public_git_reader_rejects_nonfixed_remote_or_ref(
    tmp_path: Path, remote: str, ref: str
) -> None:
    """Catches untrusted revision or remote syntax reaching Git argv parsing."""
    target = init_repository(tmp_path / "target" / "project")

    with pytest.raises(ValueError, match="shared-state ref unavailable"):
        GitRefReader(target).fetch_manifest(remote, ref)


def test_public_git_reader_rejects_unfetched_or_hostile_blob_paths(tmp_path: Path) -> None:
    """Catches caller-controlled object expressions and traversal reaching cat-file."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, _trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    reader = GitRefReader(target)

    with pytest.raises(ValueError, match="shared-state ref unavailable"):
        reader.read_blob(STATE_REF, "manifest.json")
    reader.fetch_manifest("origin", STATE_REF)
    with pytest.raises(ValueError, match="shared-state blob unavailable"):
        reader.read_blob(STATE_REF, "../config")


def test_public_git_reader_reports_an_absent_fixed_ref_without_fallback(tmp_path: Path) -> None:
    """Catches an absent protected ref falling back to the checked-out code branch."""
    target = init_repository(tmp_path / "target" / "project")

    with pytest.raises(ValueError, match="^shared-state ref unavailable$"):
        GitRefReader(target).fetch_manifest("origin", STATE_REF)


def test_public_git_reader_close_is_idempotent_and_terminal(tmp_path: Path) -> None:
    """Catches a closed object reader silently reopening repository authority."""
    target = init_repository(tmp_path / "target" / "project")
    reader = GitRefReader(target)

    reader.close()
    reader.close()

    with pytest.raises(ValueError, match="^shared-state ref unavailable$"):
        reader.fetch_manifest("origin", STATE_REF)
    with pytest.raises(ValueError, match="^shared-state ref unavailable$"):
        reader.read_blob(STATE_REF, "manifest.json")


def test_failed_refetch_discards_the_prior_snapshot_authority(tmp_path: Path) -> None:
    """Catches a failed refresh leaving a stale commit authorized for blob reads."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, _trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    reader = GitRefReader(target)
    reader.fetch_manifest("origin", STATE_REF)

    with pytest.raises(ValueError, match="^shared-state ref unavailable$"):
        reader.fetch_manifest("upstream", STATE_REF)
    with pytest.raises(ValueError, match="^shared-state ref unavailable$"):
        reader.read_blob(STATE_REF, "manifest.json")


@pytest.mark.parametrize("path", ("a" * 1025, "é" * 1025), ids=("ascii", "multibyte"))
def test_oversized_git_paths_are_rejected_before_any_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Catches oversized caller paths reaching either public or private Git argv."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, _trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    reader = GitRefReader(target)
    snapshot = reader.fetch_manifest("origin", STATE_REF)
    calls: list[tuple[object, ...]] = []

    def forbidden(*arguments: object, **_keywords: object) -> bytes:
        calls.append(arguments)
        raise AssertionError("oversized path reached Git")

    monkeypatch.setattr(restore_module, "_run_git", forbidden)

    with pytest.raises(ValueError, match="^shared-state blob unavailable$"):
        reader.read_blob(STATE_REF, path)
    with pytest.raises(ValueError, match="invalid shared-state Git path"):
        restore_module._GitRefReader(target).blob(snapshot.commit, path, 1024)
    assert calls == []


def test_recipient_key_store_trust_provider_supplies_only_the_enrolled_private_key() -> None:
    """Catches interactive restore requiring private key bytes in environment trust JSON."""
    _recipient, _signer, trust = keys()
    store = _RecipientStore(trust.recipient_key_id, trust.recipient_private_key)

    loaded = RecipientKeyStoreTrustProvider(
        project_id=trust.project_id,
        repository_id=trust.repository_id,
        recipient_key_id=trust.recipient_key_id,
        signing_keys=trust.signing_keys,
        key_store=store,
    ).load()

    assert loaded == trust
    assert store.requested == [trust.recipient_key_id]


def test_recipient_key_store_trust_provider_maps_missing_enrollment_to_unavailable() -> None:
    """Catches a missing local recipient key being misclassified as corrupt remote state."""
    _recipient, _signer, trust = keys()
    store = _RecipientStore(trust.recipient_key_id, None)

    loaded = RecipientKeyStoreTrustProvider(
        project_id=trust.project_id,
        repository_id=trust.repository_id,
        recipient_key_id=trust.recipient_key_id,
        signing_keys=trust.signing_keys,
        key_store=store,
    ).load()

    assert loaded is None


def test_team_state_restore_reports_a_missing_enrolled_private_key_as_unavailable(
    tmp_path: Path,
) -> None:
    """Catches missing OS-keyring material being blamed on valid protected Git state."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    prefetched = GitRefReader(target).fetch_manifest("origin", STATE_REF)
    provider = RecipientKeyStoreTrustProvider(
        project_id=trust.project_id,
        repository_id=trust.repository_id,
        recipient_key_id=trust.recipient_key_id,
        signing_keys=trust.signing_keys,
        key_store=_RecipientStore(trust.recipient_key_id, None),
    )

    result = TeamStateRestorer(provider).ensure(TeamStateRestoreRuntime(target), prefetched, NOW)

    assert result.status is SharedStateRestoreStatus.UNAVAILABLE
    assert not (target / ".intent").exists()


def test_team_state_restorer_ensures_the_exact_prefetched_snapshot(tmp_path: Path) -> None:
    """Catches a ref move between preview and restore installing an unreviewed release."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    first = artifacts(canonical_files(source), recipient, signer)
    first_commit = install_state_ref(target, first)
    reader = GitRefReader(target)
    prefetched = reader.fetch_manifest("origin", STATE_REF)
    first_digest = json.loads(first.manifest)["bundle_digest"]
    second = artifacts(
        canonical_files(source),
        recipient,
        signer,
        graph_version=2,
        parent_bundle_digest=first_digest,
    )
    install_state_ref(target, second, parent=first_commit)

    result = TeamStateRestorer(StaticTrustProvider(trust)).ensure(
        TeamStateRestoreRuntime(target), prefetched, NOW
    )

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()


@pytest.mark.parametrize("payload_kind", ("legacy-json", "canonical-binary"))
def test_team_state_restorer_delegates_verified_restore_without_checkout(
    tmp_path: Path, payload_kind: str
) -> None:
    """Catches the ensure façade bypassing either compatible hardened payload decoder."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    payload = None
    if payload_kind == "canonical-binary":
        payload = build_archive(
            CanonicalStateSnapshot(
                project_id="project",
                repository_id=REPOSITORY_ID,
                graph_version=1,
                files=tuple(
                    CanonicalStateFile(path=path, content=content)
                    for path, content in files.items()
                ),
            )
        )
    release = artifacts(files, recipient, signer, payload=payload)
    install_state_ref(target, release)
    reader = GitRefReader(target)
    prefetched = reader.fetch_manifest("origin", STATE_REF)
    head_before = (target / ".git/HEAD").read_bytes()

    provider = (
        StaticTrustProvider(trust)
        if payload_kind == "legacy-json"
        else RecipientKeyStoreTrustProvider(
            project_id=trust.project_id,
            repository_id=trust.repository_id,
            recipient_key_id=trust.recipient_key_id,
            signing_keys=trust.signing_keys,
            key_store=_RecipientStore(trust.recipient_key_id, trust.recipient_private_key),
        )
    )
    result = TeamStateRestorer(provider).ensure(TeamStateRestoreRuntime(target), prefetched, NOW)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert (target / ".intent/config.yaml").read_bytes() == (
        source / ".intent/config.yaml"
    ).read_bytes()
    assert (target / ".git/HEAD").read_bytes() == head_before


def test_team_state_restorer_rejects_a_live_runtime_without_mutating_it(tmp_path: Path) -> None:
    """Catches atomic replacement invalidating stores already opened by a live runtime."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    prefetched = GitRefReader(target).fetch_manifest("origin", STATE_REF)
    before = (target / ".intent/config.yaml").read_bytes()
    runtime = load_runtime(target)
    try:
        result = TeamStateRestorer(StaticTrustProvider(trust)).ensure(runtime, prefetched, NOW)

        assert result.status is SharedStateRestoreStatus.INVALID
        assert (target / ".intent/config.yaml").read_bytes() == before
        assert runtime.config.project_id == "project"
    finally:
        runtime.close()
