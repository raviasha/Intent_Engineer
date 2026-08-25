"""Observable context-selection behavior."""

from .conftest import ContextFixture


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


def test_symbol_context_uses_unicode_word_tokens(context_fixture: ContextFixture) -> None:
    """Fails if non-ASCII words cannot produce a matching context query."""
    pack = context_fixture.provider.for_symbol("CAFÉ", actor="alice@example.test")

    assert [item.id for item in pack.relevant_requirements] == ["req-local-export"]
