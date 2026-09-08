"""Real Git-ref verification and atomic approved-state restoration."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from intent_engineering.capture.base import RawSourceObject, normalize_raw_source
from intent_engineering.cli.runtime import load_runtime
from intent_engineering.core.models import ChangeSet
from intent_engineering.core.models.changeset import NodeUpdate
from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
from intent_engineering.storage.secure import SecureDirectory, SecureFile
from intent_engineering.storage.transaction import LocalTransactionCoordinator
from intent_engineering.team_state import restore as restore_module
from intent_engineering.team_state.restore import (
    GitSharedStateRestorer,
    StaticTrustProvider,
)
from intent_engineering.validation import validate_project
from tests.helpers.shared_state import (
    NOW,
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


def test_opted_in_restore_refreshes_the_fixed_remote_ref_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches prompt readiness accepting an older cached origin/intent-state tip."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    first = artifacts(canonical_files(source), recipient, signer)
    first_commit = install_state_ref(target, first)
    first_digest = json.loads(first.manifest)["bundle_digest"]
    second = artifacts(
        canonical_files(source),
        recipient,
        signer,
        parent_bundle_digest=first_digest,
    )
    second_commit = install_state_ref(target, second, parent=first_commit)
    git(target, "update-ref", restore_module.STATE_REF, first_commit)
    refreshed: list[Path] = []

    def refresh(root: Path) -> None:
        refreshed.append(root)
        git(root, "update-ref", restore_module.STATE_REF, second_commit)

    monkeypatch.setattr(restore_module, "_refresh_state_ref", refresh, raising=False)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust),
        refresh_remote=True,
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert refreshed == [target]
    marker = json.loads((target / ".intent/cache/shared-state.json").read_bytes())
    assert marker["graph_version"] == 1
    assert marker["ref_commit"] == second_commit


def test_opted_in_restore_maps_offline_or_absent_fixed_ref_to_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a failed refresh silently falling back to an untrusted stale tracking ref."""
    target = init_repository(tmp_path / "target" / "project")
    _recipient, _signer, trust = keys()
    marker = "PRIVATE-FETCH-FAILURE-8197"

    def unavailable(_root: Path) -> None:
        raise restore_module._Unavailable(marker)

    monkeypatch.setattr(restore_module, "_refresh_state_ref", unavailable, raising=False)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust),
        refresh_remote=True,
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.UNAVAILABLE
    assert not (target / ".intent").exists()


@pytest.mark.parametrize(
    ("program", "timeout"),
    [
        ("import time; time.sleep(5)", 0.05),
        ("import sys; sys.stderr.buffer.write(b'x' * 70000)", 1.0),
    ],
    ids=("time-bound", "combined-output-bound"),
)
def test_fixed_ref_fetch_is_argv_only_bounded_and_credential_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    program: str,
    timeout: float,
) -> None:
    """Catches prompt-time fetch using shell/config credentials or unbounded child work."""
    target = init_repository(tmp_path / "target" / "project")
    original_popen = subprocess.Popen
    calls: dict[str, object] = {}
    marker = "PRIVATE-FETCH-CREDENTIAL-8197"
    monkeypatch.setenv("GITHUB_TOKEN", marker)
    monkeypatch.setenv("GIT_ASKPASS", marker)
    monkeypatch.setenv("SSH_AUTH_SOCK", marker)
    monkeypatch.setattr(restore_module, "_FETCH_TIMEOUT_SECONDS", timeout, raising=False)

    def popen(argv, **kwargs):
        calls["argv"] = argv
        calls["kwargs"] = kwargs
        return original_popen(
            ["/usr/bin/python3", "-I", "-c", program],
            **kwargs,
        )

    monkeypatch.setattr(restore_module.subprocess, "Popen", popen)
    started = time.monotonic()

    with pytest.raises(restore_module._Unavailable):
        restore_module._refresh_state_ref(target)

    assert time.monotonic() - started < 1
    argv = calls["argv"]
    kwargs = calls["kwargs"]
    assert type(argv) is tuple
    assert type(kwargs) is dict
    assert argv[0] == "/usr/bin/git"
    assert argv[-2:] == (
        "origin",
        "refs/heads/intent-state:refs/remotes/origin/intent-state",
    )
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert marker not in repr(argv) + repr(kwargs)


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


def test_ordinary_restore_rollback_preserves_open_runtime_and_unrelated_local_files(
    tmp_path: Path,
) -> None:
    """Catches routine rollback replacing the live directory and stranding held stores."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    profile = target / ".intent/connectors/local-profile.yaml"
    profile.write_bytes(b"local connector settings: preserve\n")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    runtime = load_runtime(target)
    try:
        graph_path = target / ".intent/graph.yaml"
        graph_path.chmod(0o640)
        os.utime(graph_path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        before = _state_snapshot(target)
        metadata = {
            path: (target / ".intent" / path).stat()
            for path, content in before.items()
            if content is not None
        }
        directories = {
            path: path.stat().st_ino
            for path in (target / ".intent", *(target / ".intent").iterdir())
            if path.is_dir()
        }
        locks = {path: path.stat().st_ino for path in (target / ".intent").rglob("*.lock")}
        profile_inode = profile.stat().st_ino

        def fail(stage: str) -> None:
            if stage == "target:graph.yaml":
                raise OSError("ordinary disk fault")

        result = GitSharedStateRestorer(
            StaticTrustProvider(trust), fault_hook=fail
        ).verify_and_restore_approved_baseline(target)

        assert result.status is SharedStateRestoreStatus.INVALID
        assert _state_snapshot(target) == before
        assert {path: path.stat().st_ino for path in directories} == directories
        assert {path: path.stat().st_ino for path in locks} == locks
        assert profile.read_bytes() == b"local connector settings: preserve\n"
        assert profile.stat().st_ino == profile_inode
        for path, expected in metadata.items():
            observed = (target / ".intent" / path).stat()
            assert (observed.st_mode, observed.st_mtime_ns) == (
                expected.st_mode,
                expected.st_mtime_ns,
            )

        graph = runtime.graph_store.load()
        requirement = next(node for node in graph.nodes if node.id == "requirement:approved")
        runtime.graph_store.apply(
            ChangeSet(
                id="changeset:after-restore-rollback",
                actor="local:owner",
                timestamp=NOW,
                baseline_graph_version=graph.version,
                evidence_refs=requirement.evidence_refs,
                nodes_added=(),
                nodes_updated=(
                    NodeUpdate(
                        node_id=requirement.id,
                        replacement=requirement.model_copy(
                            update={"label": "Written through the already-open runtime"}
                        ),
                    ),
                ),
                nodes_superseded=(),
                edges_added=(),
                edges_updated=(),
                edges_superseded=(),
                confidence_changes=(),
                implementation_status_changes=(),
                reconciliation_cases_created=(),
                reconciliation_cases_resolved=(),
                validation_status="validated",
            )
        )
        reopened = load_runtime(target)
        try:
            visible = reopened.graph_store.load()
            assert visible.version == graph.version + 1
            assert next(node for node in visible.nodes if node.id == requirement.id).label == (
                "Written through the already-open runtime"
            )
            assert reopened.graph_store.history(requirement.id)[-1].id == (
                "changeset:after-restore-rollback"
            )
        finally:
            reopened.close()
    finally:
        runtime.close()


def test_cancelled_stage_cleanup_preserves_a_post_validation_directory_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches recursive cleanup deleting a replacement introduced after authentication."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    cancelled = _Cancelled()
    cleaning = False
    substituted = False
    original_verify = restore_module._PinnedState.verify

    def fail(stage: str) -> None:
        nonlocal cleaning
        if stage == "validated":
            cleaning = True
            raise cancelled

    def substitute_after_verify(
        state: restore_module._PinnedState, parent: SecureDirectory | None = None
    ) -> None:
        nonlocal substituted
        original_verify(state, parent)
        if cleaning and not substituted and parent is not None:
            substituted = True
            workspace = parent.path / ".intent"
            workspace.rename(tmp_path / "displaced-authenticated-stage")
            workspace.mkdir()
            (workspace / "foreign.txt").write_bytes(b"unfamiliar bytes must survive cleanup")

    monkeypatch.setattr(restore_module._PinnedState, "verify", substitute_after_verify)
    with pytest.raises(_Cancelled) as raised:
        GitSharedStateRestorer(
            StaticTrustProvider(trust), fault_hook=fail
        ).verify_and_restore_approved_baseline(target)

    assert raised.value is cancelled
    assert substituted
    assert not (target / ".intent").exists()
    assert [path.read_bytes() for path in target.rglob("foreign.txt")] == [
        b"unfamiliar bytes must survive cleanup"
    ]
    assert all(
        path.read_bytes() == b""
        for path in (tmp_path / "displaced-authenticated-stage").rglob("*")
        if path.is_file()
    )


@pytest.mark.parametrize("boundary", ["validated", "fresh_preinstall", "fresh_installed"])
def test_fresh_cache_install_rolls_back_cancellation_and_removes_plaintext_stage(
    tmp_path: Path,
    boundary: str,
) -> None:
    """Catches cancellation leaving a new cache or decrypted staging directory behind."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    install_state_ref(target, release)
    cancelled = _Cancelled()

    def fail(stage: str) -> None:
        if stage == boundary:
            raise cancelled

    with pytest.raises(_Cancelled) as raised:
        GitSharedStateRestorer(
            StaticTrustProvider(trust), fault_hook=fail
        ).verify_and_restore_approved_baseline(target)

    assert raised.value is cancelled
    assert not (target / ".intent").exists()
    assert list(target.glob(".intent-restore-*")) == []
    assert not any(
        b"Approved shared-state baseline" in path.read_bytes()
        for directory in target.glob(".intent-quarantine-*")
        for path in directory.rglob("*")
        if path.is_file()
    )
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
    root_modified = target.stat().st_mtime_ns

    assert (
        restorer.verify_and_restore_approved_baseline(target).status
        is SharedStateRestoreStatus.VERIFIED
    )
    assert _state_snapshot(target) == before
    assert target.stat().st_mtime_ns == root_modified

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


@pytest.mark.parametrize("matching_marker", [False, True])
def test_fresh_restore_rejects_signed_history_beyond_the_fixed_bound(
    tmp_path: Path, matching_marker: bool
) -> None:
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

    if matching_marker:
        ready_project(target)
        (target / ".intent/cache/shared-state.json").write_bytes(
            _canonical(
                {
                    "schema_version": 1,
                    "bundle_digest": parent_digest,
                    "graph_version": 1,
                    "ref_commit": parent_commit,
                }
            )
        )
        before = _state_snapshot(target)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.STALE
    if matching_marker:
        assert _state_snapshot(target) == before
    else:
        assert not (target / ".intent").exists()


@pytest.mark.parametrize("boundary", ["validated", "fresh_preinstall", "fresh_installed"])
@pytest.mark.parametrize("attack", ["content", "file", "directory", "marker"])
def test_promotion_rejects_changed_authenticated_stage(
    tmp_path: Path, boundary: str, attack: str
) -> None:
    """Catches promotion trusting staging bytes or identities after validation."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))

    def substitute(stage: str) -> None:
        if stage != boundary:
            return
        workspace = (
            target / ".intent"
            if boundary == "fresh_installed"
            else next(target.glob(".intent-restore-*")) / ".intent"
        )
        graph = workspace / "graph.yaml"
        if attack == "content":
            with graph.open("r+b") as writable:
                original = writable.read()
                writable.seek(0)
                writable.write(original.replace(b"Restore the approved", b"Destroy the approved"))
                writable.truncate()
        elif attack == "file":
            original = graph.read_bytes()
            graph.unlink()
            graph.write_bytes(original)
        elif attack == "directory":
            original_directory = workspace / "history"
            moved = workspace.parent / "displaced-history"
            original_directory.rename(moved)
            shutil.copytree(moved, original_directory)
        else:
            (workspace / "cache/shared-state.json").write_bytes(b"{}")

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=substitute
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()
    assert not list(target.glob(".intent-restore-*"))


def test_promotion_rejects_replaced_stage_root_without_deleting_foreign_files(
    tmp_path: Path,
) -> None:
    """Catches reopening a substituted staging root and unowned recursive cleanup."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    replacement: Path | None = None

    def substitute(stage: str) -> None:
        nonlocal replacement
        if stage == "validated":
            replacement = next(target.glob(".intent-restore-*"))
            replacement.rename(tmp_path / "displaced-stage")
            shutil.copytree(tmp_path / "displaced-stage", replacement)
            (replacement / "foreign.txt").write_bytes(b"do not remove")

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=substitute
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()
    assert replacement is not None
    assert (replacement / "foreign.txt").read_bytes() == b"do not remove"


def _extend_local_graph(root: Path, *, label: str = "Keep unpublished local decision") -> None:
    """Apply a real validated semantic decision, preserving its baseline evidence."""
    runtime = load_runtime(root)
    try:
        graph = runtime.graph_store.load()
        requirement = next(node for node in graph.nodes if node.id == "requirement:approved")
        runtime.graph_store.apply(
            ChangeSet(
                id="changeset:unpublished",
                actor="local:owner",
                timestamp=NOW,
                baseline_graph_version=graph.version,
                evidence_refs=requirement.evidence_refs,
                nodes_added=(),
                nodes_updated=(
                    NodeUpdate(
                        node_id=requirement.id,
                        replacement=requirement.model_copy(update={"label": label}),
                    ),
                ),
                nodes_superseded=(),
                edges_added=(),
                edges_updated=(),
                edges_superseded=(),
                confidence_changes=(),
                implementation_status_changes=(),
                reconciliation_cases_created=(),
                reconciliation_cases_resolved=(),
                validation_status="validated",
            )
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("advance_remote", [False, True])
def test_restore_preserves_unpublished_semantic_extension_for_reconciliation(
    tmp_path: Path, advance_remote: bool
) -> None:
    """Catches repeated restore deleting valid graph decisions and ChangeSet history."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    commit = install_state_ref(target, release)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    assert restorer.verify_and_restore_approved_baseline(target).status.value == "verified"
    _extend_local_graph(target)
    assert validate_project(target).valid
    if advance_remote:
        install_state_ref(
            target,
            artifacts(
                canonical_files(source),
                recipient,
                signer,
                parent_bundle_digest=json.loads(release.manifest)["bundle_digest"],
            ),
            parent=commit,
        )
    before = _state_snapshot(target)

    result = restorer.verify_and_restore_approved_baseline(target)

    assert result.status.value == "diverged"
    assert _state_snapshot(target) == before
    assert validate_project(target).graph_version == 2


@pytest.mark.parametrize("case", ["missing_digest", "false_genesis", "merge", "shallow"])
def test_marker_cannot_bypass_invalid_topology_behind_its_authenticated_parent(
    tmp_path: Path, case: str
) -> None:
    """Catches an unsigned marker truncating complete signed ancestry validation."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    recipient, signer, trust = keys()
    files = canonical_files(source)
    genesis = artifacts(files, recipient, signer)
    genesis_commit = install_state_ref(target, genesis)
    bad = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=(
            None
            if case in {"missing_digest", "shallow"}
            else json.loads(genesis.manifest)["bundle_digest"]
        ),
    )
    bad_commit = install_state_ref(
        target, bad, parent=None if case == "false_genesis" else genesis_commit
    )
    if case == "merge":
        tree = git(target, "rev-parse", f"{bad_commit}^{{tree}}").decode().strip()
        code_commit = git(target, "rev-parse", "HEAD").decode().strip()
        bad_commit = (
            git(
                target,
                "commit-tree",
                tree,
                "-p",
                genesis_commit,
                "-p",
                code_commit,
                input_bytes=b"merge hidden behind marker\n",
            )
            .decode()
            .strip()
        )
    if case == "shallow":
        (target / ".git/shallow").write_text(bad_commit + "\n")
    parent = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(bad.manifest)["bundle_digest"],
    )
    parent_commit = install_state_ref(target, parent, parent=bad_commit)
    tip = artifacts(
        files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(parent.manifest)["bundle_digest"],
    )
    tip_commit = install_state_ref(target, tip, parent=parent_commit)
    (target / ".intent/cache/shared-state.json").write_bytes(
        _canonical(
            {
                "schema_version": 1,
                "bundle_digest": json.loads(tip.manifest)["bundle_digest"],
                "graph_version": 1,
                "ref_commit": tip_commit,
            }
        )
    )
    before = _state_snapshot(target)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status in {SharedStateRestoreStatus.STALE, SharedStateRestoreStatus.INVALID}
    assert _state_snapshot(target) == before


def test_restore_advances_semantic_remote_when_local_still_matches_its_signed_baseline(
    tmp_path: Path,
) -> None:
    """Catches a preservation guard rejecting an ordinary approved remote advance."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    release = artifacts(canonical_files(source), recipient, signer)
    commit = install_state_ref(target, release)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    assert restorer.verify_and_restore_approved_baseline(target).status.value == "verified"
    _extend_local_graph(source, label="New approved remote decision")
    install_state_ref(
        target,
        artifacts(
            canonical_files(source),
            recipient,
            signer,
            graph_version=2,
            parent_bundle_digest=json.loads(release.manifest)["bundle_digest"],
        ),
        parent=commit,
    )

    result = restorer.verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert canonical_files(target) == canonical_files(source)
    assert validate_project(target).graph_version == 2


def test_held_writable_descriptor_cannot_mutate_promoted_graph_after_install(
    tmp_path: Path,
) -> None:
    """Catches trusting inode identity while a retained descriptor alters its bytes."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    descriptor: int | None = None
    staged_identity: int | None = None

    def mutate(stage: str) -> None:
        nonlocal descriptor, staged_identity
        if stage == "validated":
            workspace = next(target.glob(".intent-restore-*")) / ".intent"
            descriptor = os.open(workspace / "graph.yaml", os.O_RDWR)
            staged_identity = os.fstat(descriptor).st_ino
        elif stage == "fresh_installed":
            assert descriptor is not None
            os.write(descriptor, b"invalid: []\n")
            os.ftruncate(descriptor, 12)

    try:
        result = GitSharedStateRestorer(
            StaticTrustProvider(trust), fault_hook=mutate
        ).verify_and_restore_approved_baseline(target)
    finally:
        if descriptor is not None:
            os.close(descriptor)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert canonical_files(target) == canonical_files(source)
    assert (target / ".intent/graph.yaml").stat().st_ino != staged_identity


@pytest.mark.parametrize("attack", ["content", "file"])
def test_existing_restore_revalidates_installed_bytes_and_identities_before_commit(
    tmp_path: Path, attack: str
) -> None:
    """Catches corruption after a transaction writes an authenticated canonical file."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    before = _state_snapshot(target)
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))

    def mutate(stage: str) -> None:
        if stage == "existing_precommit":
            graph = target / ".intent/graph.yaml"
            if attack == "content":
                graph.write_bytes(b"unverified: []")
            else:
                original = graph.read_bytes()
                graph.unlink()
                graph.write_bytes(original)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=mutate
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert _state_snapshot(target) == before


def test_restore_does_not_overwrite_a_workspace_created_at_the_fresh_install_boundary(
    tmp_path: Path,
) -> None:
    """Catches a nonexclusive fresh rename replacing newly created local state."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    created_identity: int | None = None

    def create_workspace(stage: str) -> None:
        nonlocal created_identity
        if stage == "fresh_preinstall":
            workspace = target / ".intent"
            workspace.mkdir()
            created_identity = workspace.stat().st_ino

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=create_workspace
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert (target / ".intent").stat().st_ino == created_identity
    assert list((target / ".intent").iterdir()) == []


@pytest.mark.parametrize("boundary", ["validated", "fresh_installed"])
@pytest.mark.parametrize("path", ["approvals/webauthn-credentials.jsonl", "unexpected.json"])
def test_restore_does_not_promote_unsigned_files_added_to_validated_stage(
    tmp_path: Path, boundary: str, path: str
) -> None:
    """Catches promoting unsigned local authority or auxiliary files with a signed graph."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))

    def mutate(stage: str) -> None:
        if stage == boundary:
            workspace = (
                target / ".intent"
                if boundary == "fresh_installed"
                else next(target.glob(".intent-restore-*")) / ".intent"
            )
            (workspace / path).write_bytes(b"unsigned authority")

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=mutate
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()


def test_restore_cannot_return_verified_for_a_substituted_repository_root(tmp_path: Path) -> None:
    """Catches descriptor-held promotion detached from the repository selected by the caller."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))

    def mutate(stage: str) -> None:
        if stage == "validated":
            target.rename(tmp_path / "displaced-project")
            target.mkdir()
            (target / "foreign.txt").write_bytes(b"preserve new owner")

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=mutate
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert (target / "foreign.txt").read_bytes() == b"preserve new owner"
    assert not (tmp_path / "displaced-project/.intent").exists()


def test_repeated_restore_preserves_valid_local_evidence_appended_to_the_approved_baseline(
    tmp_path: Path,
) -> None:
    """Catches an unchanged baseline restore discarding normal post-check evidence."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    assert restorer.verify_and_restore_approved_baseline(target).status.value == "verified"
    content = "New locally captured evidence"
    digest = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
    evidence = normalize_raw_source(
        RawSourceObject(
            connector_type="markdown",
            external_object_id="path:local-evidence.md",
            external_version=digest,
            author="local:owner",
            observed_at=NOW,
            source_locator="local-evidence.md",
            content_hash=digest,
            payload={"content": content},
        )
    )
    runtime = load_runtime(target)
    try:
        runtime.evidence_store.associate("markdown", evidence)
    finally:
        runtime.close()
    before = _state_snapshot(target)
    assert validate_project(target).valid

    result = restorer.verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert _state_snapshot(target) == before


def test_missing_git_ancestor_is_not_hidden_by_a_matching_unsigned_marker(tmp_path: Path) -> None:
    """Catches a marker bypassing the existence of earlier signed release commits."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    recipient, signer, trust = keys()
    files = canonical_files(source)
    genesis = artifacts(files, recipient, signer)
    genesis_commit = install_state_ref(target, genesis)
    parent = artifacts(
        files, recipient, signer, parent_bundle_digest=json.loads(genesis.manifest)["bundle_digest"]
    )
    parent_commit = install_state_ref(target, parent, parent=genesis_commit)
    tip = artifacts(
        files, recipient, signer, parent_bundle_digest=json.loads(parent.manifest)["bundle_digest"]
    )
    tip_commit = install_state_ref(target, tip, parent=parent_commit)
    (target / ".intent/cache/shared-state.json").write_bytes(
        _canonical(
            {
                "schema_version": 1,
                "bundle_digest": json.loads(tip.manifest)["bundle_digest"],
                "graph_version": 1,
                "ref_commit": tip_commit,
            }
        )
    )
    (target / ".git/objects" / genesis_commit[:2] / genesis_commit[2:]).unlink()
    before = _state_snapshot(target)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert _state_snapshot(target) == before


def test_semantic_validation_cannot_read_substitute_bytes_then_promote_the_original_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches validating mutable filesystem bytes instead of the authenticated snapshot."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    files = canonical_files(source)
    valid_graph = files["graph.yaml"]
    files["graph.yaml"] = valid_graph.replace(b"Restore the approved", b"Destroy the approved")
    install_state_ref(target, artifacts(files, recipient, signer))
    original_read = SecureFile.read_optional_nonblocking

    def substitute_during_read(file: SecureFile, *, max_bytes: int | None = None) -> bytes | None:
        if file.path.name != "graph.yaml" or not any(
            part.startswith(".intent-restore-") for part in file.path.parts
        ):
            return original_read(file, max_bytes=max_bytes)
        with file.path.open("r+b") as writable:
            unvalidated = writable.read()
            writable.seek(0)
            writable.write(valid_graph)
            writable.truncate()
            try:
                return original_read(file, max_bytes=max_bytes)
            finally:
                writable.seek(0)
                writable.write(unvalidated)
                writable.truncate()

    monkeypatch.setattr(SecureFile, "read_optional_nonblocking", substitute_during_read)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()


@pytest.mark.parametrize("cached", [False, True])
def test_signed_descendant_cannot_replay_an_older_graph_version(
    tmp_path: Path, cached: bool
) -> None:
    """Catches parent-linked signatures allowing a semantic graph-version rollback."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    original_files = canonical_files(source)
    genesis = artifacts(original_files, recipient, signer)
    genesis_commit = install_state_ref(target, genesis)
    _extend_local_graph(source)
    advanced = artifacts(
        canonical_files(source),
        recipient,
        signer,
        graph_version=2,
        parent_bundle_digest=json.loads(genesis.manifest)["bundle_digest"],
    )
    advanced_commit = install_state_ref(target, advanced, parent=genesis_commit)
    restorer = GitSharedStateRestorer(StaticTrustProvider(trust))
    if cached:
        assert restorer.verify_and_restore_approved_baseline(target).status.value == "verified"
        before = _state_snapshot(target)
    replay = artifacts(
        original_files,
        recipient,
        signer,
        parent_bundle_digest=json.loads(advanced.manifest)["bundle_digest"],
    )
    install_state_ref(target, replay, parent=advanced_commit)

    result = restorer.verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.STALE
    if cached:
        assert _state_snapshot(target) == before
    else:
        assert not (target / ".intent").exists()


@pytest.mark.parametrize("restore_path", ["fresh", "replacement", "noop"])
@pytest.mark.parametrize("attack", ["content", "directory"])
def test_terminal_scan_rejects_an_earlier_file_changed_while_later_files_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_path: str, attack: str
) -> None:
    """Catches a final multi-file scan returning an internally inconsistent snapshot."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    if restore_path == "replacement":
        ready_project(target)
    elif restore_path == "noop":
        assert (
            GitSharedStateRestorer(StaticTrustProvider(trust))
            .verify_and_restore_approved_baseline(target)
            .status
            is SharedStateRestoreStatus.VERIFIED
        )
    before = _state_snapshot(target) if restore_path != "fresh" else None
    original_read = SecureDirectory.read_relative
    armed = False
    earlier_reads = 0
    fresh_earlier: str | None = None
    attacked = False

    def arm(boundary: str) -> None:
        nonlocal armed
        armed = (
            boundary
            == {
                "fresh": "fresh_installed",
                "replacement": "existing_precommit",
                "noop": "validated",
            }[restore_path]
            or armed
        )

    def mutate_during_scan(directory: SecureDirectory, relative: str | Path, **options: object):  # type: ignore[no-untyped-def]
        nonlocal earlier_reads, attacked, fresh_earlier
        observed = original_read(directory, relative, **options)  # type: ignore[arg-type]
        if not armed or attacked:
            return observed
        if restore_path == "fresh" and fresh_earlier is None:
            if str(relative) in canonical_files(source):
                fresh_earlier = str(relative)
            return observed
        if str(relative) == "approvals/approvals.jsonl":
            earlier_reads += 1
        if restore_path == "fresh" or (
            str(relative) == "graph.yaml" and earlier_reads == (2 if restore_path == "noop" else 1)
        ):
            workspace = target / ".intent"
            if attack == "content":
                changed = fresh_earlier if restore_path == "fresh" else "approvals/approvals.jsonl"
                assert changed is not None
                (workspace / changed).write_bytes(b"unverified decision\n")
            else:
                approvals = workspace if restore_path == "fresh" else workspace / "approvals"
                approvals.rename(tmp_path / "displaced-approvals")
                shutil.copytree(tmp_path / "displaced-approvals", approvals)
            attacked = True
        return observed

    monkeypatch.setattr(SecureDirectory, "read_relative", mutate_during_scan)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=arm
    ).verify_and_restore_approved_baseline(target)

    assert attacked
    assert result.status is SharedStateRestoreStatus.INVALID
    if restore_path == "fresh":
        assert not (target / ".intent").exists()
    else:
        assert _state_snapshot(target) == before


@pytest.mark.parametrize("collision", [False, True])
def test_staging_substitution_at_rename_is_quarantined_and_restores_absent_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collision: bool
) -> None:
    """Catches rollback abandoning a foreign promoted inode or deleting unfamiliar bytes."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    original_rename = restore_module._rename_directory_exclusive
    attacked = False

    def substitute(source_directory: SecureDirectory, destination: SecureDirectory) -> None:
        nonlocal attacked
        if destination.path == target and not attacked:
            staged = source_directory.path / ".intent"
            staged.rename(tmp_path / "displaced-authenticated-stage")
            staged.mkdir()
            (staged / "foreign.txt").write_bytes(b"preserve unfamiliar promoted bytes")
            attacked = True
            original_rename(source_directory, destination)
            if collision:
                staged.mkdir()
                (staged / "collision.txt").write_bytes(b"preserve rollback-name occupant")
            return
        original_rename(source_directory, destination)

    monkeypatch.setattr(restore_module, "_rename_directory_exclusive", substitute)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert attacked
    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()
    preserved = [path.read_bytes() for path in target.rglob("foreign.txt")]
    assert preserved == [b"preserve unfamiliar promoted bytes"]
    if collision:
        assert [path.read_bytes() for path in target.rglob("collision.txt")] == [
            b"preserve rollback-name occupant"
        ]


def test_complete_lineage_accepts_unicode_author_and_committer_headers(tmp_path: Path) -> None:
    """Catches parsing unrelated Git commit headers as ASCII while authenticating parents."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    genesis = artifacts(canonical_files(source), recipient, signer)
    original_commit = install_state_ref(target, genesis)
    commit_bytes = (
        git(target, "cat-file", "commit", original_commit)
        .replace(b"author Shared State Fixture", "author Zoë 李".encode())
        .replace(b"committer Shared State Fixture", "committer Renée राम".encode())
    )
    unicode_commit = (
        git(target, "hash-object", "-t", "commit", "-w", "--stdin", input_bytes=commit_bytes)
        .decode()
        .strip()
    )
    child = artifacts(
        canonical_files(source),
        recipient,
        signer,
        parent_bundle_digest=json.loads(genesis.manifest)["bundle_digest"],
    )
    install_state_ref(target, child, parent=unicode_commit)

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert canonical_files(target) == canonical_files(source)


def test_existing_restore_checks_canonical_state_after_the_commit_journal_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches canonical mutation in journal finalization after the precommit byte scan."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    before = _state_snapshot(target)
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    original_write = SecureFile.atomic_write
    attacked = False

    def mutate_during_commit(file: SecureFile, content: bytes, **options: object) -> None:
        nonlocal attacked
        original_write(file, content, **options)  # type: ignore[arg-type]
        if file.name == ".local-transaction.json" and b'"state":"committed"' in content:
            (target / ".intent/approvals/approvals.jsonl").write_bytes(b"unverified decision\n")
            attacked = True

    monkeypatch.setattr(SecureFile, "atomic_write", mutate_during_commit)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert attacked
    assert result.status is SharedStateRestoreStatus.INVALID
    assert _state_snapshot(target) == before


@pytest.mark.parametrize("restore_path", ["replacement", "noop"])
def test_existing_workspace_substitution_reconstructs_the_pinned_preimage_and_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore_path: str
) -> None:
    """Catches rollback leaving a swapped workspace named or losing its pinned preimage."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    if restore_path == "noop":
        assert (
            GitSharedStateRestorer(StaticTrustProvider(trust))
            .verify_and_restore_approved_baseline(target)
            .status
            is SharedStateRestoreStatus.VERIFIED
        )
    else:
        ready_project(target)
    (target / ".intent/approvals").chmod(0o750)
    graph = target / ".intent/graph.yaml"
    graph.chmod(0o640)
    os.utime(graph, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    before = _state_snapshot(target)
    original_read = SecureDirectory.read_relative
    armed = False
    graph_reads = 0
    attacked = False

    def arm(boundary: str) -> None:
        nonlocal armed
        if boundary == ("validated" if restore_path == "noop" else "existing_precommit"):
            armed = True

    def substitute(directory: SecureDirectory, relative: str | Path, **options: object):  # type: ignore[no-untyped-def]
        nonlocal attacked, graph_reads
        value = original_read(directory, relative, **options)  # type: ignore[arg-type]
        if armed and str(relative) == "graph.yaml":
            graph_reads += 1
            if graph_reads == (2 if restore_path == "noop" else 1) and not attacked:
                workspace = target / ".intent"
                workspace.rename(tmp_path / "displaced-workspace")
                shutil.copytree(tmp_path / "displaced-workspace", workspace)
                (workspace / "foreign.txt").write_bytes(b"preserve substituted workspace")
                attacked = True
        return value

    monkeypatch.setattr(SecureDirectory, "read_relative", substitute)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=arm
    ).verify_and_restore_approved_baseline(target)

    assert attacked
    assert result.status is SharedStateRestoreStatus.INVALID
    assert _state_snapshot(target) == before
    assert graph.stat().st_mode & 0o777 == 0o640
    assert graph.stat().st_mtime_ns == 1_700_000_000_000_000_000
    assert (target / ".intent/approvals").stat().st_mode & 0o777 == 0o750
    assert [path.read_bytes() for path in target.rglob("foreign.txt")] == [
        b"preserve substituted workspace"
    ]


def test_existing_restore_checks_canonical_state_after_journal_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches corruption during the last transaction side effect after its earlier scan."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    ready_project(target)
    before = _state_snapshot(target)
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))
    original_unlink = SecureFile.unlink
    attacked = False

    def mutate_during_cleanup(file: SecureFile, *, missing_ok: bool = False) -> None:
        nonlocal attacked
        original_unlink(file, missing_ok=missing_ok)
        if file.name == ".local-transaction.json" and not missing_ok:
            (target / ".intent/approvals/approvals.jsonl").write_bytes(b"unverified decision\n")
            attacked = True

    monkeypatch.setattr(SecureFile, "unlink", mutate_during_cleanup)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust)
    ).verify_and_restore_approved_baseline(target)

    assert attacked
    assert result.status is SharedStateRestoreStatus.INVALID
    assert _state_snapshot(target) == before


def test_fresh_rollback_reserves_another_container_when_its_recovery_name_is_occupied(
    tmp_path: Path,
) -> None:
    """Catches a recovery-name collision stranding a failed promotion or deleting its occupant."""
    source = _approved_source(tmp_path)
    target = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    install_state_ref(target, artifacts(canonical_files(source), recipient, signer))

    def occupy_recovery(boundary: str) -> None:
        if boundary == "fresh_installed":
            recovery = next(target.glob(".intent-quarantine-*")) / ".intent"
            recovery.mkdir()
            (recovery / "foreign.txt").write_bytes(b"preserve occupied recovery name")
            (target / ".intent/graph.yaml").write_bytes(b"unverified decision\n")

    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), fault_hook=occupy_recovery
    ).verify_and_restore_approved_baseline(target)

    assert result.status is SharedStateRestoreStatus.INVALID
    assert not (target / ".intent").exists()
    assert [path.read_bytes() for path in target.rglob("foreign.txt")] == [
        b"preserve occupied recovery name"
    ]
    assert any(path.read_bytes() == b"unverified decision\n" for path in target.rglob("graph.yaml"))
