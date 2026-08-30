"""Offline browser-ceremony coverage at the public local HTTP boundary."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
from starlette.testclient import TestClient

from intent_engineering.control_plane import build_control_plane_app
from intent_engineering.control_plane.models import CredentialRecord

ORIGIN = "http://localhost:43127"
CSRF = "csrf-process-secret-43127"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "project",
        "repository_id": "repo:sha256:" + "a" * 64,
        "actor": "local:owner",
        "action": "confirm_proposal",
        "graph_version": 3,
        "parent_bundle_digest": "sha256:" + "b" * 64,
        "subject": {"kind": "proposal", "id": "proposal:review"},
        "subject_digest": "sha256:" + "c" * 64,
        "selected_node_ids": ["requirement:local-only"],
        "result_digest": "sha256:" + "d" * 64,
        "challenge": "challenge:" + "e" * 64,
        "issued_at": "2026-08-30T12:00:00Z",
        "expires_at": "2026-08-30T12:05:00Z",
    }


@dataclass
class _PublicService:
    calls: list[tuple[str, object]] = field(default_factory=list)
    expired_decision: bool = False

    def status(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "human_attention_required",
            "attention_route": "inbox",
            "project_id": "project",
            "repository_id": "repo:sha256:" + "a" * 64,
            "graph_version": 3,
            "pending_proposal_ids": ["proposal:review"],
            "open_case_ids": [],
        }

    def proposal_preview(self, proposal_id: str) -> dict[str, object]:
        assert proposal_id == "proposal:review"
        return {
            "schema_version": 1,
            "preview_digest": "sha256:" + "f" * 64,
            "preview": {
                "affected_nodes": ["requirement:local-only"],
                "evidence_sides": [{"source": "docs/prd.md", "text": "Keep data local."}],
            },
            "payload": _payload(),
        }

    def registration_options(self) -> bytes:
        self.calls.append(("registration_options", None))
        return json.dumps(
            {
                "publicKey": {
                    "challenge": _base64url(b"register-challenge"),
                    "rp": {"id": "localhost", "name": "Intent Engineering"},
                    "user": {
                        "id": _base64url(b"local-owner"),
                        "name": "local:owner",
                        "displayName": "Local owner",
                    },
                    "authenticatorSelection": {"userVerification": "preferred"},
                }
            },
            separators=(",", ":"),
        ).encode()

    def register(self, response: bytes) -> CredentialRecord:
        self.calls.append(("register", response))
        return CredentialRecord(
            id="credential:" + "f" * 64 + ":0",
            project_id="project",
            repository_id="repo:sha256:" + "a" * 64,
            actor="local:owner",
            credential_id=_base64url(b"credential"),
            public_key=_base64url(b"public-key"),
            sign_count=0,
            created_at="2026-08-30T12:00:00Z",
        )

    def decision_options(self, payload: object) -> bytes:
        self.calls.append(("decision_options", payload))
        return json.dumps(
            {
                "publicKey": {
                    "challenge": _base64url(b"decision-challenge"),
                    "rpId": "localhost",
                    "allowCredentials": [{"id": _base64url(b"credential"), "type": "public-key"}],
                    "userVerification": "preferred",
                }
            },
            separators=(",", ":"),
        ).encode()

    def apply_decision(self, response: bytes, payload: object) -> dict[str, object]:
        self.calls.append(("apply_decision", (response, payload)))
        if self.expired_decision:
            raise ValueError("expired challenge")
        return {
            "schema_version": 1,
            "status": "resolved",
            "case_id": "case:review",
            "action": "update_requirement",
            "graph_version": 4,
            "changeset_id": "changeset:review",
        }


class _FakeBrowserAuthenticator:
    """Deterministic browser adapter; it validates converted public API options only."""

    def create(self, options: dict[str, object]) -> dict[str, object]:
        public_key = cast(dict[str, object], options["publicKey"])
        assert isinstance(public_key["challenge"], bytes)
        assert isinstance(cast(dict[str, object], public_key["user"])["id"], bytes)
        assert (
            cast(dict[str, object], public_key["authenticatorSelection"])["userVerification"]
            == "required"
        )
        return {"id": "credential", "type": "public-key"}

    def get(self, options: dict[str, object]) -> dict[str, object]:
        public_key = cast(dict[str, object], options["publicKey"])
        assert isinstance(public_key["challenge"], bytes)
        assert (
            cast(list[dict[str, object]], public_key["allowCredentials"])[0]["id"] == b"credential"
        )
        assert public_key["userVerification"] == "required"
        return {"id": "assertion", "type": "public-key"}


def _headers() -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Cookie": f"intent_csrf={CSRF}",
        "X-Intent-CSRF": CSRF,
        "Content-Type": "application/json",
    }


def _converted_options(value: dict[str, object]) -> dict[str, object]:
    """Mirror the browser's base64url conversion at the HTTP boundary."""
    public_key = cast(dict[str, object], value["publicKey"])
    converted = json.loads(json.dumps(value))
    converted_key = cast(dict[str, object], converted["publicKey"])
    for key in ("challenge",):
        converted_key[key] = base64.urlsafe_b64decode(cast(str, public_key[key]) + "===")
    if "user" in public_key:
        converted_user = cast(dict[str, object], converted_key["user"])
        source_user = cast(dict[str, object], public_key["user"])
        converted_user["id"] = base64.urlsafe_b64decode(cast(str, source_user["id"]) + "===")
        selection = cast(dict[str, object], converted_key["authenticatorSelection"])
        selection["userVerification"] = "required"
    if "allowCredentials" in public_key:
        converted_credentials = cast(list[dict[str, object]], converted_key["allowCredentials"])
        converted_credentials[0]["id"] = base64.urlsafe_b64decode(
            cast(str, cast(list[dict[str, object]], public_key["allowCredentials"])[0]["id"])
            + "==="
        )
        converted_key["userVerification"] = "required"
    return converted


def test_offline_fake_browser_completes_public_registration_and_decision_ceremonies() -> None:
    """Catches UI ceremony drift from public option/verify routes and required UV semantics."""
    service = _PublicService()
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )
    browser = _FakeBrowserAuthenticator()

    registration = client.post(
        "/api/v1/webauthn/register/options", content=b"{}", headers=_headers()
    )
    credential = browser.create(_converted_options(registration.json()))
    registered = client.post(
        "/api/v1/webauthn/register/verify",
        content=json.dumps({"response": credential}),
        headers=_headers(),
    )
    preview = client.get("/api/v1/proposals/proposal:review", headers={"Origin": ORIGIN})
    payload = cast(dict[str, object], preview.json()["payload"])
    decision = client.post(
        "/api/v1/decisions/options",
        content=json.dumps({"payload": payload}),
        headers=_headers(),
    )
    assertion = browser.get(_converted_options(decision.json()))
    applied = client.post(
        "/api/v1/decisions/verify",
        content=json.dumps({"response": assertion, "payload": payload}),
        headers=_headers(),
    )

    assert registered.status_code == 200
    assert preview.json()["preview"]["evidence_sides"] == [
        {"source": "docs/prd.md", "text": "Keep data local."}
    ]
    assert payload["selected_node_ids"] == ["requirement:local-only"]
    assert applied.json()["status"] == "resolved"
    assert [call[0] for call in service.calls] == [
        "registration_options",
        "register",
        "decision_options",
        "apply_decision",
    ]


def test_cancelled_browser_review_performs_no_verify_request() -> None:
    """Catches a cancellation flow that could submit an abandoned assertion."""
    service = _PublicService()
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )

    preview = client.get("/api/v1/proposals/proposal:review", headers={"Origin": ORIGIN})

    assert preview.status_code == 200
    assert service.calls == []


def test_expired_browser_challenge_is_rejected_without_a_decision_result() -> None:
    """Catches an expired challenge being treated as a completed review decision."""
    service = _PublicService(expired_decision=True)
    client = TestClient(
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF),
        base_url=ORIGIN,
    )
    preview = client.get("/api/v1/proposals/proposal:review", headers={"Origin": ORIGIN})
    expired_result = client.post(
        "/api/v1/decisions/verify",
        content=json.dumps({"response": {"id": "abandoned"}, "payload": preview.json()["payload"]}),
        headers=_headers(),
    )

    assert expired_result.status_code == 503
    assert [call[0] for call in service.calls] == ["apply_decision"]


@pytest.mark.manual_platform_authenticator
@pytest.mark.skip(reason="release probe: run manually with a platform authenticator")
def test_manual_platform_authenticator_release_probe() -> None:
    """Release-only probe for a real browser/platform authenticator ceremony."""
    raise AssertionError("manual release probe requires explicit platform-authenticator setup")
