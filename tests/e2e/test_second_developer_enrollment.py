"""Release proof for the complete two-developer team-state journey."""

from __future__ import annotations

import asyncio
import base64
import subprocess
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from intent_engineering.capture.github.auth import CredentialSource, GitHubCredentials
from intent_engineering.capture.github.client import GitHubClient
from intent_engineering.capture.github.errors import GitHubProtocolError
from intent_engineering.control_plane.webauthn_service import VerifiedHumanDecision
from intent_engineering.intent_workflow.check import SharedStateRestoreStatus
from intent_engineering.team_state.authority import authority_digest
from intent_engineering.team_state.enrollment import export_join_response, export_team_invite
from intent_engineering.team_state.models import (
    CanonicalStateFile,
    CanonicalStateSnapshot,
    DeviceRevocationV2,
    TeamAuthorityRegistryV2,
)
from intent_engineering.team_state.publication import (
    PublicationAuthorityV2,
    authority_from_verified_state,
    prepare_v2_publication,
)
from intent_engineering.team_state.reconciliation import ReconciliationService
from intent_engineering.team_state.restore import (
    GitSharedStateRestorer,
    VerifiedReleaseV2,
    verify_v2_release,
)
from tests.helpers.shared_state import canonical_files, git
from tests.integration.team_state.test_publication import (
    test_prepare_v1_migration_is_dual_signed_and_does_not_advance_local_trust as _prove_v1_migration,
)
from tests.integration.team_state.test_restore import enrolled_candidate
from tests.unit.team_state.test_enrollment import _credential


def _snapshot_with_comment(
    snapshot: CanonicalStateSnapshot, comment: bytes
) -> CanonicalStateSnapshot:
    return snapshot.model_copy(
        update={
            "files": tuple(
                CanonicalStateFile(
                    path=item.path,
                    content=item.content + b"\n# " + comment + b"\n"
                    if item.path == "graph.yaml"
                    else item.content,
                )
                for item in snapshot.files
            )
        }
    )


def _authority_for(release: VerifiedReleaseV2, account: int) -> PublicationAuthorityV2:
    member = next(item for item in release.authority.members if item.github_account_id == account)
    return PublicationAuthorityV2(
        registry=release.authority,
        local_member_id=member.member_id,
        local_device_certificate_id=member.device_certificate_ids[0],
        remote_state=release,
        publication_base_commit=release.commit,
    )


def _accept(
    publication: object,
    *,
    parent: VerifiedReleaseV2,
    key_id: str,
    private_key: bytes,
    commit: str,
    now: object,
) -> VerifiedReleaseV2:
    return verify_v2_release(
        manifest_bytes=publication.manifest_bytes,  # type: ignore[attr-defined]
        bundle_bytes=publication.bundle,  # type: ignore[attr-defined]
        envelope_bytes=publication.signatures,  # type: ignore[attr-defined]
        parent=parent,
        recipient_key_id=key_id,
        recipient_private_key=private_key,
        commit=commit,
        now=now,  # type: ignore[arg-type]
    )


def test_v1_migration_remains_the_only_one_way_entry_to_stable_root(
    tmp_path: Path,
) -> None:
    """Compose the already-adversarial migration scenario into the release journey."""
    _prove_v1_migration(tmp_path)


def test_two_independent_developers_enroll_restore_publish_reconcile_and_revoke(
    tmp_path: Path,
) -> None:
    """Exercise A, B, and CI custody from public join exchange through revocation."""
    fixture = enrolled_candidate(tmp_path)
    b_checkout = fixture.target
    a_checkout = tmp_path / "developer-a-checkout"
    subprocess.run(
        ["git", "clone", "--quiet", str(b_checkout), str(a_checkout)],
        check=True,
        capture_output=True,
        shell=False,
    )
    assert a_checkout != b_checkout
    assert fixture.a._device_store._backend is not fixture.b._device_store._backend
    assert fixture.a._device_store._lock_target != fixture.b._device_store._lock_target

    # GitHub linear merge rewriting may change the commit while preserving the exact release tree.
    tree = git(b_checkout, "rev-parse", fixture.commit + "^{tree}").decode().strip()
    rewritten = (
        git(
            b_checkout,
            "commit-tree",
            tree,
            "-p",
            fixture.parent.commit,
            input_bytes=b"Merge reviewed enrollment\n",
        )
        .decode()
        .strip()
    )
    git(b_checkout, "update-ref", "refs/remotes/origin/intent-state", rewritten)
    restorer = GitSharedStateRestorer(
        fixture.provider,
        clock=lambda: fixture.at,
        device_key_store=fixture.b._device_store,
        governance_registry=fixture.governance,
    )
    result = restorer.verify_and_restore_approved_baseline(b_checkout)
    assert result.status is SharedStateRestoreStatus.VERIFIED
    assert fixture.provider.load_pending_join() is None
    assert canonical_files(b_checkout) == fixture.files

    a_cert = next(
        item
        for item in fixture.publication.authority.device_certificates
        if item.claims.github_account_id == 100
    )
    b_cert = next(
        item
        for item in fixture.publication.authority.device_certificates
        if item.claims.github_account_id == 200
    )
    ci = fixture.publication.authority.ci_recipient
    enrolled_a = _accept(
        fixture.publication,
        parent=fixture.parent,
        key_id=a_cert.claims.recipient_key_id,
        private_key=b"a" * 32,
        commit=rewritten,
        now=fixture.at,
    )
    enrolled_ci = _accept(
        fixture.publication,
        parent=fixture.parent,
        key_id=ci.key_id,
        private_key=b"c" * 32,
        commit=rewritten,
        now=fixture.at,
    )
    # B's private key is deliberately unavailable through the public API; the successful
    # restorer call above is B's decryption proof. The public release object is identical.
    enrolled_b = enrolled_ci
    assert enrolled_a.snapshot == enrolled_ci.snapshot

    b_publication = prepare_v2_publication(
        snapshot=_snapshot_with_comment(enrolled_b.snapshot, b"bob ordinary publication"),
        authority=_authority_for(enrolled_b, 200),
        device_signer=fixture.b._device_store,
        now=fixture.at,
    )
    b_commit = "4" * 40
    accepted_by_a = _accept(
        b_publication,
        parent=enrolled_a,
        key_id=a_cert.claims.recipient_key_id,
        private_key=b"a" * 32,
        commit=b_commit,
        now=fixture.at,
    )
    accepted_by_ci = _accept(
        b_publication,
        parent=enrolled_ci,
        key_id=ci.key_id,
        private_key=b"c" * 32,
        commit=b_commit,
        now=fixture.at,
    )
    assert accepted_by_a.snapshot == accepted_by_ci.snapshot

    # A and B now produce competing children from the same accepted parent.
    a_publication = prepare_v2_publication(
        snapshot=_snapshot_with_comment(enrolled_a.snapshot, b"alice competing choice"),
        authority=_authority_for(enrolled_a, 100),
        device_signer=fixture.a._device_store,
        now=fixture.at,
    )
    remote = _accept(
        a_publication,
        parent=enrolled_ci,
        key_id=ci.key_id,
        private_key=b"c" * 32,
        commit="5" * 40,
        now=fixture.at,
    )
    local = _snapshot_with_comment(enrolled_b.snapshot, b"bob competing choice")
    reconciliation = ReconciliationService(
        authority_provider=lambda release: _authority_for(release, 200),
        device_signer=fixture.b._device_store,
        clock=lambda: fixture.at,
    )
    preview = reconciliation.preview(common=enrolled_b, remote=remote, local=local)
    assert preview.conflicts == ("graph.yaml",)
    resolved = reconciliation.resolve(preview=preview, choices={"graph.yaml": "local"})
    credential = _credential(200, "bob", "github:200")
    payload = reconciliation.decision_payload(
        preview=resolved,
        repository_id=credential.repository_id,
        actor=credential.actor,
        challenge="challenge:" + "6" * 64,
        now=fixture.at,
    )
    reconciled = reconciliation.prepare(
        preview=resolved,
        decision=VerifiedHumanDecision(payload, credential, fixture.at),
        current_remote=remote,
    )
    final = _accept(
        reconciled,
        parent=remote,
        key_id=ci.key_id,
        private_key=b"c" * 32,
        commit="6" * 40,
        now=fixture.at,
    )
    assert final.snapshot == local
    assert final.manifest.parent_bundle_digest == remote.manifest.bundle_digest

    b_member = next(
        item for item in final.authority.members if item.github_account_id == 200
    ).model_copy(update={"status": "revoked", "revoked_at": fixture.at})
    revoked = TeamAuthorityRegistryV2(
        **{
            **final.authority.model_dump(mode="python"),
            "sequence": final.authority.sequence + 1,
            "previous_authority_digest": authority_digest(final.authority),
            "members": tuple(
                b_member if item.github_account_id == 200 else item
                for item in final.authority.members
            ),
            "revocations": (
                DeviceRevocationV2(
                    certificate_id=b_cert.certificate_id,
                    revoked_at=fixture.at,
                    reason="member-removed",
                    sponsor_member_id=a_cert.claims.member_id,
                ),
            ),
        }
    )
    revoked_manifest = final.manifest.model_copy(
        update={
            "authority_digest": authority_digest(revoked),
            "recipient_key_ids": revoked.active_recipient_key_ids(),
        }
    )
    revoked_parent = replace(
        final,
        authority=revoked,
        manifest=revoked_manifest,
        manifest_bytes=revoked_manifest.canonical_bytes(),
    )
    b_trust = fixture.provider.load_versioned().model_copy(
        update={
            "accepted_authority_digest": authority_digest(revoked),
            "accepted_authority_sequence": revoked.sequence,
        }
    )
    with pytest.raises(ValueError, match="publication authority unavailable"):
        authority_from_verified_state(revoked_parent, b_trust)

    invite_bytes = export_team_invite(fixture.pending.invite)
    response_bytes = export_join_response(fixture.pending.response)
    public_release = (
        fixture.publication.manifest_bytes
        + fixture.publication.bundle
        + fixture.publication.signatures
    )
    received_by_a = response_bytes + public_release
    received_by_b = invite_bytes + public_release
    for private in (b"b" * 32, b"t" * 32):
        assert base64.urlsafe_b64encode(private).rstrip(b"=") not in received_by_a
    for private in (b"a" * 32, b"s" * 32, b"r" * 32):
        assert base64.urlsafe_b64encode(private).rstrip(b"=") not in received_by_b


class _OversizedStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self.chunks = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(17):
            self.chunks += 1
            yield b"x" * (64 * 1024)

    async def aclose(self) -> None:
        self.closed = True


class _CancelledStream(httpx.AsyncByteStream):
    def __init__(self, signal: BaseException) -> None:
        self.signal = signal
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"prefix"
        raise self.signal

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.anyio
async def test_release_raw_reads_stream_large_bundles_and_stop_on_bounds_or_cancel() -> None:
    credentials = GitHubCredentials(
        token=SecretStr("ephemeral-release-proof-token"),
        source=CredentialSource.ENVIRONMENT,
    )
    large = b"x" * (1024 * 1024 + 1)
    large_client = GitHubClient(
        credentials,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=large)),
    )
    assert (
        await large_client.request_bytes(
            "GET",
            "/repos/acme/project/git/blobs/" + "1" * 40,
            max_bytes=len(large),
            accept="application/vnd.github.raw+json",
        )
        == large
    )
    await large_client.aclose()

    oversized = _OversizedStream()
    oversized_client = GitHubClient(
        credentials,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=oversized)),
    )
    with pytest.raises(GitHubProtocolError):
        await oversized_client.request_bytes(
            "GET",
            "/repos/acme/project/git/blobs/" + "2" * 40,
            max_bytes=1024 * 1024,
            accept="application/vnd.github.raw+json",
        )
    await oversized_client.aclose()
    assert oversized.chunks == 17
    assert oversized.closed is True

    signal = asyncio.CancelledError()
    cancelled = _CancelledStream(signal)
    cancelled_client = GitHubClient(
        credentials,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=cancelled)),
    )
    with pytest.raises(asyncio.CancelledError) as caught:
        await cancelled_client.request_bytes(
            "GET",
            "/repos/acme/project/git/blobs/" + "3" * 40,
            max_bytes=1024 * 1024,
            accept="application/vnd.github.raw+json",
        )
    assert caught.value is signal
    await cancelled_client.aclose()
    assert cancelled.closed is True


def test_operator_docs_and_dogfood_graph_cover_the_release_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    guide = (root / "docs/intent-aware-agent.md").read_text().lower()
    graph = (root / "graph/framework-intent-graph.yaml").read_text()
    for required in (
        "intent team invite",
        "intent team join",
        "intent team approve-join",
        "repo + admin:org",
        "closed-unmerged",
        "root loss",
        "reviewed reconciliation",
    ):
        assert required in guide
    for node_id in (
        "cap-stable-root-team-enrollment",
        "cap-automatic-member-restore",
        "cap-reviewed-team-reconciliation",
    ):
        assert node_id in graph
