"""Conservative local authorization for provider-native evidence ACLs."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class AuthorizationDecision(StrEnum):
    """Whether one local actor may consume one source object."""

    ALLOW = "allow"
    DENY = "deny"


def authorize(
    local_actor: str,
    actor_principals: Mapping[str, frozenset[str]],
    evidence_acl: frozenset[str],
) -> AuthorizationDecision:
    """Allow public evidence or an exact mapped-principal intersection; deny otherwise."""
    if not evidence_acl:
        return AuthorizationDecision.ALLOW
    principals = actor_principals.get(local_actor)
    if principals is None or principals.isdisjoint(evidence_acl):
        return AuthorizationDecision.DENY
    return AuthorizationDecision.ALLOW
