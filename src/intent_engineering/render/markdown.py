"""Deterministic Markdown projection of an immutable graph."""

from collections.abc import Sequence
from unicodedata import category

from intent_engineering.core.models import (
    Graph,
    Node,
    NodeType,
    ReconciliationCase,
    is_nonterminal_case_status,
)

SEMANTIC_GROUPS: tuple[tuple[str, frozenset[NodeType]], ...] = (
    (
        "Intent",
        frozenset(
            {NodeType.CONTEXT, NodeType.NEED, NodeType.PRODUCT_INTENT, NodeType.DESIRED_OUTCOME}
        ),
    ),
    ("Requirements", frozenset({NodeType.REQUIREMENT, NodeType.CAPABILITY})),
    (
        "Decisions",
        frozenset(
            {NodeType.DECISION, NodeType.ARCHITECTURE, NodeType.INTERFACE, NodeType.DATA_CONTRACT}
        ),
    ),
    ("Constraints", frozenset({NodeType.CONSTRAINT, NodeType.POLICY})),
    ("Acceptance criteria", frozenset({NodeType.ACCEPTANCE_CRITERION})),
    (
        "Code references",
        frozenset(
            {
                NodeType.REPOSITORY,
                NodeType.MODULE,
                NodeType.FILE,
                NodeType.SYMBOL,
                NodeType.ENDPOINT,
                NodeType.SCHEMA,
            }
        ),
    ),
    ("Test references", frozenset({NodeType.TEST})),
)


def _escape_markdown(value: str) -> str:
    value = "".join(
        " " if character in "\r\n\u2028\u2029" or category(character).startswith("C") else character
        for character in value
    )
    return "".join(
        f"\\{character}" if character in r'\\`*_{}[]<>#|"' else character for character in value
    )


def _render_node_group(group: tuple[str, frozenset[NodeType]], nodes: Sequence[Node]) -> str:
    title, node_types = group
    selected = sorted((node for node in nodes if node.type in node_types), key=lambda node: node.id)
    if not selected:
        return ""
    lines = [f"## {title}"]
    for node in selected:
        lines.append(f"- `{_escape_markdown(node.id)}` — {_escape_markdown(node.label)}")
        if node.evidence_refs:
            lines.append(
                "  - Evidence: "
                + ", ".join(f"`{_escape_markdown(ref)}`" for ref in sorted(node.evidence_refs))
            )
    return "\n".join(lines)


def _render_cases(cases: Sequence[ReconciliationCase]) -> str:
    open_cases = sorted(
        (case for case in cases if is_nonterminal_case_status(case.status)),
        key=lambda case: case.id,
    )
    if not open_cases:
        return ""
    lines = ["## Open reconciliation cases"]
    for case in open_cases:
        lines.append(
            f"- `{_escape_markdown(case.id)}` — {_escape_markdown(case.case_type.value)}: {_escape_markdown(case.subject_ref)}"
        )
        lines.append(
            "  - Evidence: "
            + ", ".join(f"`{_escape_markdown(ref)}`" for ref in case.all_evidence_refs)
        )
    return "\n".join(lines)


def render_markdown(graph: Graph, cases: Sequence[ReconciliationCase]) -> str:
    """Render a deterministic Markdown view without mutating graph state."""
    sections = [
        f"# {_escape_markdown(graph.name or graph.id)}",
        _escape_markdown(graph.purpose or ""),
    ]
    sections.extend(_render_node_group(group, graph.nodes) for group in SEMANTIC_GROUPS)
    sections.append(_render_cases(cases))
    return "\n\n".join(section for section in sections if section) + "\n"
