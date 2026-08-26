"""Contracts for independent interactive approvals and append-only storage."""

from __future__ import annotations

import json
import os
import traceback
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from intent_engineering.capture.mcp import ProviderBinding
from intent_engineering.core.models import ResolutionAction
from intent_engineering.mutations.approval import ApprovalError, approve_plan
from intent_engineering.mutations.models import (
    ApprovalRecord,
    WritePlan,
    approval_id,
    write_plan_id,
)
from intent_engineering.mutations.planner import build_write_plan
from intent_engineering.storage.jsonl.approval_store import (
    JsonlApprovalStore,
    JsonlWritePlanStore,
    MutationStoreError,
)
from intent_engineering.storage.secure import UnsafePathError
from tests.unit.mutations.test_planner import (
    IDENTITY_ALIASES,
    NOW,
    base_plan,
    jira_binding,
    jira_profile,
    remote_object,
    review_case,
)


def _approve(plan: WritePlan | None = None, *, actor: str = "local:reviewer") -> ApprovalRecord:
    selected = plan or base_plan()
    return approve_plan(
        selected,
        jira_binding(),
        actor=actor,
        now=NOW + timedelta(minutes=1),
        expires_in=timedelta(minutes=10),
        confirmation=f"approve {selected.id}",
        interactive=True,
        authorized_approvers=frozenset({"local:reviewer", "local:security"}),
        identity_aliases=IDENTITY_ALIASES,
    )


def test_approval_binds_exact_plan_target_approver_and_expiry() -> None:
    plan = base_plan()
    approval = _approve(plan)

    assert approval.id.startswith("approval:sha256:")
    assert approval.plan_id == plan.id
    assert approval.plan_hash == plan.canonical_hash
    assert approval.target_version == plan.before_version
    assert approval.actor == "local:reviewer"
    assert approval.actor_aliases == (
        "jira-account-404",
        "local:reviewer",
        "slack-user-404",
    )
    assert approval.approved_at == NOW + timedelta(minutes=1)
    assert approval.expires_at == NOW + timedelta(minutes=11)
    assert approval.confirmation_method == "interactive"
    assert ApprovalRecord.model_validate_json(approval.model_dump_json()) == approval


@pytest.mark.parametrize(
    ("actor", "interactive", "confirmation", "authorized"),
    [
        ("local:proposer", True, None, frozenset({"local:proposer"})),
        ("local:alice", True, None, frozenset({"local:alice"})),
        ("local:outsider", True, None, frozenset({"local:reviewer"})),
        ("local:reviewer", False, None, frozenset({"local:reviewer"})),
        ("local:reviewer", True, "approve wrong-plan", frozenset({"local:reviewer"})),
    ],
)
def test_approval_requires_independent_authorized_exact_interactive_confirmation(
    actor: str,
    interactive: bool,
    confirmation: str | None,
    authorized: frozenset[str],
) -> None:
    plan = base_plan()

    with pytest.raises(ApprovalError) as caught:
        approve_plan(
            plan,
            jira_binding(),
            actor=actor,
            now=NOW + timedelta(minutes=1),
            expires_in=timedelta(minutes=10),
            confirmation=confirmation or f"approve {plan.id}",
            interactive=interactive,
            authorized_approvers=authorized,
            identity_aliases=IDENTITY_ALIASES,
        )

    assert caught.value.args == ("external write approval rejected",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_expired_plan_or_invalid_approval_window_is_rejected() -> None:
    plan = base_plan()

    for now, duration in (
        (plan.expires_at, timedelta(minutes=1)),
        (NOW + timedelta(minutes=1), timedelta(0)),
        (NOW + timedelta(minutes=1), timedelta(hours=2)),
    ):
        with pytest.raises(ApprovalError):
            approve_plan(
                plan,
                jira_binding(),
                actor="local:reviewer",
                now=now,
                expires_in=duration,
                confirmation=f"approve {plan.id}",
                interactive=True,
                authorized_approvers=frozenset({"local:reviewer"}),
                identity_aliases=IDENTITY_ALIASES,
            )

    with pytest.raises(ApprovalError):
        approve_plan(
            plan,
            jira_binding(),
            actor="local:reviewer",
            now=plan.created_at - timedelta(seconds=1),
            expires_in=timedelta(minutes=1),
            confirmation=f"approve {plan.id}",
            interactive=True,
            authorized_approvers=frozenset({"local:reviewer"}),
            identity_aliases=IDENTITY_ALIASES,
        )


@pytest.mark.parametrize(
    "conflicting_author",
    ["local:reviewer", "slack-user-404"],
)
def test_approval_rejects_exact_local_and_cross_provider_author_aliases(
    conflicting_author: str,
) -> None:
    plan = build_write_plan(
        review_case(first_author=conflicting_author),
        jira_profile(),
        jira_binding(),
        "update_issue",
        remote_object(),
        {"summary": "independent review required"},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=NOW,
    )
    with pytest.raises(ApprovalError):
        _approve(plan)


def test_plan_and_approval_stores_are_append_only_idempotent_and_descriptor_safe(
    tmp_path: Path,
) -> None:
    plan = base_plan()
    approval = _approve(plan)
    plans = JsonlWritePlanStore(tmp_path / "plans.jsonl")
    approvals = JsonlApprovalStore(tmp_path / "approvals.jsonl")

    assert plans.put(plan) is True
    assert plans.put(plan) is False
    assert approvals.put(approval) is True
    assert approvals.put(approval) is False
    assert plans.get(plan.id) == plan
    assert approvals.get(approval.id) == approval
    assert plans.list() == (plan,)
    assert approvals.list() == (approval,)
    assert (tmp_path / "plans.jsonl").read_bytes().endswith(b"\n")
    assert (tmp_path / "approvals.jsonl").read_bytes().endswith(b"\n")

    tampered = approval.model_copy(update={"actor": "local:security"})
    with pytest.raises(MutationStoreError):
        approvals.put(tampered)

    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(b"")
    symlink = tmp_path / "symlink.jsonl"
    symlink.symlink_to(outside)
    with pytest.raises(UnsafePathError):
        JsonlApprovalStore(symlink)
    hardlink = tmp_path / "hardlink.jsonl"
    os.link(outside, hardlink)
    with pytest.raises(UnsafePathError):
        JsonlApprovalStore(hardlink)


def test_store_rejects_duplicate_keys_unknown_fields_and_conflicting_bytes(tmp_path: Path) -> None:
    plan = base_plan()
    path = tmp_path / "plans.jsonl"
    store = JsonlWritePlanStore(path)
    assert store.put(plan)
    raw = plan.model_dump(mode="json")
    raw["created_by"] = "different"
    path.write_text(
        json.dumps(plan.model_dump(mode="json"), separators=(",", ":"))
        + "\n"
        + json.dumps(raw, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MutationStoreError):
        JsonlWritePlanStore(path)

    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text('{"id":"one","id":"two"}\n', encoding="utf-8")
    with pytest.raises(MutationStoreError):
        JsonlWritePlanStore(duplicate)

    unknown = tmp_path / "unknown.jsonl"
    unknown.write_text(
        json.dumps({**plan.model_dump(mode="json"), "unknown": True}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(MutationStoreError):
        JsonlWritePlanStore(unknown)

    reused = tmp_path / "reused.jsonl"
    compact = json.dumps(plan.model_dump(mode="json"), separators=(",", ":"))
    differently_encoded = json.dumps(plan.model_dump(mode="json"), separators=(", ", ": "))
    reused.write_text(f"{compact}\n{differently_encoded}\n", encoding="utf-8")
    with pytest.raises(MutationStoreError):
        JsonlWritePlanStore(reused)


def test_public_models_reject_unknown_fields_and_model_copy_validation_bypass() -> None:
    approval = _approve()
    payload = approval.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate_json(json.dumps(payload))

    invalid = approval.model_copy(update={"plan_hash": "sha256:" + "0" * 64})
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate_json(invalid.model_dump_json())


def test_approval_record_rejects_mismatched_plan_hash_and_unbounded_window() -> None:
    approval = _approve()
    mismatched_material = approval.model_dump(mode="json", exclude={"id"})
    mismatched_material["plan_hash"] = "sha256:" + "1" * 64
    mismatched = {**mismatched_material, "id": approval_id(mismatched_material)}
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate_json(json.dumps(mismatched))

    long_material = approval.model_dump(mode="json", exclude={"id"})
    long_material["expires_at"] = (
        (approval.approved_at + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    )
    long_window = {**long_material, "id": approval_id(long_material)}
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate_json(json.dumps(long_window))

    missing_actor_material = approval.model_dump(mode="json", exclude={"id"})
    missing_actor_material["actor_aliases"] = ["jira-account-404"]
    missing_actor = {
        **missing_actor_material,
        "id": approval_id(missing_actor_material),
    }
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate_json(json.dumps(missing_actor))


def test_approval_reauthenticates_reloaded_plan_creator_aliases() -> None:
    binding_payload = jira_binding().model_dump(mode="json")
    binding_payload["actor_principals"]["local:proposer2"] = ["jira-account-303"]
    binding = ProviderBinding.model_validate(binding_payload)
    aliases = {
        **IDENTITY_ALIASES,
        "local:proposer2": frozenset({"local:proposer2", "jira-account-303"}),
    }
    plan = build_write_plan(
        review_case(),
        jira_profile(),
        binding,
        "update_issue",
        remote_object(),
        {"summary": "same person must not approve"},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=aliases,
        resolution_action=ResolutionAction.UPDATE_REQUIREMENT,
        now=NOW,
    )
    material = plan.model_dump(mode="json", exclude={"id"})
    material["created_by_aliases"] = ["local:proposer"]
    tampered = WritePlan.model_validate_json(
        json.dumps({**material, "id": write_plan_id(material)})
    )

    with pytest.raises(ApprovalError):
        approve_plan(
            tampered,
            binding,
            actor="local:proposer2",
            now=NOW + timedelta(minutes=1),
            expires_in=timedelta(minutes=10),
            confirmation=f"approve {tampered.id}",
            interactive=True,
            authorized_approvers=frozenset({"local:proposer2"}),
            identity_aliases=aliases,
        )


def test_store_corruption_error_is_fixed_and_does_not_retain_persisted_values(
    tmp_path: Path,
) -> None:
    sentinel = "PRIVATE-PERSISTED-WRITE-SECRET"
    path = tmp_path / "plans.jsonl"
    path.write_text(f'{{"secret":"{sentinel}"}}\n', encoding="utf-8")

    with pytest.raises(MutationStoreError) as caught:
        JsonlWritePlanStore(path)

    rendered = traceback.TracebackException.from_exception(caught.value, capture_locals=True)
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("invalid mutation store",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert sentinel not in repository_locals

    clean_store = JsonlWritePlanStore(tmp_path / "clean-plans.jsonl")
    invalid = base_plan().model_copy(update={"created_by": sentinel})
    with pytest.raises(MutationStoreError) as invalid_caught:
        clean_store.put(invalid)
    invalid_rendered = traceback.TracebackException.from_exception(
        invalid_caught.value,
        capture_locals=True,
    )
    invalid_locals = "\n".join(
        str(frame.locals)
        for frame in invalid_rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert invalid_caught.value.__cause__ is None
    assert invalid_caught.value.__context__ is None
    assert sentinel not in invalid_locals
