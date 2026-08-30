"""Hostile-boundary contract for the loopback-only browser API."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from intent_engineering.control_plane import build_control_plane_app
from intent_engineering.control_plane.http_models import RegistrationVerifyRequest
from intent_engineering.control_plane.models import CredentialRecord, HumanDecisionPayload

ORIGIN = "http://localhost:43127"
CSRF = "csrf-process-secret-43127"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
FIXED_ERROR = {"schema_version": 1, "status": "rejected", "reason": "request_unavailable"}


def _payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "project",
        "repository_id": "repo:sha256:" + "a" * 64,
        "actor": "local:owner",
        "action": "resolve_conflict",
        "graph_version": 3,
        "parent_bundle_digest": "sha256:" + "b" * 64,
        "subject": {"kind": "case", "id": "case:conflict"},
        "subject_digest": "sha256:" + "c" * 64,
        "selected_node_ids": [],
        "result_digest": "sha256:" + "d" * 64,
        "challenge": "challenge:" + "e" * 64,
        "issued_at": "2026-08-30T12:00:00Z",
        "expires_at": "2026-08-30T12:05:00Z",
    }


def _credential() -> CredentialRecord:
    return CredentialRecord(
        id="credential:" + "f" * 64 + ":0",
        project_id="project",
        repository_id="repo:sha256:" + "a" * 64,
        actor="local:owner",
        credential_id="Y3JlZGVudGlhbA",
        public_key="cHVibGljLWtleQ",
        sign_count=0,
        created_at=NOW,
    )


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.status_result: dict[str, object] = {
            "schema_version": 1,
            "status": "local_only",
            "attention_route": "home",
            "project_id": "project",
            "repository_id": "repo:sha256:" + "a" * 64,
            "graph_version": 3,
            "pending_proposal_ids": [],
            "open_case_ids": [],
        }
        self.previews: dict[str, dict[str, object] | Exception] = {
            "case:conflict": {
                "schema_version": 1,
                "preview_digest": "sha256:" + "1" * 64,
                "preview": {"kind": "reconciliation_case"},
                "payload": _payload(),
            }
        }
        self.options_result = b'{"publicKey":{"userVerification":"required"}}'
        self.failure: Exception | BaseException | None = None

    def _fail(self) -> None:
        if self.failure is not None:
            raise self.failure

    def status(self) -> dict[str, object]:
        self.calls.append(("status", None))
        self._fail()
        return self.status_result

    def proposal_preview(self, proposal_id: str) -> dict[str, object]:
        self.calls.append(("proposal_preview", proposal_id))
        self._fail()
        result = self.previews[proposal_id]
        if isinstance(result, Exception):
            raise result
        return result

    def registration_options(self) -> bytes:
        self.calls.append(("registration_options", None))
        self._fail()
        return self.options_result

    def register(self, response: bytes) -> CredentialRecord:
        self.calls.append(("register", response))
        self._fail()
        return _credential()

    def decision_options(self, payload: HumanDecisionPayload) -> bytes:
        self.calls.append(("decision_options", payload))
        self._fail()
        return b'{"publicKey":{"userVerification":"required"}}'

    def apply_decision(self, response: bytes, payload: HumanDecisionPayload) -> dict[str, object]:
        self.calls.append(("apply_decision", (response, payload)))
        self._fail()
        return {
            "schema_version": 1,
            "status": "resolved",
            "case_id": "case:conflict",
            "action": "update_requirement",
            "graph_version": 4,
            "changeset_id": "changeset:result",
        }


def _app(service: _Service):
    return build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=CSRF)


def _trusted_headers(*, content_type: str | None = "application/json") -> dict[str, str]:
    headers = {
        "Origin": ORIGIN,
        "Cookie": f"intent_csrf={CSRF}",
        "X-Intent-CSRF": CSRF,
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _decision_payload() -> HumanDecisionPayload:
    return HumanDecisionPayload.model_validate_json(_json_bytes(_payload()))


def _assert_security_headers(response: Any) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'; object-src 'none'; script-src 'self'; "
        "style-src 'self'; connect-src 'self'"
    )
    assert response.headers["referrer-policy"] == "no-referrer"
    cookie = response.headers["set-cookie"]
    assert cookie == f"intent_csrf={CSRF}; Path=/; HttpOnly; SameSite=Strict"


def test_exact_routes_delegate_to_the_service_and_return_detached_json() -> None:
    service = _Service()
    client = TestClient(_app(service), base_url=ORIGIN)

    status = client.get("/api/v1/status", headers={"Origin": ORIGIN})
    inbox = client.get("/api/v1/inbox", headers={"Origin": ORIGIN})
    proposal = client.get("/api/v1/proposals/case:conflict", headers={"Origin": ORIGIN})
    registration_options = client.post(
        "/api/v1/webauthn/register/options", content=b"{}", headers=_trusted_headers()
    )
    registration = client.post(
        "/api/v1/webauthn/register/verify",
        content=_json_bytes({"response": {"id": "credential", "type": "public-key"}}),
        headers=_trusted_headers(),
    )
    decision_options = client.post(
        "/api/v1/decisions/options",
        content=_json_bytes({"payload": _payload()}),
        headers=_trusted_headers(),
    )
    decision = client.post(
        "/api/v1/decisions/verify",
        content=_json_bytes(
            {
                "response": {"id": "assertion", "type": "public-key"},
                "payload": _payload(),
            }
        ),
        headers=_trusted_headers(),
    )

    assert status.json() == service.status_result
    assert inbox.json() == {
        "schema_version": 1,
        "pending_proposal_ids": [],
        "open_case_ids": [],
    }
    assert proposal.json() == service.previews["case:conflict"]
    assert registration_options.json() == {"publicKey": {"userVerification": "required"}}
    assert registration.json() == _credential().model_dump(mode="json")
    assert decision_options.json() == {"publicKey": {"userVerification": "required"}}
    assert decision.json()["status"] == "resolved"
    assert service.calls == [
        ("status", None),
        ("status", None),
        ("proposal_preview", "case:conflict"),
        ("registration_options", None),
        (
            "register",
            b'{"id":"credential","type":"public-key"}',
        ),
        ("decision_options", _decision_payload()),
        (
            "apply_decision",
            (
                b'{"id":"assertion","type":"public-key"}',
                _decision_payload(),
            ),
        ),
    ]
    for response in (
        status,
        inbox,
        proposal,
        registration_options,
        registration,
        decision_options,
        decision,
    ):
        assert response.headers["content-type"] == "application/json"
        _assert_security_headers(response)


def test_starlette_lifespan_starts_without_bypassing_the_http_boundary() -> None:
    service = _Service()

    with TestClient(_app(service), base_url=ORIGIN) as client:
        response = client.get("/api/v1/status", headers={"Origin": ORIGIN})

    assert response.status_code == 200
    assert response.json() == service.status_result


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    (
        ({"Origin": ORIGIN, "Host": "127.0.0.1:43127"}, 403),
        ({"Origin": ORIGIN, "Host": "localhost:43128"}, 403),
        ({"Origin": "http://127.0.0.1:43127"}, 403),
        ({"Origin": "http://localhost:43127/"}, 403),
        ({"Origin": "null"}, 403),
        ({}, 403),
    ),
)
def test_host_and_origin_must_exactly_match_the_canonical_loopback_origin(
    headers: dict[str, str], expected_status: int
) -> None:
    service = _Service()
    response = TestClient(_app(service), base_url=ORIGIN).get("/api/v1/status", headers=headers)

    assert response.status_code == expected_status
    assert response.json() == FIXED_ERROR
    assert service.calls == []
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("method", ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"))
def test_read_route_rejects_every_method_other_than_get_before_behavior(method: str) -> None:
    service = _Service()
    response = TestClient(_app(service), base_url=ORIGIN).request(
        method,
        "/api/v1/status",
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 405
    if method == "HEAD":
        assert response.content == b""
        assert response.headers["content-length"] == str(len(_json_bytes(FIXED_ERROR)))
    else:
        assert response.json() == FIXED_ERROR
    assert service.calls == []


@pytest.mark.parametrize("method", ("GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"))
def test_write_route_rejects_every_method_other_than_post_before_behavior(method: str) -> None:
    service = _Service()
    response = TestClient(_app(service), base_url=ORIGIN).request(
        method,
        "/api/v1/decisions/verify",
        headers={"Origin": ORIGIN},
    )

    assert response.status_code == 405
    if method == "HEAD":
        assert response.content == b""
        assert response.headers["content-length"] == str(len(_json_bytes(FIXED_ERROR)))
    else:
        assert response.json() == FIXED_ERROR
    assert service.calls == []


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    (
        ({"Origin": ORIGIN, "Content-Type": "application/json"}, 403),
        (
            {
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "Cookie": f"intent_csrf={CSRF}",
            },
            403,
        ),
        (
            {
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-Intent-CSRF": CSRF,
            },
            403,
        ),
        (
            {
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "Cookie": "intent_csrf=wrong-secret",
                "X-Intent-CSRF": CSRF,
            },
            403,
        ),
        (_trusted_headers(content_type="application/json; charset=utf-8"), 415),
        (_trusted_headers(content_type="text/plain"), 415),
        (_trusted_headers(content_type=None), 415),
    ),
)
def test_write_routes_require_exact_csrf_cookie_header_and_content_type(
    headers: dict[str, str], expected_status: int
) -> None:
    service = _Service()
    response = TestClient(_app(service), base_url=ORIGIN).post(
        "/api/v1/webauthn/register/options", content=b"{}", headers=headers
    )

    assert response.status_code == expected_status
    assert response.json() == FIXED_ERROR
    assert service.calls == []


@pytest.mark.parametrize(
    ("body", "expected_status"),
    (
        (b'{"response":"\xff"}', 400),
        (b'{"response":{"id":"first","id":"second"}}', 400),
        (b'{"response":NaN}', 400),
        (b"[]", 400),
        (b'"scalar"', 400),
        (b'{"response":{},"extra":"PRIVATE-EXTRA"}', 400),
    ),
)
def test_raw_json_is_validated_before_registration_behavior(
    body: bytes, expected_status: int
) -> None:
    service = _Service()
    response = TestClient(_app(service), base_url=ORIGIN).post(
        "/api/v1/webauthn/register/verify", content=body, headers=_trusted_headers()
    )

    assert response.status_code == expected_status
    assert response.json() == FIXED_ERROR
    assert service.calls == []
    assert "PRIVATE" not in response.text


def test_body_over_256_kib_is_rejected_before_behavior() -> None:
    service = _Service()
    body = b'{"response":"' + b"x" * (256 * 1024) + b'"}'

    response = TestClient(_app(service), base_url=ORIGIN).post(
        "/api/v1/webauthn/register/verify", content=body, headers=_trusted_headers()
    )

    assert response.status_code == 413
    assert response.json() == FIXED_ERROR
    assert service.calls == []


def test_noncanonical_decision_timestamp_fails_before_behavior() -> None:
    service = _Service()
    payload = _payload()
    payload["issued_at"] = "2026-08-30T12:00:00+00:00"

    response = TestClient(_app(service), base_url=ORIGIN).post(
        "/api/v1/decisions/options",
        content=_json_bytes({"payload": payload}),
        headers=_trusted_headers(),
    )

    assert response.status_code == 400
    assert response.json() == FIXED_ERROR
    assert service.calls == []


def test_extra_decision_payload_fields_fail_before_behavior() -> None:
    service = _Service()
    payload = _payload()
    payload["PRIVATE-EXTRA"] = "secret"

    response = TestClient(_app(service), base_url=ORIGIN).post(
        "/api/v1/decisions/options",
        content=_json_bytes({"payload": payload}),
        headers=_trusted_headers(),
    )

    assert response.status_code == 400
    assert response.json() == FIXED_ERROR
    assert service.calls == []
    assert "PRIVATE" not in response.text


class _DictSubclass(dict[str, object]):
    pass


class _ListSubclass(list[object]):
    pass


class _StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "value",
    (
        _DictSubclass(response={}),
        {"response": {"nested": _ListSubclass()}},
        {"response": {"nested": _StringSubclass("value")}},
    ),
)
def test_direct_model_calls_reject_non_exact_json_containers_and_scalars(value: object) -> None:
    with pytest.raises(ValidationError):
        RegistrationVerifyRequest.model_validate(value)


def test_direct_model_calls_reject_cycles_and_container_aliases() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    shared: dict[str, object] = {"value": "PRIVATE-ALIASED"}

    with pytest.raises(ValidationError):
        RegistrationVerifyRequest.model_validate({"response": cycle})
    with pytest.raises(ValidationError):
        RegistrationVerifyRequest.model_validate({"response": {"first": shared, "second": shared}})


def test_status_and_inbox_drop_items_the_service_acl_projection_denies() -> None:
    service = _Service()
    service.status_result = {
        **service.status_result,
        "status": "human_attention_required",
        "attention_route": "inbox",
        "pending_proposal_ids": ["proposal:visible", "proposal:hidden"],
        "open_case_ids": ["case:visible", "case:hidden"],
    }
    service.previews = {
        "proposal:visible": {
            "schema_version": 1,
            "preview_digest": "sha256:" + "1" * 64,
            "preview": {"kind": "baseline"},
            "payload": _payload(),
        },
        "case:visible": {
            "schema_version": 1,
            "preview_digest": "sha256:" + "2" * 64,
            "preview": {"kind": "reconciliation_case"},
            "payload": _payload(),
        },
        "proposal:hidden": ValueError("PRIVATE-HIDDEN-PROPOSAL"),
        "case:hidden": ValueError("PRIVATE-HIDDEN-CASE"),
    }
    client = TestClient(_app(service), base_url=ORIGIN)

    status = client.get("/api/v1/status", headers={"Origin": ORIGIN})
    inbox = client.get("/api/v1/inbox", headers={"Origin": ORIGIN})

    assert status.json()["pending_proposal_ids"] == ["proposal:visible"]
    assert status.json()["open_case_ids"] == ["case:visible"]
    assert inbox.json() == {
        "schema_version": 1,
        "pending_proposal_ids": ["proposal:visible"],
        "open_case_ids": ["case:visible"],
    }
    assert "hidden" not in status.text
    assert "hidden" not in inbox.text
    assert "PRIVATE" not in status.text + inbox.text


def test_invalid_service_output_and_service_errors_have_one_fixed_secret_free_result() -> None:
    service = _Service()
    shared: dict[str, object] = {"PRIVATE": "OUTPUT-ALIAS"}
    service.previews["case:conflict"] = cast(dict[str, object], {"first": shared, "second": shared})
    client = TestClient(_app(service), base_url=ORIGIN)

    invalid_output = client.get("/api/v1/proposals/case:conflict", headers={"Origin": ORIGIN})
    service.failure = ValueError("PRIVATE-SERVICE-FAILURE")
    service_error = client.get("/api/v1/status", headers={"Origin": ORIGIN})

    assert invalid_output.status_code == 503
    assert service_error.status_code == 503
    assert invalid_output.json() == service_error.json() == FIXED_ERROR
    assert "PRIVATE" not in invalid_output.text + service_error.text


def _repository_traceback_locals(error: BaseException) -> str:
    repository_locals: list[str] = []
    traceback = error.__traceback__
    while traceback is not None:
        if "/src/intent_engineering/" in traceback.tb_frame.f_code.co_filename:
            repository_locals.append(repr(traceback.tb_frame.f_locals))
        traceback = traceback.tb_next
    return "".join(repository_locals)


async def _call_asgi(
    app: Callable[[Scope, Receive, Send], Awaitable[None]],
    *,
    path: str,
    body: bytes,
    send: Send | None = None,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
    scope_overrides: dict[str, object] | None = None,
) -> list[Message]:
    messages = iter(({"type": "http.request", "body": body, "more_body": False},))
    sent: list[Message] = []

    async def receive() -> Message:
        try:
            return next(messages)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def capture(message: Message) -> None:
        sent.append(message)

    encoded_length = str(len(body)).encode("ascii")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"localhost:43127"),
            (b"origin", ORIGIN.encode("ascii")),
            (b"content-type", b"application/json"),
            (b"content-length", encoded_length),
            (b"cookie", f"intent_csrf={CSRF}".encode("ascii")),
            (b"x-intent-csrf", CSRF.encode("ascii")),
            *extra_headers,
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 43127),
        "state": {},
    }
    if scope_overrides is not None:
        scope.update(scope_overrides)
    await app(cast(Scope, scope), receive, capture if send is None else send)
    return sent


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("extra_headers", "scope_overrides", "expected_status"),
    (
        ((b"transfer-encoding", b"chunked"), {}, 400),
        ((b"origin", ORIGIN.encode("ascii")), {}, 403),
        ((b"x-intent-csrf", CSRF.encode("ascii")), {}, 403),
        ((b"content-length", b"2"), {}, 400),
        ((), {"scheme": "https"}, 403),
    ),
)
async def test_noncanonical_transport_metadata_is_rejected_before_behavior(
    extra_headers: tuple[bytes, bytes] | tuple[()],
    scope_overrides: dict[str, object],
    expected_status: int,
) -> None:
    service = _Service()
    normalized_headers = () if not extra_headers else (cast(tuple[bytes, bytes], extra_headers),)

    sent = await _call_asgi(
        _app(service),
        path="/api/v1/webauthn/register/options",
        body=b"{}",
        extra_headers=normalized_headers,
        scope_overrides=scope_overrides,
    )

    assert sent[0]["status"] == expected_status
    assert json.loads(cast(bytes, sent[1]["body"])) == FIXED_ERROR
    assert service.calls == []


@pytest.mark.anyio
async def test_cancellation_preserves_identity_and_scrubs_request_body_and_models() -> None:
    secret = "PRIVATE-CANCELLED-ASSERTION-43127"
    cancellation = asyncio.CancelledError("cancelled")
    service = _Service()
    service.failure = cancellation
    app = _app(service)
    body = _json_bytes(
        {
            "response": {"id": secret, "type": "public-key"},
            "payload": _payload(),
        }
    )

    with pytest.raises(asyncio.CancelledError) as caught:
        await _call_asgi(app, path="/api/v1/decisions/verify", body=body)

    assert caught.value is cancellation
    traceback_locals = _repository_traceback_locals(caught.value)
    assert secret not in traceback_locals
    assert CSRF not in traceback_locals


@pytest.mark.anyio
async def test_send_cancellation_scrubs_detached_response_locals() -> None:
    secret = "PRIVATE-CANCELLED-RESPONSE-43127"
    cancellation = asyncio.CancelledError("send cancelled")
    service = _Service()
    service.options_result = _json_bytes({"publicKey": {"private": secret}})
    app = _app(service)

    async def cancel_send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise cancellation

    body = b"{}"
    with pytest.raises(asyncio.CancelledError) as caught:
        await _call_asgi(
            app,
            path="/api/v1/webauthn/register/options",
            body=body,
            send=cancel_send,
        )

    assert caught.value is cancellation
    traceback_locals = _repository_traceback_locals(caught.value)
    assert secret not in traceback_locals
    assert CSRF not in traceback_locals


def test_builder_rejects_noncanonical_origin_and_invalid_csrf_secret() -> None:
    service = _Service()

    for origin in (
        "https://localhost:43127",
        "http://127.0.0.1:43127",
        "http://LOCALHOST:43127",
        "http://localhost",
        "http://localhost:43127/",
    ):
        with pytest.raises(ValueError, match="^invalid control plane HTTP configuration$"):
            build_control_plane_app(cast(Any, service), origin=origin, csrf_secret=CSRF)
    secret = "PRIVATE invalid CSRF secret"
    with pytest.raises(ValueError, match="^invalid control plane HTTP configuration$") as caught:
        build_control_plane_app(cast(Any, service), origin=ORIGIN, csrf_secret=secret)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in _repository_traceback_locals(caught.value)


def test_registration_response_uses_canonical_z_timestamp() -> None:
    service = _Service()
    response = TestClient(_app(service), base_url=ORIGIN).post(
        "/api/v1/webauthn/register/verify",
        content=_json_bytes({"response": {"id": "credential", "type": "public-key"}}),
        headers=_trusted_headers(),
    )

    assert response.json()["created_at"] == "2026-08-30T12:00:00Z"
    assert response.json()["created_at"].endswith("Z")
