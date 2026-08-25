"""Descriptor-rooted local filesystem access for canonical project state."""

from __future__ import annotations

import ctypes
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_RENAME_EXCL = 4

type FileIdentity = tuple[int, int]


class UnsafePathError(ValueError):
    """Raised when local canonical I/O cannot prove descriptor containment."""

    def __init__(self, message: str = "unsafe canonical path") -> None:
        super().__init__(message)


@dataclass(frozen=True)
class SecureRead:
    """Bytes and descriptor identities observed in one no-follow read."""

    content: bytes
    modified_ns: int
    identities: tuple[FileIdentity, ...]


def _identity(metadata: os.stat_result) -> FileIdentity:
    return metadata.st_dev, metadata.st_ino


def _relative_parts(relative: str | PurePosixPath | Path) -> tuple[str, ...]:
    value = PurePosixPath(str(relative))
    parts = value.parts
    if (
        value.is_absolute()
        or not parts
        or any(part in {"", ".", ".."} or "\x00" in part for part in parts)
    ):
        raise UnsafePathError()
    return parts


def _require_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise UnsafePathError()


def _require_regular(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UnsafePathError()


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 65536):
        chunks.append(chunk)
    return b"".join(chunks)


def _read_named(parent_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise UnsafePathError() from error
    try:
        metadata = os.fstat(descriptor)
        _require_regular(metadata)
        return _read_descriptor(descriptor), metadata
    finally:
        os.close(descriptor)


def _rename_with_flags(
    parent_fd: int,
    left: str,
    right: str,
    *,
    linux_flags: int,
    darwin_flags: int,
) -> None:
    """Invoke one descriptor-rooted rename primitive or fail closed."""
    library = ctypes.CDLL(None, use_errno=True)
    try:
        function = library.renameat2
        flags = linux_flags
    except AttributeError:
        try:
            function = library.renameatx_np
            flags = darwin_flags
        except AttributeError as error:
            raise UnsafePathError() from error
    function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    result = function(
        parent_fd,
        os.fsencode(left),
        parent_fd,
        os.fsencode(right),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _exchange_names(parent_fd: int, left: str, right: str) -> None:
    """Atomically exchange two entries or fail closed when the platform cannot."""
    _rename_with_flags(
        parent_fd,
        left,
        right,
        linux_flags=_RENAME_EXCHANGE,
        darwin_flags=_RENAME_EXCHANGE,
    )


def _rename_exclusive(parent_fd: int, source: str, target: str) -> None:
    """Atomically rename only when the destination remains absent."""
    _rename_with_flags(
        parent_fd,
        source,
        target,
        linux_flags=_RENAME_NOREPLACE,
        darwin_flags=_RENAME_EXCL,
    )


class SecureDirectory:
    """A held directory descriptor whose path components were opened without following links."""

    def __init__(self, descriptor: int, path: Path) -> None:
        self._descriptor = descriptor
        self.path = path

    @classmethod
    def open(cls, path: Path, *, create: bool = False) -> SecureDirectory:
        """Traverse an absolute lexical path one no-follow directory descriptor at a time."""
        absolute = Path(os.path.abspath(path))
        anchor = absolute.anchor
        if not anchor:
            raise UnsafePathError()
        try:
            descriptor = os.open(anchor, _DIRECTORY_FLAGS)
        except OSError as error:
            raise UnsafePathError() from error
        try:
            for component in absolute.parts[1:]:
                if component in {"", ".", ".."} or "\x00" in component:
                    raise UnsafePathError()
                try:
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                _require_directory(os.fstat(next_descriptor))
                os.close(descriptor)
                descriptor = next_descriptor
        except BaseException as error:
            os.close(descriptor)
            if isinstance(error, UnsafePathError):
                raise
            raise UnsafePathError() from error
        return cls(descriptor, absolute)

    @property
    def descriptor(self) -> int:
        """Return the held descriptor for descriptor-relative operations in this package."""
        if self._descriptor < 0:
            raise UnsafePathError()
        return self._descriptor

    @property
    def identity(self) -> FileIdentity:
        return _identity(os.fstat(self.descriptor))

    def duplicate(self) -> SecureDirectory:
        return SecureDirectory(os.dup(self.descriptor), self.path)

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def __del__(self) -> None:  # pragma: no cover - deterministic owners close at process teardown
        try:
            self.close()
        except OSError:
            pass

    def subdirectory(
        self,
        relative: str | PurePosixPath | Path,
        *,
        create: bool = False,
    ) -> SecureDirectory:
        parts = _relative_parts(relative)
        descriptor = os.dup(self.descriptor)
        traversed = self.path
        try:
            for component in parts:
                try:
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                _require_directory(os.fstat(next_descriptor))
                os.close(descriptor)
                descriptor = next_descriptor
                traversed /= component
        except BaseException as error:
            os.close(descriptor)
            if isinstance(error, UnsafePathError):
                raise
            raise UnsafePathError() from error
        return SecureDirectory(descriptor, traversed)

    def file(
        self,
        relative: str | PurePosixPath | Path,
        *,
        create_parents: bool = False,
    ) -> SecureFile:
        parts = _relative_parts(relative)
        parent = self
        owned_parent: SecureDirectory | None = None
        if len(parts) > 1:
            owned_parent = self.subdirectory(
                PurePosixPath(*parts[:-1]),
                create=create_parents,
            )
            parent = owned_parent
        try:
            return SecureFile(os.dup(parent.descriptor), parts[-1], self.path.joinpath(*parts))
        finally:
            if owned_parent is not None:
                owned_parent.close()

    def read_relative(
        self,
        relative: str | PurePosixPath | Path,
        *,
        expected_identities: tuple[FileIdentity, ...] | None = None,
    ) -> SecureRead:
        """Read a descendant regular file and optionally pin every ancestor identity."""
        parts = _relative_parts(relative)
        descriptor = os.dup(self.descriptor)
        identities: list[FileIdentity] = [self.identity]
        try:
            for component in parts[:-1]:
                try:
                    next_descriptor = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=descriptor,
                    )
                except OSError as error:
                    raise UnsafePathError() from error
                metadata = os.fstat(next_descriptor)
                _require_directory(metadata)
                identities.append(_identity(metadata))
                os.close(descriptor)
                descriptor = next_descriptor
            content, metadata = _read_named(descriptor, parts[-1])
            identities.append(_identity(metadata))
        finally:
            os.close(descriptor)
        result = SecureRead(content, metadata.st_mtime_ns, tuple(identities))
        if expected_identities is not None and result.identities != expected_identities:
            raise UnsafePathError()
        return result

    def final_is_symlink(self, relative: str | PurePosixPath | Path) -> bool:
        """Inspect only the final entry beneath no-follow parent descriptors."""
        parts = _relative_parts(relative)
        descriptor = os.dup(self.descriptor)
        try:
            for component in parts[:-1]:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            metadata = os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
            return stat.S_ISLNK(metadata.st_mode)
        except OSError:
            return False
        finally:
            os.close(descriptor)

    def walk_regular_files(
        self,
        suffix: str,
        *,
        excluded: Callable[[PurePosixPath], bool] | None = None,
    ) -> tuple[tuple[PurePosixPath, SecureRead], ...]:
        """Return a stable no-follow recursive snapshot of regular single-link files."""
        results: list[tuple[PurePosixPath, SecureRead]] = []

        def walk(
            directory_fd: int,
            prefix: tuple[str, ...],
            directory_identities: tuple[FileIdentity, ...],
        ) -> None:
            try:
                names = sorted(os.listdir(directory_fd))
            except OSError as error:
                raise UnsafePathError() from error
            for name in names:
                if name in {"", ".", ".."} or "\x00" in name:
                    raise UnsafePathError()
                relative = PurePosixPath(*prefix, name)
                try:
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as error:
                    raise UnsafePathError() from error
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    try:
                        child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                    except OSError as error:
                        raise UnsafePathError() from error
                    try:
                        child_metadata = os.fstat(child_fd)
                        _require_directory(child_metadata)
                        if _identity(child_metadata) != _identity(metadata):
                            raise UnsafePathError()
                        walk(
                            child_fd,
                            (*prefix, name),
                            (*directory_identities, _identity(child_metadata)),
                        )
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(metadata.st_mode) or not name.endswith(suffix):
                    continue
                if excluded is not None and excluded(relative):
                    continue
                content, opened_metadata = _read_named(directory_fd, name)
                if _identity(opened_metadata) != _identity(metadata):
                    raise UnsafePathError()
                results.append(
                    (
                        relative,
                        SecureRead(
                            content,
                            opened_metadata.st_mtime_ns,
                            (*directory_identities, _identity(opened_metadata)),
                        ),
                    )
                )

        root_identity = self.identity
        walk(self.descriptor, (), (root_identity,))
        return tuple(results)


class SecureFile:
    """One final component addressed relative to a held parent directory descriptor."""

    def __init__(self, parent_fd: int, name: str, path: Path) -> None:
        if name in {"", ".", ".."} or "/" in name or "\x00" in name:
            os.close(parent_fd)
            raise UnsafePathError()
        self._parent_fd = parent_fd
        self.name = name
        self.path = path

    @classmethod
    def from_path(cls, path: Path, *, create_parents: bool = True) -> SecureFile:
        absolute = Path(os.path.abspath(path))
        parent = SecureDirectory.open(absolute.parent, create=create_parents)
        try:
            return cls(os.dup(parent.descriptor), absolute.name, absolute)
        finally:
            parent.close()

    @property
    def parent_fd(self) -> int:
        if self._parent_fd < 0:
            raise UnsafePathError()
        return self._parent_fd

    @property
    def lock_key(self) -> tuple[int, int, str]:
        metadata = os.fstat(self.parent_fd)
        return metadata.st_dev, metadata.st_ino, self.name

    def duplicate(self) -> SecureFile:
        return SecureFile(os.dup(self.parent_fd), self.name, self.path)

    def sibling(self, name: str) -> SecureFile:
        return SecureFile(os.dup(self.parent_fd), name, self.path.with_name(name))

    def close(self) -> None:
        if self._parent_fd >= 0:
            os.close(self._parent_fd)
            self._parent_fd = -1

    def __del__(self) -> None:  # pragma: no cover - deterministic owners close at process teardown
        try:
            self.close()
        except OSError:
            pass

    def exists(self) -> bool:
        try:
            metadata = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise UnsafePathError() from error
        _require_regular(metadata)
        return True

    def assert_regular(self) -> None:
        descriptor = -1
        try:
            descriptor = os.open(self.name, _READ_FLAGS, dir_fd=self.parent_fd)
            _require_regular(os.fstat(descriptor))
        except OSError as error:
            raise UnsafePathError() from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def read_bytes(self) -> bytes:
        return _read_named(self.parent_fd, self.name)[0]

    def read_optional(self) -> bytes | None:
        try:
            return self.read_bytes()
        except UnsafePathError:
            try:
                os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            raise

    def _assert_safe_existing_target(self) -> os.stat_result | None:
        try:
            metadata = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise UnsafePathError() from error
        _require_regular(metadata)
        return metadata

    def _install_verified_temporary(
        self,
        temporary_name: str,
        temporary_metadata: os.stat_result,
        expected_target: os.stat_result | None,
    ) -> None:
        """Install a prepared file only if the inspected target did not change."""
        if expected_target is None:
            _rename_exclusive(self.parent_fd, temporary_name, self.name)
            installed = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            installed_is_safe = (
                stat.S_ISREG(installed.st_mode)
                and installed.st_nlink == 1
                and _identity(installed) == _identity(temporary_metadata)
            )
            if not installed_is_safe:
                try:
                    os.unlink(self.name, dir_fd=self.parent_fd)
                except OSError as error:
                    raise UnsafePathError() from error
                raise UnsafePathError()
            os.fsync(self.parent_fd)
            return

        _exchange_names(self.parent_fd, temporary_name, self.name)
        try:
            displaced = os.stat(
                temporary_name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            installed = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
            unchanged = (
                stat.S_ISREG(displaced.st_mode)
                and displaced.st_nlink == 1
                and _identity(displaced) == _identity(expected_target)
                and stat.S_ISREG(installed.st_mode)
                and installed.st_nlink == 1
                and _identity(installed) == _identity(temporary_metadata)
            )
        except OSError:
            unchanged = False
        if not unchanged:
            try:
                _exchange_names(self.parent_fd, temporary_name, self.name)
            except OSError as error:
                raise UnsafePathError() from error
            raise UnsafePathError()
        os.unlink(temporary_name, dir_fd=self.parent_fd)
        os.fsync(self.parent_fd)

    def atomic_write(self, content: bytes, *, reject_target_races: bool = False) -> None:
        temporary_name: str | None = None
        descriptor = -1
        temporary_metadata: os.stat_result | None = None
        try:
            for _ in range(32):
                candidate = f".{self.name}.{secrets.token_hex(16)}.tmp"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                        0o644,
                        dir_fd=self.parent_fd,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_name is None:
                raise UnsafePathError()
            temporary_metadata = os.fstat(descriptor)
            _require_regular(temporary_metadata)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            expected_target = self._assert_safe_existing_target()
            if reject_target_races:
                self._install_verified_temporary(
                    temporary_name,
                    temporary_metadata,
                    expected_target,
                )
            else:
                os.replace(
                    temporary_name,
                    self.name,
                    src_dir_fd=self.parent_fd,
                    dst_dir_fd=self.parent_fd,
                )
            temporary_name = None
            os.fsync(self.parent_fd)
        except OSError as error:
            raise UnsafePathError() from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=self.parent_fd)
                except FileNotFoundError:
                    pass

    def append(self, content: bytes) -> None:
        try:
            descriptor = os.open(
                self.name,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _CLOEXEC,
                0o644,
                dir_fd=self.parent_fd,
            )
        except OSError as error:
            raise UnsafePathError() from error
        try:
            _require_regular(os.fstat(descriptor))
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def unlink(self, *, missing_ok: bool = False) -> None:
        try:
            self._assert_safe_existing_target()
            os.unlink(self.name, dir_fd=self.parent_fd)
            os.fsync(self.parent_fd)
        except FileNotFoundError:
            if not missing_ok:
                raise
        except OSError as error:
            raise UnsafePathError() from error

    def open_lock(self) -> int:
        lock_name = f".{self.name}.lock"
        try:
            descriptor = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=self.parent_fd,
            )
        except OSError as error:
            raise UnsafePathError() from error
        try:
            _require_regular(os.fstat(descriptor))
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor


def coerce_secure_file(path: Path | SecureFile) -> SecureFile:
    """Give one store its own descriptor while preserving an already-held root."""
    return path.duplicate() if isinstance(path, SecureFile) else SecureFile.from_path(path)


def configured_graph_relative(graph_path: str) -> PurePosixPath:
    """Normalize a configured relative graph path beneath the held ``.intent`` root."""
    try:
        parts = list(_relative_parts(graph_path))
    except UnsafePathError as error:
        raise UnsafePathError("configured graph path is unsafe") from error
    if parts and parts[0] == ".intent":
        parts.pop(0)
    if not parts:
        raise UnsafePathError("configured graph path is unsafe")
    return PurePosixPath(*parts)
