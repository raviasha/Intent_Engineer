"""Canonical, bounded archive behavior for approved team state."""

from __future__ import annotations

import base64
import hashlib
import random
import struct
import traceback
from datetime import UTC, datetime, timedelta

import pytest

from intent_engineering import team_state
from intent_engineering.team_state.archive import (
    ARCHIVE_MAGIC,
    ARCHIVE_V2_MAGIC,
    AUTHORITY_PATH,
    CANONICAL_FILE_MODE,
    CANONICAL_MTIME_NS,
    build_archive,
    build_archive_v2,
    validate_archive,
    validate_archive_v2,
    validate_versioned_archive,
)
from intent_engineering.team_state.authority import (
    authority_digest,
    canonical_authority_bytes,
    derive_certificate_id,
    derive_member_id,
    derive_recipient_key_id,
    derive_root_key_id,
    derive_signature_id,
)
from intent_engineering.team_state.models import (
    CANONICAL_STATE_PATHS,
    MAX_AUTHORITY_BYTES,
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    CanonicalStateFile,
    CanonicalStateSnapshot,
    CiRecipientRecord,
    DeviceCertificateClaimsV2,
    DeviceSignerCertificateV2,
    MemberRecordV2,
    RestoredSnapshotV2,
    TeamAuthorityPolicyV2,
    TeamAuthorityRegistryV2,
    TeamRootTrustV2,
    TeamStateManifest,
    TeamStateManifestV2,
)
from intent_engineering.team_state.restore import _parse_payload, _UpgradeRequired

REPOSITORY = "github.com/acme/project"
NOW = datetime(2026, 9, 9, 10, tzinfo=UTC)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _authority(
    *,
    project_id: str = "project",
    repository_id: str = REPOSITORY,
) -> TeamAuthorityRegistryV2:
    root_public = _b64(b"r" * 32)
    recipient_public = _b64(b"x" * 32)
    signing_public = _b64(b"s" * 32)
    member_id = derive_member_id(project_id, repository_id, 1234)
    root_id = derive_root_key_id(project_id, repository_id, root_public)
    claims = DeviceCertificateClaimsV2(
        project_id=project_id,
        repository_id=repository_id,
        authority_epoch=1,
        member_id=member_id,
        device_id="device:" + "1" * 32,
        github_account_id=1234,
        github_login="alice-dev",
        recipient_key_id=derive_recipient_key_id(project_id, repository_id, recipient_public),
        recipient_public_key=recipient_public,
        signature_id=derive_signature_id(project_id, repository_id, signing_public),
        signing_public_key=signing_public,
        webauthn_credential_digest="sha256:" + "c" * 64,
        serial=1,
        issued_at=NOW,
        expires_at=NOW + timedelta(days=366),
    )
    certificate = DeviceSignerCertificateV2(
        claims=claims,
        certificate_id=derive_certificate_id(claims),
        root_key_id=root_id,
        root_signature=_b64(b"z" * 64),
    )
    member = MemberRecordV2(
        member_id=member_id,
        actor="github:1234",
        github_account_id=1234,
        github_login="alice-dev",
        role="sponsor",
        status="active",
        device_certificate_ids=(certificate.certificate_id,),
        enrolled_at=NOW,
    )
    return TeamAuthorityRegistryV2(
        project_id=project_id,
        repository_id=repository_id,
        authority_epoch=1,
        sequence=1,
        root=TeamRootTrustV2(
            project_id=project_id,
            repository_id=repository_id,
            authority_epoch=1,
            root_key_id=root_id,
            root_public_key=root_public,
            created_at=NOW,
        ),
        policy=TeamAuthorityPolicyV2(),
        members=(member,),
        device_certificates=(certificate,),
        revocations=(),
        ci_recipient=CiRecipientRecord(
            project_id=project_id,
            repository_id=repository_id,
            runner_id="intent-state",
            public_key=_b64(b"c" * 32),
        ),
        previous_authority_digest=None,
    )


def _snapshot(contents: tuple[bytes, ...] | None = None) -> CanonicalStateSnapshot:
    values = contents or tuple(path.encode() for path in CANONICAL_STATE_PATHS)
    return CanonicalStateSnapshot(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=7,
        files=tuple(
            CanonicalStateFile(path=path, content=content)
            for path, content in zip(CANONICAL_STATE_PATHS, values, strict=True)
        ),
    )


def _raw_archive(
    entries: list[tuple[int, str, int, int, bytes]],
    *,
    magic: bytes = ARCHIVE_MAGIC,
    project_id: str = "project",
    repository_id: str = REPOSITORY,
    graph_version: int = 7,
) -> bytes:
    project = project_id.encode()
    repository = repository_id.encode()
    result = bytearray(magic)
    result.extend(struct.pack(">H", len(project)))
    result.extend(project)
    result.extend(struct.pack(">H", len(repository)))
    result.extend(repository)
    result.extend(struct.pack(">QH", graph_version, len(entries)))
    for kind, path, mode, mtime_ns, content in entries:
        encoded_path = path.encode()
        result.extend(
            struct.pack(
                ">BHHQQ",
                kind,
                len(encoded_path),
                mode,
                mtime_ns,
                len(content),
            )
        )
        result.extend(hashlib.sha256(content).digest())
        result.extend(encoded_path)
        result.extend(content)
    return bytes(result)


def _entries(contents: tuple[bytes, ...] | None = None) -> list[tuple[int, str, int, int, bytes]]:
    values = contents or tuple(path.encode() for path in CANONICAL_STATE_PATHS)
    return [
        (1, path, CANONICAL_FILE_MODE, CANONICAL_MTIME_NS, content)
        for path, content in zip(CANONICAL_STATE_PATHS, values, strict=True)
    ]


def _v2_entries(
    authority_bytes: bytes | None = None,
    contents: tuple[bytes, ...] | None = None,
) -> list[tuple[int, str, int, int, bytes]]:
    return [
        *_entries(contents),
        (
            1,
            AUTHORITY_PATH,
            CANONICAL_FILE_MODE,
            CANONICAL_MTIME_NS,
            authority_bytes
            if authority_bytes is not None
            else canonical_authority_bytes(_authority()),
        ),
    ]


def test_archive_is_deterministic_and_round_trips_exact_snapshot() -> None:
    """Catches unstable metadata/order or restore losing snapshot identity and bytes."""
    snapshot = _snapshot()

    first = build_archive(snapshot)
    second = build_archive(snapshot)

    assert first == second
    assert first == _raw_archive(_entries())
    assert validate_archive(first) == snapshot


def test_archive_v2_has_one_golden_byte_sequence_and_returns_bound_authority() -> None:
    """Catches v2 framing, inventory order, or authority restoration drifting."""
    snapshot = _snapshot()
    authority = _authority()

    encoded = build_archive_v2(snapshot, authority)
    golden = _raw_archive(_v2_entries(), magic=ARCHIVE_V2_MAGIC)

    assert encoded == golden
    assert build_archive_v2(snapshot, authority) == encoded
    restored = validate_archive_v2(encoded)
    manifest = TeamStateManifestV2(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=7,
        parent_bundle_digest="sha256:" + "a" * 64,
        bundle_digest="sha256:" + "b" * 64,
        bundle_size=1,
        recipient_key_ids=authority.active_recipient_key_ids(),
        authority_digest="sha256:"
        + hashlib.sha256(canonical_authority_bytes(authority)).hexdigest(),
        authority_epoch=authority.authority_epoch,
        root_key_id=authority.root.root_key_id,
        created_at=NOW,
    )

    assert restored == RestoredSnapshotV2(
        snapshot=snapshot,
        authority=authority,
    )
    assert authority_digest(restored.authority) == manifest.authority_digest


def test_versioned_archive_dispatch_returns_exact_v1_or_v2_model() -> None:
    """Catches callers guessing an archive schema and discarding v2 authority data."""
    snapshot = _snapshot()
    authority = _authority()

    assert validate_versioned_archive(build_archive(snapshot)) == snapshot
    assert validate_versioned_archive(build_archive_v2(snapshot, authority)) == (
        RestoredSnapshotV2(snapshot=snapshot, authority=authority)
    )


def test_legacy_payload_parser_reports_binary_archive_v2_as_upgrade_required() -> None:
    """Catches archive v2 falling through to UTF-8 decoding and becoming INVALID."""
    with pytest.raises(_UpgradeRequired, match="unsupported shared-state schema"):
        _parse_payload(build_archive_v2(_snapshot(), _authority()))


def test_archive_v2_does_not_change_the_version_one_golden_bytes() -> None:
    """Catches introducing schema dispatch by silently rewriting valid v1 archives."""
    snapshot = _snapshot()
    expected = _raw_archive(_entries(), magic=ARCHIVE_MAGIC)

    assert build_archive(snapshot) == expected
    assert validate_archive(expected) == snapshot
    with pytest.raises(ValueError, match="magic"):
        validate_archive(expected.replace(ARCHIVE_MAGIC, ARCHIVE_V2_MAGIC, 1))


@pytest.mark.parametrize(
    "entries",
    [
        _v2_entries()[:-1],
        [
            *_entries(),
            (1, "authority/renamed.json", 0o600, 0, canonical_authority_bytes(_authority())),
        ],
        [*_v2_entries(), _v2_entries()[-1]],
        [_v2_entries()[-1], *_entries()],
        [*_entries(), (1, "../authority/team-authority.json", 0o600, 0, b"{}")],
        [*_entries(), (2, AUTHORITY_PATH, 0o600, 0, canonical_authority_bytes(_authority()))],
        [*_entries(), (1, AUTHORITY_PATH, 0o644, 0, canonical_authority_bytes(_authority()))],
        [*_entries(), (1, AUTHORITY_PATH, 0o600, 1, canonical_authority_bytes(_authority()))],
    ],
    ids=(
        "missing",
        "renamed",
        "duplicate",
        "reordered",
        "traversal",
        "kind",
        "mode",
        "mtime",
    ),
)
def test_archive_v2_rejects_missing_renamed_duplicate_or_noncanonical_authority_entry(
    entries: list[tuple[int, str, int, int, bytes]],
) -> None:
    """Catches v2 accepting an extension path or mutable/special authority metadata."""
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(_raw_archive(entries, magic=ARCHIVE_V2_MAGIC))


def test_archive_versions_reject_each_others_exact_inventory() -> None:
    """Catches magic-only dispatch accepting a v1/v2 inventory mismatch."""
    with pytest.raises(ValueError, match="inventory"):
        validate_archive(_raw_archive(_v2_entries(), magic=ARCHIVE_MAGIC))
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(_raw_archive(_entries(), magic=ARCHIVE_V2_MAGIC))


@pytest.mark.parametrize(
    "authority_bytes",
    [
        canonical_authority_bytes(_authority()).replace(b"{", b"{ ", 1),
        canonical_authority_bytes(_authority()).replace(
            b'{"authority_epoch":1', b'{"authority_epoch":1,"authority_epoch":1', 1
        ),
        b"{" + b" " * MAX_AUTHORITY_BYTES,
    ],
    ids=("noncanonical", "duplicate-key", "oversized"),
)
def test_archive_v2_rejects_noncanonical_or_oversized_authority_json(
    authority_bytes: bytes,
) -> None:
    """Catches ambiguous or resource-exhausting authority data reaching restoration."""
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(_raw_archive(_v2_entries(authority_bytes), magic=ARCHIVE_V2_MAGIC))


def test_archive_v2_rejects_cross_scope_authority_and_untrusted_model_copies() -> None:
    """Catches a valid authority from another project being paired with this state."""
    foreign = _authority(project_id="other")
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(
            _raw_archive(
                _v2_entries(canonical_authority_bytes(foreign)),
                magic=ARCHIVE_V2_MAGIC,
            )
        )
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        build_archive_v2(_snapshot(), foreign)


def test_archive_v2_rejects_truncation_trailing_digest_and_declared_size_bombs() -> None:
    """Catches partial reads, hidden suffixes, tampering, and hostile length fields."""
    valid = _raw_archive(_v2_entries(), magic=ARCHIVE_V2_MAGIC)
    first_entry = len(ARCHIVE_V2_MAGIC) + 2 + len("project") + 2 + len(REPOSITORY) + 8 + 2
    truncated = tuple(
        valid[:offset]
        for offset in (
            0,
            len(ARCHIVE_V2_MAGIC) - 1,
            len(ARCHIVE_V2_MAGIC) + 1,
            first_entry + 1,
            first_entry + struct.calcsize(">BHHQQ") - 1,
            first_entry + struct.calcsize(">BHHQQ") + 31,
            len(valid) - 1,
        )
    )
    for content in (*truncated, valid + b"trailing"):
        with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
            validate_archive_v2(content)

    tampered = bytearray(valid)
    tampered[-1] ^= 1
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(bytes(tampered))

    bomb = bytearray(valid)
    project_size = len("project")
    repository_size = len(REPOSITORY)
    first_entry = len(ARCHIVE_V2_MAGIC) + 2 + project_size + 2 + repository_size + 8 + 2
    declared_size = first_entry + 1 + 2 + 2 + 8
    bomb[declared_size : declared_size + 8] = struct.pack(">Q", MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(bytes(bomb))

    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(b"x" * (MAX_BUNDLE_BYTES + 1))


def test_archive_v2_preserves_the_existing_aggregate_state_limit() -> None:
    """Catches authority bytes being used to relax the 16 MiB canonical-state cap."""
    contents = (b"a" * MAX_FILE_BYTES, b"b" * MAX_FILE_BYTES, b"c") + tuple(
        b"" for _ in CANONICAL_STATE_PATHS[3:]
    )
    with pytest.raises(ValueError, match="^canonical state archive unavailable$"):
        validate_archive_v2(_raw_archive(_v2_entries(contents=contents), magic=ARCHIVE_V2_MAGIC))


def test_team_state_package_exports_archive_and_crypto_boundaries() -> None:
    """Catches callers needing private restore helpers for the planned public APIs."""
    from intent_engineering.team_state.crypto import decrypt_bundle, encrypt_bundle

    assert team_state.build_archive is build_archive
    assert team_state.build_archive_v2 is build_archive_v2
    assert team_state.validate_archive is validate_archive
    assert team_state.validate_archive_v2 is validate_archive_v2
    assert team_state.validate_versioned_archive is validate_versioned_archive
    assert team_state.ARCHIVE_V2_MAGIC is ARCHIVE_V2_MAGIC
    assert team_state.AUTHORITY_PATH is AUTHORITY_PATH
    assert team_state.RestoredSnapshotV2 is RestoredSnapshotV2
    assert team_state.encrypt_bundle is encrypt_bundle
    assert team_state.decrypt_bundle is decrypt_bundle


def test_hardened_restore_accepts_archive_and_binds_its_snapshot_identity() -> None:
    """Catches publication producing canonical archives that current restore cannot consume."""
    snapshot = _snapshot()
    content = build_archive(snapshot)
    manifest = TeamStateManifest(
        project_id="project",
        repository_id=REPOSITORY,
        graph_version=7,
        parent_bundle_digest=None,
        bundle_digest="sha256:" + "a" * 64,
        bundle_size=1,
        recipient_key_ids=("recipient:alice",),
        required_signature_ids=("signer:release",),
        created_at=datetime(2026, 9, 8, tzinfo=UTC),
    )

    assert _parse_payload(content, manifest) == {item.path: item.content for item in snapshot.files}
    with pytest.raises(ValueError, match="identity"):
        _parse_payload(content, manifest.model_copy(update={"project_id": "other"}))


def test_one_hundred_deterministic_randomized_archives_round_trip() -> None:
    """Catches framing bugs across empty, binary, and varied-length canonical contents."""
    source = random.Random(20260908)
    for _ in range(100):
        contents = tuple(
            source.randbytes(source.randrange(0, 513)) for _path in CANONICAL_STATE_PATHS
        )
        snapshot = _snapshot(contents)
        encoded = build_archive(snapshot)

        assert build_archive(snapshot) == encoded
        assert validate_archive(encoded) == snapshot


@pytest.mark.parametrize(
    "entries",
    [
        _entries()[:-1],
        list(reversed(_entries())),
        [*_entries()[:-1], _entries()[0]],
        [*_entries()[:-1], (1, "../graph.yaml", 0o600, 0, b"x")],
        [*_entries()[:-1], (1, "/graph.yaml", 0o600, 0, b"x")],
        [*_entries()[:-1], (1, "graph\\yaml", 0o600, 0, b"x")],
        [(2, *_entries()[0][1:]), *_entries()[1:]],
        [(3, *_entries()[0][1:]), *_entries()[1:]],
        [(1, _entries()[0][1], 0o120000, 0, b"target"), *_entries()[1:]],
        [(1, _entries()[0][1], 0o600, 1, b"x"), *_entries()[1:]],
    ],
)
def test_archive_rejects_incomplete_reordered_duplicate_unsafe_or_special_entries(
    entries: list[tuple[int, str, int, int, bytes]],
) -> None:
    """Catches ambiguous inventory, escape paths, links/devices, or mutable metadata."""
    with pytest.raises(ValueError, match="invalid|noncanonical"):
        validate_archive(_raw_archive(entries))


@pytest.mark.parametrize(
    "content",
    [
        b"",
        ARCHIVE_MAGIC[:-1],
        _raw_archive(_entries())[:-1],
        _raw_archive(_entries()) + b"trailing",
        _raw_archive(_entries()).replace(ARCHIVE_MAGIC, b"BADMAGIC", 1),
    ],
)
def test_archive_rejects_truncated_wrong_magic_or_trailing_bytes(content: bytes) -> None:
    """Catches prefix acceptance that could hide a substituted or truncated payload."""
    with pytest.raises(ValueError, match="invalid|noncanonical"):
        validate_archive(content)


def test_archive_rejects_digest_tamper_and_declared_size_bombs() -> None:
    """Catches undetected content substitution and allocation from hostile length fields."""
    valid = bytearray(_raw_archive(_entries()))
    valid[-1] ^= 1
    with pytest.raises(ValueError, match="invalid"):
        validate_archive(bytes(valid))

    bomb = bytearray(_raw_archive(_entries()))
    project_size = len("project")
    repository_size = len(REPOSITORY)
    first_entry = len(ARCHIVE_MAGIC) + 2 + project_size + 2 + repository_size + 8 + 2
    declared_size = first_entry + 1 + 2 + 2 + 8
    bomb[declared_size : declared_size + 8] = struct.pack(">Q", MAX_FILE_BYTES + 1)
    with pytest.raises(ValueError, match="oversized"):
        validate_archive(bytes(bomb))


def test_archive_rejects_aggregate_bomb_without_decompression() -> None:
    """Catches individually bounded files exceeding the aggregate plaintext budget."""
    contents = (b"a" * MAX_FILE_BYTES, b"b" * MAX_FILE_BYTES, b"c") + tuple(
        b"" for _ in CANONICAL_STATE_PATHS[3:]
    )
    with pytest.raises(ValueError, match="oversized"):
        validate_archive(_raw_archive(_entries(contents)))


def test_archive_revalidates_untrusted_model_copies() -> None:
    """Catches Pydantic copy-update bypasses crossing archive construction."""
    forged = _snapshot().model_copy(update={"files": tuple(reversed(_snapshot().files))})

    with pytest.raises(ValueError):
        build_archive(forged)


def _assert_fixed_archive_failure(error: BaseException, marker: str) -> None:
    assert str(error) == "canonical state archive unavailable"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert marker not in repr(error)
    for frame, _line in traceback.walk_tb(error.__traceback__):
        if frame.f_globals.get("__name__") != "intent_engineering.team_state.archive":
            continue
        assert all(marker not in repr(value) for value in frame.f_locals.values())


def test_archive_v2_public_build_error_does_not_retain_hostile_model_values() -> None:
    """Catches forged model fields surviving in error chains or traceback locals."""
    marker = "PRIVATE-ARCHIVE-BUILD-8197"
    forged = _snapshot().model_copy(update={"repository_id": marker})

    with pytest.raises(ValueError) as caught:
        build_archive_v2(forged, _authority())

    _assert_fixed_archive_failure(caught.value, marker)


@pytest.mark.parametrize(
    "operation",
    [validate_archive_v2, validate_versioned_archive],
    ids=("v2-validator", "version-dispatch"),
)
def test_archive_v2_public_parse_errors_do_not_retain_hostile_authority_values(
    operation: object,
) -> None:
    """Catches secret-shaped authority input surviving public parser failures."""
    marker = "PRIVATE-ARCHIVE-AUTHORITY-8197"
    hostile_authority = canonical_authority_bytes(_authority()).replace(
        b"alice-dev", marker.encode()
    )
    content = _raw_archive(_v2_entries(hostile_authority), magic=ARCHIVE_V2_MAGIC)

    with pytest.raises(ValueError) as caught:
        operation(content)  # type: ignore[operator]

    _assert_fixed_archive_failure(caught.value, marker)
