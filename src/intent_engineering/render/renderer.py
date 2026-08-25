"""Write generated graph views without touching canonical graph storage."""

from __future__ import annotations

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
        output_dir.mkdir(parents=True, exist_ok=True)
        markdown_path = output_dir / "graph.md"
        mermaid_path = output_dir / "graph.mmd"
        markdown_path.write_text(render_markdown(graph, self._cases), encoding="utf-8")
        mermaid_path.write_text(render_mermaid(graph), encoding="utf-8")
        return markdown_path, mermaid_path
