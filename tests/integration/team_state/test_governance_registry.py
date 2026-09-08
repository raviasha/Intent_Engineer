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
