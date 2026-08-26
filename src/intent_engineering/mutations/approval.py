"""Create independent local approvals after exact interactive confirmation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from intent_engineering.capture.mcp.profile_models import ProviderBinding
from intent_engineering.core.models import JsonValue
from intent_engineering.mutations.models import (
    ApprovalRecord,
    WritePlan,
    approval_id,
    identity_aliases_for,
    provider_binding_hash,
)

_MAX_APPROVAL_WINDOW = timedelta(minutes=15)


class ApprovalError(ValueError):
    """Fixed public failure for missing, stale, unauthorized, or noninteractive approval."""


def _approval_result(
    plan: WritePlan,
    binding: ProviderBinding,
    actor: object,
    now: object,
    expires_in: object,
    confirmation: object,
    interactive: object,
    authorized_approvers: object,
    identity_aliases: object,
) -> ApprovalRecord | None:
    try:
        plan = WritePlan.model_validate_json(plan.model_dump_json())
        binding = ProviderBinding.model_validate_json(binding.model_dump_json())
        provider_principals = (
            binding.actor_principals.get(actor) if type(actor) is str else None
        )
        if (
            type(actor) is not str
            or not actor.strip()
            or type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() is None
            or type(expires_in) is not timedelta
            or expires_in <= timedelta(0)
            or expires_in > _MAX_APPROVAL_WINDOW
            or type(interactive) is not bool
            or interactive is not True
            or type(confirmation) is not str
            or confirmation != f"approve {plan.id}"
            or type(authorized_approvers) is not frozenset
            or any(type(value) is not str or not value.strip() for value in authorized_approvers)
            or actor not in authorized_approvers
            or actor == plan.created_by
            or provider_binding_hash(binding) != plan.binding_hash
            or binding.profile_id != plan.profile_id
            or binding.profile_version != plan.profile_version
            or actor in plan.conflicting_authors
            or not provider_principals
        ):
            return None
        creator_provider_principals = binding.actor_principals.get(plan.created_by)
        if not creator_provider_principals:
            return None
        authenticated_creator_aliases = identity_aliases_for(
            identity_aliases,
            plan.created_by,
            creator_provider_principals,
        )
        if authenticated_creator_aliases != plan.created_by_aliases:
            return None
        actor_aliases = frozenset(
            identity_aliases_for(identity_aliases, actor, provider_principals)
        )
        if not actor_aliases.isdisjoint(plan.created_by_aliases) or not actor_aliases.isdisjoint(
            plan.conflicting_authors
        ):
            return None
        approved_at = now.astimezone(UTC)
        if approved_at < plan.created_at or approved_at >= plan.expires_at:
            return None
        expires_at = min(approved_at + expires_in, plan.expires_at)
        if expires_at <= approved_at:
            return None
        material: dict[str, JsonValue] = {
            "schema_version": 1,
            "plan_id": plan.id,
            "plan_hash": plan.canonical_hash,
            "target_version": plan.before_version,
            "actor": actor,
            "actor_aliases": cast(JsonValue, sorted(actor_aliases)),
            "plan_created_at": plan.created_at.isoformat().replace("+00:00", "Z"),
            "plan_expires_at": plan.expires_at.isoformat().replace("+00:00", "Z"),
            "approved_at": approved_at.isoformat().replace("+00:00", "Z"),
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            "confirmation_method": "interactive",
        }
        return ApprovalRecord(
            id=approval_id(material),
            plan_id=plan.id,
            plan_hash=plan.canonical_hash,
            target_version=plan.before_version,
            actor=actor,
            actor_aliases=tuple(sorted(actor_aliases)),
            plan_created_at=plan.created_at,
            plan_expires_at=plan.expires_at,
            approved_at=approved_at,
            expires_at=expires_at,
            confirmation_method="interactive",
        )
    except Exception:  # noqa: BLE001 - plan/confirmation data never crosses this boundary.
        return None


def approve_plan(
    plan: WritePlan,
    binding: ProviderBinding,
    *,
    actor: str,
    now: datetime,
    expires_in: timedelta,
    confirmation: str,
    interactive: bool,
    authorized_approvers: frozenset[str],
    identity_aliases: dict[str, frozenset[str]],
) -> ApprovalRecord:
    """Record a separately authorized exact confirmation for one unchanged plan."""
    result = _approval_result(
        plan,
        binding,
        actor,
        now,
        expires_in,
        confirmation,
        interactive,
        authorized_approvers,
        identity_aliases,
    )
    del (
        plan,
        binding,
        actor,
        now,
        expires_in,
        confirmation,
        interactive,
        authorized_approvers,
        identity_aliases,
    )
    if result is None:
        raise ApprovalError("external write approval rejected") from None
    return result
