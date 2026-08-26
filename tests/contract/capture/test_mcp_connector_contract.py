"""Provider-neutral contract for the profile-driven MCP read connector."""

from __future__ import annotations

import copy
import hashlib
import json
import traceback
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.base import ConnectorError, normalize_raw_source
from intent_engineering.capture.mcp import McpConnectorConfig, load_profile
from intent_engineering.capture.mcp.connector import McpCheckpoint, McpConnector, _resource_uri
from intent_engineering.core.models import JsonValue

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ROOT / "profiles" / "mcp"
FIXTURES = ROOT / "tests" / "fixtures" / "mcp" / "slack"


def _json(name: str) -> dict[str, JsonValue]:
    loaded = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert type(loaded) is dict
    return cast(dict[str, JsonValue], loaded)


def _connector_config() -> McpConnectorConfig:
    loaded = yaml.safe_load((PROFILES / "example-bindings" / "slack.yaml").read_text())
    assert type(loaded) is dict
    return McpConnectorConfig.model_validate(loaded)


class ScriptedRuntime:
    """Small operation-level fake that records the exact profile binding calls."""

    def __init__(
        self,
        *,
        pages: dict[object, JsonValue],
        objects: dict[str, JsonValue],
        failure_cursor: object = object(),
    ) -> None:
        self.pages = pages
        self.objects = objects
        self.failure_cursor = failure_cursor
        self.validations = 0
        self.calls: list[tuple[str, dict[str, JsonValue]]] = []

    async def validate_binding(self, _server: object, _binding: object) -> None:
        self.validations += 1

    async def call(
        self, _server: object, tool_name: str, arguments: dict[str, JsonValue]
    ) -> JsonValue:
        self.calls.append((tool_name, copy.deepcopy(arguments)))
        if tool_name == "search_messages":
            cursor = arguments["cursor"]
            if cursor == self.failure_cursor:
                raise RuntimeError("provider-private-failure")
            return copy.deepcopy(self.pages[cursor])
        if tool_name == "get_message":
            return copy.deepcopy(self.objects[cast(str, arguments["message_id"])])
        raise AssertionError(f"unexpected tool: {tool_name}")

    async def read_resource(self, _server: object, _uri: str) -> JsonValue:
        raise AssertionError("Slack reference reads must use bound tools")


def _runtime_for_message() -> ScriptedRuntime:
    message = cast(dict[str, JsonValue], _json("message.json")["raw"])
    object_id = cast(str, message["id"])
    return ScriptedRuntime(
        pages={None: {"messages": [{"id": object_id}], "next_cursor": None}},
        objects={object_id: message},
    )


def _connector(runtime: ScriptedRuntime, *, actor: str = "local-asha") -> McpConnector:
    return McpConnector(
        runtime,
        config=_connector_config(),
        profile=load_profile(PROFILES / "slack.yaml"),
        object_name="message",
        local_actor=actor,
    )


@pytest.mark.anyio
async def test_mcp_connector_normalizes_versioned_authored_evidence() -> None:
    runtime = _runtime_for_message()
    connector = _connector(runtime)

    discovered = await connector.discover(None)
    raw = await connector.fetch(
        discovered[0].external_object_id,
        discovered[0].external_version,
    )
    record = connector.normalize(raw)
    cursor = connector.finalize_checkpoint(discovered, (record,))

    assert runtime.validations == 1
    assert record.external_object_id == "slack:workspace-1:C111:1700000000.000100"
    assert record.external_version == "1700000000.000200"
    assert record.author == "U123"
    assert record.observed_at.isoformat() == "2026-08-20T10:15:30+00:00"
    assert record.acl == ("U123", "slack-group:ENG")
    assert record.parent_ref is None
    assert record.payload == {
        "kind": "mcp_object",
        "profile_id": "slack",
        "profile_version": "1",
        "object_type": "message",
        "scope_hash": connector.scope_hash,
        "source_hash": connector.source_hash,
        "parent_context": "slack:workspace-1:C111:1699999999.000100",
        "content": {
            "original_author_id": "U123",
            "channel_id": "C111",
            "text": "The export must remain local by default.",
        },
    }
    serialized_content = cast(
        dict[str, JsonValue],
        record.model_dump(mode="json")["payload"]["content"],
    )
    expected_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                serialized_content,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert record.content_hash == expected_hash
    assert "_transport" not in record.model_dump_json()
    assert cursor is not None
    checkpoint = McpCheckpoint.decode(cursor, connector=connector)
    assert checkpoint.observed_versions == {
        "slack:workspace-1:C111:1700000000.000100": "1700000000.000200"
    }


@pytest.mark.anyio
async def test_cursor_pagination_is_bounded_and_uses_only_bound_operations() -> None:
    first = cast(dict[str, JsonValue], _json("message.json")["raw"])
    second = copy.deepcopy(first)
    second["id"] = "workspace-1:C111:1700000001.000100"
    second["updated"] = "1700000001.000200"
    second["updated_at"] = "2026-08-20T10:16:30Z"
    second["permalink"] = "https://example.slack.com/archives/C111/p1700000001000100"
    runtime = ScriptedRuntime(
        pages={
            None: {"messages": [{"id": first["id"]}], "next_cursor": "page-2"},
            "page-2": {"messages": [{"id": second["id"]}], "next_cursor": None},
        },
        objects={cast(str, first["id"]): first, cast(str, second["id"]): second},
    )
    connector = _connector(runtime)

    discovered = await connector.discover(None)

    assert len(discovered) == 2
    assert [call for call in runtime.calls if call[0] == "search_messages"] == [
        ("search_messages", {"cursor": None, "scope": {"workspace_id": "workspace-1"}}),
        ("search_messages", {"cursor": "page-2", "scope": {"workspace_id": "workspace-1"}}),
    ]
    assert all(call[0] in {"search_messages", "get_message"} for call in runtime.calls)


@pytest.mark.anyio
async def test_fetch_and_normalize_accept_only_the_active_discovery_generation() -> None:
    connector = _connector(_runtime_for_message())
    discovered = await connector.discover(None)
    raw = await connector.fetch(
        discovered[0].external_object_id,
        discovered[0].external_version,
    )
    foreign = raw.model_copy(update={"external_version": "secret-wrong-version"})

    with pytest.raises(ConnectorError, match="MCP normalization failed") as caught:
        connector.normalize(foreign)

    assert "secret-wrong-version" not in repr(caught.value.args)
    assert connector.cached_evidence_ids == ()
    with pytest.raises(ConnectorError, match="MCP fetch failed"):
        await connector.fetch(
            discovered[0].external_object_id,
            discovered[0].external_version,
        )


@pytest.mark.anyio
async def test_checkpoint_rejects_an_incomplete_or_failed_generation() -> None:
    message = cast(dict[str, JsonValue], _json("message.json")["raw"])
    object_id = cast(str, message["id"])
    runtime = ScriptedRuntime(
        pages={None: {"messages": [{"id": object_id}], "next_cursor": "page-2"}},
        objects={object_id: message},
        failure_cursor="page-2",
    )
    connector = _connector(runtime)
    discovered = await connector.discover(None)
    raw = await connector.fetch(discovered[0].external_object_id, discovered[0].external_version)

    with pytest.raises(ConnectorError, match="MCP checkpoint failed"):
        connector.finalize_checkpoint(discovered, (normalize_raw_source(raw),))


def test_actor_scoping_separates_checkpoint_and_replay_ledgers() -> None:
    runtime = _runtime_for_message()
    asha = _connector(runtime, actor="local-asha")
    ben = _connector(runtime, actor="local-ben")

    assert asha.connector_id != ben.connector_id
    assert asha.connector_type == ben.connector_type == "mcp"


def test_actor_principal_mapping_is_part_of_the_replay_scope() -> None:
    runtime = _runtime_for_message()
    config = _connector_config()
    profile = load_profile(PROFILES / "slack.yaml")
    original = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config,
        profile=profile,
        object_name="message",
        local_actor="local-asha",
    )
    changed_binding = config.binding.model_copy(
        update={
            "actor_principals": {
                **config.binding.actor_principals,
                "local-asha": frozenset({"U999"}),
            }
        }
    )
    changed = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config.model_copy(update={"binding": changed_binding}),
        profile=profile,
        object_name="message",
        local_actor="local-asha",
    )

    assert changed.connector_id != original.connector_id


def test_server_profile_and_read_binding_are_part_of_the_replay_scope() -> None:
    runtime = _runtime_for_message()
    config = _connector_config()
    profile = load_profile(PROFILES / "slack.yaml")
    original = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config,
        profile=profile,
        object_name="message",
        local_actor="local-asha",
    )
    changed_server = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config.model_copy(
            update={"server": config.server.model_copy(update={"id": "different-server"})}
        ),
        profile=profile,
        object_name="message",
        local_actor="local-asha",
    )
    changed_tools = dict(config.binding.tools)
    changed_tools["fetch_message"] = "different_get_message"
    changed_binding = config.binding.model_copy(update={"tools": changed_tools})
    changed_operation = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config.model_copy(update={"binding": changed_binding}),
        profile=profile,
        object_name="message",
        local_actor="local-asha",
    )
    changed_profile = profile.model_copy(update={"version": "2"})
    profile_binding = config.binding.model_copy(update={"profile_version": "2"})
    changed_profile_connector = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config.model_copy(update={"binding": profile_binding}),
        profile=changed_profile,
        object_name="message",
        local_actor="local-asha",
    )
    changed_object = profile.objects["message"].model_copy(
        update={
            "content": {
                **profile.objects["message"].content,
                "text": profile.objects["message"]
                .content["text"]
                .model_copy(update={"path": "$.title"}),
            }
        }
    )
    changed_objects = dict(profile.objects)
    changed_objects["message"] = changed_object
    changed_semantics = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config,
        profile=profile.model_copy(update={"objects": changed_objects}),
        object_name="message",
        local_actor="local-asha",
    )

    assert (
        len(
            {
                original.connector_id,
                changed_server.connector_id,
                changed_operation.connector_id,
                changed_profile_connector.connector_id,
                changed_semantics.connector_id,
            }
        )
        == 5
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [("allowed_principals", None), ("last_modified_by", {"id": None})],
)
async def test_unknown_acl_or_missing_author_fails_closed_without_caching_provider_content(
    field: str,
    invalid_value: JsonValue,
) -> None:
    message = cast(dict[str, JsonValue], _json("message.json")["raw"])
    message[field] = invalid_value
    secret = "PRIVATE-MCP-CONTENT"
    message["text"] = secret
    runtime = ScriptedRuntime(
        pages={None: {"messages": [{"id": message["id"]}], "next_cursor": None}},
        objects={cast(str, message["id"]): message},
    )
    profile = load_profile(PROFILES / "slack.yaml")
    object_profile = profile.objects["message"]
    selector_name = "acl" if field == "allowed_principals" else "author"
    selector = getattr(object_profile, selector_name)
    assert selector is not None
    transform_free = selector.model_copy(update={"transforms": ()})
    changed_object = object_profile.model_copy(update={selector_name: transform_free})
    changed_objects = dict(profile.objects)
    changed_objects["message"] = changed_object
    connector = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=_connector_config(),
        profile=profile.model_copy(update={"objects": changed_objects}),
        object_name="message",
        local_actor="local-asha",
    )

    with pytest.raises(ConnectorError, match="MCP discovery failed") as caught:
        await connector.discover(None)

    assert secret not in repr(caught.value.args)
    assert connector.cached_evidence_ids == ()


@pytest.mark.anyio
async def test_one_generation_rejects_two_versions_of_the_same_external_object() -> None:
    first = cast(dict[str, JsonValue], _json("message.json")["raw"])
    second_reference = {"id": first["id"]}
    runtime = ScriptedRuntime(
        pages={
            None: {
                "messages": [{"id": first["id"]}, second_reference],
                "next_cursor": None,
            }
        },
        objects={cast(str, first["id"]): first},
    )
    connector = _connector(runtime)

    discovered = await connector.discover(None)
    assert len(discovered) == 1
    raw = await connector.fetch(discovered[0].external_object_id, discovered[0].external_version)

    with pytest.raises(ConnectorError, match="MCP checkpoint failed"):
        connector.finalize_checkpoint(discovered, (normalize_raw_source(raw),))


@pytest.mark.anyio
async def test_duplicate_denied_and_unchanged_versions_are_rejected_outside_the_cache() -> None:
    message = cast(dict[str, JsonValue], _json("message.json")["raw"])
    object_id = cast(str, message["id"])
    duplicate_pages = {
        None: {
            "messages": [{"id": object_id}, {"id": object_id}],
            "next_cursor": None,
        }
    }
    denied = _connector(
        ScriptedRuntime(pages=duplicate_pages, objects={object_id: message}),
        actor="local-ben",
    )
    with pytest.raises(ConnectorError, match="MCP discovery failed"):
        await denied.discover(None)

    baseline = _connector(_runtime_for_message())
    discovered = await baseline.discover(None)
    raw = await baseline.fetch(discovered[0].external_object_id, discovered[0].external_version)
    cursor = baseline.finalize_checkpoint(discovered, (normalize_raw_source(raw),))
    unchanged = _connector(ScriptedRuntime(pages=duplicate_pages, objects={object_id: message}))
    with pytest.raises(ConnectorError, match="MCP discovery failed"):
        await unchanged.discover(cursor)


@pytest.mark.anyio
async def test_argumentized_resource_fetch_uses_one_exact_encoded_uri() -> None:
    message = cast(dict[str, JsonValue], _json("message.json")["raw"])
    object_id = cast(str, message["id"])
    profile = load_profile(PROFILES / "slack.yaml")
    fetch = profile.operations["fetch_message"].model_copy(update={"kind": "resource"})
    operations = dict(profile.operations)
    operations["fetch_message"] = fetch
    resource_profile = profile.model_copy(update={"operations": operations})
    config = _connector_config()
    tools = dict(config.binding.tools)
    tools.pop("fetch_message")
    binding = config.binding.model_copy(
        update={
            "tools": tools,
            "resources": {"fetch_message": "message://{message_id}"},
        }
    )

    class ResourceRuntime:
        def __init__(self) -> None:
            self.uris: list[str] = []

        async def validate_binding(self, _server: object, _binding: object) -> None:
            return None

        async def call(
            self, _server: object, tool_name: str, _arguments: dict[str, JsonValue]
        ) -> JsonValue:
            assert tool_name == "search_messages"
            return {"messages": [{"id": object_id}], "next_cursor": None}

        async def read_resource(self, _server: object, uri: str) -> JsonValue:
            self.uris.append(uri)
            return copy.deepcopy(message)

    runtime = ResourceRuntime()
    connector = McpConnector(
        runtime,  # type: ignore[arg-type]
        config=config.model_copy(update={"binding": binding}),
        profile=resource_profile,
        object_name="message",
        local_actor="local-asha",
    )

    discovered = await connector.discover(None)

    assert len(discovered) == 1
    assert runtime.uris == ["message://workspace-1%3AC111%3A1700000000.000100"]


def test_literal_resource_uri_accepts_no_arguments_and_rejects_implicit_ones() -> None:
    assert _resource_uri("resource://current-status", {}) == "resource://current-status"
    with pytest.raises(ValueError, match="resource binding"):
        _resource_uri("resource://current-status", {"ignored": "value"})


@pytest.mark.parametrize(
    "template",
    [
        "resource://messages/{left}{right}",
        "resource://messages/{left}-{right}",
    ],
)
def test_resource_uri_rejects_ambiguous_multi_placeholder_boundaries(template: str) -> None:
    with pytest.raises(ValueError, match="resource binding"):
        _resource_uri(template, {"left": "x-", "right": "y"})


def test_resource_uri_accepts_reserved_delimiters_without_tuple_collisions() -> None:
    assert _resource_uri(
        "resource://messages/{workspace}/{message}",
        {"workspace": "eng/core", "message": "one?two"},
    ) == "resource://messages/eng%2Fcore/one%3Ftwo"


def test_checkpoint_is_canonical_and_never_emits_a_cursor_it_cannot_decode() -> None:
    connector = _connector(_runtime_for_message())
    checkpoint = connector._initial_checkpoint()
    encoded = checkpoint.encode()

    with pytest.raises(ValueError, match="canonical"):
        McpCheckpoint.decode_unscoped(" " + encoded)
    with pytest.raises(ValueError, match="too large"):
        checkpoint.model_copy(update={"provider_cursor": "x" * 1_048_576}).encode()


def test_public_checkpoint_decoder_redacts_malformed_cursor_contents() -> None:
    secret = "PRIVATE-MCP-CURSOR"

    with pytest.raises(ValueError, match="invalid MCP checkpoint") as caught:
        McpCheckpoint.decode_unscoped('{"provider_cursor":"' + secret + '"')

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("invalid MCP checkpoint",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert secret not in repr(caught.value)
    assert secret not in repository_locals


def test_scoped_checkpoint_decoder_redacts_valid_foreign_provider_cursor() -> None:
    secret = "PRIVATE-SCOPED-MCP-CURSOR"
    asha = _connector(_runtime_for_message(), actor="local-asha")
    ben = _connector(_runtime_for_message(), actor="local-ben")
    encoded = asha._initial_checkpoint().model_copy(
        update={"provider_cursor": secret}
    ).encode()

    with pytest.raises(ValueError, match="scope mismatch") as caught:
        McpCheckpoint.decode(encoded, connector=ben)

    rendered = traceback.TracebackException.from_exception(
        caught.value,
        capture_locals=True,
    )
    repository_locals = "\n".join(
        str(frame.locals)
        for frame in rendered.stack
        if "/src/intent_engineering/" in frame.filename
    )
    assert caught.value.args == ("MCP checkpoint scope mismatch",)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert secret not in repr(caught.value)
    assert secret not in repository_locals


@pytest.mark.parametrize(
    ("provider", "object_name", "actor"),
    [
        ("slack", "message", "local-asha"),
        ("slack", "thread", "local-ben"),
        ("notion", "page", "local-asha"),
        ("notion", "block", "local-ben"),
        ("jira", "issue", "local-asha"),
        ("jira", "comment", "local-ben"),
        ("confluence", "page", "local-asha"),
        ("confluence", "comment", "local-ben"),
    ],
)
@pytest.mark.anyio
async def test_all_reference_object_profiles_capture_authorized_authored_evidence(
    provider: str,
    object_name: str,
    actor: str,
) -> None:
    profile = load_profile(PROFILES / f"{provider}.yaml")
    binding_payload = yaml.safe_load(
        (PROFILES / "example-bindings" / f"{provider}.yaml").read_text(encoding="utf-8")
    )
    assert type(binding_payload) is dict
    config = McpConnectorConfig.model_validate(binding_payload)
    object_profile = profile.objects[object_name]
    discover_name = config.binding.tools[object_profile.discover_operation]
    fetch_name = config.binding.tools[object_profile.fetch_operation]
    discover_fixture = cast(
        dict[str, JsonValue],
        _provider_json(provider, object_profile.discover_operation)["raw"],
    )
    object_fixture = cast(
        dict[str, JsonValue],
        _provider_json(provider, object_name)["raw"],
    )

    class ReferenceRuntime:
        async def validate_binding(self, _server: object, _binding: object) -> None:
            return None

        async def call(
            self, _server: object, tool_name: str, _arguments: dict[str, JsonValue]
        ) -> JsonValue:
            if tool_name == discover_name:
                response = copy.deepcopy(discover_fixture)
                if profile.operations[object_profile.discover_operation].next_cursor_selector:
                    response["next_cursor"] = None
                return response
            if tool_name == fetch_name:
                return copy.deepcopy(object_fixture)
            raise AssertionError(tool_name)

        async def read_resource(self, _server: object, _uri: str) -> JsonValue:
            raise AssertionError("unexpected resource")

    connector = McpConnector(
        ReferenceRuntime(),  # type: ignore[arg-type]
        config=config,
        profile=profile,
        object_name=object_name,
        local_actor=actor,
    )

    discovered = await connector.discover(None)
    fetched = await connector.fetch(
        discovered[0].external_object_id,
        discovered[0].external_version,
    )
    evidence = connector.normalize(fetched)

    assert evidence.external_object_id.startswith(f"{provider}:")
    assert evidence.author in config.binding.actor_principals[actor]
    assert set(evidence.acl).intersection(config.binding.actor_principals[actor])


def _provider_json(provider: str, name: str) -> dict[str, JsonValue]:
    loaded = json.loads(
        (ROOT / "tests" / "fixtures" / "mcp" / provider / f"{name}.json").read_text(
            encoding="utf-8"
        )
    )
    assert type(loaded) is dict
    return cast(dict[str, JsonValue], loaded)
