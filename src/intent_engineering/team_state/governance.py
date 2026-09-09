"""Durable owner-only evidence that a local checkout entered team governance."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator

from intent_engineering.control_plane import local_repository_identity
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage._atomic import same_path_lock
from intent_engineering.storage.secure import SecureDirectory, SecureFile, UnsafePathError

_REGISTRY_NAME = "governance-v1.json"
_MAX_REGISTRY_BYTES = 128 * 1024
_MAX_RECORDS = 1024
_MAX_CHECKOUTS = 64
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPOSITORY_ID = re.compile(r"^[a-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOCAL_REPOSITORY_ID = re.compile(r"^repo:sha256:[0-9a-f]{64}$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class _RegistryModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class GovernanceRecord(_RegistryModel):
    """Non-secret authenticated state metadata for one canonical repository."""

    schema_version: Literal[1] = 1
    repository_id: str
    project_id: str
    checkout_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=_MAX_CHECKOUTS)]
    bundle_digest: str
    graph_version: Annotated[int, Field(ge=1)]
    ref_commit: str

    @field_validator("schema_version", "graph_version", mode="before")
    @classmethod
    def require_integers(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid governance integer")
        return value

    @field_validator("repository_id")
    @classmethod
    def require_repository_id(cls, value: str) -> str:
        if _REPOSITORY_ID.fullmatch(value) is None:
            raise ValueError("invalid governance repository")
        return value

    @field_validator("project_id")
    @classmethod
    def require_project_id(cls, value: str) -> str:
        if _PROJECT_ID.fullmatch(value) is None:
            raise ValueError("invalid governance project")
        return value

    @field_validator("checkout_ids", mode="before")
    @classmethod
    def require_json_checkout_list(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        if type(value) is not tuple:
            raise ValueError("invalid governance checkouts")
        return value

    @field_validator("checkout_ids")
    @classmethod
    def require_checkout_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _LOCAL_REPOSITORY_ID.fullmatch(item) is None for item in value
        ):
            raise ValueError("invalid governance checkouts")
        return value

    @field_validator("bundle_digest")
    @classmethod
    def require_bundle_digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("invalid governance digest")
        return value

    @field_validator("ref_commit")
    @classmethod
    def require_ref_commit(cls, value: str) -> str:
        if _GIT_COMMIT.fullmatch(value) is None:
            raise ValueError("invalid governance commit")
        return value

    def marker(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "bundle_digest": self.bundle_digest,
            "graph_version": self.graph_version,
            "ref_commit": self.ref_commit,
        }


class _GovernanceRegistryDocument(_RegistryModel):
    schema_version: Literal[1] = 1
    records: Annotated[tuple[GovernanceRecord, ...], Field(max_length=_MAX_RECORDS)] = ()

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_schema_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid governance schema")
        return value

    @field_validator("records", mode="before")
    @classmethod
    def require_json_record_list(cls, value: object) -> object:
        if type(value) is list:
            return tuple(value)
        if type(value) is not tuple:
            raise ValueError("invalid governance records")
        return value

    @field_validator("records")
    @classmethod
    def require_sorted_records(
        cls, value: tuple[GovernanceRecord, ...]
    ) -> tuple[GovernanceRecord, ...]:
        identities = tuple(item.repository_id for item in value)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("invalid governance records")
        return value

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class GovernanceLookup:
    """A matching durable record, if this exact checkout was governed."""

    record: GovernanceRecord | None


def default_governance_registry_root() -> Path:
    """Return a fixed account-owned state path without consulting HOME or XDG."""
    if os.name != "posix" or not hasattr(os, "getuid"):
        raise UnsafePathError()
    import pwd

    account = pwd.getpwuid(os.getuid())
    if not account.pw_dir or not os.path.isabs(account.pw_dir):
        raise UnsafePathError()
    return Path(account.pw_dir) / ".intent-engineering"


def _require_owner_only(metadata: os.stat_result, *, directory: bool) -> None:
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    expected_mode = 0o700 if directory else 0o600
    if (
        not expected_kind(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != expected_mode
        or (not directory and metadata.st_nlink != 1)
    ):
        raise UnsafePathError()


def _open_registry_root(path: Path, *, create: bool) -> SecureDirectory | None:
    absolute = Path(os.path.abspath(path))
    parent = SecureDirectory.open(absolute.parent)
    try:
        if create:
            try:
                os.mkdir(absolute.name, 0o700, dir_fd=parent.descriptor)
                os.fsync(parent.descriptor)
            except FileExistsError:
                pass
        else:
            try:
                os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return None
        try:
            root = parent.subdirectory(absolute.name)
        except FileNotFoundError:
            raise UnsafePathError() from None
        try:
            _require_owner_only(os.fstat(root.descriptor), directory=True)
        except BaseException:
            root.close()
            raise
        return root
    finally:
        parent.close()


def _require_owner_file(directory: SecureDirectory, name: str) -> os.stat_result:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=directory.descriptor,
        )
        metadata = os.fstat(descriptor)
        _require_owner_only(metadata, directory=False)
        return metadata
    except OSError as error:
        raise UnsafePathError() from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_document(directory: SecureDirectory, target: SecureFile) -> _GovernanceRegistryDocument:
    raw = target.read_optional_nonblocking(max_bytes=_MAX_REGISTRY_BYTES)
    if raw is None:
        return _GovernanceRegistryDocument()
    _require_owner_file(directory, _REGISTRY_NAME)
    document = _GovernanceRegistryDocument.model_validate_json(raw)
    if raw != document.canonical_bytes():
        raise UnsafePathError()
    return document


def _atomic_owner_write(directory: SecureDirectory, content: bytes) -> None:
    if len(content) > _MAX_REGISTRY_BYTES:
        raise UnsafePathError()
    temporary = ""
    descriptor = -1
    identity: tuple[int, int] | None = None
    installed = False
    view = memoryview(content)
    try:
        for _attempt in range(32):
            temporary = f".{_REGISTRY_NAME}.{secrets.token_hex(16)}.tmp"
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                    0o600,
                    dir_fd=directory.descriptor,
                )
            except FileExistsError:
                continue
            break
        if descriptor < 0:
            raise UnsafePathError()
        metadata = os.fstat(descriptor)
        _require_owner_only(metadata, directory=False)
        identity = (metadata.st_dev, metadata.st_ino)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise UnsafePathError()
            offset += written
        os.fsync(descriptor)
        current = os.fstat(descriptor)
        _require_owner_only(current, directory=False)
        if (current.st_dev, current.st_ino) != identity or current.st_size != len(content):
            raise UnsafePathError()
        try:
            _require_owner_file(directory, _REGISTRY_NAME)
        except UnsafePathError:
            try:
                os.stat(_REGISTRY_NAME, dir_fd=directory.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise
        os.replace(
            temporary,
            _REGISTRY_NAME,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
        )
        temporary = ""
        installed = True
        os.fsync(directory.descriptor)
        final = _require_owner_file(directory, _REGISTRY_NAME)
        if (final.st_dev, final.st_ino) != identity or final.st_size != len(content):
            raise UnsafePathError()
    finally:
        view.release()
        if descriptor >= 0:
            os.close(descriptor)
        if temporary and identity is not None:
            try:
                leftover = os.stat(temporary, dir_fd=directory.descriptor, follow_symlinks=False)
                if (leftover.st_dev, leftover.st_ino) == identity:
                    os.unlink(temporary, dir_fd=directory.descriptor)
                    os.fsync(directory.descriptor)
            except (FileNotFoundError, OSError):
                pass
        if installed:
            content = b""


class GovernanceRegistry:
    """Locked canonical store for durable non-secret team-governance evidence."""

    def __init__(self, root: Path) -> None:
        self._root = Path(os.path.abspath(root))

    @contextmanager
    def activation_target(self, *, recovering: bool = False) -> Iterator[SecureFile]:
        """Hold the public registry target for the restore-owned cross-state journal."""
        directory = _open_registry_root(self._root, create=True)
        if directory is None:
            raise UnsafePathError()
        target = directory.file(_REGISTRY_NAME)
        try:
            with same_path_lock(target):
                if not recovering:
                    _read_document(directory, target)
                yield target
        finally:
            target.close()
            directory.close()

    @staticmethod
    def activation_content(content: bytes | None, record: GovernanceRecord) -> bytes:
        """Preserve other repositories and checkout identities in an exact transaction."""
        from intent_engineering.storage.jsonl.strict import loads_strict_object

        if content is not None:
            if len(content) > _MAX_REGISTRY_BYTES:
                raise UnsafePathError()
            loads_strict_object(content.decode("utf-8"))
            document = _GovernanceRegistryDocument.model_validate_json(content)
            if document.canonical_bytes() != content:
                raise UnsafePathError()
        else:
            document = _GovernanceRegistryDocument()
        records = {item.repository_id: item for item in document.records}
        previous = records.get(record.repository_id)
        if previous is not None:
            if previous.project_id != record.project_id:
                raise UnsafePathError()
            record = record.model_copy(
                update={
                    "checkout_ids": tuple(sorted(set(previous.checkout_ids + record.checkout_ids)))
                }
            )
        records[record.repository_id] = record
        result = _GovernanceRegistryDocument(records=tuple(records[k] for k in sorted(records)))
        if len(result.canonical_bytes()) > _MAX_REGISTRY_BYTES:
            raise UnsafePathError()
        return result.canonical_bytes()

    def lookup(
        self, repository_id: str | None, directory_identity: tuple[int, int]
    ) -> GovernanceLookup:
        directory = _open_registry_root(self._root, create=False)
        if directory is None:
            return GovernanceLookup(None)
        target = directory.file(_REGISTRY_NAME)
        try:
            try:
                _require_owner_file(directory, _REGISTRY_NAME)
            except UnsafePathError:
                try:
                    os.stat(_REGISTRY_NAME, dir_fd=directory.descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return GovernanceLookup(None)
                raise
            with same_path_lock(target):
                _require_owner_file(directory, f".{_REGISTRY_NAME}.lock")
                document = _read_document(directory, target)
            for record in document.records:
                checkout_id = local_repository_identity(record.project_id, directory_identity)
                if repository_id == record.repository_id or checkout_id in record.checkout_ids:
                    return GovernanceLookup(record)
            return GovernanceLookup(None)
        finally:
            target.close()
            directory.close()

    def remember(
        self,
        *,
        repository_id: str,
        project_id: str,
        directory_identity: tuple[int, int],
        marker: dict[str, object],
    ) -> GovernanceRecord:
        checkout_id = local_repository_identity(project_id, directory_identity)
        bundle_digest = marker.get("bundle_digest")
        graph_version = marker.get("graph_version")
        ref_commit = marker.get("ref_commit")
        if (
            type(bundle_digest) is not str
            or type(graph_version) is not int
            or type(ref_commit) is not str
        ):
            raise UnsafePathError()
        candidate = GovernanceRecord(
            repository_id=repository_id,
            project_id=project_id,
            checkout_ids=(checkout_id,),
            bundle_digest=bundle_digest,
            graph_version=graph_version,
            ref_commit=ref_commit,
        )
        directory = _open_registry_root(self._root, create=True)
        if directory is None:
            raise UnsafePathError()
        target = directory.file(_REGISTRY_NAME)
        try:
            with same_path_lock(target):
                _require_owner_file(directory, f".{_REGISTRY_NAME}.lock")
                document = _read_document(directory, target)
                records = {item.repository_id: item for item in document.records}
                current = records.get(repository_id)
                if current is not None:
                    if current.project_id != project_id:
                        raise UnsafePathError()
                    candidate = candidate.model_copy(
                        update={"checkout_ids": tuple(sorted({*current.checkout_ids, checkout_id}))}
                    )
                records[repository_id] = candidate
                updated = _GovernanceRegistryDocument(
                    records=tuple(records[key] for key in sorted(records))
                )
                _atomic_owner_write(directory, updated.canonical_bytes())
            return candidate
        finally:
            target.close()
            directory.close()
