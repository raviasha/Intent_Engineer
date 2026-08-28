"""Focused authority-read policy contracts for proposal confirmation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.intent_workflow.clarification import (
    ProposalConfirmationService,
    ProposalConfirmationStatus,
)
from intent_engineering.storage.transaction import LocalTransactionExtraReadPolicy
from tests.integration.intent_workflow.test_clarification import NOW, _harness


def test_confirmation_without_policy_preserves_legacy_defaults(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    proposal = harness.propose()

    result = harness.confirmation.confirm(
        proposal.id,
        actor="local:asha",
        at=NOW + timedelta(microseconds=7),
    )

    assert result.status is ProposalConfirmationStatus.APPLIED


@pytest.mark.parametrize("confirmation_path", ["apply", "review", "review_approved"])
def test_confirmation_propagates_one_exact_policy_through_every_authority_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    confirmation_path: str,
) -> None:
    harness = _harness(tmp_path)
    proposal = harness.propose(conflict=confirmation_path != "apply")
    binding_path = harness.paths["graph"].parent / "binding.yaml"
    binding = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "profiles/mcp/example-bindings/jira.yaml").read_text(
            encoding="utf-8"
        )
    )
    binding["binding"]["actor_principals"] = {"unused": ["provider:unused"]}
    binding_path.write_text(yaml.safe_dump(binding, sort_keys=True), encoding="utf-8")
    config_file = harness.directory.file("config.yaml")
    policy_file = harness.directory.file("approvals/policy.yaml")
    binding_file = harness.directory.file("binding.yaml")
    policies = {
        "authority_config": LocalTransactionExtraReadPolicy(
            max_bytes=1_048_576,
            nonblocking_regular=True,
        ),
        "authority_policy": LocalTransactionExtraReadPolicy(
            max_bytes=1_048_576,
            nonblocking_regular=True,
        ),
        "authority_binding_0": LocalTransactionExtraReadPolicy(
            max_bytes=1_048_576,
            nonblocking_regular=True,
            aggregate_group="connector_bindings",
            max_aggregate_bytes=8_388_608,
        ),
    }
    service = ProposalConfirmationService(
        graph_store=harness.graph_store,
        evidence_store=harness.evidence_store,
        case_store=harness.case_store,
        proposal_store=harness.proposal_store,
        changeset_executor=harness.confirmation._executor,
        transactions=harness.transactions,
        config_file=config_file,
        policy_file=policy_file,
        binding_files={"binding": binding_file},
        authority_read_policies=policies,
    )
    config_file.close()
    policy_file.close()
    binding_file.close()
    held_policies = service._authority_read_policies
    snapshot_policies: list[object] = []
    executor_policies: list[object] = []
    transaction_policies: list[object] = []
    original_snapshot = harness.transactions.snapshot
    original_transaction = harness.transactions.transaction
    original_apply = service._executor.apply

    def snapshot_with_observation(*args: object, **kwargs: object):
        if args and isinstance(args[0], dict) and set(args[0]) == set(policies):
            snapshot_policies.append(kwargs.get("extra_read_policies"))
        return original_snapshot(*args, **kwargs)  # type: ignore[arg-type]

    def apply_with_observation(*args: object, **kwargs: object):
        executor_policies.append(kwargs.get("extra_read_policies"))
        return original_apply(*args, **kwargs)  # type: ignore[arg-type]

    @contextmanager
    def transaction_with_observation(*args: object, **kwargs: object):
        extras = kwargs.get("extras")
        if isinstance(extras, dict) and set(extras) == set(policies):
            transaction_policies.append(kwargs.get("extra_read_policies"))
        with original_transaction(*args, **kwargs) as transaction:  # type: ignore[arg-type]
            yield transaction

    monkeypatch.setattr(harness.transactions, "snapshot", snapshot_with_observation)
    monkeypatch.setattr(harness.transactions, "transaction", transaction_with_observation)
    monkeypatch.setattr(service._executor, "apply", apply_with_observation)
    try:
        if confirmation_path == "review_approved":
            review = service.confirm(
                proposal.id,
                actor="local:asha",
                at=NOW + timedelta(microseconds=7),
            )
            assert review.status is ProposalConfirmationStatus.REVIEW_REQUIRED
            snapshot_policies.clear()
            executor_policies.clear()
            transaction_policies.clear()
        result = service.confirm(
            proposal.id,
            actor="local:ben" if confirmation_path == "review_approved" else "local:asha",
            at=NOW + timedelta(microseconds=8 if confirmation_path == "review_approved" else 7),
        )
        expected_status = (
            ProposalConfirmationStatus.REVIEW_REQUIRED
            if confirmation_path == "review"
            else ProposalConfirmationStatus.APPLIED
        )
        expected_snapshots = {"apply": 2, "review": 1, "review_approved": 3}
        assert result.status is expected_status
        assert held_policies == policies
        assert len(snapshot_policies) == expected_snapshots[confirmation_path]
        assert all(item is held_policies for item in snapshot_policies)
        assert transaction_policies == [held_policies]
        assert transaction_policies[0] is held_policies
        expected_executor_policies = [] if confirmation_path == "review" else [held_policies]
        assert executor_policies == expected_executor_policies
        assert all(item is held_policies for item in executor_policies)
    finally:
        service.close()
