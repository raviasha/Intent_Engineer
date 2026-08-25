"""Deterministic, non-canonical view rendering."""

from pathlib import Path

import pytest

from intent_engineering.core.models import ReconciliationStatus
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


@pytest.mark.parametrize(
    ("status", "visible"),
    [
        (ReconciliationStatus.OPEN, True),
        (ReconciliationStatus.PROPOSED, True),
        (ReconciliationStatus.NEEDS_HUMAN, True),
        (ReconciliationStatus.RESOLVED, False),
        (ReconciliationStatus.DEFERRED, False),
        (ReconciliationStatus.FALSE_POSITIVE, False),
    ],
)
def test_markdown_uses_all_and_only_nonterminal_cases(
    render_fixture: RenderFixture, status: ReconciliationStatus, visible: bool
) -> None:
    """Fails if Markdown hides reviewable cases or emits terminal ones."""
    case = render_fixture.cases[0].model_copy(update={"status": status})

    rendered = render_markdown(render_fixture.graph, (case,))

    assert ("case-render" in rendered) is visible


def test_markdown_normalizes_injected_line_breaks_and_controls(
    render_fixture: RenderFixture,
) -> None:
    """Fails if graph, node, evidence, or case text can inject Markdown lines."""
    node = render_fixture.graph.nodes[0].model_copy(
        update={
            "id": "req\n# injected-node\x00",
            "label": "label\r\n- injected-label\u2028\u2029",
            "evidence_refs": ("ev\n- injected-evidence\x1f",),
        }
    )
    graph = render_fixture.graph.model_copy(
        update={
            "id": "graph\n# injected-id",
            "name": None,
            "purpose": "purpose\r\n## injected-purpose\u2028\x00",
            "nodes": (node,),
            "edges": (),
        }
    )
    side = (
        render_fixture.cases[0]
        .evidence_sides[0]
        .model_copy(update={"evidence_refs": ("case\r\n- injected-case-evidence\x00",)})
    )
    case = render_fixture.cases[0].model_copy(
        update={
            "id": "case\n- injected-case",
            "subject_ref": "subject\r\n## injected-subject",
            "evidence_sides": (side,),
        }
    )

    rendered = render_markdown(graph, (case,))

    assert "\n# injected-id" not in rendered
    assert "\n## injected-purpose" not in rendered
    assert "\n- injected-label" not in rendered
    assert "\n- injected-evidence" not in rendered
    assert "\n- injected-case" not in rendered
    assert "\n## injected-subject" not in rendered
    assert "\x00" not in rendered
    assert "\u2028" not in rendered
    assert "\u2029" not in rendered


def test_renderer_rejects_an_output_directory_symlink(
    render_fixture: RenderFixture, tmp_path: Path
) -> None:
    """Fails if generated views can be redirected outside the supplied directory."""
    outside = tmp_path / "outside"
    outside.mkdir()
    output_link = tmp_path / "generated-link"
    output_link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        render_fixture.renderer.render_all(output_link)

    assert not (outside / "graph.md").exists()
    assert not (outside / "graph.mmd").exists()


def test_renderer_rejects_a_preexisting_output_file_symlink(
    render_fixture: RenderFixture, tmp_path: Path
) -> None:
    """Fails if a generated target can overwrite an external symlink target."""
    output_dir = tmp_path / "generated"
    output_dir.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside remains unchanged", encoding="utf-8")
    (output_dir / "graph.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        render_fixture.renderer.render_all(output_dir)

    assert outside.read_text(encoding="utf-8") == "outside remains unchanged"
    assert not (output_dir / "graph.mmd").exists()
