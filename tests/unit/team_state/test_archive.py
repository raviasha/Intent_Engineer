"""Canonical, bounded archive behavior for approved team state."""

from __future__ import annotations

import hashlib
import random
import struct
from datetime import UTC, datetime

import pytest

from intent_engineering import team_state
from intent_engineering.team_state.archive import (
    ARCHIVE_MAGIC,
    CANONICAL_FILE_MODE,
    CANONICAL_MTIME_NS,
    build_archive,
    validate_archive,
)
from intent_engineering.team_state.models import (
    CANONICAL_STATE_PATHS,
    MAX_FILE_BYTES,
    CanonicalStateFile,
    CanonicalStateSnapshot,
    TeamStateManifest,
)
from intent_engineering.team_state.restore import _parse_payload

REPOSITORY = "github.com/acme/project"


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
    project_id: str = "project",
    repository_id: str = REPOSITORY,
    graph_version: int = 7,
) -> bytes:
    project = project_id.encode()
    repository = repository_id.encode()
    result = bytearray(ARCHIVE_MAGIC)
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


def test_archive_is_deterministic_and_round_trips_exact_snapshot() -> None:
    """Catches unstable metadata/order or restore losing snapshot identity and bytes."""
    snapshot = _snapshot()

    first = build_archive(snapshot)
    second = build_archive(snapshot)

    assert first == second
    assert first == _raw_archive(_entries())
    assert validate_archive(first) == snapshot


def test_team_state_package_exports_archive_and_crypto_boundaries() -> None:
    """Catches callers needing private restore helpers for the planned public APIs."""
    from intent_engineering.team_state.crypto import decrypt_bundle, encrypt_bundle

    assert team_state.build_archive is build_archive
    assert team_state.validate_archive is validate_archive
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
