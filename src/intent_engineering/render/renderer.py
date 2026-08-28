"""Write generated graph views without touching canonical graph storage."""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Sequence
from pathlib import Path

from intent_engineering.core.models import ReconciliationCase
from intent_engineering.render.markdown import render_markdown
from intent_engineering.render.mermaid import render_mermaid
from intent_engineering.storage.interfaces import GraphStore


class GraphRenderer:
    """Render a graph-store snapshot into explicitly supplied generated files."""

    def __init__(self, graph_store: GraphStore, cases: Sequence[ReconciliationCase] = ()) -> None:
        self._graph_store = graph_store
        self._cases = tuple(cases)

    def render_all(self, output_dir: Path) -> tuple[Path, Path]:
        """Write Markdown and Mermaid files below ``output_dir`` only."""
        graph = self._graph_store.load()
        output_dir = output_dir.absolute()
        markdown_path = output_dir / "graph.md"
        mermaid_path = output_dir / "graph.mmd"
        directory_fd = self._open_output_directory(output_dir)
        try:
            if not self._path_matches_directory_fd(output_dir, directory_fd):
                raise ValueError("generated view output directory must not be a symlink")
            self._assert_safe_target(directory_fd, markdown_path.name)
            self._assert_safe_target(directory_fd, mermaid_path.name)
            self._atomic_write(
                directory_fd, markdown_path.name, render_markdown(graph, self._cases)
            )
            self._atomic_write(directory_fd, mermaid_path.name, render_mermaid(graph))
        finally:
            os.close(directory_fd)
        return markdown_path, mermaid_path

    @staticmethod
    def _open_output_directory(path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.anchor, flags)
        try:
            for component in path.parts[1:]:
                if component in {".", ".."}:
                    raise ValueError("generated view output directory must be normalized")
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o755, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except (OSError, ValueError) as error:
            os.close(descriptor)
            if isinstance(error, ValueError):
                raise
            raise ValueError(
                "generated view output directory must not contain a symlink"
            ) from error
        return descriptor

    @staticmethod
    def _path_matches_directory_fd(path: Path, directory_fd: int) -> bool:
        try:
            path_stat = os.stat(path, follow_symlinks=False)
            directory_stat = os.fstat(directory_fd)
        except OSError as error:
            raise ValueError("generated view output directory must not be a symlink") from error
        return (
            stat.S_ISDIR(path_stat.st_mode)
            and path_stat.st_dev == directory_stat.st_dev
            and path_stat.st_ino == directory_stat.st_ino
        )

    @staticmethod
    def _assert_safe_target(directory_fd: int, filename: str) -> None:
        try:
            target_stat = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ValueError("generated view target must not be a symlink") from error
        if stat.S_ISLNK(target_stat.st_mode) or not stat.S_ISREG(target_stat.st_mode):
            raise ValueError("generated view target must not be a symlink")

    @staticmethod
    def _atomic_write(directory_fd: int, filename: str, content: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        temporary_name: str | None = None
        try:
            for _ in range(10):
                candidate = f".{filename}.{secrets.token_hex(16)}.tmp"
                try:
                    temporary_fd = os.open(candidate, flags, 0o644, dir_fd=directory_fd)
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            else:
                raise ValueError("could not create generated view temporary file")
            with os.fdopen(temporary_fd, "wb") as temporary:
                temporary.write(content.encode("utf-8"))
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_name = None
            os.fsync(directory_fd)
        finally:
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
