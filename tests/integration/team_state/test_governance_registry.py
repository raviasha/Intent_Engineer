"""Owner-only durable governance evidence remains bounded and race-safe."""

from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from intent_engineering.storage.secure import UnsafePathError
from intent_engineering.team_state import governance
from intent_engineering.team_state.governance import GovernanceRegistry

REPOSITORY_ID = "github.com/acme/project"
MARKER = {
    "schema_version": 1,
    "bundle_digest": "sha256:" + "a" * 64,
    "graph_version": 7,
    "ref_commit": "b" * 40,
}


@pytest.mark.parametrize("same_repository", [False, True])
@pytest.mark.parametrize("boundary", ["journal_prepared", "target:governance"])
def test_activation_recovery_preserves_independently_committed_governance(
    tmp_path, same_repository, boundary
):
    from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
    from intent_engineering.team_state.restore import GitSharedStateRestorer
    from tests.integration.team_state.test_restore import enrolled_candidate

    f = enrolled_candidate(tmp_path)
    pid = os.fork()
    if pid == 0:
        GitSharedStateRestorer(
            f.provider,
            clock=lambda: f.at,
            device_key_store=f.b._device_store,
            governance_registry=f.governance,
            fault_hook=lambda stage: os._exit(83) if stage == boundary else None,
        ).verify_and_restore_approved_baseline(f.target)
        os._exit(84)
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 83
    repository_id = REPOSITORY_ID if same_repository else "github.com/acme/other"
    other = f.governance.remember(
        repository_id=repository_id,
        project_id="project" if same_repository else "other",
        directory_identity=(4, 5),
        marker=MARKER,
    )
    result = GitSharedStateRestorer(
        f.provider,
        clock=lambda: f.at,
        device_key_store=f.b._device_store,
        governance_registry=f.governance,
    ).verify_and_restore_approved_baseline(f.target)
    assert result.status is SharedStateRestoreStatus.VERIFIED
    recovered = f.governance.lookup(repository_id, (4, 5)).record
    assert recovered.marker() == other.marker()
    assert set(other.checkout_ids) <= set(recovered.checkout_ids)


def test_join_activation_rejects_permissive_existing_governance_without_hardening_it(tmp_path):
    from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
    from intent_engineering.team_state.restore import GitSharedStateRestorer
    from tests.integration.team_state.test_restore import enrolled_candidate

    f = enrolled_candidate(tmp_path)
    identity = (f.target.stat().st_dev, f.target.stat().st_ino)
    f.governance.remember(
        repository_id=REPOSITORY_ID,
        project_id="project",
        directory_identity=identity,
        marker=MARKER,
    )
    path = tmp_path / "governance/governance-v1.json"
    before = path.read_bytes()
    path.chmod(0o644)
    result = GitSharedStateRestorer(
        f.provider,
        clock=lambda: f.at,
        device_key_store=f.b._device_store,
        governance_registry=f.governance,
    ).verify_and_restore_approved_baseline(f.target)
    assert result.status is SharedStateRestoreStatus.INVALID
    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert f.provider.load_pending_join() == f.pending


def test_recovery_rejects_foreign_record_preimages_even_when_current_registry_is_corrupt(tmp_path):
    import base64
    import hashlib

    from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
    from intent_engineering.team_state.restore import GitSharedStateRestorer
    from tests.integration.team_state.test_restore import enrolled_candidate

    f = enrolled_candidate(tmp_path)
    pid = os.fork()
    if pid == 0:
        GitSharedStateRestorer(
            f.provider,
            clock=lambda: f.at,
            device_key_store=f.b._device_store,
            governance_registry=f.governance,
            fault_hook=lambda stage: os._exit(83) if stage == "target:governance" else None,
        ).verify_and_restore_approved_baseline(f.target)
        os._exit(84)
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 83
    journal = f.target / ".intent/join-activation.json"
    document = json.loads(journal.read_bytes())
    postimage = document["postimages"][0]
    registry = json.loads(base64.b64decode(postimage["content"]))
    registry["records"][0]["repository_id"] = "github.com/acme/foreign"
    content = json.dumps(registry, sort_keys=True, separators=(",", ":")).encode()
    postimage["content"] = base64.b64encode(content).decode()
    postimage["digest"] = "sha256:" + hashlib.sha256(content).hexdigest()
    journal.write_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())
    (tmp_path / "governance/governance-v1.json").write_bytes(b"{}")
    original = (f.target / ".intent/graph.yaml").read_bytes()
    result = GitSharedStateRestorer(
        f.provider,
        clock=lambda: f.at,
        device_key_store=f.b._device_store,
        governance_registry=f.governance,
    ).verify_and_restore_approved_baseline(f.target)
    assert result.status is SharedStateRestoreStatus.INVALID
    assert journal.exists()
    assert (f.target / ".intent/graph.yaml").read_bytes() == original


@pytest.mark.parametrize("content", [b"{}", b" " * (129 * 1024)], ids=["noncanonical", "oversized"])
def test_crash_recovery_rejects_noncanonical_or_oversized_shared_target(tmp_path, content):
    from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
    from intent_engineering.team_state.restore import GitSharedStateRestorer
    from tests.integration.team_state.test_restore import enrolled_candidate

    f = enrolled_candidate(tmp_path)
    pid = os.fork()
    if pid == 0:
        GitSharedStateRestorer(
            f.provider,
            clock=lambda: f.at,
            device_key_store=f.b._device_store,
            governance_registry=f.governance,
            fault_hook=lambda stage: os._exit(83) if stage == "target:governance" else None,
        ).verify_and_restore_approved_baseline(f.target)
        os._exit(84)
    assert os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) == 83
    path = tmp_path / "governance/governance-v1.json"
    path.write_bytes(content)
    result = GitSharedStateRestorer(
        f.provider,
        clock=lambda: f.at,
        device_key_store=f.b._device_store,
        governance_registry=f.governance,
    ).verify_and_restore_approved_baseline(f.target)
    assert result.status is SharedStateRestoreStatus.INVALID
    assert path.read_bytes() == content
    assert (f.target / ".intent/join-activation.json").exists()


def test_join_activation_rechecks_governance_at_the_last_transaction_boundary(tmp_path):
    from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
    from intent_engineering.team_state.restore import GitSharedStateRestorer
    from tests.integration.team_state.test_restore import enrolled_candidate

    f = enrolled_candidate(tmp_path)
    path = tmp_path / "governance/governance-v1.json"

    def change(stage):
        if stage == "existing_precommit":
            path.write_bytes(b"{}")

    result = GitSharedStateRestorer(
        f.provider,
        clock=lambda: f.at,
        device_key_store=f.b._device_store,
        governance_registry=f.governance,
        fault_hook=change,
    ).verify_and_restore_approved_baseline(f.target)
    assert result.status is SharedStateRestoreStatus.INVALID
    assert not path.exists()
    assert f.provider.load_pending_join() == f.pending
    assert f.provider.load_versioned() is None


def test_registry_is_canonical_owner_only_and_binds_each_exact_checkout(tmp_path: Path) -> None:
    """Catches secrets, permissive modes, or last-checkout-wins governance records."""
    registry_root = tmp_path / "registry"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    registry = GovernanceRegistry(registry_root)

    registry.remember(
        repository_id=REPOSITORY_ID,
        project_id="project",
        directory_identity=(first.stat().st_dev, first.stat().st_ino),
        marker=MARKER,
    )
    registry.remember(
        repository_id=REPOSITORY_ID,
        project_id="project",
        directory_identity=(second.stat().st_dev, second.stat().st_ino),
        marker=MARKER,
    )

    path = registry_root / "governance-v1.json"
    raw = path.read_bytes()
    loaded = json.loads(raw)
    assert raw == json.dumps(
        loaded, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert len(loaded["records"]) == 1
    assert len(loaded["records"][0]["checkout_ids"]) == 2
    assert "recipient_private_key" not in raw.decode("utf-8")
    assert stat.S_IMODE(registry_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((registry_root / ".governance-v1.json.lock").stat().st_mode) == 0o600
    assert registry.lookup(None, (first.stat().st_dev, first.stat().st_ino)).record is not None
    assert registry.lookup(None, (second.stat().st_dev, second.stat().st_ino)).record is not None


def test_registry_serializes_concurrent_checkout_updates(tmp_path: Path) -> None:
    """Catches a lost update when concurrent verified restores persist governance."""
    registry = GovernanceRegistry(tmp_path / "registry")
    checkouts = [tmp_path / f"checkout-{index}" for index in range(12)]
    for checkout in checkouts:
        checkout.mkdir()

    def remember(checkout: Path) -> None:
        registry.remember(
            repository_id=REPOSITORY_ID,
            project_id="project",
            directory_identity=(checkout.stat().st_dev, checkout.stat().st_ino),
            marker=MARKER,
        )

    with ThreadPoolExecutor(max_workers=6) as executor:
        tuple(executor.map(remember, checkouts))

    loaded = json.loads((tmp_path / "registry/governance-v1.json").read_bytes())
    assert len(loaded["records"]) == 1
    assert len(loaded["records"][0]["checkout_ids"]) == len(checkouts)


def test_registry_cancellation_cleans_only_its_temporary_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cancellation leaking partial authority or permanently retaining the lock."""
    registry_root = tmp_path / "registry"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    registry = GovernanceRegistry(registry_root)
    original_replace = governance.os.replace

    class Cancelled(BaseException):
        pass

    monkeypatch.setattr(
        governance.os,
        "replace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Cancelled()),
    )
    with pytest.raises(Cancelled):
        registry.remember(
            repository_id=REPOSITORY_ID,
            project_id="project",
            directory_identity=(checkout.stat().st_dev, checkout.stat().st_ino),
            marker=MARKER,
        )
    assert sorted(path.name for path in registry_root.iterdir()) == [".governance-v1.json.lock"]

    monkeypatch.setattr(governance.os, "replace", original_replace)
    registry.remember(
        repository_id=REPOSITORY_ID,
        project_id="project",
        directory_identity=(checkout.stat().st_dev, checkout.stat().st_ino),
        marker=MARKER,
    )
    assert (
        registry.lookup(REPOSITORY_ID, (checkout.stat().st_dev, checkout.stat().st_ino)).record
        is not None
    )


@pytest.mark.parametrize("unsafe", ["permissive", "symlink", "hardlink"])
def test_registry_rejects_non_owner_only_or_substitutable_state(
    tmp_path: Path, unsafe: str
) -> None:
    """Catches permissive, linked, or redirected registry authority."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir(mode=0o700)
    path = registry_root / "governance-v1.json"
    if unsafe == "permissive":
        registry_root.chmod(0o755)
    elif unsafe == "symlink":
        path.symlink_to(tmp_path / "outside")
    else:
        outside = tmp_path / "outside"
        outside.write_bytes(b"{}")
        os.link(outside, path)

    with pytest.raises(UnsafePathError):
        GovernanceRegistry(registry_root).lookup(REPOSITORY_ID, (1, 1))
