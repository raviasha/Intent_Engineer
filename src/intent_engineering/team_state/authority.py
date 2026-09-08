"""Canonical identifiers and bytes for version-two team authority."""

from __future__ import annotations

import hashlib

from intent_engineering.team_state.models import (
    _PROJECT_ID,
    _REPOSITORY_ID,
    MAX_AUTHORITY_BYTES,
    DeviceCertificateClaimsV2,
    TeamAuthorityRegistryV2,
    _canonical_json,
    _derived_member_id,
    _derived_recipient_key_id,
    _derived_root_key_id,
    _derived_signature_id,
)


def _require_scope(project_id: object, repository_id: object) -> tuple[str, str]:
    if type(project_id) is not str or type(repository_id) is not str:
        raise ValueError("invalid team authority scope")
    try:
        if (
            _PROJECT_ID.fullmatch(project_id) is None
            or _REPOSITORY_ID.fullmatch(repository_id) is None
        ):
            raise ValueError("invalid team authority scope")
        _host, owner, repository = repository_id.split("/")
        if owner in {".", ".."} or repository in {".", ".."} or repository.endswith(".git"):
            raise ValueError("invalid team authority scope")
    except (AttributeError, ValueError) as error:
        raise ValueError("invalid team authority scope") from error
    return project_id, repository_id


def derive_root_key_id(project_id: str, repository_id: str, public_key: str) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(public_key) is not str:
        raise ValueError("invalid root public key")
    return _derived_root_key_id(project_id, repository_id, public_key)


def derive_member_id(project_id: str, repository_id: str, github_account_id: int) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(github_account_id) is not int or github_account_id <= 0:
        raise ValueError("invalid GitHub account identity")
    return _derived_member_id(project_id, repository_id, github_account_id)


def derive_recipient_key_id(project_id: str, repository_id: str, public_key: str) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(public_key) is not str:
        raise ValueError("invalid recipient public key")
    return _derived_recipient_key_id(project_id, repository_id, public_key)


def derive_signature_id(project_id: str, repository_id: str, public_key: str) -> str:
    project_id, repository_id = _require_scope(project_id, repository_id)
    if type(public_key) is not str:
        raise ValueError("invalid signing public key")
    return _derived_signature_id(project_id, repository_id, public_key)


def canonical_certificate_claims_bytes(claims: DeviceCertificateClaimsV2) -> bytes:
    if type(claims) is not DeviceCertificateClaimsV2:
        raise TypeError("claims must be DeviceCertificateClaimsV2")
    validated = DeviceCertificateClaimsV2.model_validate(claims.model_dump(mode="python"))
    content = _canonical_json(validated.model_dump(mode="json"))
    if not content or len(content) > 16 * 1024:
        raise ValueError("device certificate claims are oversized")
    return content


def derive_certificate_id(claims: DeviceCertificateClaimsV2) -> str:
    return (
        "certificate:sha256:"
        + hashlib.sha256(
            b"intent.team-device-certificate.v2\0" + canonical_certificate_claims_bytes(claims)
        ).hexdigest()
    )


def canonical_authority_bytes(authority: TeamAuthorityRegistryV2) -> bytes:
    if type(authority) is not TeamAuthorityRegistryV2:
        raise TypeError("authority must be TeamAuthorityRegistryV2")
    validated = TeamAuthorityRegistryV2.model_validate(authority.model_dump(mode="python"))
    content = _canonical_json(validated.model_dump(mode="json"))
    if not content or len(content) > MAX_AUTHORITY_BYTES:
        raise ValueError("team authority registry is oversized")
    return content


def authority_digest(authority: TeamAuthorityRegistryV2) -> str:
    return "sha256:" + hashlib.sha256(canonical_authority_bytes(authority)).hexdigest()


__all__ = [
    "authority_digest",
    "canonical_authority_bytes",
    "canonical_certificate_claims_bytes",
    "derive_certificate_id",
    "derive_member_id",
    "derive_recipient_key_id",
    "derive_root_key_id",
    "derive_signature_id",
]
