"""Deterministic, bounded plaintext archives for canonical team state.

The formats are deliberately small and non-extensible within each schema version.
They have no compression, path indirection, ownership, or platform metadata.  Every
integer uses unsigned network byte order and every variable field is preceded by
its exact length.  Regular-file kind, mode, and modification time are fixed so a
snapshot has exactly one byte representation on every platform.
"""

from __future__ import annotations

import hashlib
import struct
from typing import Final

from pydantic import ValidationError

from intent_engineering.team_state.authority import canonical_authority_bytes
from intent_engineering.team_state.models import (
    CANONICAL_STATE_PATHS,
    MAX_AUTHORITY_BYTES,
    MAX_BUNDLE_BYTES,
    MAX_FILE_BYTES,
    MAX_STATE_BYTES,
    MAX_STATE_FILES,
    CanonicalStateFile,
    CanonicalStateSnapshot,
    RestoredSnapshotV2,
    TeamAuthorityRegistryV2,
)

ARCHIVE_MAGIC: Final = b"IEAR\x00\x01\x00\x00"
ARCHIVE_V2_MAGIC: Final = b"IEAR\x00\x02\x00\x00"
AUTHORITY_PATH: Final = "authority/team-authority.json"
CANONICAL_FILE_MODE: Final = 0o600
CANONICAL_MTIME_NS: Final = 0
_REGULAR_FILE: Final = 1
_ENTRY_PREFIX = struct.Struct(">BHHQQ")
_SNAPSHOT_PREFIX = struct.Struct(">QH")
_U16 = struct.Struct(">H")

# Restoration yields the same immutable semantic shape as capture.  Keeping one
# model also prevents archive and publication snapshots from drifting.
RestoredSnapshot = CanonicalStateSnapshot


class TeamStateArchiveError(ValueError):
    """Fixed public failure that retains no hostile archive or model material."""

    def __init__(self) -> None:
        super().__init__("canonical state archive unavailable")


def _safe_failure(caught: BaseException) -> BaseException:
    caught.__traceback__ = None
    caught.__cause__ = None
    caught.__context__ = None
    return caught if not isinstance(caught, Exception) else TeamStateArchiveError()


def _validated_snapshot(snapshot: CanonicalStateSnapshot) -> CanonicalStateSnapshot:
    if not isinstance(snapshot, CanonicalStateSnapshot):
        raise TypeError("snapshot must be a CanonicalStateSnapshot")
    try:
        return CanonicalStateSnapshot.model_validate(snapshot.model_dump(mode="python"))
    except ValidationError as error:
        raise ValueError("invalid canonical state snapshot") from error


def _u16(value: int, label: str) -> bytes:
    try:
        return _U16.pack(value)
    except struct.error as error:
        raise ValueError(f"invalid archive {label}") from error


def build_archive(snapshot: CanonicalStateSnapshot) -> bytes:
    """Serialize one validated snapshot to its sole canonical archive bytes."""
    value = _validated_snapshot(snapshot)
    entries = tuple((item.path, item.content) for item in value.files)
    return _build_archive(value, magic=ARCHIVE_MAGIC, entries=entries)


def build_archive_v2(
    snapshot: CanonicalStateSnapshot,
    authority: TeamAuthorityRegistryV2,
) -> bytes:
    """Serialize state and its exact canonical authority as an archive-v2 payload."""
    failure: BaseException | None = None
    try:
        return _build_archive_v2(snapshot, authority)
    except BaseException as caught:  # noqa: BLE001 - fixed public/cancellation boundary
        failure = _safe_failure(caught)
    finally:
        snapshot = None  # type: ignore[assignment]
        authority = None  # type: ignore[assignment]
    assert failure is not None
    raise failure.with_traceback(None) from None


def _build_archive_v2(
    snapshot: CanonicalStateSnapshot,
    authority: TeamAuthorityRegistryV2,
) -> bytes:
    value = _validated_snapshot(snapshot)
    authority_content = canonical_authority_bytes(authority)
    restored = RestoredSnapshotV2(snapshot=value, authority=authority)
    entries = (
        *((item.path, item.content) for item in value.files),
        (AUTHORITY_PATH, authority_content),
    )
    return _build_archive(restored.snapshot, magic=ARCHIVE_V2_MAGIC, entries=entries)


def _build_archive(
    snapshot: CanonicalStateSnapshot,
    *,
    magic: bytes,
    entries: tuple[tuple[str, bytes], ...],
) -> bytes:
    value = snapshot
    project = value.project_id.encode("utf-8")
    repository = value.repository_id.encode("utf-8")
    try:
        prefix = _SNAPSHOT_PREFIX.pack(value.graph_version, len(entries))
    except struct.error as error:
        raise ValueError("invalid archive graph version") from error

    result = bytearray(magic)
    result.extend(_u16(len(project), "project identity"))
    result.extend(project)
    result.extend(_u16(len(repository), "repository identity"))
    result.extend(repository)
    result.extend(prefix)
    total = 0
    for item_path, item_content in entries:
        path = item_path.encode("utf-8")
        size = len(item_content)
        if item_path == AUTHORITY_PATH:
            if size > MAX_AUTHORITY_BYTES:
                raise ValueError("canonical state archive is oversized")
        else:
            total += size
            if size > MAX_FILE_BYTES or total > MAX_STATE_BYTES:
                raise ValueError("canonical state archive is oversized")
        result.extend(
            _ENTRY_PREFIX.pack(
                _REGULAR_FILE,
                len(path),
                CANONICAL_FILE_MODE,
                CANONICAL_MTIME_NS,
                size,
            )
        )
        result.extend(hashlib.sha256(item_content).digest())
        result.extend(path)
        result.extend(item_content)
        if len(result) > MAX_BUNDLE_BYTES:
            raise ValueError("canonical state archive is oversized")
    return bytes(result)


class _Reader:
    def __init__(self, content: bytes) -> None:
        self._content = memoryview(content)
        self._offset = 0

    def take(self, size: int) -> bytes:
        if size < 0 or size > len(self._content) - self._offset:
            raise ValueError("invalid truncated canonical state archive")
        start = self._offset
        self._offset += size
        return bytes(self._content[start : self._offset])

    @property
    def finished(self) -> bool:
        return self._offset == len(self._content)


def _decode_identity(reader: _Reader, label: str) -> str:
    size = _U16.unpack(reader.take(_U16.size))[0]
    try:
        encoded = reader.take(size)
        value = encoded.decode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"invalid archive {label}") from error
    if not value or value.encode("utf-8") != encoded:
        raise ValueError(f"invalid archive {label}")
    return value


def validate_archive(content: bytes) -> RestoredSnapshot:
    """Validate all framing, inventory, digests, and bounds before restoration."""
    snapshot, _authority = _validate_archive(
        content,
        magic=ARCHIVE_MAGIC,
        expected_paths=CANONICAL_STATE_PATHS,
    )
    if build_archive(snapshot) != content:
        raise ValueError("noncanonical canonical state archive")
    return snapshot


def validate_archive_v2(content: bytes) -> RestoredSnapshotV2:
    """Validate and restore an exact archive-v2 state and authority pair."""
    failure: BaseException | None = None
    try:
        return _validate_archive_v2(content)
    except BaseException as caught:  # noqa: BLE001 - fixed public/cancellation boundary
        failure = _safe_failure(caught)
    finally:
        content = b""
    assert failure is not None
    raise failure.with_traceback(None) from None


def _validate_archive_v2(content: bytes) -> RestoredSnapshotV2:
    snapshot, authority_content = _validate_archive(
        content,
        magic=ARCHIVE_V2_MAGIC,
        expected_paths=(*CANONICAL_STATE_PATHS, AUTHORITY_PATH),
    )
    if authority_content is None:
        raise ValueError("invalid canonical state archive inventory")
    try:
        authority = TeamAuthorityRegistryV2.model_validate_json(authority_content)
        restored = RestoredSnapshotV2(snapshot=snapshot, authority=authority)
    except (ValidationError, ValueError) as error:
        raise ValueError("invalid canonical team authority") from error
    if _build_archive_v2(restored.snapshot, restored.authority) != content:
        raise ValueError("noncanonical canonical state archive")
    return restored


def validate_versioned_archive(content: bytes) -> RestoredSnapshot | RestoredSnapshotV2:
    """Dispatch one bounded archive by its exact public schema magic."""
    failure: BaseException | None = None
    try:
        if type(content) is not bytes:
            raise ValueError("invalid canonical state archive")
        magic = content[: len(ARCHIVE_MAGIC)]
        if magic == ARCHIVE_MAGIC:
            return validate_archive(content)
        if magic == ARCHIVE_V2_MAGIC:
            return _validate_archive_v2(content)
        raise ValueError("invalid canonical state archive magic")
    except BaseException as caught:  # noqa: BLE001 - fixed public/cancellation boundary
        failure = _safe_failure(caught)
    finally:
        content = b""
    assert failure is not None
    raise failure.with_traceback(None) from None


def _validate_archive(
    content: bytes,
    *,
    magic: bytes,
    expected_paths: tuple[str, ...],
) -> tuple[CanonicalStateSnapshot, bytes | None]:
    if type(content) is not bytes or not content or len(content) > MAX_BUNDLE_BYTES:
        raise ValueError("invalid canonical state archive")
    reader = _Reader(content)
    if reader.take(len(magic)) != magic:
        raise ValueError("invalid canonical state archive magic")
    project_id = _decode_identity(reader, "project identity")
    repository_id = _decode_identity(reader, "repository identity")
    graph_version, count = _SNAPSHOT_PREFIX.unpack(reader.take(_SNAPSHOT_PREFIX.size))
    if count != len(expected_paths) or count > MAX_STATE_FILES:
        raise ValueError("invalid canonical state archive inventory")

    files: list[CanonicalStateFile] = []
    authority_content: bytes | None = None
    total = 0
    for expected_path in expected_paths:
        kind, path_size, mode, mtime_ns, size = _ENTRY_PREFIX.unpack(
            reader.take(_ENTRY_PREFIX.size)
        )
        digest = reader.take(32)
        if kind != _REGULAR_FILE or mode != CANONICAL_FILE_MODE or mtime_ns != 0:
            raise ValueError("invalid canonical state archive metadata")
        if expected_path == AUTHORITY_PATH:
            if size > MAX_AUTHORITY_BYTES:
                raise ValueError("canonical state archive is oversized")
        else:
            if size > MAX_FILE_BYTES:
                raise ValueError("canonical state archive is oversized")
            total += size
            if total > MAX_STATE_BYTES:
                raise ValueError("canonical state archive is oversized")
        try:
            path_bytes = reader.take(path_size)
            path = path_bytes.decode("utf-8")
        except UnicodeError as error:
            raise ValueError("invalid canonical state archive path") from error
        if path != expected_path or path.encode("utf-8") != path_bytes:
            raise ValueError("invalid canonical state archive path or order")
        file_content = reader.take(size)
        if hashlib.sha256(file_content).digest() != digest:
            raise ValueError("invalid canonical state archive digest")
        if expected_path == AUTHORITY_PATH:
            authority_content = file_content
        else:
            try:
                files.append(CanonicalStateFile(path=path, content=file_content))
            except ValidationError as error:
                raise ValueError("invalid canonical state archive entry") from error

    if not reader.finished:
        raise ValueError("invalid trailing canonical state archive bytes")
    try:
        snapshot = CanonicalStateSnapshot(
            project_id=project_id,
            repository_id=repository_id,
            graph_version=graph_version,
            files=tuple(files),
        )
    except ValidationError as error:
        raise ValueError("invalid canonical state archive snapshot") from error
    return snapshot, authority_content


__all__ = [
    "ARCHIVE_MAGIC",
    "ARCHIVE_V2_MAGIC",
    "AUTHORITY_PATH",
    "CANONICAL_FILE_MODE",
    "CANONICAL_MTIME_NS",
    "RestoredSnapshot",
    "TeamStateArchiveError",
    "build_archive",
    "build_archive_v2",
    "validate_archive",
    "validate_archive_v2",
    "validate_versioned_archive",
]
