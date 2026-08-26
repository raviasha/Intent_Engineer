"""Contracts for exact, authorship-preserving external write previews."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from pydantic import ValidationError

from intent_engineering.capture.mcp import ProviderBinding, ProviderProfile, load_profile
from intent_engineering.core.models import (
    ClassificationEvent,
    EvidenceSide,
    ReconciliationCase,
    ReconciliationCaseType,
    ReconciliationStatus,
)
from intent_engineering.mutations.models import RemoteObject, WritePlan
from intent_engineering.mutations.planner import WritePlanError, build_write_plan

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
IDENTITY_ALIASES = {
    "local:proposer": frozenset(
        {"local:proposer", "jira-account-303", "git:proposer@example.com"}
    ),
    "local:reviewer": frozenset(
        {"local:reviewer", "jira-account-404", "slack-user-404"}
    ),
    "local:security": frozenset({"local:security", "jira-account-505"}),
    "local:alice": frozenset(
        {"local:alice", "jira-account-101", "slack-user-101", "git:alice@example.com"}
    ),
}


def _side(label: str, author: str, evidence_id: str) -> EvidenceSide:
    return EvidenceSide(
        label=label,
        claim=f"{label} claim",
        evidence_refs=(evidence_id,),
        observed_at=NOW,
        authors=(author,),
        confidence=0.9,
    )


def review_case(
    first_author: str = "jira-account-101",
    second_author: str = "jira-account-202",
) -> ReconciliationCase:
    return ReconciliationCase(
        id="case-write-1",
        subject_ref="requirement:export-policy",
        case_type=ReconciliationCaseType.CONFLICTING_SOURCES,
        affected_refs=("requirement:export-policy", "jira:ENG-7"),
        evidence_sides=(
            _side("intent", first_author, "evidence:intent"),
            _side("requirement", second_author, "evidence:requirement"),
        ),
        detector_id="cross_source_conflict",
        fingerprint="f" * 64,
        created_at=NOW,
        created_by="detector:cross_source_conflict",
        status=ReconciliationStatus.NEEDS_HUMAN,
        requires_human=True,
        history=(
            ClassificationEvent(
                actor="system:planner",
                at=NOW,
                prior=ReconciliationStatus.OPEN,
                new=ReconciliationStatus.PROPOSED,
            ),
            ClassificationEvent(
                actor="system:planner",
                at=NOW,
                prior=ReconciliationStatus.PROPOSED,
                new=ReconciliationStatus.NEEDS_HUMAN,
            ),
        ),
    )


def remote_object() -> RemoteObject:
    return RemoteObject(
        connector_id="mcp:jira-local:profile:source:scope:actor",
        profile_id="jira",
        profile_version="1",
        object_type="issue",
        id="ENG-7",
        version="2026-08-26T11:00:00Z",
        content={
            "summary": "Exports remain local",
            "description": "No centralized export by default.",
            "status": "Open",
            "issue_key": "ENG-7",
            "original_author_id": "jira-account-101",
        },
    )


def jira_profile() -> ProviderProfile:
    return load_profile(ROOT / "profiles" / "mcp" / "jira.yaml")


def jira_binding() -> ProviderBinding:
    return ProviderBinding(
        profile_id="jira",
        profile_version="1",
        tools={
            "discover_issues": "search_issues",
            "fetch_issue": "get_issue",
            "discover_comments": "search_comments",
            "fetch_comment": "get_comment",
            "update_issue": "update_issue",
            "add_comment": "add_comment",
        },
        resources={},
        actor_principals={
            "local:proposer": frozenset({"jira-account-303"}),
            "local:reviewer": frozenset({"jira-account-404"}),
            "local:security": frozenset({"jira-account-505"}),
            "local:alice": frozenset({"jira-account-101"}),
        },
    )


def base_plan() -> WritePlan:
    return build_write_plan(
        review_case(),
        jira_profile(),
        jira_binding(),
        "update_issue",
        remote_object(),
        {"summary": "Exports require an explicit opt-in"},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
        now=NOW,
    )


def test_plan_is_an_exact_hash_bound_preview_with_authorship() -> None:
    plan = base_plan()

    assert plan.id == f"write-plan:{plan.canonical_hash}"
    assert plan.target_id == "ENG-7"
    assert plan.before_version == "2026-08-26T11:00:00Z"
    assert dict(plan.before) == remote_object().content
    assert dict(plan.after) == {
        "summary": "Exports require an explicit opt-in",
        "description": "No centralized export by default.",
        "status": "Open",
        "issue_key": "ENG-7",
        "original_author_id": "jira-account-101",
    }
    assert dict(plan.arguments) == {
        "issue_id": "ENG-7",
        "expected_version": "2026-08-26T11:00:00Z",
        "summary": "Exports require an explicit opt-in",
        "description": "No centralized export by default.",
        "status": "Open",
    }
    assert plan.evidence_refs == ("evidence:intent", "evidence:requirement")
    assert plan.conflicting_authors == ("jira-account-101", "jira-account-202")
    assert plan.created_by == "local:proposer"
    assert plan.created_by_aliases == (
        "git:proposer@example.com",
        "jira-account-303",
        "local:proposer",
    )
    assert plan.provider_operation == "update_issue"
    assert plan.binding_hash.startswith("sha256:")
    assert plan.write_contract_hash.startswith("sha256:")
    assert plan.expires_at.isoformat() == "2026-08-26T12:15:00+00:00"


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("target_id", "ENG-8"),
        ("before_version", "different-version"),
        ("binding_hash", "sha256:" + "1" * 64),
        ("write_contract_hash", "sha256:" + "2" * 64),
        ("provider_operation", "different_provider_tool"),
        ("after", {"summary": "Different"}),
        ("arguments", {"summary": "Different"}),
        ("evidence_refs", ("evidence:different",)),
        ("conflicting_authors", ("provider:carol",)),
        ("created_by_aliases", ("provider:proposer",)),
    ],
)
def test_plan_hash_changes_for_every_preview_or_provenance_change(
    field: str,
    changed: object,
) -> None:
    plan = base_plan()
    changed_plan = plan.model_copy(update={field: changed})

    assert changed_plan.canonical_hash != plan.canonical_hash


def test_plan_roundtrips_strictly_and_detaches_nested_json() -> None:
    plan = base_plan()
    roundtripped = WritePlan.model_validate_json(plan.model_dump_json())

    assert roundtripped == plan
    assert isinstance(plan.before, MappingProxyType)
    with pytest.raises(TypeError):
        plan.after["summary"] = "mutated"  # type: ignore[index]
    payload = plan.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        WritePlan.model_validate(payload)
    with pytest.raises(ValidationError):
        WritePlan.model_validate(
            {**plan.model_dump(mode="json"), "before": {"bad": ("not", "json")}}
        )


@pytest.mark.parametrize(
    "requested",
    [
        {"private_token": "PRIVATE-WRITE-SECRET"},
        {"summary": ""},
        {"summary": float("nan")},
        MappingProxyType({"summary": "hostile"}),
    ],
)
def test_planner_fails_closed_without_retaining_requested_values(requested: object) -> None:
    with pytest.raises(WritePlanError) as caught:
        build_write_plan(
            review_case(),
            jira_profile(),
            jira_binding(),
            "update_issue",
            remote_object(),
            requested,  # type: ignore[arg-type]
            actor="local:proposer",
            authorized_contributors=frozenset({"local:proposer"}),
            identity_aliases=IDENTITY_ALIASES,
            now=NOW,
        )

    rendered = traceback.TracebackException.from_exception(caught.value, capture_locals=True)
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("invalid external write plan",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "PRIVATE-WRITE-SECRET" not in repository_locals


def test_planning_requires_a_human_review_case_and_nonempty_exact_change() -> None:
    open_case = review_case().model_copy(
        update={"status": ReconciliationStatus.OPEN, "history": ()}
    )

    for case, requested in ((open_case, {"summary": "different"}), (review_case(), {})):
        with pytest.raises(WritePlanError):
            build_write_plan(
                case,
                jira_profile(),
                jira_binding(),
                "update_issue",
                remote_object(),
                requested,
                actor="local:proposer",
                authorized_contributors=frozenset({"local:proposer"}),
                identity_aliases=IDENTITY_ALIASES,
                now=NOW,
            )


def test_remote_object_is_strict_frozen_and_rejects_invalid_json() -> None:
    payload: dict[str, Any] = remote_object().model_dump(mode="json")
    payload["content"] = {"invalid": object()}
    with pytest.raises(ValidationError):
        RemoteObject.model_validate(payload)


def test_planning_requires_an_authorized_contributor_and_matching_profile() -> None:
    with pytest.raises(WritePlanError):
        build_write_plan(
            review_case(),
            jira_profile(),
            jira_binding(),
            "update_issue",
            remote_object(),
            {"summary": "authorized contributors only"},
            actor="local:outsider",
            authorized_contributors=frozenset({"local:proposer"}),
            identity_aliases=IDENTITY_ALIASES,
            now=NOW,
        )

    mismatched = remote_object().model_copy(update={"profile_id": "slack"})
    with pytest.raises(WritePlanError):
        build_write_plan(
            review_case(),
            jira_profile(),
            jira_binding(),
            "update_issue",
            mismatched,
            {"summary": "wrong profile"},
            actor="local:proposer",
            authorized_contributors=frozenset({"local:proposer"}),
            identity_aliases=IDENTITY_ALIASES,
            now=NOW,
        )


def test_planning_rejects_a_write_contract_without_target_and_version_guards() -> None:
    profile_payload = jira_profile().model_dump(mode="json")
    operation = profile_payload["writes"]["update_issue"]
    operation["arguments"] = {
        "summary": {"source": "field", "field": "summary"},
        "description": {"source": "field", "field": "description"},
        "status": {"source": "field", "field": "status"},
    }
    unguarded_profile = ProviderProfile.model_validate(profile_payload)

    with pytest.raises(WritePlanError):
        build_write_plan(
            review_case(),
            unguarded_profile,
            jira_binding(),
            "update_issue",
            remote_object(),
            {"summary": "unguarded"},
            actor="local:proposer",
            authorized_contributors=frozenset({"local:proposer"}),
            identity_aliases=IDENTITY_ALIASES,
            now=NOW,
        )


def test_planning_rejects_the_wrong_object_type_for_the_write_contract() -> None:
    wrong_type = remote_object().model_copy(
        update={"object_type": "comment", "id": "COMMENT-77"}
    )
    with pytest.raises(WritePlanError):
        build_write_plan(
            review_case(),
            jira_profile(),
            jira_binding(),
            "update_issue",
            wrong_type,
            {"summary": "must not target a comment"},
            actor="local:proposer",
            authorized_contributors=frozenset({"local:proposer"}),
            identity_aliases=IDENTITY_ALIASES,
            now=NOW,
        )


def test_planning_allows_absent_optional_write_fields() -> None:
    profile_payload = jira_profile().model_dump(mode="json")
    operation = profile_payload["writes"]["update_issue"]
    operation["input_schema"]["required"] = ["summary"]
    optional_profile = ProviderProfile.model_validate(profile_payload)
    current = remote_object().model_copy(
        update={
            "content": {
                "summary": "old",
                "issue_key": "ENG-7",
                "original_author_id": "jira-account-101",
            }
        }
    )

    plan = build_write_plan(
        review_case(),
        optional_profile,
        jira_binding(),
        "update_issue",
        current,
        {"summary": "new"},
        actor="local:proposer",
        authorized_contributors=frozenset({"local:proposer"}),
        identity_aliases=IDENTITY_ALIASES,
        now=NOW,
    )

    assert dict(plan.arguments) == {
        "issue_id": "ENG-7",
        "expected_version": current.version,
        "summary": "new",
    }
