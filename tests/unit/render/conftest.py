"""Real YAML-store fixture for renderer tests."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intent_engineering.core.models import Edge, Graph, Node, NodeType
from intent_engineering.render.renderer import GraphRenderer
from intent_engineering.storage.yaml.graph_store import YamlGraphStore

NOW = datetime(2026, 8, 25, tzinfo=UTC)


def _node(node_id: str, label: str, node_type: NodeType) -> Node:
    return Node(
        id=node_id,
        type=node_type,
        label=label,
        status="active",
        created_by="fixture@example.test",
        created_at=NOW,
        last_modified_by="fixture@example.test",
        last_modified_at=NOW,
    )


@dataclass(frozen=True)
class RenderFixture:
    graph_path: Path
    output_dir: Path
    renderer: GraphRenderer
    graph: Graph


@pytest.fixture
def render_fixture(tmp_path: Path) -> RenderFixture:
    graph = Graph(
        id="render-graph",
        version=1,
        name="Render [graph]",
        purpose="Render *only* generated views.",
        nodes=(
            _node("req-z", 'Zeta "export"', NodeType.REQUIREMENT),
            _node("req-a", "Alpha export", NodeType.REQUIREMENT),
        ),
        edges=(
            Edge(
                id="edge-z",
                from_id="req-z",
                relation="REFINES",
                to_id="req-a",
                status="active",
                created_by="fixture@example.test",
                created_at=NOW,
                last_modified_by="fixture@example.test",
                last_modified_at=NOW,
            ),
        ),
    )
    graph_path = tmp_path / "graph.yaml"
    store = YamlGraphStore(graph_path)
    store.initialize(graph)
    return RenderFixture(
        graph_path=graph_path,
        output_dir=tmp_path / "generated",
        renderer=GraphRenderer(store),
        graph=graph,
    )
