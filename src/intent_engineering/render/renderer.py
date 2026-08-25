"""Write generated graph views without touching canonical graph storage."""

from __future__ import annotations

import os
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
        self._assert_no_symlink_components(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = output_dir / "graph.md"
        mermaid_path = output_dir / "graph.mmd"
        targets = (markdown_path, mermaid_path)
        if any(target.is_symlink() for target in targets):
            raise ValueError("generated view target must not be a symlink")
        self._write_no_follow(output_dir, markdown_path.name, render_markdown(graph, self._cases))
        self._write_no_follow(output_dir, mermaid_path.name, render_mermaid(graph))
        return markdown_path, mermaid_path

    @staticmethod
    def _assert_no_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            if current.is_symlink():
                raise ValueError("generated view output directory must not contain a symlink")

    @staticmethod
    def _write_no_follow(output_dir: Path, filename: str, content: str) -> None:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(output_dir, directory_flags)
        except OSError as error:
            raise ValueError("generated view output directory must not be a symlink") from error
        try:
            try:
                file_fd = os.open(filename, file_flags, 0o644, dir_fd=directory_fd)
            except OSError as error:
                raise ValueError("generated view target must not be a symlink") from error
            with os.fdopen(file_fd, "wb") as target:
                target.write(content.encode("utf-8"))
        finally:
            os.close(directory_fd)
