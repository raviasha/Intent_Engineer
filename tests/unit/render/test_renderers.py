"""Deterministic, non-canonical view rendering."""

from intent_engineering.render.markdown import render_markdown
from intent_engineering.render.mermaid import render_mermaid

from .conftest import RenderFixture


def test_render_does_not_change_graph_file(render_fixture: RenderFixture) -> None:
    """Fails if producing views writes through the canonical YAML store."""
    before = render_fixture.graph_path.read_bytes()

    render_fixture.renderer.render_all(render_fixture.output_dir)

    assert render_fixture.graph_path.read_bytes() == before
    assert (render_fixture.output_dir / "graph.md").is_file()
    assert (render_fixture.output_dir / "graph.mmd").is_file()


def test_markdown_is_sorted_and_escapes_node_text(render_fixture: RenderFixture) -> None:
    """Fails if graph labels can change Markdown structure or output order."""
    rendered = render_markdown(render_fixture.graph, ())

    assert "# Render \\[graph\\]" in rendered
    assert rendered.index("req-a") < rendered.index("req-z")
    assert 'Zeta \\"export\\"' in rendered


def test_mermaid_is_sorted_safe_and_has_a_stable_header(render_fixture: RenderFixture) -> None:
    """Fails if Mermaid views are unsafe, unordered, or use another graph direction."""
    rendered = render_mermaid(render_fixture.graph)

    assert rendered.startswith("flowchart LR\n")
    assert rendered.index("Alpha export") < rendered.index("Zeta")
    assert 'Zeta \\"export\\"' in rendered
    assert rendered.endswith("\n")
