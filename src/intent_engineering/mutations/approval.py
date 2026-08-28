"""Create independent local approvals after exact interactive confirmation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from intent_engineering.capture.mcp.profile_models import ProviderBinding
from intent_engineering.core.models import JsonValue
from intent_engineering.mutations.authorization import authenticated_approval_aliases
from intent_engineering.mutations.models import (
    ApprovalRecord,
    WritePlan,
    approval_id,
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
        ):
            return None
        authenticated_actor_aliases = authenticated_approval_aliases(
            plan,
            binding,
            actor,
            authorized_contributors=frozenset({plan.created_by}),
            authorized_approvers=authorized_approvers,
            identity_aliases=identity_aliases,
        )
        if authenticated_actor_aliases is None:
            return None
        actor_aliases = frozenset(authenticated_actor_aliases)
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
