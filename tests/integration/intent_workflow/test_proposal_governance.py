"""Integration proof for deterministic local proposal confirmation governance."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.core.graph.applier import apply_changeset_with_case_effects
from intent_engineering.core.models import (
    ChangeKind,
    ConfidenceChange,
    Edge,
    ImplementationStatus,
    ImplementationStatusChange,
    ReconciliationCase,
    ReconciliationStatus,
    RelationType,
    SourceMode,
)
from intent_engineering.intent_workflow.clarification import (
    ClarificationError,
    ProposalConfirmationService,
    ProposalConfirmationStatus,
    _digest,
    _event,
)
from intent_engineering.intent_workflow.models import ProposalDecisionV3
from intent_engineering.intent_workflow.proposal_store import (
    IntentLedgerRecord,
    serialize_intent_ledger_record,
)
from intent_engineering.storage.jsonl.case_store import parse_case_versions, serialize_case
from intent_engineering.storage.transaction import LocalTransactionSnapshot
from tests.integration.intent_workflow.test_clarification import (
    NOW,
    ClarificationHarness,
    _base_evidence,
    _harness,
    _policy_payload,
    _repository_traceback_locals,
)


class CancellationSignal(BaseException):
    """Test-only cancellation whose exact identity must cross the public boundary."""


@pytest.fixture
def clarification_harness(tmp_path: Path) -> ClarificationHarness:
    return _harness(tmp_path)


def test_current_contributor_confirms_pure_addition_with_exact_audit_binding(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose()
    result = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )

    assert result.status is ProposalConfirmationStatus.APPLIED
    assert result.graph_version == 3
    assert result.case_id is None
    graph = clarification_harness.graph_store.load()
    assert {node.id for node in graph.nodes} >= {"req-read-only-sharing"}
    decision = clarification_harness.proposal_store.decision_for(proposal.id)
    assert isinstance(decision, ProposalDecisionV3)
    assert decision.proposal_digest == proposal.digest
    assert decision.activation_changeset_id.startswith(
        "changeset:clarification-activation:sha256:"
    )
    assert decision.actor_aliases == ("github:asha", "local:asha", "slack:asha")
    assert decision.selected_node_ids == ("req-read-only-sharing",)
    history = clarification_harness.graph_store.history("req-read-only-sharing")
    assert len(history) == 1
    assert history[0].id == decision.activation_changeset_id
    closed = clarification_harness.proposal_store.clarification_events(
        proposal.clarification_session_id
    )[-1]
    assert closed.event_type == "closed"
    assert closed.proposal_id == proposal.id
    assert closed.decision_id == decision.id
    assert closed.activation_changeset_id == decision.activation_changeset_id
    assert clarification_harness.proposal_store.session(
        proposal.clarification_session_id
    ).status == "closed"


def test_confirmation_replay_is_an_exact_semantic_noop(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose()
    first = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    before = clarification_harness.state_bytes()
    second = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    assert second == first
    assert clarification_harness.state_bytes() == before


def test_author_cannot_self_approve_update_and_graph_remains_unchanged(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose(
        conflict=True, conflicting_authors=("product:priya",)
    )
    graph_before = clarification_harness.paths["graph"].read_bytes()
    result = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )

    assert result.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    assert result.case_id is not None and result.case_id.startswith("case:sha256:")
    assert clarification_harness.paths["graph"].read_bytes() == graph_before
    assert clarification_harness.proposal_store.decision_for(proposal.id) is None
    assert (
        clarification_harness.case_store.get(result.case_id).status
        is ReconciliationStatus.NEEDS_HUMAN
    )


def test_independent_reviewer_applies_update_and_resolves_same_case_atomically(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose(
        conflict=True, conflicting_authors=("product:priya",)
    )
    blocked = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    result = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
    )

    assert result.status is ProposalConfirmationStatus.APPLIED
    assert result.case_id == blocked.case_id
    decision = clarification_harness.proposal_store.decision_for(proposal.id)
    assert isinstance(decision, ProposalDecisionV3)
    assert decision.review_case_id == blocked.case_id
    assert decision.actor_aliases == ("github:ben", "jira:ben", "local:ben")
    case = clarification_harness.case_store.get(blocked.case_id or "")
    assert case.status is ReconciliationStatus.RESOLVED
    assert case.resolved_by_changeset == decision.activation_changeset_id
    assert clarification_harness.graph_store.load().nodes[1].label == (
        "Upload raw conversations for sharing"
    )
    updated = clarification_harness.graph_store.load().nodes[1]
    assert updated.created_by == "product:priya"
    assert updated.created_at == NOW - timedelta(days=1)
    assert updated.source_mode.value == "explicit"
    assert updated.intent_fidelity_confidence == 0.95
    assert updated.confidence_basis == "Approved PRD"
    assert updated.last_reassessed_at == NOW - timedelta(days=1)
    assert updated.evidence_refs[0] == "evidence:base"
    assert tuple(side.label for side in case.evidence_sides) == ("current", "proposal")
    assert tuple(side.current for side in case.evidence_sides) == (True, False)
    assert case.evidence_sides[0].evidence_refs == ("evidence:base",)
    assert case.evidence_sides[0].authors == ("product:priya",)
    assert case.evidence_sides[1].evidence_refs == proposal.evidence_refs
    assert set(case.evidence_sides[0].evidence_refs).isdisjoint(
        case.evidence_sides[1].evidence_refs
    )


def test_applied_high_risk_confirmation_exact_replay_is_a_detached_byte_noop(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose(
        conflict=True, conflicting_authors=("product:priya",)
    )
    blocked = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    first = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
    )
    before = clarification_harness.state_bytes()
    graph_before = clarification_harness.graph_store.load()

    replay = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
    )

    assert replay == first
    assert replay is not first
    assert replay.status is ProposalConfirmationStatus.APPLIED
    assert replay.case_id == blocked.case_id
    graph_after = clarification_harness.graph_store.load()
    assert graph_after == graph_before
    assert graph_after is not graph_before
    assert clarification_harness.state_bytes() == before


@pytest.mark.parametrize("attack", ["actor", "time", "subset", "policy", "stale"])
def test_applied_high_risk_replay_conflicts_remain_fixed_failures(
    clarification_harness: ClarificationHarness,
    attack: str,
) -> None:
    proposal = clarification_harness.propose(
        conflicting_authors=("product:priya",)
    )
    clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    clarification_harness.confirmation.confirm(
        proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
    )
    actor = "local:ben"
    at = NOW + timedelta(microseconds=8)
    selected_node_ids: tuple[str, ...] = ()
    if attack == "actor":
        actor = "local:asha"
    elif attack == "time":
        at = NOW + timedelta(microseconds=9)
    elif attack == "subset":
        selected_node_ids = ("unknown-node",)
    elif attack == "policy":
        policy = _policy_payload()
        policy["approvers"] = []
        clarification_harness.paths["policy"].write_text(
            yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
        )
    else:
        graph = clarification_harness.graph_store.load()
        clarification_harness.graph_store.initialize(
            graph.model_copy(update={"version": graph.version + 1})
        )
    before = clarification_harness.state_bytes()

    with pytest.raises(ClarificationError):
        clarification_harness.confirmation.confirm(
            proposal.id,
            actor=actor,
            at=at,
            selected_node_ids=selected_node_ids,
        )

    assert clarification_harness.state_bytes() == before


def _apply_high_risk_confidence_change(
    harness: ClarificationHarness,
) -> object:
    session = harness.answer_required(harness.open())
    submission = harness.submission(session)
    confidence_change = ConfidenceChange(
        change_id="confidence:req-local:replay",
        timestamp=submission.timestamp,
        actor=submission.actor,
        subject_ref="req-local",
        change_kind=ChangeKind.REFINE,
        prior_confidence=0.95,
        new_confidence=0.7,
        evidence_refs=submission.evidence_refs,
        reason="Replay-authenticated confidence change",
    )
    changeset = submission.changeset.model_copy(
        update={"nodes_added": (), "confidence_changes": (confidence_change,)}
    )
    proposal = harness.coordinator.propose(
        submission.model_copy(update={"changeset": changeset, "core_node_ids": ()}),
        principals=harness.principals,
    )
    harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    harness.confirmation.confirm(
        proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
    )
    return proposal


@pytest.mark.parametrize("tamper", ["label", "evidence-inject", "evidence-remove", "evidence-reorder"])
def test_applied_replay_rejects_same_version_complete_node_tampering(
    clarification_harness: ClarificationHarness,
    tamper: str,
) -> None:
    proposal = _apply_high_risk_confidence_change(clarification_harness)
    graph = clarification_harness.graph_store.load()
    current = next(node for node in graph.nodes if node.id == "req-local")
    if tamper == "label":
        changed = current.model_copy(update={"label": "FORGED SAME-VERSION LABEL"})
    elif tamper == "evidence-inject":
        hidden = _base_evidence(acl=("product:priya",)).model_copy(
            update={
                "id": "evidence:hidden-replay",
                "external_object_id": "docs/hidden-replay.md",
                "external_version": "hidden-v1",
                "source_locator": "docs/hidden-replay.md",
            }
        )
        clarification_harness.evidence_store.associate("markdown", hidden)
        changed = current.model_copy(
            update={"evidence_refs": (*current.evidence_refs, hidden.id)}
        )
    elif tamper == "evidence-remove":
        changed = current.model_copy(
            update={
                "evidence_refs": tuple(
                    reference
                    for reference in current.evidence_refs
                    if reference != "evidence:base"
                )
            }
        )
    else:
        changed = current.model_copy(update={"evidence_refs": current.evidence_refs[::-1]})
    clarification_harness.graph_store.initialize(
        graph.model_copy(
            update={
                "nodes": tuple(
                    changed if node.id == changed.id else node for node in graph.nodes
                )
            }
        )
    )
    before = clarification_harness.state_bytes()

    with pytest.raises(ClarificationError):
        clarification_harness.confirmation.confirm(
            proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
        )

    assert clarification_harness.state_bytes() == before


@pytest.mark.parametrize("forgery", ["impact", "subject", "affected", "evidence-side"])
def test_applied_replay_rejects_coordinated_review_case_version_forgery(
    clarification_harness: ClarificationHarness,
    forgery: str,
) -> None:
    proposal = clarification_harness.propose(
        conflict=True, conflicting_authors=("product:priya",)
    )
    clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    clarification_harness.confirmation.confirm(
        proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
    )
    path = clarification_harness.paths["cases"]
    versions = parse_case_versions(path.read_bytes())
    assert len(versions) == 2
    forged = []
    for case in versions:
        if forgery == "impact":
            update = {"impact": "FORGED CASE IMPACT"}
        elif forgery == "subject":
            update = {"subject_ref": "forged:subject"}
        elif forgery == "affected":
            update = {"affected_refs": (*case.affected_refs, "forged:affected")}
        else:
            sides = tuple(
                side.model_copy(update={"claim": f"FORGED {side.claim}"})
                for side in case.evidence_sides
            )
            update = {"evidence_sides": sides}
        forged.append(case.model_copy(update=update))
    path.write_bytes(b"".join(serialize_case(case) for case in forged))
    before = clarification_harness.state_bytes()

    with pytest.raises(ClarificationError):
        clarification_harness.confirmation.confirm(
            proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
        )

    assert clarification_harness.state_bytes() == before


def test_review_case_preserves_derived_current_and_proposed_update_epistemics(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        requirement_source_mode=SourceMode.DERIVED,
        requirement_confidence=0.63,
    )
    proposal = harness.propose(conflict=True)
    result = harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )

    assert result.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    sides = harness.case_store.get(result.case_id or "").evidence_sides
    assert tuple(side.label for side in sides) == ("current", "proposal")
    assert tuple(side.source_mode for side in sides) == (
        SourceMode.DERIVED,
        SourceMode.DERIVED,
    )
    assert tuple(side.confidence for side in sides) == (0.63, 0.63)


def test_review_case_preserves_inferred_proposed_node_epistemics(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose(
        conflicting_authors=("product:priya",)
    )
    result = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )

    assert result.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    case = clarification_harness.case_store.get(result.case_id or "")
    assert case.affected_refs == ("req-read-only-sharing",)
    assert tuple(side.label for side in case.evidence_sides) == ("proposal",)
    assert case.evidence_sides[0].current is False
    assert case.evidence_sides[0].source_mode is SourceMode.INFERRED
    assert case.evidence_sides[0].confidence == 0.8
    assert case.evidence_sides[0].evidence_refs == proposal.evidence_refs


def test_review_case_rejects_current_evidence_outside_actor_acl_without_mutation(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, current_evidence_acl=("product:priya",))
    proposal = harness.propose(conflict=True)
    before = harness.state_bytes()

    with pytest.raises(ClarificationError):
        harness.confirmation.confirm(
            proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
        )

    assert harness.state_bytes() == before


def test_live_alias_drift_blocks_cross_provider_self_approval_before_graph_mutation(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose(conflict=True)
    blocked = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    assert blocked.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    policy = _policy_payload()
    identities = policy["identities"]
    assert isinstance(identities, dict)
    identities["local:ben"] = ["github:asha", "local:ben"]
    clarification_harness.paths["policy"].write_text(
        yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
    )
    before = clarification_harness.state_bytes()

    with pytest.raises(ClarificationError):
        clarification_harness.confirmation.confirm(
            proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
        )

    assert clarification_harness.state_bytes() == before


def test_review_case_alias_revocation_is_reauthenticated_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, current_evidence_acl=("jira:ben",))
    proposal = harness.propose(conflict=True)
    original = harness.confirmation._ensure_case
    state_after_revocation: dict[str, bytes | None] = {}

    def ensure_then_revoke(
        snapshot: LocalTransactionSnapshot,
        candidate: ReconciliationCase,
    ) -> ReconciliationCase:
        ensured = original(snapshot, candidate)
        policy = _policy_payload()
        identities = policy["identities"]
        assert isinstance(identities, dict)
        identities["local:ben"] = ["local:ben"]
        harness.paths["policy"].write_text(
            yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
        )
        state_after_revocation.update(harness.state_bytes())
        return ensured

    monkeypatch.setattr(harness.confirmation, "_ensure_case", ensure_then_revoke)

    with pytest.raises(ClarificationError):
        harness.confirmation.confirm(
            proposal.id, actor="local:ben", at=NOW + timedelta(microseconds=8)
        )

    assert state_after_revocation
    assert harness.state_bytes() == state_after_revocation
    assert harness.graph_store.load().version == proposal.baseline_graph_version
    assert harness.proposal_store.decision_for(proposal.id) is None


def test_revoked_contributor_is_rejected_using_live_policy(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose()
    policy = _policy_payload()
    policy["contributors"] = ["local:ben"]
    clarification_harness.paths["policy"].write_text(
        yaml.safe_dump(policy, sort_keys=True), encoding="utf-8"
    )
    before = clarification_harness.state_bytes()

    with pytest.raises(ClarificationError):
        clarification_harness.confirmation.confirm(
            proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
        )

    assert clarification_harness.state_bytes() == before


def test_two_identical_concurrent_confirmations_converge_on_one_decision(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose()

    def confirm() -> object:
        return clarification_harness.confirmation.confirm(
            proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: confirm(), range(2)))
    assert results[0] == results[1]
    assert clarification_harness.graph_store.load().version == 3
    assert clarification_harness.proposal_store.bytes().count(b'"decision":{') == 1


def test_closed_ledger_without_activation_never_returns_applied(
    clarification_harness: ClarificationHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = clarification_harness.propose(conflict=True)
    blocked = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    assert blocked.case_id is not None
    case = clarification_harness.case_store.get(blocked.case_id)
    actor = "local:ben"
    decided_at = NOW + timedelta(microseconds=8)
    activation = clarification_harness.confirmation._activation(
        proposal, actor, decided_at, (), case.id
    )
    expected_graph = apply_changeset_with_case_effects(
        clarification_harness.graph_store.load(), activation
    )
    decision = clarification_harness.confirmation._decision(
        proposal,
        activation,
        actor,
        ("github:ben", "jira:ben", "local:ben"),
        ("github:asha", "local:asha", "slack:asha"),
        (),
        decided_at,
        (),
        case.id,
        clarification_harness.confirmation._activation_graph_effect_digest(
            expected_graph, activation
        ),
        _digest(case.model_dump(mode="json")),
    )
    proposed_session = clarification_harness.proposal_store.session(
        proposal.clarification_session_id
    )
    closed = _event(
        proposed_session.model_copy(update={"status": "closed"}),
        "closed",
        actor,
        decided_at,
        proposed_session.latest_event_id,
        proposal.id,
        decision.id,
        activation.id,
    )
    original_session = clarification_harness.proposal_store.session
    state_after_injection: dict[str, bytes | None] = {}

    def inject_torn_state(session_id: str):
        ledger = clarification_harness.proposal_store.bytes()
        sequence = len(ledger.splitlines())
        frames = serialize_intent_ledger_record(
            IntentLedgerRecord(sequence=sequence, decision=decision)
        ) + serialize_intent_ledger_record(
            IntentLedgerRecord(sequence=sequence + 1, clarification=closed)
        )
        clarification_harness.paths["intent_proposals"].write_bytes(ledger + frames)
        state_after_injection.update(clarification_harness.state_bytes())
        monkeypatch.setattr(
            clarification_harness.proposal_store, "session", original_session
        )
        return original_session(session_id)

    monkeypatch.setattr(
        clarification_harness.proposal_store, "session", inject_torn_state
    )

    with pytest.raises(ClarificationError):
        clarification_harness.confirmation.confirm(
            proposal.id, actor=actor, at=decided_at
        )

    assert state_after_injection
    assert clarification_harness.state_bytes() == state_after_injection
    assert clarification_harness.graph_store.load().version == proposal.baseline_graph_version
    assert clarification_harness.paths["history"].read_bytes() == b""
    assert clarification_harness.case_store.get(case.id).status is ReconciliationStatus.NEEDS_HUMAN


@pytest.mark.parametrize(
    "stage",
    [
        "journal_prepared",
        "target:intent_proposals",
        "target:graph",
        "target:history",
        "journal_committed",
    ],
)
def test_confirmation_crash_rolls_back_decision_graph_and_history(
    clarification_harness: ClarificationHarness,
    stage: str,
) -> None:
    proposal = clarification_harness.propose()
    before = clarification_harness.state_bytes()

    def fail(current: str) -> None:
        if current == stage:
            raise RuntimeError("PRIVATE-CONFIRMATION-FAILURE")

    clarification_harness.transactions._fault_hook = fail
    with pytest.raises(ClarificationError) as caught:
        clarification_harness.confirmation.confirm(
            proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
        )
    assert caught.value.args == ("intent clarification unavailable",)
    assert caught.value.__context__ is None
    assert clarification_harness.state_bytes() == before


def test_confirmation_cancellation_preserves_identity_state_and_secrecy(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose()
    before = clarification_harness.state_bytes()
    signal = CancellationSignal()

    def cancel(current: str) -> None:
        if current == "target:graph":
            raise signal

    clarification_harness.transactions._fault_hook = cancel
    with pytest.raises(CancellationSignal) as caught:
        clarification_harness.confirmation.confirm(
            proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
        )
    assert caught.value is signal
    assert caught.value.__context__ is None
    assert clarification_harness.state_bytes() == before
    rendered = _repository_traceback_locals(caught.value)
    for private in (proposal.id, "local:asha", "Workspace admins may share"):
        assert private not in rendered


def test_malformed_live_binding_denies_before_mutation_without_leaking_payload(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose()
    binding_path = clarification_harness.paths["graph"].parent / "binding.yaml"
    binding_path.write_text("private_alias: PRIVATE-BINDING-8197\n", encoding="utf-8")
    config_file = clarification_harness.directory.file("config.yaml")
    policy_file = clarification_harness.directory.file("approvals/policy.yaml")
    binding_file = clarification_harness.directory.file("binding.yaml")
    service = ProposalConfirmationService(
        graph_store=clarification_harness.graph_store,
        evidence_store=clarification_harness.evidence_store,
        case_store=clarification_harness.case_store,
        proposal_store=clarification_harness.proposal_store,
        changeset_executor=clarification_harness.confirmation._executor,
        transactions=clarification_harness.transactions,
        config_file=config_file,
        policy_file=policy_file,
        binding_files={"hostile": binding_file},
    )
    config_file.close()
    policy_file.close()
    binding_file.close()
    before = clarification_harness.state_bytes()
    try:
        with pytest.raises(ClarificationError) as caught:
            service.confirm(
                proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
            )
        assert caught.value.__context__ is None
        assert "PRIVATE-BINDING-8197" not in _repository_traceback_locals(caught.value)
        assert clarification_harness.state_bytes() == before
    finally:
        service.close()


def test_destructive_flag_is_deterministically_review_gated(
    clarification_harness: ClarificationHarness,
) -> None:
    session = clarification_harness.answer_required(clarification_harness.open())
    proposal = clarification_harness.coordinator.propose(
        clarification_harness.submission(session).model_copy(update={"destructive": True}),
        principals=clarification_harness.principals,
    )
    graph_before = clarification_harness.paths["graph"].read_bytes()
    result = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    assert result.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    assert result.case_id is not None
    assert clarification_harness.paths["graph"].read_bytes() == graph_before


def test_stale_high_risk_confirmation_fails_before_review_case_creation(
    clarification_harness: ClarificationHarness,
) -> None:
    proposal = clarification_harness.propose(conflict=True)
    clarification_harness.graph_store.apply(
        proposal.changeset.model_copy(update={"id": "changeset:external-stale-risk"})
    )
    before = clarification_harness.state_bytes()
    with pytest.raises(ClarificationError):
        clarification_harness.confirmation.confirm(
            proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
        )
    assert clarification_harness.state_bytes() == before


@pytest.mark.parametrize(
    ("risk_group", "expected_refs"),
    [
        ("edge", ("intent-local", "req-local")),
        ("confidence", ("req-local",)),
        ("implementation", ("req-local",)),
    ],
)
def test_review_case_names_every_exact_edge_confidence_or_status_subject(
    clarification_harness: ClarificationHarness,
    risk_group: str,
    expected_refs: tuple[str, ...],
) -> None:
    session = clarification_harness.answer_required(clarification_harness.open())
    submission = clarification_harness.submission(session)
    base = submission.changeset.model_copy(
        update={
            "nodes_added": (),
        }
    )
    updates: dict[str, object]
    if risk_group == "edge":
        updates = {
            "edges_added": (
                Edge(
                    id="edge:clarified-conflict",
                    **{"from": "intent-local", "to": "req-local"},
                    relation=RelationType.CONTRADICTS,
                    status="proposed",
                    created_by=submission.actor,
                    created_at=submission.timestamp,
                    last_modified_by=submission.actor,
                    last_modified_at=submission.timestamp,
                ),
            )
        }
    elif risk_group == "confidence":
        updates = {
            "confidence_changes": (
                ConfidenceChange(
                    change_id="confidence:req-local:clarification",
                    timestamp=submission.timestamp,
                    actor=submission.actor,
                    subject_ref="req-local",
                    change_kind=ChangeKind.REFINE,
                    prior_confidence=0.95,
                    new_confidence=0.7,
                    evidence_refs=submission.evidence_refs,
                    reason="Clarification requires independent review",
                ),
            )
        }
    else:
        updates = {
            "implementation_status_changes": (
                ImplementationStatusChange(
                    claim_id="req-local",
                    prior=ImplementationStatus.UNKNOWN,
                    new=ImplementationStatus.PARTIAL,
                    evidence_refs=submission.evidence_refs,
                ),
            )
        }
    changed = base.model_copy(update=updates)
    proposal = clarification_harness.coordinator.propose(
        submission.model_copy(update={"changeset": changed, "core_node_ids": ()}),
        principals=clarification_harness.principals,
    )
    result = clarification_harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )
    assert result.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    case = clarification_harness.case_store.get(result.case_id or "")
    assert case.subject_ref == expected_refs[0]
    assert case.affected_refs == expected_refs
    assert tuple(side.label for side in case.evidence_sides) == ("current", "proposal")
    assert tuple(side.current for side in case.evidence_sides) == (True, False)
    assert case.evidence_sides[0].evidence_refs == ("evidence:base",)
    assert case.evidence_sides[0].authors == ("product:priya",)
    assert case.evidence_sides[1].evidence_refs == proposal.evidence_refs
    assert case.evidence_sides[0].source_mode is SourceMode.EXPLICIT
    assert case.evidence_sides[0].confidence == 0.95
    assert case.evidence_sides[1].source_mode is SourceMode.EXPLICIT
    assert case.evidence_sides[1].confidence == (
        0.7 if risk_group == "confidence" else 0.95
    )
    assert set(case.evidence_sides[0].evidence_refs).isdisjoint(
        case.evidence_sides[1].evidence_refs
    )


def test_mixed_edge_assertions_emit_stable_separate_epistemic_sides(tmp_path: Path) -> None:
    harness = _harness(
        tmp_path,
        requirement_source_mode=SourceMode.DERIVED,
        requirement_confidence=0.63,
    )
    session = harness.answer_required(harness.open())
    submission = harness.submission(session)
    edge = Edge(
        id="edge:clarified-conflict",
        **{"from": "intent-local", "to": "req-local"},
        relation=RelationType.CONTRADICTS,
        status="proposed",
        created_by=submission.actor,
        created_at=submission.timestamp,
        last_modified_by=submission.actor,
        last_modified_at=submission.timestamp,
    )
    changeset = submission.changeset.model_copy(
        update={"nodes_added": (), "edges_added": (edge,)}
    )
    proposal = harness.coordinator.propose(
        submission.model_copy(update={"changeset": changeset, "core_node_ids": ()}),
        principals=harness.principals,
    )
    result = harness.confirmation.confirm(
        proposal.id, actor="local:asha", at=NOW + timedelta(microseconds=7)
    )

    assert result.status is ProposalConfirmationStatus.REVIEW_REQUIRED
    sides = harness.case_store.get(result.case_id or "").evidence_sides
    assert tuple(side.label for side in sides) == (
        "current:intent-local",
        "current:req-local",
        "proposal:intent-local",
        "proposal:req-local",
    )
    assert tuple(side.source_mode for side in sides) == (
        SourceMode.EXPLICIT,
        SourceMode.DERIVED,
        SourceMode.EXPLICIT,
        SourceMode.DERIVED,
    )
    assert tuple(side.confidence for side in sides) == (0.95, 0.63, 0.95, 0.63)
    assert tuple(side.current for side in sides) == (True, True, False, False)
