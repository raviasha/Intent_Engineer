"""Kernel-verified rootful, read-only container image boundary for CI evidence.

The trusted host/daemon is outside the repository threat boundary. A writable
checkout, read-only bind mount, user-namespace overlay, or producer-supplied flag
cannot establish this contract. The image root is immutable to the unprivileged
reviewed process for the entire container lifetime, including mmap writes.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from intent_engineering.storage.secure import SecureDirectory, _read_descriptor


class ImmutableExecutionUnavailable(ValueError):
    """Fixed failure when the kernel cannot establish immutable CI material."""

    def __init__(self) -> None:
        super().__init__("immutable execution unavailable")


def _kernel_text(path: str, maximum: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _read_descriptor(descriptor, max_bytes=maximum).decode("ascii")
    finally:
        os.close(descriptor)


class ImmutableExecutionGuard:
    """Require a non-root, capability-free process on a read-only root image.

    Rootful Docker's root overlay is controlled by the trusted daemon. Requiring
    the initial UID namespace excludes a user-created overlay backed by their
    mutable files. Every relevant descriptor must share that read-only root
    device; source/baseline bind mounts and writable output mounts are rejected.
    """

    _device: int

    def __init__(self, directory: SecureDirectory) -> None:
        try:
            if sys.platform != "linux" or os.geteuid() == 0 or os.getuid() != os.geteuid():
                raise ValueError("unsupported execution host")
            fields = dict(
                line.split(":", 1)
                for line in _kernel_text("/proc/self/status", 64 * 1024).splitlines()
                if ":" in line
            )
            if (
                fields["NoNewPrivs"].strip() != "1"
                or fields["TracerPid"].strip() != "0"
                or any(int(fields[name], 16) for name in ("CapInh", "CapPrm", "CapEff", "CapAmb"))
                or _kernel_text("/proc/self/uid_map", 1024).split() != ["0", "0", "4294967295"]
            ):
                raise ValueError("privileged or mapped execution host")
            interfaces = {
                line.split(":", 1)[0].strip()
                for line in _kernel_text("/proc/self/net/dev", 64 * 1024).splitlines()
                if ":" in line
            }
            # Some kernels include inert tunnel devices even in Docker's "none"
            # network. Without NET_ADMIN these DOWN interfaces cannot be enabled.
            if (
                "lo" not in interfaces
                or len(interfaces) > 256
                or any(
                    not name
                    or len(name) > 15
                    or "/" in name
                    or name in {".", ".."}
                    or (
                        name != "lo"
                        and int(_kernel_text(f"/sys/class/net/{name}/flags", 64), 16) & 1
                    )
                    for name in interfaces
                )
            ):
                raise ValueError("network-enabled execution host")
            mounts = _kernel_text("/proc/self/mountinfo", 1024 * 1024).splitlines()
            roots = [
                line.split() for line in mounts if len(line.split()) > 6 and line.split()[4] == "/"
            ]
            if len(roots) != 1:
                raise ValueError("ambiguous image root")
            root = roots[0]
            separator = root.index("-")
            if root[3] != "/" or "ro" not in root[5].split(",") or root[separator + 1] != "overlay":
                raise ValueError("execution root is not a read-only image")
            self._device = os.stat("/", follow_symlinks=False).st_dev
            # No source or baseline submount may hide mutable host material.
            project = str(directory.path)
            for line in mounts:
                mount = line.split()
                if (
                    len(mount) > 6
                    and mount[4].startswith(project + "/")
                    and mount[4] != project + "/.intent-ci"
                ):
                    raise ValueError("mutable source mount")
            self.require_descriptor(directory.descriptor, directory=True)
            # Git metadata must also be in the image, not a linked host checkout.
            git_directory = directory.subdirectory(".git")
            try:
                self.require_descriptor(git_directory.descriptor, directory=True)
            finally:
                git_directory.close()
        except (OSError, ValueError, KeyError, IndexError):
            raise ImmutableExecutionUnavailable() from None

    def require_descriptor(self, descriptor: int, *, directory: bool = False) -> None:
        """Check a held inode without trusting its pathname or a producer assertion."""
        metadata = os.fstat(descriptor)
        if (
            metadata.st_dev != self._device
            or not os.fstatvfs(descriptor).f_flag & os.ST_RDONLY
            or not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode))
        ):
            raise ImmutableExecutionUnavailable()

    def require_file(self, root: SecureDirectory, relative: str | Path) -> None:
        target = root.file(relative)
        try:
            descriptor = os.open(
                target.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=target.parent_fd,
            )
            try:
                self.require_descriptor(descriptor)
            finally:
                os.close(descriptor)
        finally:
            target.close()
