"""Source-visible attribution for agent-drafted PRDs, never human approval."""

INFERRED_PRD_MARKER = b"<!-- intent-engineering: inferred-prd; author=agent:codex -->"


def is_inferred_prd(content: bytes) -> bool:
    return content.split(b"\n", 1)[0].rstrip(b"\r") == INFERRED_PRD_MARKER


def markdown_author(content: bytes, local_actor: str) -> str:
    """Keep capture and later sync attribution identical for the same source bytes."""
    return "agent:codex" if is_inferred_prd(content) else local_actor
