"""Observable context-selection behavior."""

import pytest

from intent_engineering.context.provider import ContextProvider
from intent_engineering.core.models import (
    Edge,
    Graph,
    NodeType,
    ProjectConfig,
    ReconciliationStatus,
)

from .conftest import ContextFixture


def _provider(
    fixture: ContextFixture,
    *,
    graph: Graph | None = None,
    cases: tuple = (),
    evidence: tuple = (),
    config: ProjectConfig | None = None,
) -> ContextProvider:
    return ContextProvider(
        graph or fixture.graph,
        fixture.cases if not cases else cases,
        config or fixture.config,
        fixture.evidence if not evidence else evidence,
    )


def test_task_context_returns_only_connected_active_items(context_fixture: ContextFixture) -> None:
    """Fails if inactive or disconnected nodes become task context."""
    pack = context_fixture.provider.for_task(
        "add local export", repository_scope="repo-a", actor="alice@example.test"
    )

    assert [item.id for item in pack.relevant_requirements] == ["req-local-export"]
    assert [item.id for item in pack.open_reconciliation_cases] == ["case-export-tests"]
    assert "req-unrelated" not in pack.model_dump_json()


def test_context_caps_categories_and_warns_for_low_confidence(
    context_fixture: ContextFixture,
) -> None:
    """Fails if configured caps or review warnings are bypassed."""
    pack = context_fixture.provider.for_task(
        "local export", repository_scope="repo-a", actor="alice@example.test"
    )

    assert [item.id for item in pack.relevant_requirements] == ["req-local-export"]
    assert "low confidence: constraint-low-confidence" in pack.warnings
    assert pack.evidence_refs == ("ev-case", "ev-export")


def test_context_fails_closed_for_unapproved_actor(context_fixture: ContextFixture) -> None:
    """Fails if evidence protected by an ACL is exposed to another actor."""
    pack = context_fixture.provider.for_task(
        "local export", repository_scope="repo-a", actor="bob@example.test"
    )

    assert pack.evidence_refs == ()
    assert pack.relevant_requirements == ()
    assert pack.open_reconciliation_cases == ()


def test_context_requires_an_explicit_actor_for_acl_evidence(
    context_fixture: ContextFixture,
) -> None:
    """Fails if ACL-protected evidence is treated as public without an actor."""
    pack = context_fixture.provider.for_task("add local export", repository_scope="repo-a")

    assert pack.evidence_refs == ()
    assert pack.relevant_requirements == ()
    assert pack.open_reconciliation_cases == ()


def test_context_rejects_a_conflicting_requested_repository_scope(
    context_fixture: ContextFixture,
) -> None:
    """Fails if scope filtering accepts evidence from another repository scope."""
    pack = context_fixture.provider.for_task(
        "add local export", repository_scope="repo-b", actor="alice@example.test"
    )

    assert pack.evidence_refs == ()
    assert pack.relevant_requirements == ()
    assert pack.open_reconciliation_cases == ()


def test_symbol_context_seeds_exact_stable_id(context_fixture: ContextFixture) -> None:
    """Fails if a symbol reference is treated as a label-token task query."""
    symbol = context_fixture.graph.nodes[1].model_copy(
        update={
            "id": "symbol-opaque-id",
            "type": NodeType.SYMBOL,
            "label": "Internal implementation",
        }
    )
    graph = context_fixture.graph.model_copy(
        update={
            "nodes": (*context_fixture.graph.nodes, symbol),
            "edges": (
                *context_fixture.graph.edges,
                Edge(
                    id="edge-requirement-symbol",
                    from_id="req-local-export",
                    relation="IMPLEMENTED_BY",
                    to_id="symbol-opaque-id",
                    status="active",
                    created_by="fixture@example.test",
                    created_at=context_fixture.graph.nodes[0].created_at,
                    last_modified_by="fixture@example.test",
                    last_modified_at=context_fixture.graph.nodes[0].last_modified_at,
                ),
            ),
        }
    )

    pack = _provider(context_fixture, graph=graph).for_symbol(
        "symbol-opaque-id", actor="alice@example.test"
    )

    assert [item.id for item in pack.relevant_requirements] == ["req-local-export"]
    assert [item.id for item in pack.code_refs] == ["symbol-opaque-id"]


def test_symbol_context_is_empty_for_an_empty_reference(context_fixture: ContextFixture) -> None:
    """Fails if an empty stable reference can select label-matched context."""
    pack = context_fixture.provider.for_symbol("", actor="alice@example.test")

    assert pack.task == ""
    assert pack.evidence_refs == ()
    assert pack.relevant_requirements == ()
    assert pack.open_reconciliation_cases == ()


def test_symbol_context_is_empty_for_an_unknown_reference(context_fixture: ContextFixture) -> None:
    """Fails if an unknown stable reference falls back to label-token matching."""
    pack = context_fixture.provider.for_symbol("unknown-stable-id", actor="alice@example.test")

    assert pack.task == "unknown-stable-id"
    assert pack.evidence_refs == ()
    assert pack.relevant_requirements == ()
    assert pack.open_reconciliation_cases == ()


@pytest.mark.parametrize("missing_ref", ["ev-missing", "ev-also-missing"])
def test_context_excludes_nodes_with_unresolved_evidence(
    context_fixture: ContextFixture, missing_ref: str
) -> None:
    """Fails if an unresolved evidence reference can leak a selected node or case."""
    replaced = context_fixture.graph.nodes[1].model_copy(
        update={"evidence_refs": ("ev-export", missing_ref)}
    )
    graph = context_fixture.graph.model_copy(
        update={
            "nodes": (context_fixture.graph.nodes[0], replaced, *context_fixture.graph.nodes[2:])
        }
    )

    pack = _provider(context_fixture, graph=graph).for_task(
        "add local export", repository_scope="repo-a", actor="alice@example.test"
    )

    assert pack.relevant_requirements == ()
    assert missing_ref not in pack.evidence_refs


def test_context_excludes_cases_with_mixed_resolved_and_unresolved_evidence(
    context_fixture: ContextFixture,
) -> None:
    """Fails if one unresolved case reference is hidden behind an authorized reference."""
    side = (
        context_fixture.cases[0]
        .evidence_sides[0]
        .model_copy(update={"evidence_refs": ("ev-case", "ev-missing")})
    )
    case = context_fixture.cases[0].model_copy(update={"evidence_sides": (side,)})

    pack = _provider(context_fixture, cases=(case,)).for_task(
        "add local export", repository_scope="repo-a", actor="alice@example.test"
    )

    assert pack.open_reconciliation_cases == ()
    assert "ev-missing" not in pack.evidence_refs


def test_context_excludes_unscoped_evidence_for_a_requested_scope(
    context_fixture: ContextFixture,
) -> None:
    """Fails if a requested repository scope permits evidence without a scope."""
    unscoped = context_fixture.evidence[0].model_copy(update={"payload": {}})
    provider = _provider(context_fixture, evidence=(unscoped, *context_fixture.evidence[1:]))

    pack = provider.for_task(
        "add local export", repository_scope="repo-a", actor="alice@example.test"
    )

    assert "req-local-export" not in pack.model_dump_json()
    assert "ev-export" not in pack.evidence_refs


def test_context_keeps_evidenceless_nodes_with_a_warning(context_fixture: ContextFixture) -> None:
    """Fails if fail-closed evidence handling hides nodes with no evidence references."""
    node = context_fixture.graph.nodes[1].model_copy(
        update={"id": "req-no-evidence", "label": "Evidence free export", "evidence_refs": ()}
    )
    graph = context_fixture.graph.model_copy(update={"nodes": (node,), "edges": ()})
    provider = _provider(context_fixture, graph=graph, cases=())

    pack = provider.for_task("evidence free", actor="alice@example.test")

    assert [item.id for item in pack.relevant_requirements] == ["req-no-evidence"]
    assert pack.warnings == ("missing evidence: req-no-evidence",)


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
def test_context_uses_all_and_only_nonterminal_cases(
    context_fixture: ContextFixture, status: ReconciliationStatus, visible: bool
) -> None:
    """Fails if a reviewable case status is omitted or a terminal case is exposed."""
    case = context_fixture.cases[0].model_copy(update={"status": status})
    pack = _provider(context_fixture, cases=(case,)).for_task(
        "add local export", repository_scope="repo-a", actor="alice@example.test"
    )

    assert bool(pack.open_reconciliation_cases) is visible


def test_default_context_limit_caps_evidence_references(context_fixture: ContextFixture) -> None:
    """Fails if the default evidence collection can grow without a configured cap."""
    references = tuple(f"ev-{index:02d}" for index in range(21))
    node = context_fixture.graph.nodes[1].model_copy(update={"evidence_refs": references})
    graph = context_fixture.graph.model_copy(update={"nodes": (node,), "edges": ()})
    evidence = tuple(
        context_fixture.evidence[0].model_copy(update={"id": reference}) for reference in references
    )
    provider = ContextProvider(
        graph,
        (),
        ProjectConfig(project_id="defaults", local_actor="alice@example.test"),
        evidence,
    )

    pack = provider.for_task("local export", actor="alice@example.test")

    assert len(pack.evidence_refs) == 20
