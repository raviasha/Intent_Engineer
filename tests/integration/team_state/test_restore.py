"""Real Git-ref verification and atomic approved-state restoration."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
from intent_engineering.storage.secure import SecureDirectory
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.team_state.restore import (
    GitSharedStateRestorer,
    StaticTrustProvider,
)
from intent_engineering.validation import validate_project
from tests.helpers.shared_state import (
    artifacts,
    canonical_files,
    git,
    init_repository,
    install_state_ref,
    keys,
    ready_project,
)


def _approved_source(path: Path) -> Path:
    source = path / "source" / "project"
    source.mkdir(parents=True)
    ready_project(source)
    return source


def test_restores_a_verified_repository_bound_state_ref_without_checkout(tmp_path: Path) -> None:
    """Catches the production restorer remaining an unwired future-only protocol."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release)
    head_before = (target / ".git/HEAD").read_bytes()

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert validate_project(target).valid
    assert (target / ".intent/config.yaml").read_bytes() == (
        source / ".intent/config.yaml"
    ).read_bytes()
    assert (target / ".git/HEAD").read_bytes() == head_before


def _canonical(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode()


def _malicious_payload(files: dict[str, bytes], *, replacement_path: str) -> bytes:
    entries = [
        {
            "path": replacement_path if index == 0 else path,
            "size": len(content),
            "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "content_base64": base64.urlsafe_b64encode(content).rstrip(b"=").decode(),
        }
        for index, (path, content) in enumerate(sorted(files.items()))
    ]
    digest = hashlib.sha256(_canonical({"schema_version": 1, "entries": entries})).hexdigest()
    return _canonical(
        {
            "schema_version": 1,
            "state_digest": f"sha256:{digest}",
            "entries": entries,
        }
    )


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("signature", SharedStateRestoreStatus.INVALID),
        ("ciphertext", SharedStateRestoreStatus.INVALID),
        ("repository", SharedStateRestoreStatus.INVALID),
        ("project", SharedStateRestoreStatus.INVALID),
        ("size", SharedStateRestoreStatus.INVALID),
        ("schema", SharedStateRestoreStatus.UPGRADE_REQUIRED),
        ("lineage", SharedStateRestoreStatus.STALE),
    ],
)
def test_fails_closed_for_untrusted_or_incompatible_ref_state(
    tmp_path: Path,
    case: str,
    expected: SharedStateRestoreStatus,
) -> None:
    """Catches any signed-envelope, binding, schema, or lineage bypass."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    options: dict[str, object] = {}
    if case == "repository":
        options["repository_id"] = "github.com/acme/other"
    elif case == "project":
        options["project_id"] = "other"
    elif case == "lineage":
        options["parent_bundle_digest"] = "sha256:" + "0" * 64
    release = artifacts(canonical_files(source), recipient, signer, **options)  # type: ignore[arg-type]
    if case == "signature":
        parsed = json.loads(release.signatures)
        parsed["signatures"][0]["signature"] = (
            "A" if parsed["signatures"][0]["signature"][0] != "A" else "B"
        ) + parsed["signatures"][0]["signature"][1:]
        release = replace(release, signatures=_canonical(parsed))
    elif case == "ciphertext":
        parsed = json.loads(release.bundle)
        parsed["ciphertext"] = ("A" if parsed["ciphertext"][0] != "A" else "B") + parsed[
            "ciphertext"
        ][1:]
        release = replace(release, bundle=_canonical(parsed))
    elif case == "schema":
        parsed = json.loads(release.manifest)
        parsed["schema_version"] = 2
        release = replace(release, manifest=_canonical(parsed))
    elif case == "size":
        parsed = json.loads(release.manifest)
        parsed["bundle_size"] += 1
        release = replace(release, manifest=_canonical(parsed))
    install_state_ref(target, release)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is expected
    assert not (target / ".intent").exists()


def test_missing_ci_key_fails_before_reading_or_creating_local_state(tmp_path: Path) -> None:
    """Catches a missing production secret silently becoming a permissive restore."""
    target = init_repository(tmp_path / "target" / "project")

    result = GitSharedStateRestorer(StaticTrustProvider(None)).verify_and_restore_approved_baseline(
        target
    )

    assert result.status is SharedStateRestoreStatus.UNAVAILABLE
    assert not (target / ".intent").exists()


def test_wrong_recipient_private_key_fails_after_signature_verification(tmp_path: Path) -> None:
    """Catches a recipient identifier being accepted without key possession."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    other_recipient, _, _ = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release)
    wrong_key_trust = replace(trust, recipient_private_key=other_recipient.private_bytes_raw())

    result = GitSharedStateRestorer(
        StaticTrustProvider(wrong_key_trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()


@pytest.mark.parametrize(
    "replacement_path",
    [
        "../config.yaml",
        "/config.yaml",
        "./config.yaml",
        "config\\yaml",
        "approvals/plans.jsonl",
    ],
)
def test_rejects_path_attacks_inside_an_authentic_encrypted_payload(
    tmp_path: Path,
    replacement_path: str,
) -> None:
    """Catches authenticated plaintext escaping the explicit state-file allowlist."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    release = artifacts(
        files,
        recipient,
        signer,
        payload=_malicious_payload(files, replacement_path=replacement_path),
    )
    install_state_ref(target, release)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()


def test_rejects_a_special_file_at_an_expected_ref_path(tmp_path: Path) -> None:
    """Catches a symlink entry on the protected ref being treated as regular bytes."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release, modes={release.bundle_path: "120000"})

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()


@pytest.mark.parametrize(
    "path",
    ["approvals/approvals.jsonl", "approvals/plans.jsonl", "approvals/policy.yaml"],
)
def test_complete_restore_validation_rejects_invalid_approval_state(
    tmp_path: Path,
    path: str,
) -> None:
    """Catches encrypted approval state bypassing semantic restore validation."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    files[path] = b"not valid\n"
    release = artifacts(files, recipient, signer)
    install_state_ref(target, release)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()


def _state_snapshot(root: Path) -> dict[str, bytes | None]:
    paths = (
        *canonical_files(root),
        "approvals/webauthn-challenges.jsonl",
        "approvals/webauthn-credentials.jsonl",
        "cache/checkpoints.yaml",
        "cache/shared-state.json",
    )
    return {
        path: (root / ".intent" / path).read_bytes() if (root / ".intent" / path).exists() else None
        for path in paths
    }


class _Cancelled(BaseException):
    pass


@pytest.mark.parametrize("failure", [RuntimeError("disk fault"), _Cancelled()])
def test_existing_cache_is_byte_identical_after_failure_or_cancellation(
    tmp_path: Path,
    failure: BaseException,
) -> None:
    """Catches partial replacement of an existing local cache."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    before = _state_snapshot(target)
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release)

    def fail(stage: str) -> None:
        if stage == "target:graph.yaml":
            raise failure

    restorer = GitSharedStateRestorer(StaticTrustProvider(trust), fault_hook=fail)
    if isinstance(failure, Exception):
        result = restorer.verify_and_restore_approved_baseline(target)
        assert result.status is SharedStateRestoreStatus.INVALID
    else:
        with pytest.raises(_Cancelled) as raised:
            restorer.verify_and_restore_approved_baseline(target)
        assert raised.value is failure

    assert _state_snapshot(target) == before


def test_fresh_cache_install_rolls_back_cancellation_and_removes_plaintext_stage(
    tmp_path: Path,
) -> None:
    """Catches cancellation leaving a new cache or decrypted staging directory behind."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release)
    cancelled = _Cancelled()

    def fail(stage: str) -> None:
        if stage == "fresh_installed":
            raise cancelled

    with pytest.raises(_Cancelled) as raised:
        GitSharedStateRestorer(
            StaticTrustProvider(trust), fault_hook=fail
        ).verify_and_restore_approved_baseline(target)

    assert raised.value is cancelled
    assert not (target / ".intent").exists()
    assert list(target.glob(".intent-restore-*")) == []
    traceback_values: list[str] = []
    cursor = raised.value.__traceback__
    while cursor is not None:
        if "intent_engineering/team_state" in cursor.tb_frame.f_code.co_filename:
            traceback_values.extend(repr(value) for value in cursor.tb_frame.f_locals.values())
        cursor = cursor.tb_next
    retained = "\n".join(traceback_values)
    assert repr(trust.recipient_private_key) not in retained
    assert "Approved shared-state baseline" not in retained


def test_equal_verified_state_is_a_byte_noop_but_local_tampering_is_restored(
    tmp_path: Path,
) -> None:
    """Catches a copied valid marker authenticating locally changed graph bytes."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    before = _state_snapshot(target)

    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    assert _state_snapshot(target) == before

    graph = target / ".intent/graph.yaml"
    graph.write_bytes(graph.read_bytes().replace(b"version: 1", b"version: 01"))
    assert graph.read_bytes() != before["graph.yaml"]

    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    expected = {**before, "cache/checkpoints.yaml": b""}
    assert _state_snapshot(target) == expected


def test_restore_recovers_the_existing_runtime_journal_before_replacement(tmp_path: Path) -> None:
    """Catches the restore transaction rejecting an existing runtime target schema."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    workspace = SecureDirectory.open(target / ".intent")

    def crash(stage: str) -> None:
        if stage == "target:graph":
            raise SystemExit

    coordinator = LocalTransactionCoordinator(
        workspace.file("history/.local-transaction.json"),
        {
            "graph": workspace.file("graph.yaml"),
            "history": workspace.file("history/changesets.jsonl"),
            "cases": workspace.file("reconciliation/cases.jsonl"),
        },
        fault_hook=crash,
    )
    try:
        with pytest.raises(SystemExit), coordinator.transaction() as transaction:
            transaction.write("graph", b"torn: [")
    finally:
        coordinator.close()
        workspace.close()
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert validate_project(target).valid
    assert not (target / ".intent/history/.local-transaction.json").exists()


@pytest.mark.parametrize("cached_genesis", [True, False], ids=["genesis", "non_genesis"])
def test_authentic_cache_advances_across_two_skipped_signed_releases(
    tmp_path: Path,
    cached_genesis: bool,
) -> None:
    """Catches ancestry validation accepting only the tip's immediate parent."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    parent_commit: str | None = None
    parent_digest: str | None = None
    if not cached_genesis:
        parent = artifacts(files, recipient, signer)
        parent_commit = install_state_ref(target, parent)
        parent_digest = json.loads(parent.manifest)["bundle_digest"]
    release_a = artifacts(files, recipient, signer, parent_bundle_digest=parent_digest)
    commit_a = install_state_ref(target, release_a, parent=parent_commit)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    release_b = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(release_a.manifest)["bundle_digest"],
    )
    commit_b = install_state_ref(target, release_b, parent=commit_a)
    release_c = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(release_b.manifest)["bundle_digest"],
    )
    commit_c = install_state_ref(target, release_c, parent=commit_b)

    result = restorer.verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    marker = json.loads((target / ".intent/cache/shared-state.json").read_bytes())
    assert marker["ref_commit"] == commit_c
    assert marker["bundle_digest"] == json.loads(release_c.manifest)["bundle_digest"]
    before = _state_snapshot(target)

    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    assert _state_snapshot(target) == before


@pytest.mark.parametrize("matched_ancestor", [False, True], ids=["tip", "ancestor"])
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("merge", SharedStateRestoreStatus.INVALID),
        ("false_genesis", SharedStateRestoreStatus.STALE),
        ("missing_parent_digest", SharedStateRestoreStatus.STALE),
        ("mismatched_parent_digest", SharedStateRestoreStatus.STALE),
        ("invalid_parent_signature", SharedStateRestoreStatus.INVALID),
    ],
)
def test_matching_local_marker_cannot_bypass_endpoint_lineage_validation(
    tmp_path: Path,
    case: str,
    expected: SharedStateRestoreStatus,
    matched_ancestor: bool,
) -> None:
    """Catches unsigned cache metadata bypassing the matched signed release's lineage."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    recipient, signer, trust = keys()
    files = canonical_files(source)
    parent = artifacts(files, recipient, signer)
    if case == "invalid_parent_signature":
        signature = json.loads(parent.signatures)
        signature["signatures"][0]["signature"] = (
            base64.urlsafe_b64encode(b"\0" * 64).rstrip(b"=").decode()
        )
        parent = replace(parent, signatures=_canonical(signature))
    parent_commit = install_state_ref(target, parent)
    parent_digest = json.loads(parent.manifest)["bundle_digest"]
    if case == "missing_parent_digest":
        parent_digest = None
    elif case == "mismatched_parent_digest":
        parent_digest = "sha256:" + "0" * 64
    endpoint = artifacts(files, recipient, signer, parent_bundle_digest=parent_digest)
    endpoint_commit = install_state_ref(
        target, endpoint, parent=None if case == "false_genesis" else parent_commit
    )
    if case == "merge":
        tree = git(target, "rev-parse", f"{endpoint_commit}^{{tree}}").decode().strip()
        code_commit = git(target, "rev-parse", "HEAD").decode().strip()
        endpoint_commit = (
            git(
                target,
                "commit-tree",
                tree,
                "-p",
                parent_commit,
                "-p",
                code_commit,
                input_bytes=b"unsupported shared-state merge\n",
            )
            .decode()
            .strip()
        )
        git(target, "update-ref", "refs/remotes/origin/intent-state", endpoint_commit)
    manifest = json.loads(endpoint.manifest)
    if matched_ancestor:
        successor = artifacts(
            files, recipient, signer, parent_bundle_digest=manifest["bundle_digest"]
        )
        install_state_ref(target, successor, parent=endpoint_commit)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    before = _state_snapshot(target)

    assert restorer.verify_and_restore_approved_baseline(target).status is expected
    assert _state_snapshot(target) == before

    (target / ".intent/cache/shared-state.json").write_bytes(
        _canonical(
            {
                "schema_version": 1,
                "bundle_digest": manifest["bundle_digest"],
                "graph_version": 1,
                "ref_commit": endpoint_commit,
            }
        )
    )
    before = _state_snapshot(target)

    assert restorer.verify_and_restore_approved_baseline(target).status is expected
    assert _state_snapshot(target) == before


def test_skipped_release_fails_closed_when_an_intermediate_signature_is_invalid(
    tmp_path: Path,
) -> None:
    """Catches traversal trusting an intermediate parent link before its signature."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    release_a = artifacts(files, recipient, signer)
    commit_a = install_state_ref(target, release_a)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    before = _state_snapshot(target)
    release_b = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(release_a.manifest)["bundle_digest"],
    )
    signature = json.loads(release_b.signatures)
    signature["signatures"][0]["signature"] = (
        "A" if signature["signatures"][0]["signature"][0] != "A" else "B"
    ) + signature["signatures"][0]["signature"][1:]
    release_b = replace(release_b, signatures=_canonical(signature))
    commit_b = install_state_ref(target, release_b, parent=commit_a)
    release_c = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(release_b.manifest)["bundle_digest"],
    )
    install_state_ref(target, release_c, parent=commit_b)

    result = restorer.verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert _state_snapshot(target) == before


def test_divergent_signed_fork_and_rollback_to_an_older_tip_remain_stale(tmp_path: Path) -> None:
    """Catches any authentic release being accepted without descending from the cache."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    release_a = artifacts(files, recipient, signer)
    commit_a = install_state_ref(target, release_a)
    release_b = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(release_a.manifest)["bundle_digest"],
    )
    install_state_ref(target, release_b, parent=commit_a)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    before = _state_snapshot(target)

    git(target, "update-ref", "refs/remotes/origin/intent-state", commit_a)
    rollback = restorer.verify_and_restore_approved_baseline(target)

    assert rollback.status is SharedStateRestoreStatus.STALE
    assert _state_snapshot(target) == before

    release_fork = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(release_a.manifest)["bundle_digest"],
    )
    install_state_ref(target, release_fork, parent=commit_a)
    divergence = restorer.verify_and_restore_approved_baseline(target)

    assert divergence.status is SharedStateRestoreStatus.STALE
    assert _state_snapshot(target) == before


def test_fresh_restore_rejects_signed_history_beyond_the_fixed_bound(tmp_path: Path) -> None:
    """Catches unbounded ancestry walks over attacker-amplified Git history."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    parent_digest: str | None = None
    parent_commit: str | None = None
    for _ in range(65):
        release = artifacts(
            files,
            recipient,
            signer,
            parent_bundle_digest=parent_digest,
        )
        parent_commit = install_state_ref(target, release, parent=parent_commit)
        parent_digest = json.loads(release.manifest)["bundle_digest"]

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.STALE
    assert not (target / ".intent").exists()
