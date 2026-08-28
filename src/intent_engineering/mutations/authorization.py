"""Shared person-level independence checks for approved external writes."""

from __future__ import annotations

from intent_engineering.capture.mcp.profile_models import ProviderBinding
from intent_engineering.mutations.models import (
    WritePlan,
    identity_aliases_for,
    provider_binding_hash,
)


def _exact_policy(value: object) -> frozenset[str] | None:
    if type(value) is not frozenset or any(
        type(item) is not str or not item.strip() for item in value
    ):
        return None
    return value


def authenticated_approval_aliases(
    plan: WritePlan,
    binding: ProviderBinding,
    actor: object,
    *,
    authorized_contributors: object,
    authorized_approvers: object,
    identity_aliases: object,
) -> tuple[str, ...] | None:
    """Return current authenticated approver aliases only for an independent actor."""
    try:
        validated_plan = WritePlan.model_validate_json(plan.model_dump_json())
        validated_binding = ProviderBinding.model_validate_json(binding.model_dump_json())
        contributors = _exact_policy(authorized_contributors)
        approvers = _exact_policy(authorized_approvers)
        if (
            type(actor) is not str
            or not actor.strip()
            or contributors is None
            or approvers is None
            or validated_plan.created_by not in contributors
            or actor not in approvers
            or actor == validated_plan.created_by
            or actor in validated_plan.conflicting_authors
            or validated_binding.profile_id != validated_plan.profile_id
            or validated_binding.profile_version != validated_plan.profile_version
            or provider_binding_hash(validated_binding) != validated_plan.binding_hash
        ):
            return None
        creator_principals = validated_binding.actor_principals.get(validated_plan.created_by)
        actor_principals = validated_binding.actor_principals.get(actor)
        if not creator_principals or not actor_principals:
            return None
        creator_aliases = identity_aliases_for(
            identity_aliases,
            validated_plan.created_by,
            creator_principals,
        )
        actor_aliases = identity_aliases_for(
            identity_aliases,
            actor,
            actor_principals,
        )
        if (
            creator_aliases != validated_plan.created_by_aliases
            or not set(actor_aliases).isdisjoint(validated_plan.created_by_aliases)
            or not set(actor_aliases).isdisjoint(validated_plan.conflicting_authors)
        ):
            return None
        return actor_aliases
    except Exception:  # noqa: BLE001 - authorization inputs fail closed.
        return None


__all__ = ["authenticated_approval_aliases"]
