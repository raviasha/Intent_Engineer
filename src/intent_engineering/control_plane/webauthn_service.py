"""Repository-bound WebAuthn enrollment and human-decision verification."""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from urllib.parse import urlsplit

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes,
    options_to_json,
    parse_authentication_credential_json,
    parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    UserVerificationRequirement,
)

from intent_engineering.control_plane.models import (
    ChallengeRecord,
    CredentialRecord,
    HumanDecisionPayload,
)
from intent_engineering.control_plane.webauthn_store import (
    WebAuthnChallengeStore,
    WebAuthnCredentialStore,
    challenge_record_for_timestamp,
)
from intent_engineering.storage.transaction import LocalTransactionCoordinator

_RP_ID = "localhost"
_RP_NAME = "Intent Engineering"
_CHALLENGE_BYTES = 32
_CHALLENGE_LIFETIME = timedelta(minutes=5)
_CEREMONY_TIMEOUT_MS = 5 * 60 * 1000
_REDACTED_TIME = datetime(1970, 1, 1, tzinfo=UTC)


class HumanAuthorityError(ValueError):
    """The deliberately fixed public failure for unavailable human authority."""


@dataclass(frozen=True, slots=True)
class RegistrationRequest:
    """Exact server-selected inputs to one registration ceremony."""

    challenge: bytes
    rp_id: str
    expected_origin: str
    project_id: str
    repository_id: str
    actor: str
    user_verification: Literal["required"] = "required"


@dataclass(frozen=True, slots=True)
class VerifiedRegistration:
    """The bounded registration material returned by an authenticator verifier."""

    credential_id: bytes
    public_key: bytes
    sign_count: int
    user_verified: bool


@dataclass(frozen=True, slots=True)
class AuthenticationRequest:
    """Exact server-selected inputs to one authentication ceremony."""

    challenge: bytes
    rp_id: str
    expected_origin: str
    project_id: str
    repository_id: str
    actor: str
    payload_bytes: bytes
    credentials: tuple[CredentialRecord, ...]
    user_verification: Literal["required"] = "required"


@dataclass(frozen=True, slots=True)
class VerifiedAuthentication:
    """The bounded assertion result returned by an authenticator verifier."""

    credential_id: bytes
    new_sign_count: int
    user_verified: bool


@dataclass(frozen=True, slots=True)
class VerifiedHumanDecision:
    """One canonical decision proven by a repository-bound registered credential."""

    payload: HumanDecisionPayload
    credential: CredentialRecord
    verified_at: datetime


class WebAuthnVerifier(Protocol):
    """Port around the four platform WebAuthn operations used by the service."""

    def registration_options(self, request: RegistrationRequest) -> bytes: ...

    def verify_registration(
        self, response: bytes, request: RegistrationRequest
    ) -> VerifiedRegistration: ...

    def authentication_options(self, request: AuthenticationRequest) -> bytes: ...

    def verify_authentication(
        self, response: bytes, request: AuthenticationRequest
    ) -> VerifiedAuthentication: ...


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _credential_material(value: str) -> bytes:
    decoded = base64url_to_bytes(value)
    if _b64url(decoded) != value:
        raise ValueError("noncanonical credential material")
    return decoded


def _payload_digest(payload_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload_bytes).hexdigest()}"


def _counter_record_id(credential_id: bytes, sign_count: int) -> str:
    identity = hashlib.sha256(credential_id).hexdigest()
    return f"credential:{identity}:{sign_count}"


def _valid_origin(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == _RP_ID
        and port is not None
        and 1 <= port <= 65535
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.netloc == f"{_RP_ID}:{port}"
    )


def _validated_now(value: object) -> datetime:
    return challenge_record_for_timestamp(value).issued_at


def _validated_payload(value: object) -> HumanDecisionPayload:
    if not isinstance(value, HumanDecisionPayload):
        raise TypeError("invalid decision payload")
    return HumanDecisionPayload.model_validate_json(value.model_dump_json())


def _registration_challenge(response: bytes) -> bytes:
    if type(response) is not bytes:
        raise ValueError("invalid registration response")
    credential = None
    client_data = None
    challenge = b""
    try:
        credential = parse_registration_credential_json(response.decode("utf-8"))
        client_data = parse_client_data_json(credential.response.client_data_json)
        challenge = client_data.challenge
        if type(challenge) is not bytes or len(challenge) != _CHALLENGE_BYTES:
            raise ValueError("invalid registration challenge")
        return challenge
    finally:
        response = b""
        credential = None
        client_data = None
        challenge = b""


class PythonWebAuthnVerifier:
    """Production adapter for the reviewed ``webauthn`` package boundary."""

    def registration_options(self, request: RegistrationRequest) -> bytes:
        user_id = hashlib.sha256(
            f"{request.project_id}\0{request.repository_id}\0{request.actor}".encode()
        ).digest()
        options = generate_registration_options(
            rp_id=request.rp_id,
            rp_name=_RP_NAME,
            user_name=request.actor,
            user_id=user_id,
            user_display_name=request.actor,
            challenge=request.challenge,
            timeout=_CEREMONY_TIMEOUT_MS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.REQUIRED
            ),
        )
        return options_to_json(options).encode("utf-8")

    def verify_registration(
        self, response: bytes, request: RegistrationRequest
    ) -> VerifiedRegistration:
        encoded_response = ""
        verified = None
        try:
            encoded_response = response.decode("utf-8")
            verified = verify_registration_response(
                credential=encoded_response,
                expected_challenge=request.challenge,
                expected_rp_id=request.rp_id,
                expected_origin=request.expected_origin,
                require_user_presence=True,
                require_user_verification=True,
            )
            return VerifiedRegistration(
                credential_id=verified.credential_id,
                public_key=verified.credential_public_key,
                sign_count=verified.sign_count,
                user_verified=verified.user_verified,
            )
        finally:
            response = b""
            encoded_response = ""
            verified = None

    def authentication_options(self, request: AuthenticationRequest) -> bytes:
        options = generate_authentication_options(
            rp_id=request.rp_id,
            challenge=request.challenge,
            timeout=_CEREMONY_TIMEOUT_MS,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=_credential_material(record.credential_id))
                for record in request.credentials
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return options_to_json(options).encode("utf-8")

    def verify_authentication(
        self, response: bytes, request: AuthenticationRequest
    ) -> VerifiedAuthentication:
        encoded_response = ""
        credential = None
        current = None
        verified = None
        try:
            encoded_response = response.decode("utf-8")
            credential = parse_authentication_credential_json(encoded_response)
            current = next(
                (
                    record
                    for record in request.credentials
                    if _credential_material(record.credential_id) == credential.raw_id
                ),
                None,
            )
            if current is None:
                raise ValueError("unknown credential")
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=request.challenge,
                expected_rp_id=request.rp_id,
                expected_origin=request.expected_origin,
                credential_public_key=_credential_material(current.public_key),
                credential_current_sign_count=current.sign_count,
                require_user_verification=True,
            )
            return VerifiedAuthentication(
                credential_id=verified.credential_id,
                new_sign_count=verified.new_sign_count,
                user_verified=verified.user_verified,
            )
        finally:
            response = b""
            encoded_response = ""
            credential = None
            current = None
            verified = None


class WebAuthnService:
    """Issue and verify one-shot human authority for one exact local repository."""

    def __init__(
        self,
        *,
        project_id: str,
        repository_id: str,
        expected_origin: str,
        credentials: WebAuthnCredentialStore,
        challenges: WebAuthnChallengeStore,
        transactions: LocalTransactionCoordinator,
        verifier: WebAuthnVerifier | None = None,
        challenge_source: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        if (
            type(project_id) is not str
            or not project_id
            or type(repository_id) is not str
            or not repository_id
            or not _valid_origin(expected_origin)
            or not transactions.target_names.issuperset(
                {"webauthn_credentials", "webauthn_challenges"}
            )
            or credentials._transactions is not transactions
            or challenges._transactions is not transactions
        ):
            raise ValueError("invalid human authority configuration")
        # Reuse the strict repository identifier validation from the canonical model.
        ChallengeRecord(
            id="challenge:" + "0" * 64,
            project_id=project_id,
            repository_id=repository_id,
            actor="configuration",
            ceremony="registration",
            challenge="configuration",
            issued_at=datetime(2000, 1, 1, tzinfo=UTC),
            expires_at=datetime(2000, 1, 1, tzinfo=UTC),
        )
        self._project_id = project_id
        self._repository_id = repository_id
        self._expected_origin = expected_origin
        self._credentials = credentials
        self._challenges = challenges
        self._transactions = transactions
        self._verifier = verifier or PythonWebAuthnVerifier()
        self._challenge_source = challenge_source

    def _valid_actor_call(self, actor: object, origin: object, now: object) -> datetime:
        if (
            type(actor) is not str
            or not actor
            or type(origin) is not str
            or origin != self._expected_origin
        ):
            raise ValueError("invalid authority call")
        return _validated_now(now)

    def _registration_request(self, challenge: bytes, actor: str) -> RegistrationRequest:
        return RegistrationRequest(
            challenge=challenge,
            rp_id=_RP_ID,
            expected_origin=self._expected_origin,
            project_id=self._project_id,
            repository_id=self._repository_id,
            actor=actor,
        )

    def _current_credentials(self, actor: str) -> tuple[CredentialRecord, ...]:
        groups: dict[str, list[CredentialRecord]] = {}
        for record in self._credentials.list():
            if record.project_id != self._project_id or record.repository_id != self._repository_id:
                raise ValueError("credential binding mismatch")
            groups.setdefault(record.credential_id, []).append(record)
        current: list[CredentialRecord] = []
        for records in groups.values():
            first = records[0]
            invariant = first.model_dump(exclude={"id", "sign_count"})
            if any(
                record.model_dump(exclude={"id", "sign_count"}) != invariant
                for record in records[1:]
            ):
                raise ValueError("credential history mismatch")
            maximum = max(record.sign_count for record in records)
            latest = [record for record in records if record.sign_count == maximum]
            if len(latest) != 1:
                raise ValueError("ambiguous credential history")
            if first.actor == actor:
                current.append(latest[0])
        return tuple(sorted(current, key=lambda item: item.credential_id))

    def _registration_options(self, actor: str, origin: str, now: datetime) -> bytes:
        issued_at = self._valid_actor_call(actor, origin, now)
        challenge = self._challenge_source(_CHALLENGE_BYTES)
        if type(challenge) is not bytes or len(challenge) != _CHALLENGE_BYTES:
            raise ValueError("invalid challenge source")
        request = self._registration_request(challenge, actor)
        options = self._verifier.registration_options(request)
        if type(options) is not bytes or not options:
            raise ValueError("invalid registration options")
        record = ChallengeRecord(
            id=f"challenge:{challenge.hex()}",
            project_id=self._project_id,
            repository_id=self._repository_id,
            actor=actor,
            ceremony="registration",
            challenge=_b64url(challenge),
            issued_at=issued_at,
            expires_at=issued_at + _CHALLENGE_LIFETIME,
        )
        if not self._challenges.issue(record):
            raise ValueError("challenge collision")
        return options

    def registration_options(self, actor: str, origin: str, now: datetime) -> bytes:
        """Create one exact five-minute user-verifying registration ceremony."""
        result: bytes | None = None
        try:
            result = self._registration_options(actor, origin, now)
        except Exception:  # noqa: BLE001 - fixed public authority boundary
            result = None
        finally:
            actor = origin = ""
            now = _REDACTED_TIME
        if result is None:
            raise HumanAuthorityError("human authority unavailable") from None
        return result

    def _register(
        self,
        response: bytes,
        actor: str,
        origin: str,
        now: datetime,
        *,
        github_account_id: str | None = None,
        github_login: str | None = None,
    ) -> CredentialRecord:
        challenge = b""
        request = None
        try:
            verified_now = self._valid_actor_call(actor, origin, now)
            challenge = _registration_challenge(response)
            request = self._registration_request(challenge, actor)
            with self._transactions.transaction(rollback_base_exceptions=True):
                challenge_record = self._challenges.consume(
                    f"challenge:{challenge.hex()}", verified_now
                )
                if (
                    challenge_record.project_id != self._project_id
                    or challenge_record.repository_id != self._repository_id
                    or challenge_record.actor != actor
                    or challenge_record.ceremony != "registration"
                    or challenge_record.payload_digest is not None
                    or challenge_record.challenge != _b64url(challenge)
                ):
                    raise ValueError("registration binding mismatch")
                verified = self._verifier.verify_registration(response, request)
                if (
                    type(verified) is not VerifiedRegistration
                    or type(verified.credential_id) is not bytes
                    or not verified.credential_id
                    or type(verified.public_key) is not bytes
                    or not verified.public_key
                    or type(verified.sign_count) is not int
                    or not 0 <= verified.sign_count <= 2**32 - 1
                    or verified.user_verified is not True
                    or any(
                        record.credential_id == _b64url(verified.credential_id)
                        for record in self._credentials.list()
                    )
                ):
                    raise ValueError("registration verification mismatch")
                credential = CredentialRecord(
                    id=_counter_record_id(verified.credential_id, verified.sign_count),
                    project_id=self._project_id,
                    repository_id=self._repository_id,
                    actor=actor,
                    credential_id=_b64url(verified.credential_id),
                    public_key=_b64url(verified.public_key),
                    sign_count=verified.sign_count,
                    created_at=verified_now,
                    local_only=github_account_id is None and github_login is None,
                    github_account_id=github_account_id,
                    github_login=github_login,
                )
                if not self._credentials.put(credential):
                    raise ValueError("credential collision")
                return credential
        finally:
            response = b""
            challenge = b""
            request = None

    def register(
        self,
        response: bytes,
        actor: str,
        origin: str,
        now: datetime,
        *,
        github_account_id: str | None = None,
        github_login: str | None = None,
    ) -> CredentialRecord:
        """Verify and persist one repository-bound credential enrollment."""
        result: CredentialRecord | None = None
        try:
            result = self._register(
                response,
                actor,
                origin,
                now,
                github_account_id=github_account_id,
                github_login=github_login,
            )
        except Exception:  # noqa: BLE001 - fixed public authority boundary
            result = None
        finally:
            response = b""
            actor = origin = ""
            github_account_id = github_login = None
            now = _REDACTED_TIME
        if result is None:
            raise HumanAuthorityError("human authority unavailable") from None
        return result

    def _authentication_request(
        self, payload: HumanDecisionPayload, credentials: tuple[CredentialRecord, ...]
    ) -> AuthenticationRequest:
        payload_bytes = payload.canonical_bytes()
        return AuthenticationRequest(
            challenge=hashlib.sha256(payload_bytes).digest(),
            rp_id=_RP_ID,
            expected_origin=self._expected_origin,
            project_id=self._project_id,
            repository_id=self._repository_id,
            actor=payload.actor,
            payload_bytes=payload_bytes,
            credentials=credentials,
        )

    def _validate_decision_call(
        self, payload: object, origin: object, now: object
    ) -> tuple[HumanDecisionPayload, datetime]:
        validated = _validated_payload(payload)
        verified_now = _validated_now(now)
        if (
            type(origin) is not str
            or origin != self._expected_origin
            or validated.project_id != self._project_id
            or validated.repository_id != self._repository_id
            or not validated.issued_at <= verified_now < validated.expires_at
        ):
            raise ValueError("decision binding mismatch")
        return validated, verified_now

    def _authentication_options(
        self, payload: HumanDecisionPayload, origin: str, now: datetime
    ) -> bytes:
        validated, issued_at = self._validate_decision_call(payload, origin, now)
        credentials = self._current_credentials(validated.actor)
        if not credentials:
            raise ValueError("credential unavailable")
        request = self._authentication_request(validated, credentials)
        options = self._verifier.authentication_options(request)
        if type(options) is not bytes or not options:
            raise ValueError("invalid authentication options")
        record = ChallengeRecord(
            id=validated.challenge,
            project_id=self._project_id,
            repository_id=self._repository_id,
            actor=validated.actor,
            ceremony="authentication",
            challenge=_b64url(request.challenge),
            payload_digest=_payload_digest(request.payload_bytes),
            issued_at=issued_at,
            expires_at=issued_at + _CHALLENGE_LIFETIME,
        )
        if not self._challenges.issue(record):
            raise ValueError("challenge collision")
        return options

    def authentication_options(
        self, payload: HumanDecisionPayload, origin: str, now: datetime
    ) -> bytes:
        """Create one exact five-minute assertion bound to canonical payload bytes."""
        result: bytes | None = None
        try:
            result = self._authentication_options(payload, origin, now)
        except Exception:  # noqa: BLE001 - fixed public authority boundary
            result = None
        finally:
            payload = None  # type: ignore[assignment]
            origin = ""
            now = _REDACTED_TIME
        if result is None:
            raise HumanAuthorityError("human authority unavailable") from None
        return result

    @staticmethod
    def _verified_authentication(value: object) -> VerifiedAuthentication:
        if (
            type(value) is not VerifiedAuthentication
            or type(value.credential_id) is not bytes
            or not value.credential_id
            or type(value.new_sign_count) is not int
            or not 0 <= value.new_sign_count <= 2**32 - 1
            or value.user_verified is not True
        ):
            raise ValueError("authentication verification mismatch")
        return value

    def _verify(
        self,
        response: bytes,
        payload: HumanDecisionPayload,
        origin: str,
        now: datetime,
    ) -> VerifiedHumanDecision:
        validated, verified_at = self._validate_decision_call(payload, origin, now)
        payload_bytes = validated.canonical_bytes()
        digest = _payload_digest(payload_bytes)
        challenge_bytes = hashlib.sha256(payload_bytes).digest()
        try:
            with self._transactions.transaction(rollback_base_exceptions=True):
                challenge = self._challenges.consume(validated.challenge, verified_at)
                if (
                    challenge.project_id != self._project_id
                    or challenge.repository_id != self._repository_id
                    or challenge.actor != validated.actor
                    or challenge.ceremony != "authentication"
                    or challenge.payload_digest != digest
                    or challenge.challenge != _b64url(challenge_bytes)
                ):
                    raise ValueError("authentication binding mismatch")
                current = self._current_credentials(validated.actor)
                if not current:
                    raise ValueError("credential unavailable")
                request = self._authentication_request(validated, current)
                result = self._verified_authentication(
                    self._verifier.verify_authentication(response, request)
                )
                encoded_id = _b64url(result.credential_id)
                credential = next(
                    (record for record in current if record.credential_id == encoded_id), None
                )
                if credential is None:
                    raise ValueError("credential substitution")
                if (credential.sign_count, result.new_sign_count) != (0, 0) and (
                    result.new_sign_count <= credential.sign_count
                ):
                    raise ValueError("credential counter rollback")
                if result.new_sign_count == credential.sign_count:
                    verified_credential = credential
                else:
                    verified_credential = credential.model_copy(
                        update={
                            "id": _counter_record_id(result.credential_id, result.new_sign_count),
                            "sign_count": result.new_sign_count,
                        }
                    )
                    if not self._credentials.put(verified_credential):
                        raise ValueError("credential counter collision")
                return VerifiedHumanDecision(
                    payload=validated,
                    credential=verified_credential,
                    verified_at=verified_at,
                )
        finally:
            response = b""
            payload = None  # type: ignore[assignment]
            origin = ""
            now = _REDACTED_TIME
            payload_bytes = challenge_bytes = b""
            digest = ""

    def verify(
        self,
        response: bytes,
        payload: HumanDecisionPayload,
        origin: str,
        now: datetime,
    ) -> VerifiedHumanDecision:
        """Verify one exact decision and consume its authority at most once."""
        result: VerifiedHumanDecision | None = None
        try:
            result = self._verify(response, payload, origin, now)
        except Exception:  # noqa: BLE001 - fixed public authority boundary
            result = None
        finally:
            response = b""
            payload = None  # type: ignore[assignment]
            origin = ""
            now = _REDACTED_TIME
        if result is None:
            raise HumanAuthorityError("human authority unavailable") from None
        return result


__all__ = [
    "AuthenticationRequest",
    "HumanAuthorityError",
    "PythonWebAuthnVerifier",
    "RegistrationRequest",
    "VerifiedAuthentication",
    "VerifiedHumanDecision",
    "VerifiedRegistration",
    "WebAuthnService",
    "WebAuthnVerifier",
]
