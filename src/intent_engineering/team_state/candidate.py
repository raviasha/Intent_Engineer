"""Read-only validation of an exact proposed state release, never candidate code."""

from __future__ import annotations

import re
import traceback
from datetime import datetime
from pathlib import Path

from intent_engineering.team_state.ci import CiTrustError
from intent_engineering.team_state.models import (
    MAX_BUNDLE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_SIGNATURE_BYTES,
    CanonicalStateFile,
    CanonicalStateSnapshot,
    TeamStateManifest,
    TeamStateManifestV2,
)
from intent_engineering.team_state.restore import (
    MAX_ANCESTRY_COMMITS,
    SharedStateTrust,
    TrustedSigningKey,
    TrustProvider,
    VerifiedReleaseV2,
    VerifiedV1Release,
    _decrypt_release_payload,
    _GitRefReader,
    _origin_repository,
    _release_name,
    _run_git,
    _validate_authenticated_state,
    _verify_release_lineage,
    parse_team_manifest,
    verify_v2_migration_release,
    verify_v2_release,
)


def _scrub_candidate_error(error: BaseException) -> BaseException:
    error_traceback = error.__traceback__
    if error_traceback is not None:
        traceback.clear_frames(error_traceback)
    error_traceback = None
    error.args = ()
    error.__dict__.clear()
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
    return error


def _v2_artifacts(
    reader: _GitRefReader, commit: str, manifest: TeamStateManifestV2
) -> tuple[bytes, bytes, bytes]:
    manifest_bytes = reader.blob(commit, "manifest.json", MAX_MANIFEST_BYTES)
    suffix = _release_name(manifest)  # type: ignore[arg-type]
    bundle = reader.blob(commit, f"bundles/{suffix}.intent", MAX_BUNDLE_BYTES)
    envelope = reader.blob(commit, f"signatures/{suffix}.json", MAX_SIGNATURE_BYTES)
    return manifest_bytes, bundle, envelope


def _verified_v2_base(
    reader: _GitRefReader,
    base: str,
    trust: SharedStateTrust,
    at: datetime,
) -> VerifiedReleaseV2:
    """Replay the bounded v1 bridge and v2 chain to recover trusted parent authority."""
    commit = base
    newer: list[tuple[str, TeamStateManifestV2]] = []
    for _depth in range(MAX_ANCESTRY_COMMITS):
        content = reader.blob(commit, "manifest.json", MAX_MANIFEST_BYTES)
        manifest = parse_team_manifest(content)
        if type(manifest) is TeamStateManifest:
            lineage = _verify_release_lineage(reader, commit, trust, at, None)
            files = _decrypt_release_payload(reader, lineage.tip, trust)
            snapshot = CanonicalStateSnapshot(
                project_id=manifest.project_id,
                repository_id=manifest.repository_id,
                graph_version=manifest.graph_version,
                files=tuple(
                    CanonicalStateFile(path=path, content=files[path]) for path in sorted(files)
                ),
            )
            legacy = VerifiedV1Release(
                manifest=lineage.tip.manifest,
                manifest_bytes=reader.blob(commit, "manifest.json", MAX_MANIFEST_BYTES),
                signing_keys=tuple(
                    TrustedSigningKey(item.signature_id, item.public_key)
                    for item in trust.signing_keys
                ),
                snapshot=snapshot,
            )
            verified: VerifiedReleaseV2 | None = None
            previous_commit = commit
            for child_commit, child_manifest in reversed(newer):
                if reader.parents(child_commit) != (previous_commit,):
                    raise ValueError("state candidate changed")
                manifest_bytes, bundle, envelope = _v2_artifacts(
                    reader, child_commit, child_manifest
                )
                if verified is None:
                    verified = verify_v2_migration_release(
                        manifest_bytes=manifest_bytes,
                        bundle_bytes=bundle,
                        envelope_bytes=envelope,
                        current=legacy,
                        recipient_key_id=trust.recipient_key_id,
                        recipient_private_key=trust.recipient_private_key,
                        commit=child_commit,
                        now=at,
                    )
                else:
                    verified = verify_v2_release(
                        manifest_bytes=manifest_bytes,
                        bundle_bytes=bundle,
                        envelope_bytes=envelope,
                        parent=verified,
                        recipient_key_id=trust.recipient_key_id,
                        recipient_private_key=trust.recipient_private_key,
                        commit=child_commit,
                        now=at,
                    )
                previous_commit = child_commit
            if verified is None or verified.commit != base:
                raise ValueError("state candidate changed")
            return verified
        if type(manifest) is not TeamStateManifestV2:
            raise ValueError("state candidate changed")
        newer.append((commit, manifest))
        parents = reader.parents(commit)
        if len(parents) != 1:
            raise ValueError("state candidate changed")
        commit = parents[0]
    raise ValueError("state candidate history exceeds traversal bound")


def _validate_v2_candidate(
    reader: _GitRefReader,
    trust: SharedStateTrust,
    *,
    root: Path,
    base: str,
    head: str,
    at: datetime,
) -> None:
    base_manifest_bytes = reader.blob(base, "manifest.json", MAX_MANIFEST_BYTES)
    base_manifest = parse_team_manifest(base_manifest_bytes)
    legacy_parent: VerifiedV1Release | None = None
    parent: VerifiedReleaseV2 | None = None
    if type(base_manifest) is TeamStateManifest:
        lineage = _verify_release_lineage(reader, base, trust, at, None)
        files = _decrypt_release_payload(reader, lineage.tip, trust)
        legacy_parent = VerifiedV1Release(
            manifest=lineage.tip.manifest,
            manifest_bytes=base_manifest_bytes,
            signing_keys=tuple(
                TrustedSigningKey(item.signature_id, item.public_key) for item in trust.signing_keys
            ),
            snapshot=CanonicalStateSnapshot(
                project_id=base_manifest.project_id,
                repository_id=base_manifest.repository_id,
                graph_version=base_manifest.graph_version,
                files=tuple(
                    CanonicalStateFile(path=path, content=files[path]) for path in sorted(files)
                ),
            ),
        )
    elif type(base_manifest) is TeamStateManifestV2:
        parent = _verified_v2_base(reader, base, trust, at)
    else:
        raise ValueError("state candidate changed")
    manifest_bytes = reader.blob(head, "manifest.json", MAX_MANIFEST_BYTES)
    manifest = parse_team_manifest(manifest_bytes)
    if type(manifest) is not TeamStateManifestV2:
        raise ValueError("state candidate schema rollback")
    suffix = _release_name(manifest)  # type: ignore[arg-type]
    expected = {"manifest.json", f"bundles/{suffix}.intent", f"signatures/{suffix}.json"}
    listing = _run_git(root, ("ls-tree", "-rz", "--full-tree", head), maximum=65536)
    paths: set[str] = set()
    for item in listing.rstrip(b"\0").split(b"\0"):
        metadata, path = item.split(b"\t", 1)
        if not metadata.startswith(b"100644 blob "):
            raise ValueError("unsafe state candidate")
        paths.add(path.decode("utf-8"))
    if paths != expected:
        raise ValueError("state candidate inventory changed")
    _manifest, bundle, envelope = _v2_artifacts(reader, head, manifest)
    if legacy_parent is not None:
        verified = verify_v2_migration_release(
            manifest_bytes=manifest_bytes,
            bundle_bytes=bundle,
            envelope_bytes=envelope,
            current=legacy_parent,
            recipient_key_id=trust.recipient_key_id,
            recipient_private_key=trust.recipient_private_key,
            commit=head,
            now=at,
        )
        parent_graph_version = legacy_parent.manifest.graph_version
    else:
        assert parent is not None
        verified = verify_v2_release(
            manifest_bytes=manifest_bytes,
            bundle_bytes=bundle,
            envelope_bytes=envelope,
            parent=parent,
            recipient_key_id=trust.recipient_key_id,
            recipient_private_key=trust.recipient_private_key,
            commit=head,
            now=at,
        )
        parent_graph_version = parent.manifest.graph_version
    if verified.manifest.graph_version < parent_graph_version:
        raise ValueError("state candidate graph version rollback")


def validate_candidate(
    root: Path, provider: TrustProvider, *, base: str, head: str, at: datetime
) -> None:
    """Require exact live base, sole-parent lineage, signatures and canonical plaintext."""
    reader: _GitRefReader | None = None
    trust = None
    files: dict[str, bytes] = {}
    failure: BaseException | None = None
    try:
        reader = _GitRefReader(root)
        if any(
            re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", value) is None for value in (base, head)
        ):
            raise ValueError("invalid state candidate")
        trust = provider.load()
        if type(trust) is not SharedStateTrust or _origin_repository(root) != trust.repository_id:
            raise ValueError("invalid state trust")
        if reader.commit() != base or reader.parents(head) != (base,):
            raise ValueError("state candidate changed")
        head_manifest = parse_team_manifest(reader.blob(head, "manifest.json", MAX_MANIFEST_BYTES))
        if type(head_manifest) is TeamStateManifestV2:
            _validate_v2_candidate(reader, trust, root=root, base=base, head=head, at=at)
            if reader.commit() != base:
                raise ValueError("state base changed")
        elif type(head_manifest) is not TeamStateManifest:
            raise ValueError("state candidate changed")
        else:
            lineage = _verify_release_lineage(reader, head, trust, at, None)
            suffix = _release_name(lineage.tip.manifest)
            expected = {"manifest.json", f"bundles/{suffix}.intent", f"signatures/{suffix}.json"}
            listing = _run_git(root, ("ls-tree", "-rz", "--full-tree", head), maximum=65536)
            paths = set()
            for item in listing.rstrip(b"\0").split(b"\0"):
                metadata, path = item.split(b"\t", 1)
                if not metadata.startswith(b"100644 blob "):
                    raise ValueError("unsafe state candidate")
                paths.add(path.decode("utf-8"))
            if paths != expected:
                raise ValueError("state candidate inventory changed")
            files = _decrypt_release_payload(reader, lineage.tip, trust)
            _validate_authenticated_state(files, lineage.tip.manifest, trust)
            if reader.commit() != base:
                raise ValueError("state base changed")
    except BaseException as caught:  # noqa: BLE001 - erase secret-bearing verifier frames
        caught = _scrub_candidate_error(caught)
        failure = (
            caught
            if not isinstance(caught, Exception)
            else CiTrustError()
            if type(caught) is CiTrustError
            else ValueError("state candidate unavailable")
        )
    finally:
        files.clear()
        trust = None
        provider = None  # type: ignore[assignment]
        close_failure: BaseException | None = None
        if reader is not None:
            try:
                reader.close()
            except BaseException as caught:  # noqa: BLE001 - public cancellation boundary
                caught = _scrub_candidate_error(caught)
                close_failure = (
                    caught
                    if not isinstance(caught, Exception)
                    else ValueError("state candidate unavailable")
                )
        reader = None
        root = Path()
        base = ""
        head = ""
        at = None  # type: ignore[assignment]
        if close_failure is not None and (
            failure is None
            or (isinstance(failure, Exception) and not isinstance(close_failure, Exception))
        ):
            failure = close_failure
        close_failure = None
    if failure is not None:
        detached = failure
        failure = None
        raise detached.with_traceback(None) from None
