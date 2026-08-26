"""Build exact provider-write previews from human-review reconciliation cases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from intent_engineering.capture.mcp.profile_models import ProviderBinding, ProviderProfile
from intent_engineering.capture.mcp.selectors import bind_arguments
from intent_engineering.capture.mcp.session import detached_json
from intent_engineering.core.models import JsonValue, ReconciliationCase, ReconciliationStatus
from intent_engineering.mutations.models import (
    RemoteObject,
    WritePlan,
    identity_aliases_for,
    provider_binding_hash,
    provider_write_contract_hash,
    write_plan_id,
)

_DEFAULT_EXPIRY = timedelta(minutes=15)


class WritePlanError(ValueError):
    """Fixed public failure for an invalid or unsafe external write preview."""


def _build_result(
    case: ReconciliationCase,
    profile: ProviderProfile,
    binding: ProviderBinding,
    operation_name: object,
    current: RemoteObject,
    requested_fields: object,
    actor: object,
    authorized_contributors: object,
    identity_aliases: object,
    now: object,
) -> WritePlan | None:
    try:
        case = ReconciliationCase.model_validate(case.model_dump(mode="json"))
        profile = ProviderProfile.model_validate_json(profile.model_dump_json())
        binding = ProviderBinding.model_validate_json(binding.model_dump_json())
        binding.validate_against(profile)
        current = RemoteObject.model_validate(current.model_dump(mode="json"))
        if (
            case.status is not ReconciliationStatus.NEEDS_HUMAN
            or not case.requires_human
            or type(operation_name) is not str
            or operation_name not in profile.writes
            or type(actor) is not str
            or not actor.strip()
            or type(authorized_contributors) is not frozenset
            or any(
                type(value) is not str or not value.strip()
                for value in authorized_contributors
            )
            or actor not in authorized_contributors
            or not binding.actor_principals.get(actor)
            or current.profile_id != profile.id
            or current.profile_version != profile.version
            or type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() is None
            or type(requested_fields) is not dict
            or not requested_fields
        ):
            return None
        operation = profile.writes[operation_name]
        if current.object_type != operation.target_object:
            return None
        argument_sources = {argument.source for argument in operation.arguments.values()}
        if not {"target_id", "before_version"}.issubset(argument_sources):
            return None
        requested = detached_json(requested_fields)
        if type(requested) is not dict:
            return None
        if set(requested) - set(operation.allowed_fields):
            return None
        before = cast(dict[str, JsonValue], detached_json(dict(current.content)))
        after = {**before, **requested}
        if after == before:
            return None
        write_fields = {
            field: after[field] for field in operation.allowed_fields if field in after
        }
        Draft202012Validator(dict(operation.input_schema)).validate(write_fields)
        active_arguments = {
            name: argument
            for name, argument in operation.arguments.items()
            if argument.source != "field" or argument.field in write_fields
        }
        arguments = bind_arguments(
            active_arguments,
            {
                "target_id": current.id,
                "before_version": current.version,
                "fields": write_fields,
            },
        )
        if type(arguments) is not dict:
            return None
        created_at = now.astimezone(UTC)
        binding_hash = provider_binding_hash(binding)
        contract_hash = provider_write_contract_hash(profile, binding, operation_name)
        creator_aliases = identity_aliases_for(
            identity_aliases,
            actor,
            binding.actor_principals[actor],
        )
        conflicting_authors: list[JsonValue] = [
            cast(JsonValue, author)
            for author in sorted(
                {author for side in case.evidence_sides for author in side.authors}
            )
        ]
        material: dict[str, JsonValue] = {
            "schema_version": 1,
            "case_id": case.id,
            "connector_id": current.connector_id,
            "profile_id": current.profile_id,
            "profile_version": current.profile_version,
            "object_type": current.object_type,
            "binding_hash": binding_hash,
            "write_contract_hash": contract_hash,
            "operation": operation.semantic_name,
            "provider_operation": binding.tools[operation_name],
            "target_id": current.id,
            "before_version": current.version,
            "before": before,
            "after": after,
            "arguments": arguments,
            "evidence_refs": list(case.all_evidence_refs),
            "conflicting_authors": conflicting_authors,
            "created_by": actor,
            "created_by_aliases": list(creator_aliases),
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "expires_at": (created_at + _DEFAULT_EXPIRY).isoformat().replace("+00:00", "Z"),
        }
        return WritePlan(
            id=write_plan_id(material),
            case_id=case.id,
            connector_id=current.connector_id,
            profile_id=current.profile_id,
            profile_version=current.profile_version,
            object_type=current.object_type,
            binding_hash=binding_hash,
            write_contract_hash=contract_hash,
            operation=operation.semantic_name,
            provider_operation=binding.tools[operation_name],
            target_id=current.id,
            before_version=current.version,
            before=before,
            after=after,
            arguments=arguments,
            evidence_refs=case.all_evidence_refs,
            conflicting_authors=tuple(
                sorted({author for side in case.evidence_sides for author in side.authors})
            ),
            created_by=actor,
            created_by_aliases=creator_aliases,
            created_at=created_at,
            expires_at=created_at + _DEFAULT_EXPIRY,
        )
    except Exception:  # noqa: BLE001 - requested/provider data never crosses this boundary.
        return None


def build_write_plan(
    case: ReconciliationCase,
    profile: ProviderProfile,
    binding: ProviderBinding,
    operation_name: str,
    current: RemoteObject,
    requested_fields: dict[str, JsonValue],
    actor: str,
    authorized_contributors: frozenset[str],
    identity_aliases: dict[str, frozenset[str]],
    now: datetime,
) -> WritePlan:
    """Create one exact, expiring preview without constituting human approval."""
    result = _build_result(
        case,
        profile,
        binding,
        operation_name,
        current,
        requested_fields,
        actor,
        authorized_contributors,
        identity_aliases,
        now,
    )
    del (
        case,
        profile,
        binding,
        operation_name,
        current,
        requested_fields,
        actor,
        authorized_contributors,
        identity_aliases,
        now,
    )
    if result is None:
        raise WritePlanError("invalid external write plan") from None
    return result
