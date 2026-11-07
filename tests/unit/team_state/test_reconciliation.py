"""Concurrent children require an exact reviewed descendant, never an overwrite."""

import hashlib
from dataclasses import replace
from datetime import timedelta

import pytest

from intent_engineering.control_plane.models import CredentialRecord
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.team_state.models import CanonicalStateFile, CanonicalStateSnapshot
from intent_engineering.team_state.publication import PublicationAuthorityV2, prepare_v2_publication
from intent_engineering.team_state.restore import verify_v2_release
from tests.integration.team_state.test_restore import enrolled_candidate


def _changed(snapshot, path, content):
    return CanonicalStateSnapshot(
        project_id=snapshot.project_id,
        repository_id=snapshot.repository_id,
        graph_version=snapshot.graph_version,
        files=tuple(
            CanonicalStateFile(path=f.path, content=content if f.path == path else f.content)
            for f in snapshot.files
        ),
    )


def reconciliation_fixture(tmp_path):
    f = enrolled_candidate(tmp_path)
    common = verify_v2_release(
        manifest_bytes=f.publication.manifest_bytes,
        bundle_bytes=f.publication.bundle,
        envelope_bytes=f.publication.signatures,
        parent=f.parent,
        recipient_key_id=f.parent.authority.ci_recipient.key_id,
        recipient_private_key=b"c" * 32,
        commit=f.commit,
        now=f.at,
    )

    def authority(release):
        member = next(m for m in release.authority.members if m.github_account_id == 100)
        return PublicationAuthorityV2(
            release.authority,
            member.member_id,
            member.device_certificate_ids[0],
            release,
            release.commit,
        )

    remote_snapshot = _changed(
        common.snapshot, "graph.yaml", f.files["graph.yaml"] + b"\n# remote choice\n"
    )
    prepared = prepare_v2_publication(
        snapshot=remote_snapshot,
        authority=authority(common),
        device_signer=f.a._device_store,
        now=f.at,
    )
    remote = verify_v2_release(
        manifest_bytes=prepared.manifest_bytes,
        bundle_bytes=prepared.bundle,
        envelope_bytes=prepared.signatures,
        parent=common,
        recipient_key_id=common.authority.ci_recipient.key_id,
        recipient_private_key=b"c" * 32,
        commit="9" * 40,
        now=f.at,
    )
    local = _changed(
        common.snapshot, "graph.yaml", f.files["graph.yaml"] + b"\n# private local choice\n"
    )
    return f, common, remote, local, authority


def test_three_way_preview_is_bounded_secret_free_and_requires_explicit_conflict_choice(tmp_path):
    from intent_engineering.team_state.reconciliation import ReconciliationService

    f, common, remote, local, authority = reconciliation_fixture(tmp_path)
    service = ReconciliationService(
        authority_provider=authority, device_signer=f.a._device_store, clock=lambda: f.at
    )
    preview = service.preview(common=common, remote=remote, local=local)
    assert preview == service.preview(common=common, remote=remote, local=local)
    assert preview.conflicts == ("graph.yaml",)
    assert preview.case.changed_paths == ("graph.yaml",)
    assert len(preview.case.canonical_bytes()) < 4096
    assert b"private local choice" not in preview.case.canonical_bytes()
    assert "private local choice" not in repr(preview)
    assert preview.resolved is None
    resolved = service.resolve(preview=preview, choices={"graph.yaml": "local"})
    assert resolved.resolved == local
    assert preview.local == local
    assert remote.snapshot != local


@pytest.mark.parametrize("choice,expected", [("local", 3), ("remote", 2)])
def test_reconciliation_graph_version_follows_the_selected_graph(tmp_path, choice, expected):
    import yaml

    from intent_engineering.team_state.reconciliation import ReconciliationService

    f, common, remote, local, authority = reconciliation_fixture(tmp_path)

    def versioned(snapshot, version):
        graph = yaml.safe_load(next(v.content for v in snapshot.files if v.path == "graph.yaml"))
        graph["version"] = version
        changed = _changed(snapshot, "graph.yaml", yaml.safe_dump(graph).encode())
        return changed.model_copy(update={"graph_version": version})

    local = versioned(local, 3)
    remote = replace(
        remote,
        snapshot=versioned(remote.snapshot, 2),
        manifest=remote.manifest.model_copy(update={"graph_version": 2}),
        manifest_bytes=remote.manifest.model_copy(update={"graph_version": 2}).canonical_bytes(),
    )
    service = ReconciliationService(
        authority_provider=authority, device_signer=f.a._device_store, clock=lambda: f.at
    )
    result = service.resolve(
        preview=service.preview(common=common, remote=remote, local=local),
        choices={"graph.yaml": choice},
    )
    assert result.resolved.graph_version == expected


def test_reconciliation_accepts_a_later_verified_remote_descendant(tmp_path):
    from intent_engineering.team_state.reconciliation import ReconciliationService
    from tests.integration.team_state.test_restore import ordinary_descendant

    f, common, _remote, local, authority = reconciliation_fixture(tmp_path)
    first = ordinary_descendant(f)
    later = ordinary_descendant(f, first)
    service = ReconciliationService(
        authority_provider=authority, device_signer=f.a._device_store, clock=lambda: f.at
    )
    preview = service.preview(common=common, remote=later, local=local)
    assert preview.conflicts == ("graph.yaml",)
    assert preview.case.remote_commit == later.commit
    with pytest.raises(ValueError, match="reconciliation"):
        service.preview(common=replace(common, commit="8" * 40), remote=later, local=local)


def test_reconciliation_requires_fresh_exact_decision_and_current_remote_then_builds_descendant(
    tmp_path,
):
    from intent_engineering.team_state.reconciliation import ReconciliationService

    f, common, remote, local, authority = reconciliation_fixture(tmp_path)
    service = ReconciliationService(
        authority_provider=authority, device_signer=f.a._device_store, clock=lambda: f.at
    )
    preview = service.resolve(
        preview=service.preview(common=common, remote=remote, local=local),
        choices={"graph.yaml": "local"},
    )
    payload = service.decision_payload(
        preview=preview,
        repository_id="repo:sha256:" + "5" * 64,
        actor="github:100",
        challenge="challenge:" + "6" * 64,
        now=f.at,
    )
    credential = CredentialRecord(
        id="credential:alice",
        project_id="project",
        repository_id=payload.repository_id,
        actor="github:100",
        credential_id="YWxpY2U",
        public_key="cHVibGlj",
        sign_count=0,
        created_at=f.at,
    )
    decision = VerifiedHumanDecision(payload=payload, credential=credential, verified_at=f.at)
    service._authority_provider = lambda release: authority(common)
    with pytest.raises(ValueError, match="reconciliation"):
        service.prepare(preview=preview, decision=decision, current_remote=remote)
    service._authority_provider = authority
    for changed in (replace(remote, commit="8" * 40), common):
        with pytest.raises(ValueError, match="reconciliation"):
            service.prepare(preview=preview, decision=decision, current_remote=changed)
    for changed in (
        replace(decision, verified_at=f.at - timedelta(seconds=1)),
        replace(
            decision, payload=payload.model_copy(update={"result_digest": "sha256:" + "0" * 64})
        ),
    ):
        with pytest.raises(ValueError, match="reconciliation"):
            service.prepare(preview=preview, decision=changed, current_remote=remote)
    prepared = service.prepare(preview=preview, decision=decision, current_remote=remote)
    assert prepared.manifest.parent_bundle_digest == remote.manifest.bundle_digest
    assert prepared.authority == remote.authority
    accepted = verify_v2_release(
        manifest_bytes=prepared.manifest_bytes,
        bundle_bytes=prepared.bundle,
        envelope_bytes=prepared.signatures,
        parent=remote,
        recipient_key_id=remote.authority.ci_recipient.key_id,
        recipient_private_key=b"c" * 32,
        commit="7" * 40,
        now=f.at,
    )
    assert accepted.snapshot == local
    assert hashlib.sha256(prepared.bundle).hexdigest() == prepared.manifest.bundle_digest[7:]


def test_authority_change_always_requires_explicit_remote_choice(tmp_path):
    from intent_engineering.team_state.reconciliation import ReconciliationService

    f, common, _remote, _local, authority = reconciliation_fixture(tmp_path)
    service = ReconciliationService(
        authority_provider=authority, device_signer=f.a._device_store, clock=lambda: f.at
    )
    preview = service.preview(common=f.parent, remote=common, local=f.parent.snapshot)
    assert preview.conflicts == ("authority/team-authority.json",)
    assert preview.resolved is None
    with pytest.raises(ValueError, match="reconciliation"):
        service.resolve(preview=preview, choices={"authority/team-authority.json": "local"})
    assert (
        service.resolve(
            preview=preview, choices={"authority/team-authority.json": "remote"}
        ).resolved
        == common.snapshot
    )


def test_nonconflicting_three_way_preserves_both_changes_and_hostile_cases_fail_closed(tmp_path):
    from intent_engineering.control_plane.models import TeamStateDivergenceCaseV2
    from intent_engineering.team_state.reconciliation import ReconciliationService

    f, common, remote, _local, authority = reconciliation_fixture(tmp_path)
    local = _changed(common.snapshot, "config.yaml", f.files["config.yaml"] + b"\n# local config\n")
    service = ReconciliationService(
        authority_provider=authority, device_signer=f.a._device_store, clock=lambda: f.at
    )
    preview = service.preview(common=common, remote=remote, local=local)
    assert preview.conflicts == ()
    files = {item.path: item.content for item in preview.resolved.files}
    assert files["config.yaml"].endswith(b"# local config\n")
    assert files["graph.yaml"].endswith(b"# remote choice\n")
    raw = preview.case.canonical_bytes()
    assert TeamStateDivergenceCaseV2.parse(raw) == preview.case
    for malformed in (raw + b" ", b" " * 4097, raw.rstrip()[:-1] + b',"project_id":"private"}\n'):
        with pytest.raises(ValueError, match="invalid divergence case"):
            TeamStateDivergenceCaseV2.parse(malformed)
