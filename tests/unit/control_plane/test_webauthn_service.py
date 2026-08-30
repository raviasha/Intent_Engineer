"""Security contract for repository-bound WebAuthn human decisions."""

from __future__ import annotations

import base64
import json
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from intent_engineering.control_plane.models import (
    CredentialRecord,
    DecisionAction,
    DecisionSubject,
    HumanDecisionPayload,
)
from intent_engineering.control_plane.webauthn_service import (
    AuthenticationRequest,
    HumanAuthorityError,
    PythonWebAuthnVerifier,
    RegistrationRequest,
    VerifiedAuthentication,
    VerifiedHumanDecision,
    VerifiedRegistration,
    WebAuthnService,
)
from intent_engineering.control_plane.webauthn_store import (
    WebAuthnChallengeStore,
    WebAuthnCredentialStore,
)
from intent_engineering.storage.secure import SecureFile
from intent_engineering.storage.transaction import LocalTransactionCoordinator

NOW = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)
PROJECT_ID = "project:alpha"
REPOSITORY_ID = "repo:sha256:" + "a" * 64
ACTOR = "local:asha"
ORIGIN = "http://localhost:8765"


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _registration_response(
    request: RegistrationRequest,
    *,
    credential_id: bytes = b"credential-primary",
    public_key: bytes = b"public-key-primary",
    sign_count: int = 0,
    origin: str | None = None,
    rp_id: str | None = None,
    user_verified: bool = True,
) -> bytes:
    client_data = json.dumps(
        {
            "challenge": _b64(request.challenge),
            "origin": origin or request.expected_origin,
            "type": "webauthn.create",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return json.dumps(
        {
            "id": _b64(credential_id),
            "rawId": _b64(credential_id),
            "response": {
                "attestationObject": _b64(b"fake-attestation"),
                "clientDataJSON": _b64(client_data),
            },
            "type": "public-key",
            "fake": {
                "public_key": _b64(public_key),
                "rp_id": rp_id or request.rp_id,
                "sign_count": sign_count,
                "user_verified": user_verified,
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _authentication_response(
    request: AuthenticationRequest,
    *,
    credential_id: bytes = b"credential-primary",
    new_sign_count: int = 1,
    origin: str | None = None,
    rp_id: str | None = None,
    user_verified: bool = True,
    secret: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "credential_id": _b64(credential_id),
            "new_sign_count": new_sign_count,
            "origin": origin or request.expected_origin,
            "rp_id": rp_id or request.rp_id,
            "secret": secret,
            "user_verified": user_verified,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _platform_authentication_response(
    request: AuthenticationRequest,
    *,
    secret: str,
    credential_id: bytes = b"credential-primary",
) -> bytes:
    client_data = json.dumps(
        {
            "challenge": _b64(request.challenge),
            "origin": secret,
            "type": "webauthn.get",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return json.dumps(
        {
            "id": _b64(credential_id),
            "rawId": _b64(credential_id),
            "response": {
                "authenticatorData": _b64(b"fake-authenticator-data"),
                "clientDataJSON": _b64(client_data),
                "signature": _b64(b"fake-signature"),
                "userHandle": None,
            },
            "type": "public-key",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _repository_traceback_locals(error: BaseException) -> str:
    rendered = traceback.TracebackException.from_exception(error, capture_locals=True)
    return "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )


class FakeVerifier:
    """Deterministic authenticator boundary; no platform cryptography is emulated."""

    def __init__(self) -> None:
        self.registration_request: RegistrationRequest | None = None
        self.authentication_request: AuthenticationRequest | None = None
        self.registration_cancel: BaseException | None = None
        self.cancel: BaseException | None = None
        self.fail_with_response = False

    def registration_options(self, request: RegistrationRequest) -> bytes:
        self.registration_request = request
        return b'{"publicKey":"registration"}'

    def verify_registration(
        self, response: bytes, request: RegistrationRequest
    ) -> VerifiedRegistration:
        if self.registration_cancel is not None:
            raise self.registration_cancel
        material = cast(dict[str, object], json.loads(response))
        fake = cast(dict[str, object], material["fake"])
        client = cast(dict[str, object], material["response"])
        client_data = cast(
            dict[str, object],
            json.loads(base64.urlsafe_b64decode(cast(str, client["clientDataJSON"]) + "==")),
        )
        if client_data["origin"] != request.expected_origin or fake["rp_id"] != request.rp_id:
            raise ValueError("wrong relying party")
        return VerifiedRegistration(
            credential_id=base64.urlsafe_b64decode(cast(str, material["rawId"]) + "=="),
            public_key=base64.urlsafe_b64decode(cast(str, fake["public_key"]) + "=="),
            sign_count=cast(int, fake["sign_count"]),
            user_verified=cast(bool, fake["user_verified"]),
        )

    def authentication_options(self, request: AuthenticationRequest) -> bytes:
        self.authentication_request = request
        return b'{"publicKey":"authentication"}'

    def verify_authentication(
        self, response: bytes, request: AuthenticationRequest
    ) -> VerifiedAuthentication:
        try:
            if self.cancel is not None:
                raise self.cancel
            if self.fail_with_response:
                raise ValueError(response.decode("utf-8"))
            material = cast(dict[str, object], json.loads(response))
            if material["origin"] != request.expected_origin or material["rp_id"] != request.rp_id:
                raise ValueError("wrong relying party")
            return VerifiedAuthentication(
                credential_id=base64.urlsafe_b64decode(cast(str, material["credential_id"]) + "=="),
                new_sign_count=cast(int, material["new_sign_count"]),
                user_verified=cast(bool, material["user_verified"]),
            )
        finally:
            response = b""


def _payload(**overrides: object) -> HumanDecisionPayload:
    material: dict[str, object] = {
        "project_id": PROJECT_ID,
        "repository_id": REPOSITORY_ID,
        "actor": ACTOR,
        "action": DecisionAction.CONFIRM_PROPOSAL,
        "graph_version": 7,
        "parent_bundle_digest": "sha256:" + "b" * 64,
        "subject": DecisionSubject(kind="proposal", id="proposal:" + "c" * 64),
        "subject_digest": "sha256:" + "d" * 64,
        "selected_node_ids": ("requirement:export",),
        "result_digest": "sha256:" + "e" * 64,
        "challenge": "challenge:" + "f" * 64,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    material.update(overrides)
    return HumanDecisionPayload(**material)


@pytest.fixture
def authority(
    tmp_path: Path,
) -> tuple[WebAuthnService, FakeVerifier, WebAuthnCredentialStore, WebAuthnChallengeStore]:
    credential_file = SecureFile.from_path(tmp_path / "credentials.jsonl")
    challenge_file = SecureFile.from_path(tmp_path / "challenges.jsonl")
    transactions = LocalTransactionCoordinator(
        SecureFile.from_path(tmp_path / "transaction.json"),
        {
            "webauthn_credentials": credential_file,
            "webauthn_challenges": challenge_file,
        },
    )
    credentials = WebAuthnCredentialStore(credential_file, transactions=transactions)
    challenges = WebAuthnChallengeStore(challenge_file, transactions=transactions)
    verifier = FakeVerifier()
    service = WebAuthnService(
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        expected_origin=ORIGIN,
        credentials=credentials,
        challenges=challenges,
        transactions=transactions,
        verifier=verifier,
        challenge_source=lambda size: b"r" * size,
    )
    return service, verifier, credentials, challenges


def _enroll(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
    *,
    sign_count: int = 0,
) -> CredentialRecord:
    service, verifier, _, _ = authority
    assert service.registration_options(ACTOR, ORIGIN, NOW) == b'{"publicKey":"registration"}'
    request = verifier.registration_request
    assert request is not None
    return service.register(
        _registration_response(request, sign_count=sign_count), ACTOR, ORIGIN, NOW
    )


def test_registration_binds_exact_origin_rp_actor_and_required_uv(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
) -> None:
    _, verifier, credentials, _ = authority

    record = _enroll(authority)

    request = verifier.registration_request
    assert request is not None
    assert (request.rp_id, request.expected_origin, request.user_verification) == (
        "localhost",
        ORIGIN,
        "required",
    )
    assert (request.project_id, request.repository_id, request.actor) == (
        PROJECT_ID,
        REPOSITORY_ID,
        ACTOR,
    )
    assert record == credentials.list()[0]


def test_production_options_use_localhost_five_minutes_and_required_uv() -> None:
    verifier = PythonWebAuthnVerifier()
    registration = RegistrationRequest(
        challenge=b"r" * 32,
        rp_id="localhost",
        expected_origin=ORIGIN,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        actor=ACTOR,
    )
    credential = CredentialRecord(
        id="credential:primary",
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        actor=ACTOR,
        credential_id=_b64(b"credential-primary"),
        public_key=_b64(b"public-key-primary"),
        sign_count=0,
        created_at=NOW,
    )
    authentication = AuthenticationRequest(
        challenge=b"a" * 32,
        rp_id="localhost",
        expected_origin=ORIGIN,
        project_id=PROJECT_ID,
        repository_id=REPOSITORY_ID,
        actor=ACTOR,
        payload_bytes=b"canonical-payload\n",
        credentials=(credential,),
    )

    registration_options = cast(
        dict[str, object], json.loads(verifier.registration_options(registration))
    )
    authentication_options = cast(
        dict[str, object], json.loads(verifier.authentication_options(authentication))
    )

    assert registration_options["rp"] == {"id": "localhost", "name": "Intent Engineering"}
    assert registration_options["timeout"] == 300_000
    assert registration_options["authenticatorSelection"] == {
        "requireResidentKey": False,
        "userVerification": "required",
    }
    assert authentication_options["rpId"] == "localhost"
    assert authentication_options["timeout"] == 300_000
    assert authentication_options["userVerification"] == "required"


@pytest.mark.parametrize("ceremony", ["registration", "authentication"])
def test_production_verifier_cancellation_clears_raw_and_parsed_response_locals(
    monkeypatch: pytest.MonkeyPatch,
    ceremony: str,
) -> None:
    class Cancelled(BaseException):
        pass

    verifier = PythonWebAuthnVerifier()
    secret = f"PRIVATE-PRODUCTION-{ceremony.upper()}-RESPONSE-7391"
    cancellation = Cancelled()

    def cancel(**_kwargs: object) -> None:
        raise cancellation

    if ceremony == "registration":
        request: RegistrationRequest | AuthenticationRequest = RegistrationRequest(
            challenge=b"r" * 32,
            rp_id="localhost",
            expected_origin=ORIGIN,
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            actor=ACTOR,
        )
        response_material = cast(dict[str, object], json.loads(_registration_response(request)))
        response_material["secret"] = secret
        response = json.dumps(response_material, separators=(",", ":"), sort_keys=True).encode()
        monkeypatch.setattr(
            "intent_engineering.control_plane.webauthn_service.verify_registration_response",
            cancel,
        )
        invoke = lambda: verifier.verify_registration(response, cast(RegistrationRequest, request))
    else:
        credential = CredentialRecord(
            id="credential:primary",
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            actor=ACTOR,
            credential_id=_b64(b"credential-primary"),
            public_key=_b64(b"public-key-primary"),
            sign_count=0,
            created_at=NOW,
        )
        request = AuthenticationRequest(
            challenge=b"a" * 32,
            rp_id="localhost",
            expected_origin=ORIGIN,
            project_id=PROJECT_ID,
            repository_id=REPOSITORY_ID,
            actor=ACTOR,
            payload_bytes=b"canonical-payload\n",
            credentials=(credential,),
        )
        response = _platform_authentication_response(request, secret=secret)
        monkeypatch.setattr(
            "intent_engineering.control_plane.webauthn_service.verify_authentication_response",
            cancel,
        )
        invoke = lambda: verifier.verify_authentication(
            response, cast(AuthenticationRequest, request)
        )

    with pytest.raises(Cancelled) as caught:
        invoke()

    assert caught.value is cancellation
    assert secret not in _repository_traceback_locals(caught.value)


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("origin", "http://localhost:9999"),
        ("rp_id", "example.test"),
        ("user_verified", False),
    ],
)
def test_registration_rejects_wrong_relying_party_or_missing_user_verification(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
    change: str,
    value: object,
) -> None:
    service, verifier, credentials, _ = authority
    service.registration_options(ACTOR, ORIGIN, NOW)
    request = verifier.registration_request
    assert request is not None

    with pytest.raises(HumanAuthorityError, match="^human authority unavailable$"):
        service.register(
            _registration_response(request, **{change: value}),  # type: ignore[arg-type]
            ACTOR,
            ORIGIN,
            NOW,
        )

    assert credentials.list() == ()


def test_authentication_verifies_payload_and_appends_counter_atomically(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
) -> None:
    service, verifier, credentials, _ = authority
    enrolled = _enroll(authority)
    payload = _payload()

    assert service.authentication_options(payload, ORIGIN, NOW) == (
        b'{"publicKey":"authentication"}'
    )
    request = verifier.authentication_request
    assert request is not None
    assert (request.rp_id, request.expected_origin, request.user_verification) == (
        "localhost",
        ORIGIN,
        "required",
    )
    assert request.payload_bytes == payload.canonical_bytes()
    assert request.credentials == (enrolled,)

    decision = service.verify(_authentication_response(request), payload, ORIGIN, NOW)

    assert decision == VerifiedHumanDecision(
        payload=payload,
        credential=credentials.list()[-1],
        verified_at=NOW,
    )
    assert [record.sign_count for record in credentials.list()] == [0, 1]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("actor", "local:mallory"),
        ("repository_id", "repo:sha256:" + "9" * 64),
        ("graph_version", 8),
        ("result_digest", "sha256:" + "9" * 64),
    ],
)
def test_authentication_rejects_actor_repository_version_or_payload_substitution(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
    field: str,
    value: object,
) -> None:
    service, verifier, _, _ = authority
    _enroll(authority)
    payload = _payload()
    service.authentication_options(payload, ORIGIN, NOW)
    request = verifier.authentication_request
    assert request is not None
    substituted = _payload(**{field: value})

    with pytest.raises(HumanAuthorityError, match="^human authority unavailable$"):
        service.verify(_authentication_response(request), substituted, ORIGIN, NOW)

    assert (
        service.verify(_authentication_response(request), payload, ORIGIN, NOW).payload == payload
    )


def test_authentication_rejects_wrong_origin_rp_missing_uv_and_credential(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
) -> None:
    service, verifier, _, _ = authority
    _enroll(authority)
    cases: tuple[dict[str, object], ...] = (
        {"origin": "http://localhost:9999"},
        {"rp_id": "example.test"},
        {"user_verified": False},
        {"credential_id": b"credential-other"},
    )
    for index, changes in enumerate(cases):
        payload = _payload(challenge=f"challenge:{index + 1:064x}")
        service.authentication_options(payload, ORIGIN, NOW)
        request = verifier.authentication_request
        assert request is not None
        with pytest.raises(HumanAuthorityError, match="^human authority unavailable$"):
            service.verify(
                _authentication_response(request, **changes),  # type: ignore[arg-type]
                payload,
                ORIGIN,
                NOW,
            )


def test_counter_rollback_and_challenge_replay_fail_closed(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
) -> None:
    service, verifier, credentials, _ = authority
    _enroll(authority, sign_count=5)
    payload = _payload()
    service.authentication_options(payload, ORIGIN, NOW)
    request = verifier.authentication_request
    assert request is not None

    with pytest.raises(HumanAuthorityError, match="^human authority unavailable$"):
        service.verify(_authentication_response(request, new_sign_count=4), payload, ORIGIN, NOW)
    assert [record.sign_count for record in credentials.list()] == [5]

    response = _authentication_response(request, new_sign_count=6)
    service.verify(response, payload, ORIGIN, NOW)
    with pytest.raises(HumanAuthorityError, match="^human authority unavailable$"):
        service.verify(response, payload, ORIGIN, NOW)
    assert [record.sign_count for record in credentials.list()] == [5, 6]


def test_expired_challenge_is_rejected(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
) -> None:
    service, verifier, _, _ = authority
    _enroll(authority)
    payload = _payload()
    service.authentication_options(payload, ORIGIN, NOW)
    request = verifier.authentication_request
    assert request is not None

    with pytest.raises(HumanAuthorityError, match="^human authority unavailable$"):
        service.verify(
            _authentication_response(request), payload, ORIGIN, NOW + timedelta(minutes=5)
        )


def test_counter_failure_rolls_back_challenge_consumption(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, verifier, credentials, _ = authority
    _enroll(authority)
    payload = _payload()
    service.authentication_options(payload, ORIGIN, NOW)
    request = verifier.authentication_request
    assert request is not None
    response = _authentication_response(request)
    original_put = credentials.put
    monkeypatch.setattr(credentials, "put", lambda _record: (_ for _ in ()).throw(OSError()))

    with pytest.raises(HumanAuthorityError, match="^human authority unavailable$"):
        service.verify(response, payload, ORIGIN, NOW)

    monkeypatch.setattr(credentials, "put", original_put)
    assert service.verify(response, payload, ORIGIN, NOW).credential.sign_count == 1


def test_verifier_cancellation_preserves_identity_and_rolls_back_challenge(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
) -> None:
    class Cancelled(BaseException):
        pass

    service, verifier, _, _ = authority
    _enroll(authority)
    payload = _payload()
    service.authentication_options(payload, ORIGIN, NOW)
    request = verifier.authentication_request
    assert request is not None
    response = _authentication_response(request)
    cancellation = Cancelled()
    verifier.cancel = cancellation

    with pytest.raises(Cancelled) as caught:
        service.verify(response, payload, ORIGIN, NOW)

    assert caught.value is cancellation
    verifier.cancel = None
    assert service.verify(response, payload, ORIGIN, NOW).payload == payload


def test_registration_cancellation_preserves_identity_without_response_in_service_traceback(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Cancelled(BaseException):
        pass

    service, verifier, _, _ = authority
    service.registration_options(ACTOR, ORIGIN, NOW)
    request = verifier.registration_request
    assert request is not None
    secret = "PRIVATE-WEBAUTHN-REGISTRATION-2831"
    material = cast(dict[str, object], json.loads(_registration_response(request)))
    material["secret"] = secret
    response = json.dumps(material, separators=(",", ":"), sort_keys=True).encode()
    cancellation = Cancelled()

    def cancel(_response: str) -> None:
        raise cancellation

    monkeypatch.setattr(
        "intent_engineering.control_plane.webauthn_service.parse_registration_credential_json",
        cancel,
    )

    with pytest.raises(Cancelled) as caught:
        service.register(response, ACTOR, ORIGIN, NOW)

    assert caught.value is cancellation
    assert secret not in _repository_traceback_locals(caught.value)


def test_public_failure_is_fixed_unchained_and_does_not_retain_response_secret(
    authority: tuple[
        WebAuthnService,
        FakeVerifier,
        WebAuthnCredentialStore,
        WebAuthnChallengeStore,
    ],
) -> None:
    service, verifier, _, _ = authority
    _enroll(authority)
    payload = _payload()
    service.authentication_options(payload, ORIGIN, NOW)
    request = verifier.authentication_request
    assert request is not None
    secret = "PRIVATE-WEBAUTHN-ASSERTION-9173"
    verifier.fail_with_response = True

    with pytest.raises(HumanAuthorityError) as caught:
        service.verify(_authentication_response(request, secret=secret), payload, ORIGIN, NOW)

    rendered = traceback.TracebackException.from_exception(caught.value, capture_locals=True)
    service_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "intent_engineering/control_plane/webauthn_service.py" in frame.filename
    )
    assert caught.value.args == ("human authority unavailable",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert secret not in service_locals
