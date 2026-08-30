"""Strict, canonical records for local human authority."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from intent_engineering.control_plane.models import (
    ChallengeRecord,
    CredentialRecord,
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
)


def _payload(**overrides: object) -> HumanDecisionPayload:
    material: dict[str, object] = {
        "schema_version": 1,
        "project_id": "project:alpha",
        "repository_id": "repo:sha256:" + "a" * 64,
        "actor": "local:asha",
        "action": DecisionAction.CONFIRM_PROPOSAL,
        "graph_version": 7,
        "parent_bundle_digest": "sha256:" + "b" * 64,
        "subject": DecisionSubject(kind="proposal", id="proposal:" + "c" * 64),
        "subject_digest": "sha256:" + "d" * 64,
        "selected_node_ids": ("requirement:export",),
        "result_digest": "sha256:" + "e" * 64,
        "challenge": "challenge:" + "f" * 64,
        "issued_at": datetime(2026, 8, 30, tzinfo=UTC),
        "expires_at": datetime(2026, 8, 30, 0, 5, tzinfo=UTC),
    }
    material.update(overrides)
    return HumanDecisionPayload(**material)


def test_decision_payload_is_project_repository_version_and_digest_bound() -> None:
    """Catches signatures that omit a decision's exact semantic preimage."""
    payload = _payload()

    assert payload == HumanDecisionPayload.model_validate_json(payload.model_dump_json())
    assert payload.canonical_bytes().endswith(b"\n")
    assert payload.canonical_bytes() == (
        b'{"action":"confirm_proposal","actor":"local:asha","challenge":"challenge:'
        + b"f" * 64
        + b'","expires_at":"2026-08-30T00:05:00Z","graph_version":7,'
        + b'"issued_at":"2026-08-30T00:00:00Z","parent_bundle_digest":"sha256:'
        + b"b" * 64
        + b'","project_id":"project:alpha","repository_id":"repo:sha256:'
        + b"a" * 64
        + b'","result_digest":"sha256:'
        + b"e" * 64
        + b'","schema_version":1,"selected_node_ids":["requirement:export"],'
        + b'"subject":{"id":"proposal:'
        + b"c" * 64
        + b'","kind":"proposal"},"subject_digest":"sha256:'
        + b"d" * 64
        + b'"}\n'
    )


def test_payload_rejects_noncanonical_or_ambiguous_authority_inputs() -> None:
    """Catches payload substitutions that would make a WebAuthn assertion replayable."""
    payload = _payload()

    with pytest.raises(ValidationError, match="unique and sorted"):
        _payload(selected_node_ids=("requirement:z", "requirement:a", "requirement:z"))
    with pytest.raises(ValidationError, match="selected node"):
        _payload(selected_node_ids=())
    with pytest.raises(ValidationError, match="timestamp must use canonical UTC Z"):
        HumanDecisionPayload.model_validate_json(
            payload.model_dump_json().replace("2026-08-30T00:00:00Z", "2026-08-30T00:00:00+00:00")
        )
    with pytest.raises(ValidationError, match="five minutes"):
        _payload(expires_at=payload.issued_at + timedelta(minutes=5, microseconds=1))
    with pytest.raises(ValidationError):
        HumanDecisionPayload.model_validate(
            {**payload.model_dump(), "action": "approve_everything"}
        )
    with pytest.raises(ValidationError):
        HumanDecisionPayload.model_validate({**payload.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError, match="maximum size"):
        _payload(actor="a" * 513)


@pytest.mark.parametrize(
    "action",
    (
        DecisionAction.ANSWER_CLARIFICATION,
        DecisionAction.RESOLVE_CONFLICT,
        DecisionAction.APPROVE_EXTERNAL_WRITE,
    ),
)
def test_non_selection_decisions_accept_an_exact_empty_selection(
    action: DecisionAction,
) -> None:
    """Catches non-proposal authority being blocked by a global selection rule."""
    subject = {
        DecisionAction.ANSWER_CLARIFICATION: DecisionSubject(
            kind="answer", id="answer:" + "1" * 64
        ),
        DecisionAction.RESOLVE_CONFLICT: DecisionSubject(kind="case", id="case:conflict"),
        DecisionAction.APPROVE_EXTERNAL_WRITE: DecisionSubject(
            kind="write-plan", id="write-plan:" + "2" * 64
        ),
    }[action]

    assert _payload(action=action, subject=subject, selected_node_ids=()).selected_node_ids == ()


@pytest.mark.parametrize(
    "action",
    (DecisionAction.CONFIRM_BASELINE, DecisionAction.CONFIRM_PROPOSAL),
)
def test_selection_bound_decisions_still_require_nodes(action: DecisionAction) -> None:
    """Catches baseline or proposal confirmation losing its exact node binding."""
    with pytest.raises(ValidationError, match="selected node identifiers are required"):
        _payload(action=action, selected_node_ids=())


def test_payload_rejects_string_subclasses_and_is_frozen() -> None:
    """Catches mutable or subclass-shaped data crossing the signed record boundary."""

    class TaintedString(str):
        pass

    with pytest.raises(ValidationError, match="exact string"):
        _payload(actor=TaintedString("local:asha"))

    payload = _payload()
    with pytest.raises(ValidationError):
        payload.actor = "local:ben"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("record_type", "material"),
    [
        (HumanDecisionPayload, _payload().model_dump()),
        (
            CredentialRecord,
            {
                "schema_version": 1,
                "id": "credential:asha-laptop",
                "project_id": "project:alpha",
                "repository_id": "repo:sha256:" + "a" * 64,
                "actor": "local:asha",
                "credential_id": "Y3JlZGVudGlhbA",
                "public_key": "cHVibGljLWtleQ",
                "sign_count": 0,
                "created_at": datetime(2026, 8, 30, tzinfo=UTC),
            },
        ),
        (
            ChallengeRecord,
            {
                "schema_version": 1,
                "id": "challenge:" + "a" * 64,
                "project_id": "project:alpha",
                "repository_id": "repo:sha256:" + "b" * 64,
                "actor": "local:asha",
                "ceremony": "authentication",
                "challenge": "Y2hhbGxlbmdl",
                "payload_digest": "sha256:" + "c" * 64,
                "issued_at": datetime(2026, 8, 30, tzinfo=UTC),
                "expires_at": datetime(2026, 8, 30, 0, 5, tzinfo=UTC),
            },
        ),
    ],
)
def test_authority_records_reject_boolean_schema_versions(
    record_type: type[HumanDecisionPayload] | type[CredentialRecord] | type[ChallengeRecord],
    material: dict[str, object],
) -> None:
    """Catches booleans that Pydantic would otherwise normalize to schema version one."""
    with pytest.raises(ValidationError, match="schema_version must be an exact integer"):
        record_type.model_validate({**material, "schema_version": True})


@pytest.mark.parametrize(
    ("local_only", "github_account_id", "github_login", "accepted"),
    (
        (True, None, None, True),
        (True, "101", "asha", False),
        (True, "101", None, False),
        (True, None, "asha", False),
        (False, "101", "asha", True),
        (False, None, None, False),
        (False, "101", None, False),
        (False, None, "asha", False),
    ),
)
def test_credential_identity_fields_are_complete_and_match_local_only_mode(
    local_only: bool,
    github_account_id: str | None,
    github_login: str | None,
    accepted: bool,
) -> None:
    """Catches partial or mode-mismatched future team identity enrollment records."""
    material = {
        "id": "credential:asha-laptop",
        "project_id": "project:alpha",
        "repository_id": "repo:sha256:" + "a" * 64,
        "actor": "local:asha",
        "credential_id": "Y3JlZGVudGlhbA",
        "public_key": "cHVibGljLWtleQ",
        "sign_count": 0,
        "created_at": datetime(2026, 8, 30, tzinfo=UTC),
        "local_only": local_only,
        "github_account_id": github_account_id,
        "github_login": github_login,
    }

    if accepted:
        assert CredentialRecord(**material).local_only is local_only
    else:
        with pytest.raises(ValidationError, match="identity is inconsistent"):
            CredentialRecord(**material)
