"""Repository-bound local human authority control plane."""

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
]
