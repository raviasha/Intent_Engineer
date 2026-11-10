"""Profile-driven, ACL-aware read connector for collaboration MCP servers."""

from __future__ import annotations

import json
import re
from asyncio import CancelledError
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import ClassVar, Literal, cast
from urllib.parse import quote

from pydantic import ConfigDict, Field, ValidationError, field_serializer, field_validator

from intent_engineering.capture.base import (
    ConnectorError,
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.capture.mcp.authorization import AuthorizationDecision, authorize
from intent_engineering.capture.mcp.profile_models import (
    ProviderProfile,
    ReadOperation,
)
from intent_engineering.capture.mcp.runtime import McpRuntime
from intent_engineering.capture.mcp.selectors import bind_arguments, select_value
from intent_engineering.capture.mcp.session import McpConnectorConfig, thaw_json
from intent_engineering.core.models import EvidenceRecord, JsonValue
from intent_engineering.core.models._base import StrictModel
from intent_engineering.storage.jsonl.strict import loads_strict_object

_MAX_PAGES = 128
_MAX_OBJECTS = 10_000
_MAX_CURSOR_BYTES = 1_048_576
_MAX_PROVIDER_CURSOR_BYTES = 65_536
_CHECKPOINT_HASH = r"^sha256:[0-9a-f]{64}$"
_RESOURCE_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_-]{0,255})\}")
_RESOURCE_BOUNDARIES = frozenset(":/?#[]@!$&'()*+,;=")


def _canonical_json(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(value: JsonValue) -> str:
    return f"sha256:{sha256(_canonical_json(value)).hexdigest()}"


def _mcp_profile_identity(profile_id: str, profile_version: str, object_type: str) -> str:
    """Return the reversible-envelope digest used to bind profile fields to a ledger."""
    return _digest(
        {
            "object_type": object_type,
            "profile_id": profile_id,
            "profile_version": profile_version,
        }
    ).removeprefix("sha256:")


def _parse_utc(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid observed time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid observed time")
    return parsed.astimezone(UTC)


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"invalid {label}")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _cursor_value(value: object) -> str | int | None:
    if value is None or type(value) in {str, int}:
        if type(value) is str and not value:
            raise ValueError("invalid MCP cursor")
        if type(value) is int and value < 0:
            raise ValueError("invalid MCP cursor")
        if len(str(value).encode("utf-8")) > _MAX_PROVIDER_CURSOR_BYTES:
            raise ValueError("invalid MCP cursor")
        return cast(str | int | None, value)
    raise ValueError("invalid MCP cursor")


def _resource_argument(value: JsonValue) -> str:
    if type(value) is str:
        encoded = value
    else:
        encoded = _canonical_json(value).decode("utf-8")
    return quote(encoded, safe="")


def _resource_uri(template: str, arguments: dict[str, JsonValue]) -> str:
    """Expand one exact allowlisted URI template without expressions or implicit arguments."""
    placeholders = _RESOURCE_PLACEHOLDER.findall(template)
    if not placeholders:
        if arguments or "{" in template or "}" in template:
            raise ValueError("invalid MCP resource binding")
        return template
    if set(placeholders) != set(arguments):
        raise ValueError("invalid MCP resource binding")
    for match in _RESOURCE_PLACEHOLDER.finditer(template):
        if match.start() and template[match.start() - 1] not in _RESOURCE_BOUNDARIES:
            raise ValueError("invalid MCP resource binding")
        if match.end() < len(template) and template[match.end()] not in _RESOURCE_BOUNDARIES:
            raise ValueError("invalid MCP resource binding")
    expanded = _RESOURCE_PLACEHOLDER.sub(
        lambda match: _resource_argument(arguments[match.group(1)]),
        template,
    )
    if "{" in expanded or "}" in expanded or len(expanded.encode("utf-8")) > 8192:
        raise ValueError("invalid MCP resource binding")
    return expanded


class McpCheckpoint(StrictModel):
    """Canonical cursor bound to one actor-scoped profile object connector."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        arbitrary_types_allowed=True,
    )

    cursor_schema_version: Literal[1] = 1
    connector_id: str
    profile_id: str
    profile_version: str
    object_type: str
    scope_hash: str = Field(pattern=_CHECKPOINT_HASH)
    source_hash: str = Field(pattern=_CHECKPOINT_HASH)
    provider_cursor: str | int | None = None
    observed_versions: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("cursor_schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("cursor schema version must be an integer")
        return value

    @field_validator("connector_id", "profile_id", "profile_version", "object_type", mode="before")
    @classmethod
    def validate_text(cls, value: object) -> object:
        return _required_text(value, "checkpoint field")

    @field_validator("provider_cursor", mode="before")
    @classmethod
    def validate_provider_cursor(cls, value: object) -> object:
        return _cursor_value(value)

    @field_validator("observed_versions", mode="before")
    @classmethod
    def reject_nonexact_versions(cls, value: object) -> object:
        if type(value) is not dict:
            raise ValueError("invalid observed versions")
        return value

    @field_validator("observed_versions")
    @classmethod
    def freeze_versions(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if len(value) > _MAX_OBJECTS:
            raise ValueError("too many observed versions")
        frozen: dict[str, str] = {}
        for object_id, version in value.items():
            frozen[_required_text(object_id, "external object identifier")] = _required_text(
                version, "external object version"
            )
        return MappingProxyType(dict(sorted(frozen.items())))

    @field_serializer("observed_versions")
    def serialize_versions(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)

    def encode(self) -> str:
        """Return strict deterministic JSON for the shared string cursor store."""
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > _MAX_CURSOR_BYTES:
            raise ValueError("MCP checkpoint is too large")
        return encoded

    @classmethod
    def decode(cls, value: str, *, connector: McpConnector) -> McpCheckpoint:
        """Decode and authenticate one cursor for its exact connector instance."""
        code, decoded = _decode_scoped_checkpoint_result(cls, value, connector)
        del value, connector
        if code == "noncanonical":
            raise ValueError("noncanonical MCP checkpoint")
        if code == "invalid":
            raise ValueError("invalid MCP checkpoint")
        if code == "scope" or decoded is None:
            raise ValueError("MCP checkpoint scope mismatch")
        return decoded

    @classmethod
    def decode_unscoped(cls, value: str) -> McpCheckpoint:
        """Strictly decode a cursor for deep validation before runtime assembly."""
        code, decoded = _decode_checkpoint_result(cls, value)
        del value
        if code == "noncanonical":
            raise ValueError("noncanonical MCP checkpoint")
        if code == "invalid" or decoded is None:
            raise ValueError("invalid MCP checkpoint")
        return decoded


def _decode_checkpoint_result(
    model: type[McpCheckpoint], value: object
) -> tuple[Literal["ok", "invalid", "noncanonical"], McpCheckpoint | None]:
    """Decode behind a non-raising boundary so public failures retain no cursor text."""
    if type(value) is not str or not value or len(value.encode("utf-8")) > _MAX_CURSOR_BYTES:
        return "invalid", None
    try:
        payload = loads_strict_object(value)
        decoded = model.model_validate(payload)
        if decoded.encode() != value:
            return "noncanonical", None
        return "ok", decoded
    except (TypeError, ValidationError, ValueError):
        return "invalid", None


def _decode_scoped_checkpoint_result(
    model: type[McpCheckpoint],
    value: object,
    connector: McpConnector,
) -> tuple[Literal["ok", "invalid", "noncanonical", "scope"], McpCheckpoint | None]:
    """Authenticate behind a non-raising boundary so mismatches retain no decoded cursor."""
    code, decoded = _decode_checkpoint_result(model, value)
    if code != "ok" or decoded is None:
        return code, None
    if (
        decoded.connector_id != connector.connector_id
        or decoded.profile_id != connector.profile.id
        or decoded.profile_version != connector.profile.version
        or decoded.object_type != connector.object_name
        or decoded.scope_hash != connector.scope_hash
        or decoded.source_hash != connector.source_hash
        or any(
            not object_id.startswith(f"{connector.profile.id}:")
            for object_id in decoded.observed_versions
        )
    ):
        return "scope", None
    return "ok", decoded


class McpConnector:
    """Capture one profile object type for one locally authorized actor."""

    connector_type: ClassVar[str] = "mcp"

    def __init__(
        self,
        runtime: McpRuntime,
        *,
        config: McpConnectorConfig,
        profile: ProviderProfile,
        object_name: str,
        local_actor: str,
    ) -> None:
        config.binding.validate_against(profile)
        if object_name not in profile.objects:
            raise ValueError("unknown MCP object profile")
        self.runtime = runtime
        self.config = config
        self.profile = profile
        self.object_name = object_name
        self.object_profile = profile.objects[object_name]
        self.local_actor = _required_text(local_actor, "local actor")
        scope = cast(JsonValue, config.model_dump(mode="json")["scope"])
        self.scope_hash = _digest(scope)
        self.source_hash = self._source_hash()
        actor_identity = _digest(
            {
                "actor": self.local_actor,
                "principals": cast(
                    JsonValue,
                    sorted(config.binding.actor_principals.get(self.local_actor, frozenset())),
                ),
            }
        ).removeprefix("sha256:")
        profile_identity = _mcp_profile_identity(profile.id, profile.version, object_name)
        source_identity = self.source_hash.removeprefix("sha256:")
        scope_identity = self.scope_hash.removeprefix("sha256:")
        self.connector_id = (
            f"mcp:{config.id}:{profile_identity}:{source_identity}:"
            f"{scope_identity}:{actor_identity}"
        )
        self._cache: dict[tuple[str, str], RawSourceObject] = {}
        self._seen_versions: set[tuple[str, str]] = set()
        self._last_discovered: tuple[SourceObject, ...] = ()
        self._prior_checkpoint: McpCheckpoint | None = None
        self._provider_cursor: str | int | None = None
        self._checkpoint_pending = False
        self._discovery_failed = False

    @property
    def cached_evidence_ids(self) -> tuple[str, ...]:
        """Expose only immutable IDs for diagnostics, never cached provider content."""
        return tuple(
            normalize_raw_source(raw).id
            for raw in sorted(
                self._cache.values(),
                key=lambda item: (item.external_object_id, item.external_version),
            )
        )

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        """Discover one bounded generation and retain authorized fetched objects only."""
        if self._checkpoint_pending:
            raise ConnectorError("MCP discovery failed")
        self.abort_sync()
        prior: McpCheckpoint | None = None
        invalid_cursor = False
        try:
            prior = (
                self._initial_checkpoint()
                if cursor is None
                else McpCheckpoint.decode(cursor, connector=self)
            )
        except (TypeError, ValidationError, ValueError):
            invalid_cursor = True
        if invalid_cursor or prior is None:
            cursor = None
            self.abort_sync()
            raise ConnectorError("MCP discovery failed") from None
        self._prior_checkpoint = prior
        self._provider_cursor = prior.provider_cursor
        self._checkpoint_pending = True
        try:
            failed = await self._discover_generation(prior)
        except CancelledError:
            cursor = None
            prior = None
            self.abort_sync()
            raise
        if failed and not self._cache:
            cursor = None
            prior = None
            self.abort_sync()
            raise ConnectorError("MCP discovery failed") from None
        discovered = tuple(
            sorted(
                (
                    SourceObject(
                        external_object_id=raw.external_object_id,
                        external_version=raw.external_version,
                        locator=raw.source_locator,
                    )
                    for raw in self._cache.values()
                ),
                key=lambda item: (item.external_object_id, item.external_version),
            )
        )
        self._last_discovered = discovered
        return discovered

    async def _discover_generation(self, prior: McpCheckpoint) -> bool:
        operation = self.profile.operations[self.object_profile.discover_operation]
        cursor = prior.provider_cursor
        seen_cursors: set[str | int | None] = set()
        object_count = 0
        try:
            await self.runtime.validate_binding(self.config.server, self.config.binding)
            for _page in range(_MAX_PAGES):
                if cursor in seen_cursors:
                    raise ValueError("cyclic MCP cursor")
                seen_cursors.add(cursor)
                response = await self._invoke_read(
                    operation,
                    {
                        "cursor": cursor,
                        "scope": self._scope_copy(),
                        "fields": {},
                    },
                )
                selected_items = select_value(response, operation.item_selector)
                if type(selected_items) is not list:
                    raise ValueError("invalid MCP discovery items")
                for item in selected_items:
                    object_count += 1
                    if object_count > _MAX_OBJECTS:
                        raise ValueError("too many MCP source objects")
                    await self._fetch_discovered_item(item, prior)
                if operation.pagination == "none":
                    cursor = None
                    break
                assert operation.next_cursor_selector is not None
                cursor = _cursor_value(select_value(response, operation.next_cursor_selector))
                if cursor is None:
                    break
            else:
                raise ValueError("too many MCP pages")
            self._provider_cursor = cursor
            return False
        except CancelledError:
            raise
        except Exception:  # noqa: BLE001 - provider payload and error are discarded here
            self._discovery_failed = True
            return True

    async def _fetch_discovered_item(
        self,
        item: JsonValue,
        prior: McpCheckpoint,
    ) -> None:
        provider_object_id = _required_text(
            select_value(item, self.object_profile.external_id),
            "external object identifier",
        )
        operation = self.profile.operations[self.object_profile.fetch_operation]
        response = await self._invoke_read(
            operation,
            {
                "object_id": provider_object_id,
                "object_version": prior.observed_versions.get(
                    self._external_object_id(provider_object_id)
                ),
                "scope": self._scope_copy(),
                "fields": {},
            },
        )
        fetched = select_value(response, operation.item_selector)
        raw = self._raw_from_payload(fetched)
        if raw.external_object_id != self._external_object_id(provider_object_id):
            raise ValueError("MCP fetch identity mismatch")
        key = (raw.external_object_id, raw.external_version)
        if key in self._seen_versions:
            raise ValueError("duplicate MCP discovery identity")
        self._seen_versions.add(key)
        if prior.observed_versions.get(raw.external_object_id) == raw.external_version:
            return
        if (
            authorize(
                self.local_actor,
                self.config.binding.actor_principals,
                frozenset(raw.acl),
            )
            is AuthorizationDecision.ALLOW
        ):
            self._cache[key] = raw

    async def _invoke_read(
        self,
        operation: ReadOperation,
        context: dict[str, JsonValue],
    ) -> JsonValue:
        arguments = bind_arguments(operation.arguments, context)
        if operation.kind == "tool":
            name = self.config.binding.tools[operation.semantic_name]
            return await self.runtime.call(self.config.server, name, arguments)
        template = self.config.binding.resources[operation.semantic_name]
        uri = _resource_uri(template, arguments)
        return await self.runtime.read_resource(self.config.server, uri)

    def _raw_from_payload(self, payload: JsonValue | None) -> RawSourceObject:
        object_profile = self.object_profile
        provider_object_id = _required_text(
            select_value(payload, object_profile.external_id),
            "external object identifier",
        )
        version = _required_text(
            select_value(payload, object_profile.external_version),
            "external object version",
        )
        author_value = select_value(payload, object_profile.author)
        author = _required_text(author_value, "provider author")
        observed_at = _parse_utc(select_value(payload, object_profile.observed_at))
        locator = _required_text(
            select_value(payload, object_profile.locator),
            "source locator",
        )
        parent = (
            _optional_text(select_value(payload, object_profile.parent_ref), "parent reference")
            if object_profile.parent_ref is not None
            else None
        )
        acl_value = [] if object_profile.acl is None else select_value(payload, object_profile.acl)
        if type(acl_value) is not list or any(type(item) is not str for item in acl_value):
            raise ValueError("invalid evidence ACL")
        acl = tuple(sorted(set(cast(list[str], acl_value))))
        content: dict[str, JsonValue] = {
            field: select_value(payload, selector)
            for field, selector in object_profile.content.items()
        }
        evidence_payload: dict[str, JsonValue] = {
            "kind": "mcp_object",
            "profile_id": self.profile.id,
            "profile_version": self.profile.version,
            "object_type": self.object_name,
            "scope_hash": self.scope_hash,
            "source_hash": self.source_hash,
            "parent_context": self._external_object_id(parent) if parent is not None else None,
            "content": content,
        }
        return RawSourceObject(
            connector_type=self.connector_type,
            external_object_id=self._external_object_id(provider_object_id),
            external_version=version,
            author=author,
            observed_at=observed_at,
            source_locator=locator,
            content_hash=_digest(content),
            payload=evidence_payload,
            acl=acl,
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        """Return only an authorized exact object retained by the active generation."""
        raw = self._cache.get((object_id, version))
        if raw is None:
            raise ConnectorError("MCP fetch failed")
        return raw

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        """Normalize only an exact active-generation object through the shared constructor."""
        record: EvidenceRecord | None = None
        failed = False
        try:
            cached = self._cache.get((raw.external_object_id, raw.external_version))
            if raw.connector_type != self.connector_type or cached != raw:
                failed = True
            else:
                record = normalize_raw_source(raw)
        except Exception:  # noqa: BLE001 - fixed failure is raised after raw is deleted
            failed = True
        del raw
        if failed or record is None:
            self.abort_sync()
            raise ConnectorError("MCP normalization failed") from None
        return record

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        """Finalize a direct connector consumer against all active cached evidence."""
        records = tuple(normalize_raw_source(raw) for raw in self._cache.values())
        return self.finalize_checkpoint(discovered, records)

    def finalize_checkpoint(
        self,
        discovered: Sequence[SourceObject],
        consumed_evidence: Sequence[EvidenceRecord],
    ) -> str | None:
        """Commit only a complete generation and exact durable authorized evidence prefix."""
        cursor: str | None = None
        failed = False
        consumed_record: EvidenceRecord | None = None
        try:
            if (
                not self._checkpoint_pending
                or tuple(discovered) != self._last_discovered
                or self._discovery_failed
                or self._prior_checkpoint is None
            ):
                raise ValueError("invalid MCP generation")
            consumed = tuple(consumed_evidence)
            consumed_ids = {record.id for record in consumed}
            discovered_ids = {normalize_raw_source(raw).id for raw in self._cache.values()}
            if not discovered_ids.issubset(consumed_ids):
                raise ValueError("MCP evidence prefix is incomplete")
            observed: dict[str, str] = {}
            for consumed_record in consumed:
                if not self._is_scoped_record(consumed_record) or (
                    authorize(
                        self.local_actor,
                        self.config.binding.actor_principals,
                        frozenset(consumed_record.acl),
                    )
                    is not AuthorizationDecision.ALLOW
                ):
                    raise ValueError("foreign MCP evidence")
                observed[consumed_record.external_object_id] = consumed_record.external_version
            cursor = McpCheckpoint(
                connector_id=self.connector_id,
                profile_id=self.profile.id,
                profile_version=self.profile.version,
                object_type=self.object_name,
                scope_hash=self.scope_hash,
                source_hash=self.source_hash,
                provider_cursor=self._provider_cursor,
                observed_versions=observed,
            ).encode()
        except (TypeError, ValidationError, ValueError):
            failed = True
        consumed_evidence = ()
        consumed = ()
        observed = {}
        consumed_record = None
        self.abort_sync()
        if failed:
            raise ConnectorError("MCP checkpoint failed") from None
        return cursor

    def abort_sync(self) -> None:
        """Invalidate every object and cursor tied to the current generation."""
        self._cache = {}
        self._seen_versions = set()
        self._last_discovered = ()
        self._prior_checkpoint = None
        self._provider_cursor = None
        self._checkpoint_pending = False
        self._discovery_failed = False

    def _initial_checkpoint(self) -> McpCheckpoint:
        return McpCheckpoint(
            connector_id=self.connector_id,
            profile_id=self.profile.id,
            profile_version=self.profile.version,
            object_type=self.object_name,
            scope_hash=self.scope_hash,
            source_hash=self.source_hash,
        )

    def _source_hash(self) -> str:
        operations: dict[str, JsonValue] = {}
        for semantic_name in (
            self.object_profile.discover_operation,
            self.object_profile.fetch_operation,
        ):
            operation = self.profile.operations[semantic_name]
            provider_name = (
                self.config.binding.tools[semantic_name]
                if operation.kind == "tool"
                else self.config.binding.resources[semantic_name]
            )
            operations[semantic_name] = {
                "kind": operation.kind,
                "provider_name": provider_name,
                "operation": cast(JsonValue, operation.model_dump(mode="json")),
            }
        server = self.config.server.model_dump(mode="json")
        server.pop("timeout_seconds", None)
        return _digest(
            cast(
                JsonValue,
                {
                    "profile_id": self.profile.id,
                    "profile_version": self.profile.version,
                    "object_type": self.object_name,
                    "object_profile": self.object_profile.model_dump(mode="json"),
                    "server": server,
                    "read_operations": operations,
                },
            )
        )

    def _scope_copy(self) -> dict[str, JsonValue]:
        return {key: thaw_json(value) for key, value in self.config.scope.items()}

    def _external_object_id(self, provider_object_id: str) -> str:
        return f"{self.profile.id}:{provider_object_id}"

    def _is_scoped_record(self, record: EvidenceRecord) -> bool:
        if record.connector_type != self.connector_type or not record.external_object_id.startswith(
            f"{self.profile.id}:"
        ):
            return False
        payload = record.payload
        return (
            payload.get("profile_id") == self.profile.id
            and payload.get("profile_version") == self.profile.version
            and payload.get("object_type") == self.object_name
            and payload.get("scope_hash") == self.scope_hash
            and payload.get("source_hash") == self.source_hash
        )
