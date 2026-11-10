"""Repository-bound local human authority control plane."""

import hashlib
import json
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from intent_engineering.control_plane.models import (
    AttentionRoute,
    ChallengeRecord,
    CredentialRecord,
    DecisionAction,
    DecisionSubject,
    DevStatus,
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
    WebAuthnVerifier,
)

if TYPE_CHECKING:
    from intent_engineering.control_plane.service import ControlPlaneError, ControlPlaneService
    from intent_engineering.control_plane.web import build_control_plane_app


_ASSET_NAMES = frozenset({"index.html", "app.js", "styles.css"})


def control_plane_asset(name: str) -> bytes:
    """Read one explicitly packaged review asset without accepting a filesystem path."""
    if type(name) is not str or name not in _ASSET_NAMES:
        raise ValueError("control plane asset unavailable")
    return files("intent_engineering.control_plane").joinpath("assets", name).read_bytes()


def local_repository_identity(project_id: str, directory_identity: tuple[int, int]) -> str:
    """Derive the stable local repository binding shared by service and process metadata."""
    if (
        type(project_id) is not str
        or not project_id
        or type(directory_identity) is not tuple
        or len(directory_identity) != 2
        or any(type(value) is not int or value < 0 for value in directory_identity)
    ):
        raise ValueError("invalid local repository identity")
    material = json.dumps(
        {
            "schema": "intent.local-repository.v1",
            "project_id": project_id,
            "directory_device": directory_identity[0],
            "directory_inode": directory_identity[1],
        },
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"repo:sha256:{hashlib.sha256(material).hexdigest()}"


def __getattr__(name: str) -> Any:
    """Load service/web exports lazily so Runtime submodule imports stay acyclic."""
    if name in {"ControlPlaneError", "ControlPlaneService"}:
        from intent_engineering.control_plane.service import ControlPlaneError, ControlPlaneService

        return {
            "ControlPlaneError": ControlPlaneError,
            "ControlPlaneService": ControlPlaneService,
        }[name]
    if name == "build_control_plane_app":
        from intent_engineering.control_plane.web import build_control_plane_app

        return build_control_plane_app
    raise AttributeError(name)


__all__ = [
    "AttentionRoute",
    "AuthenticationRequest",
    "ChallengeRecord",
    "ControlPlaneError",
    "ControlPlaneService",
    "CredentialRecord",
    "DecisionAction",
    "DecisionSubject",
    "DevStatus",
    "HumanAuthorityError",
    "HumanDecisionPayload",
    "PythonWebAuthnVerifier",
    "RegistrationRequest",
    "VerifiedAuthentication",
    "VerifiedHumanDecision",
    "VerifiedRegistration",
    "WebAuthnService",
    "WebAuthnVerifier",
    "build_control_plane_app",
    "control_plane_asset",
    "local_repository_identity",
]
