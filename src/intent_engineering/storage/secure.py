"""Descriptor-rooted local filesystem access for canonical project state."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_RENAME_EXCL = 4
_ROLLBACK_ATTEMPTS = 32

type FileIdentity = tuple[int, int]


class UnsafePathError(ValueError):
    """Raised when local canonical I/O cannot prove descriptor containment."""

    def __init__(self, message: str = "unsafe canonical path") -> None:
        super().__init__(message)


class AtomicWriteRollbackError(UnsafePathError):
    """Raised when strict report output cannot authenticate its final state."""

    def __init__(self) -> None:
        super().__init__("atomic write state is indeterminate")


@dataclass
class _StrictWriteState:
    committed: bool = False
    cleaned: bool = False
    indeterminate: bool = False
    displaced_touched: bool = False
    displaced_scrubbed: bool = False


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


def _read_named_nonblocking(parent_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    """Authenticate the final descriptor without blocking on a FIFO before its kind is known."""
    try:
        descriptor = os.open(name, _READ_FLAGS | _NONBLOCK, dir_fd=parent_fd)
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

    def read_bytes_nonblocking(self) -> bytes:
        """Read one regular file after a nonblocking descriptor-kind authentication."""
        return _read_named_nonblocking(self.parent_fd, self.name)[0]

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

    def _entry_matches(self, name: str, expected: os.stat_result) -> bool:
        return self._matching_entry(name, expected) is not None

    def _matching_entry(
        self,
        name: str,
        expected: os.stat_result,
    ) -> os.stat_result | None:
        try:
            observed = os.stat(name, dir_fd=self.parent_fd, follow_symlinks=False)
        except OSError:
            return None
        return observed if _identity(observed) == _identity(expected) else None

    def _entry_is_safe(self, name: str, expected: os.stat_result) -> bool:
        observed = self._matching_entry(name, expected)
        return bool(
            observed is not None and stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1
        )

    def _strict_fault(self, stage: str) -> None:
        """Named test boundary for cancellation between strict transaction phases."""

    @staticmethod
    def _scrub_descriptor(
        descriptor: int,
        expected: os.stat_result,
        *,
        single_link: bool,
    ) -> bool:
        try:
            observed = os.fstat(descriptor)
            if (
                _identity(observed) != _identity(expected)
                or not stat.S_ISREG(observed.st_mode)
                or (single_link and observed.st_nlink != 1)
            ):
                return False
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
            scrubbed = os.fstat(descriptor)
        except BaseException:  # noqa: BLE001 - fixed indeterminate outcome at caller
            return False
        return bool(
            _identity(scrubbed) == _identity(expected)
            and stat.S_ISREG(scrubbed.st_mode)
            and scrubbed.st_size == 0
            and (not single_link or scrubbed.st_nlink == 1)
        )

    @staticmethod
    def _close_descriptor(descriptor: int) -> bool:
        for _ in range(_ROLLBACK_ATTEMPTS):
            try:
                os.close(descriptor)
                return True
            except OSError as error:
                try:
                    os.fstat(descriptor)
                except OSError as observed:
                    if observed.errno == errno.EBADF:
                        return True
                if error.errno == errno.EBADF:
                    return True
        return False

    def _terminal_report_snapshot(
        self,
        report: os.stat_result,
        report_descriptor: int,
    ) -> bool:
        """Authenticate the published report twice with no intervening mutation."""
        for _ in range(2):
            try:
                held = os.fstat(report_descriptor)
            except OSError:
                return False
            if (
                not self._entry_is_safe(self.name, report)
                or _identity(held) != _identity(report)
                or not stat.S_ISREG(held.st_mode)
                or held.st_nlink != 1
                or held.st_size != report.st_size
            ):
                return False
        return True

    def _terminal_existing_snapshot(
        self,
        quarantine: str,
        original: os.stat_result,
        displaced_descriptor: int,
        report: os.stat_result,
        report_descriptor: int,
    ) -> bool:
        """Authenticate the report and durable zero tombstone as one terminal bundle."""
        for _ in range(2):
            tombstone = self._matching_entry(quarantine, original)
            try:
                displaced = os.fstat(displaced_descriptor)
            except OSError:
                return False
            if (
                tombstone is None
                or not stat.S_ISREG(tombstone.st_mode)
                or tombstone.st_nlink != 1
                or tombstone.st_size != 0
                or _identity(displaced) != _identity(original)
                or displaced.st_nlink != 1
                or displaced.st_size != 0
                or not self._terminal_report_snapshot(report, report_descriptor)
            ):
                return False
        return True

    def _terminal_rollback_snapshot(
        self,
        original: os.stat_result | None,
        displaced_descriptor: int,
        report: os.stat_result,
        report_descriptor: int,
    ) -> bool:
        """Authenticate the exact preimage and the scrubbed report after rollback."""
        for _ in range(2):
            try:
                os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
                target_absent = False
            except FileNotFoundError:
                target_absent = True
            except OSError:
                return False
            try:
                held_report = os.fstat(report_descriptor)
            except OSError:
                return False
            report_is_zero = bool(
                _identity(held_report) == _identity(report)
                and stat.S_ISREG(held_report.st_mode)
                and held_report.st_nlink <= 1
                and held_report.st_size == 0
            )
            if original is None:
                if not target_absent or not report_is_zero:
                    return False
                continue
            try:
                held_original = os.fstat(displaced_descriptor)
            except OSError:
                return False
            if (
                target_absent
                or not self._entry_is_safe(self.name, original)
                or _identity(held_original) != _identity(original)
                or held_original.st_nlink != 1
                or held_original.st_size != original.st_size
                or not report_is_zero
            ):
                return False
        return True

    def _quarantine_owned_entry(
        self,
        name: str,
        expected: os.stat_result,
        descriptor: int,
    ) -> bool:
        """Scrub an owned inode, then retain it under a private zero-byte name."""
        if not self._scrub_descriptor(descriptor, expected, single_link=False):
            return False
        for _ in range(_ROLLBACK_ATTEMPTS):
            try:
                live = os.stat(name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return True
            except OSError:
                return False
            if _identity(live) != _identity(expected):
                return False
            quarantine = f".{self.name}.{secrets.token_hex(16)}.rollback"
            try:
                _rename_exclusive(self.parent_fd, name, quarantine)
            except FileExistsError:
                continue
            except OSError:
                return False
            quarantined = self._matching_entry(quarantine, expected)
            if quarantined is None:
                try:
                    os.stat(
                        quarantine,
                        dir_fd=self.parent_fd,
                        follow_symlinks=False,
                    )
                    os.stat(name, dir_fd=self.parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        _rename_exclusive(self.parent_fd, quarantine, name)
                    except OSError:
                        pass
                    return False
                except OSError:
                    return False
                return False
            try:
                remaining = os.stat(name, dir_fd=self.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                remaining = None
            except OSError:
                return False
            if remaining is not None:
                return False
            retained = self._matching_entry(quarantine, expected)
            return bool(
                retained is not None
                and stat.S_ISREG(retained.st_mode)
                and retained.st_nlink == 1
                and retained.st_size == 0
            )
        return False

    def _restore_existing_target(
        self,
        temporary_name: str,
        report: os.stat_result,
        original: os.stat_result,
        quarantine: str | None,
    ) -> bool:
        """Restore the exact preimage before its held inode has been scrubbed."""
        if quarantine is not None and self._entry_matches(quarantine, original):
            try:
                _rename_exclusive(self.parent_fd, quarantine, temporary_name)
            except OSError:
                return False
        if self._entry_matches(temporary_name, report) and self._entry_is_safe(
            self.name,
            original,
        ):
            return True
        if not self._entry_matches(self.name, report) or not self._entry_matches(
            temporary_name,
            original,
        ):
            return False
        try:
            _exchange_names(self.parent_fd, temporary_name, self.name)
            os.fsync(self.parent_fd)
        except OSError:
            return False
        return self._entry_matches(temporary_name, report) and self._entry_is_safe(
            self.name,
            original,
        )

    def _install_verified_temporary(
        self,
        temporary_name: str,
        report: os.stat_result,
        expected_target: os.stat_result | None,
        report_descriptor: int,
        displaced_descriptor: int,
        state: _StrictWriteState,
    ) -> None:
        """Install a prepared report or prove rollback under named phase faults."""
        failure: BaseException | None = None
        quarantine: str | None = None
        try:
            if expected_target is None:
                _rename_exclusive(self.parent_fd, temporary_name, self.name)
                self._strict_fault("new-installed")
                if not self._entry_is_safe(self.name, report):
                    raise UnsafePathError()
                os.fsync(self.parent_fd)
                self._strict_fault("new-durable")
                if not self._terminal_report_snapshot(report, report_descriptor):
                    raise UnsafePathError()
                state.committed = True
                return

            _exchange_names(self.parent_fd, temporary_name, self.name)
            self._strict_fault("existing-exchanged")
            if not self._entry_is_safe(self.name, report) or not self._entry_is_safe(
                temporary_name,
                expected_target,
            ):
                raise UnsafePathError()
            os.fsync(self.parent_fd)
            self._strict_fault("existing-durable")
            if not self._entry_is_safe(self.name, report) or not self._entry_is_safe(
                temporary_name,
                expected_target,
            ):
                raise UnsafePathError()
            quarantine = f".{self.name}.{secrets.token_hex(16)}.rollback"
            _rename_exclusive(self.parent_fd, temporary_name, quarantine)
            self._strict_fault("original-quarantined")
            if not self._entry_is_safe(quarantine, expected_target):
                raise UnsafePathError()
            self._strict_fault("original-pre-scrub")
            state.displaced_touched = True
            if not self._scrub_descriptor(
                displaced_descriptor,
                expected_target,
                single_link=True,
            ):
                raise AtomicWriteRollbackError()
            state.displaced_scrubbed = True
            os.fsync(self.parent_fd)
            self._strict_fault("original-scrubbed")
            if not self._terminal_existing_snapshot(
                quarantine,
                expected_target,
                displaced_descriptor,
                report,
                report_descriptor,
            ):
                raise AtomicWriteRollbackError()
            state.committed = True
            return
        except BaseException as error:  # noqa: BLE001 - phase-aware result below
            failure = error

        if state.committed:
            return
        if expected_target is None:
            target_cleaned = self._quarantine_owned_entry(
                self.name,
                report,
                report_descriptor,
            )
            temporary_cleaned = self._quarantine_owned_entry(
                temporary_name,
                report,
                report_descriptor,
            )
            state.cleaned = target_cleaned and temporary_cleaned
            state.cleaned = state.cleaned and self._terminal_rollback_snapshot(
                None,
                displaced_descriptor,
                report,
                report_descriptor,
            )
        elif state.displaced_touched:
            if (
                quarantine is not None
                and state.displaced_scrubbed
                and self._terminal_existing_snapshot(
                    quarantine,
                    expected_target,
                    displaced_descriptor,
                    report,
                    report_descriptor,
                )
            ):
                state.committed = True
                return
        else:
            restored = self._restore_existing_target(
                temporary_name,
                report,
                expected_target,
                quarantine,
            )
            state.cleaned = restored and self._quarantine_owned_entry(
                temporary_name,
                report,
                report_descriptor,
            )
            state.cleaned = state.cleaned and self._terminal_rollback_snapshot(
                expected_target,
                displaced_descriptor,
                report,
                report_descriptor,
            )
        if not state.cleaned:
            self._scrub_descriptor(report_descriptor, report, single_link=False)
            state.indeterminate = True
            failure = None
            raise AtomicWriteRollbackError() from None
        if failure is not None:
            raise failure from None
        raise AtomicWriteRollbackError() from None

    def _atomic_write_unverified(self, content: bytes) -> None:
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
            self._assert_safe_existing_target()
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
                    if temporary_metadata is not None and self._entry_is_safe(
                        temporary_name,
                        temporary_metadata,
                    ):
                        os.unlink(temporary_name, dir_fd=self.parent_fd)
                except BaseException:  # noqa: BLE001, S110 - preserve the primary failure
                    pass

    def _atomic_write_strict(self, content: memoryview) -> None:
        temporary_name: str | None = None
        report_descriptor = -1
        displaced_descriptor = -1
        report: os.stat_result | None = None
        state = _StrictWriteState()
        failure: BaseException | None = None
        view = content
        content = memoryview(b"")
        try:
            for _ in range(_ROLLBACK_ATTEMPTS):
                temporary_name = f".{self.name}.{secrets.token_hex(16)}.tmp"
                try:
                    report_descriptor = os.open(
                        temporary_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                        0o644,
                        dir_fd=self.parent_fd,
                    )
                except FileExistsError:
                    temporary_name = None
                    continue
                break
            if temporary_name is None or report_descriptor < 0:
                raise UnsafePathError()
            report = os.fstat(report_descriptor)
            _require_regular(report)
            while view:
                written = os.write(report_descriptor, view)
                view = view[written:]
            os.fsync(report_descriptor)
            report = os.fstat(report_descriptor)
            _require_regular(report)
            self._strict_fault("prepared")
            expected_target = self._assert_safe_existing_target()
            if expected_target is not None:
                displaced_descriptor = os.open(
                    self.name,
                    os.O_WRONLY | _NOFOLLOW | _CLOEXEC,
                    dir_fd=self.parent_fd,
                )
                displaced = os.fstat(displaced_descriptor)
                _require_regular(displaced)
                if _identity(displaced) != _identity(expected_target):
                    raise UnsafePathError()
            self._install_verified_temporary(
                temporary_name,
                report,
                expected_target,
                report_descriptor,
                displaced_descriptor,
                state,
            )
            if state.committed:
                temporary_name = None
        except BaseException as error:  # noqa: BLE001 - clear payload before dispatch
            failure = error

        if not state.committed and not state.cleaned and report is not None:
            state.cleaned = bool(
                temporary_name is not None
                and self._quarantine_owned_entry(
                    temporary_name,
                    report,
                    report_descriptor,
                )
            )
            if not state.cleaned:
                self._scrub_descriptor(report_descriptor, report, single_link=False)
                failure = None

        view.release()
        view = memoryview(b"")
        close_failed = False
        for descriptor in (report_descriptor, displaced_descriptor):
            if descriptor >= 0:
                close_failed = not self._close_descriptor(descriptor) or close_failed

        if state.committed and failure is None and not close_failed:
            return
        if close_failed or state.indeterminate or not state.cleaned or failure is None:
            failure = None
            raise AtomicWriteRollbackError() from None
        if isinstance(failure, OSError):
            os_failure = failure
            failure = None
            raise UnsafePathError() from os_failure
        raise failure from None

    def atomic_write(self, content: bytes, *, reject_target_races: bool = False) -> None:
        if reject_target_races:
            payload = memoryview(content)
            content = b""
            try:
                self._atomic_write_strict(payload)
            finally:
                payload.release()
                payload = memoryview(b"")
            return
        self._atomic_write_unverified(content)

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
