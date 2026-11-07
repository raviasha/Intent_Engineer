"""The production empty anchor can become a linear, restorable first release."""

from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
from intent_engineering.team_state.restore import GitSharedStateRestorer, StaticTrustProvider
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


def test_empty_orphan_anchor_publication_promotes_to_restorable_release(tmp_path):
    source = tmp_path / "source" / "project"
    source.mkdir(parents=True)
    ready_project(source)
    repo = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    tree = git(repo, "mktree", input_bytes=b"").decode().strip()
    anchor = git(repo, "commit-tree", tree, input_bytes=b"bootstrap\n").decode().strip()
    release = artifacts(canonical_files(source), recipient, signer)
    commit = install_state_ref(repo, release, parent=anchor)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), clock=lambda: NOW
    ).verify_and_restore_approved_baseline(repo)
    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert (repo / ".intent/graph.yaml").read_bytes() == canonical_files(source)["graph.yaml"]
    assert (
        git(repo, "rev-list", "--parents", "-n", "1", commit).decode().strip()
        == f"{commit} {anchor}"
    )


def test_genesis_rejects_code_parent_even_when_linear(tmp_path):
    source = tmp_path / "source" / "project"
    source.mkdir(parents=True)
    ready_project(source)
    repo = init_repository(tmp_path / "target" / "project")
    recipient, signer, trust = keys()
    code = git(repo, "rev-parse", "HEAD").decode().strip()
    install_state_ref(repo, artifacts(canonical_files(source), recipient, signer), parent=code)
    result = GitSharedStateRestorer(
        StaticTrustProvider(trust), clock=lambda: NOW
    ).verify_and_restore_approved_baseline(repo)
    assert result.status in {SharedStateRestoreStatus.INVALID, SharedStateRestoreStatus.STALE}
    assert not (repo / ".intent").exists()
