"""Offline contracts for the shipped collaboration-provider profiles."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]
from jsonschema import ValidationError as JsonSchemaValidationError  # type: ignore[import-untyped]
from jsonschema.validators import Draft202012Validator  # type: ignore[import-untyped]

from intent_engineering.capture.mcp import (
    McpConnectorConfig,
    ProviderProfile,
    bind_arguments,
    load_profile,
    select_value,
)
from intent_engineering.core.models import JsonValue

ROOT = Path(__file__).resolve().parents[3]
PROFILES = ROOT / "profiles" / "mcp"
FIXTURES = ROOT / "tests" / "fixtures" / "mcp"
_ENV_REFERENCE = re.compile(r"^env:[A-Z_][A-Z0-9_]{0,127}$")

PROFILE_MATRIX = (
    ("slack", {"message", "thread"}, {"post_message", "reply", "update_message"}),
    ("notion", {"page", "block"}, {"update_page", "append_blocks"}),
    ("jira", {"issue", "comment"}, {"update_issue", "add_comment"}),
    ("confluence", {"page", "comment"}, {"update_page", "add_comment"}),
)
WRITE_TARGETS = {
    "slack": {
        "post_message": "channel",
        "reply": "thread",
        "update_message": "message",
    },
    "notion": {"update_page": "page", "append_blocks": "page"},
    "jira": {"update_issue": "issue", "add_comment": "issue"},
    "confluence": {"update_page": "page", "add_comment": "page"},
}


def _load_json(path: Path) -> dict[str, JsonValue]:
    def unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise AssertionError(f"duplicate fixture key: {key}")
            result[key] = value
        return result

    loaded = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    assert type(loaded) is dict
    return cast(dict[str, JsonValue], loaded)


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert type(loaded) is dict
    return cast(dict[str, Any], loaded)


def _selected_content(
    profile: ProviderProfile, object_name: str, raw: JsonValue
) -> dict[str, JsonValue]:
    return {
        field: select_value(raw, selector)
        for field, selector in profile.objects[object_name].content.items()
    }


def _content_hash(content: dict[str, JsonValue]) -> str:
    encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _set_selector_value(payload: JsonValue, path: str, value: JsonValue) -> None:
    assert type(payload) is dict
    current: JsonValue = payload
    segments = path[2:].split(".")
    for segment in segments[:-1]:
        if type(current) is dict:
            current = current[segment]
        else:
            assert type(current) is list
            current = current[int(segment)]
    if type(current) is dict:
        current[segments[-1]] = value
    else:
        assert type(current) is list
        current[int(segments[-1])] = value


def _walk_strings(value: object) -> Iterable[str]:
    if type(value) is str:
        yield value
    elif type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)
    elif type(value) is list:
        for item in cast(list[object], value):
            yield from _walk_strings(item)


@pytest.mark.parametrize(("provider", "read_objects", "write_operations"), PROFILE_MATRIX)
def test_reference_profile_contract(
    provider: str,
    read_objects: set[str],
    write_operations: set[str],
) -> None:
    profile = load_profile(PROFILES / f"{provider}.yaml")

    assert profile.id == provider
    assert profile.version == "1"
    assert set(profile.objects) == read_objects
    assert set(profile.writes) == write_operations

    referenced_operations = {
        operation
        for object_profile in profile.objects.values()
        for operation in (object_profile.discover_operation, object_profile.fetch_operation)
    }
    assert referenced_operations == set(profile.operations)


@pytest.mark.parametrize(("provider", "read_objects", "_write_operations"), PROFILE_MATRIX)
def test_reference_objects_preserve_provider_authorship_acl_and_versioned_content(
    provider: str,
    read_objects: set[str],
    _write_operations: set[str],
) -> None:
    profile = load_profile(PROFILES / f"{provider}.yaml")
    authors: set[str] = set()
    acl_sets: set[frozenset[str]] = set()
    external_ids: set[str] = set()

    for object_name in sorted(read_objects):
        document = _load_json(FIXTURES / provider / f"{object_name}.json")
        raw = document["raw"]
        expected = cast(dict[str, JsonValue], document["expected"])
        object_profile = profile.objects[object_name]

        selected = {
            "external_id": select_value(raw, object_profile.external_id),
            "external_version": select_value(raw, object_profile.external_version),
            "author": select_value(raw, object_profile.author),
            "observed_at": select_value(raw, object_profile.observed_at),
            "locator": select_value(raw, object_profile.locator),
            "parent_ref": (
                select_value(raw, object_profile.parent_ref)
                if object_profile.parent_ref is not None
                else None
            ),
            "acl": select_value(raw, object_profile.acl)
            if object_profile.acl is not None
            else None,
            "content": _selected_content(profile, object_name, raw),
        }
        assert selected == expected
        assert isinstance(selected["author"], str)
        assert isinstance(selected["external_id"], str)
        assert isinstance(selected["acl"], list) and selected["acl"]
        assert selected["author"] in selected["acl"]
        authors.add(selected["author"])
        acl_sets.add(frozenset(cast(list[str], selected["acl"])))
        external_ids.add(selected["external_id"])

        semantic_hash = _content_hash(cast(dict[str, JsonValue], selected["content"]))
        envelope_changed = copy.deepcopy(raw)
        assert type(envelope_changed) is dict
        envelope_changed["_transport"] = {"request_id": "different-envelope"}
        assert (
            _content_hash(_selected_content(profile, object_name, envelope_changed))
            == semantic_hash
        )

        content_changed = copy.deepcopy(raw)
        first_selector = next(iter(object_profile.content.values()))
        _set_selector_value(content_changed, first_selector.path, "semantic-content-changed")
        assert (
            _content_hash(_selected_content(profile, object_name, content_changed)) != semantic_hash
        )

        discover = profile.operations[object_profile.discover_operation]
        envelope = _load_json(FIXTURES / provider / f"{object_profile.discover_operation}.json")[
            "raw"
        ]
        items = select_value(envelope, discover.item_selector)
        assert type(items) is list and items
        if discover.next_cursor_selector is not None:
            cursor = select_value(envelope, discover.next_cursor_selector)
            assert cursor is None or isinstance(cursor, (str, int))
        discover_arguments = bind_arguments(
            discover.arguments,
            {"cursor": "cursor-1", "scope": {"provider": provider}},
        )
        assert discover_arguments["cursor"] == "cursor-1"
        assert discover_arguments["scope"] == {"provider": provider}

        fetch = profile.operations[object_profile.fetch_operation]
        fetched = select_value(raw, fetch.item_selector)
        assert fetched == raw
        assert bind_arguments(
            fetch.arguments,
            {"object_id": selected["external_id"], "scope": {"provider": provider}},
        )

    assert len(authors) == len(read_objects)
    assert len(acl_sets) == len(read_objects)
    assert len(external_ids) == len(read_objects)


@pytest.mark.parametrize(("provider", "_read_objects", "_write_operations"), PROFILE_MATRIX)
def test_revision_fixtures_keep_same_object_versions_and_authors_separate(
    provider: str,
    _read_objects: set[str],
    _write_operations: set[str],
) -> None:
    profile = load_profile(PROFILES / f"{provider}.yaml")
    document = _load_json(FIXTURES / provider / "revisions.json")
    object_name = cast(str, document["object_type"])
    revisions = cast(list[JsonValue], document["revisions"])
    object_profile = profile.objects[object_name]
    primary = _load_json(FIXTURES / provider / f"{object_name}.json")["raw"]

    assert len(revisions) == 2
    assert len({select_value(item, object_profile.external_id) for item in revisions}) == 1
    revision_versions = {select_value(item, object_profile.external_version) for item in revisions}
    assert len(revision_versions) == 2
    assert select_value(primary, object_profile.external_version) not in revision_versions
    assert len({select_value(item, object_profile.author) for item in revisions}) == 2
    assert (
        len({_content_hash(_selected_content(profile, object_name, item)) for item in revisions})
        == 2
    )
    assert object_profile.acl is not None
    for item in revisions:
        author = select_value(item, object_profile.author)
        acl = select_value(item, object_profile.acl)
        assert isinstance(acl, list) and author in acl


@pytest.mark.parametrize(("provider", "_read_objects", "write_operations"), PROFILE_MATRIX)
def test_reference_writes_are_strict_versioned_previews(
    provider: str,
    _read_objects: set[str],
    write_operations: set[str],
) -> None:
    profile = load_profile(PROFILES / f"{provider}.yaml")
    samples = _load_json(FIXTURES / provider / "writes.json")

    assert set(samples) == write_operations
    for operation_name in sorted(write_operations):
        operation = profile.writes[operation_name]
        assert operation.target_object == WRITE_TARGETS[provider][operation_name]
        sample = cast(dict[str, JsonValue], samples[operation_name])
        fields = cast(dict[str, JsonValue], sample["fields"])
        target = sample["target"]
        result = sample["result"]

        assert operation.target_id.required
        assert operation.before_version.required
        assert operation.result_version.required
        assert set(fields) == set(operation.allowed_fields)
        Draft202012Validator(cast(dict[str, Any], operation.input_schema)).validate(fields)

        target_id = select_value(target, operation.target_id)
        before_version = select_value(target, operation.before_version)
        resulting_version = select_value(result, operation.result_version)
        assert isinstance(target_id, str) and target_id
        assert isinstance(before_version, str) and before_version
        assert isinstance(resulting_version, str) and resulting_version != before_version

        arguments = bind_arguments(
            operation.arguments,
            {
                "target_id": target_id,
                "before_version": before_version,
                "fields": fields,
                "scope": {"provider": provider},
            },
        )
        assert arguments == sample["expected_arguments"]


def test_slack_write_identity_arguments_derive_only_from_the_guarded_target() -> None:
    profile = load_profile(PROFILES / "slack.yaml")
    expected = {
        "post_message": ("channel_id", "channel:C111"),
        "reply": ("thread_ref", "workspace-1:C111:1699999999.000100"),
        "update_message": ("message_ref", "workspace-1:C111:1700000000.000100"),
    }

    for operation_name, (argument_name, target_id) in expected.items():
        operation = profile.writes[operation_name]
        assert not ({"channel_id", "thread_id", "message_id"} & operation.allowed_fields)
        fields = {field: "approved content" for field in operation.allowed_fields}
        arguments = bind_arguments(
            operation.arguments,
            {"target_id": target_id, "before_version": "v1", "fields": fields},
        )
        assert arguments[argument_name] == target_id


@pytest.mark.parametrize(
    ("operation_name", "invalid_fields"),
    [
        ("update_page", {"title": "Title", "properties": {"unexpected": "value"}}),
        ("append_blocks", {"children": [{}]}),
        ("append_blocks", {"children": [{"type": "paragraph", "unexpected": "value"}]}),
    ],
)
def test_notion_nested_write_schemas_reject_unrecognized_shapes(
    operation_name: str,
    invalid_fields: dict[str, JsonValue],
) -> None:
    profile = load_profile(PROFILES / "notion.yaml")

    with pytest.raises(JsonSchemaValidationError):
        Draft202012Validator(
            cast(dict[str, Any], profile.writes[operation_name].input_schema)
        ).validate(invalid_fields)


@pytest.mark.parametrize(("provider", "_read_objects", "_write_operations"), PROFILE_MATRIX)
def test_example_binding_is_complete_and_contains_only_environment_references(
    provider: str,
    _read_objects: set[str],
    _write_operations: set[str],
) -> None:
    profile = load_profile(PROFILES / f"{provider}.yaml")
    payload = _load_yaml(PROFILES / "example-bindings" / f"{provider}.yaml")
    config = McpConnectorConfig.model_validate(payload)

    assert config.profile_path == f"profiles/mcp/{provider}.yaml"
    assert config.binding.profile_id == provider
    assert config.binding.profile_version == profile.version
    config.binding.validate_against(profile)
    assert config.binding.actor_principals
    assert all(config.binding.actor_principals.values())
    fixture_authors = {
        cast(
            str,
            _load_json(FIXTURES / provider / f"{object_name}.json")["expected"]["author"],
        )
        for object_name in profile.objects
    }
    bound_principals = {
        principal
        for principals in config.binding.actor_principals.values()
        for principal in principals
    }
    assert fixture_authors <= bound_principals
    assert len(config.binding.actor_principals) >= 2
    object_acls = [
        set(
            cast(
                list[str],
                _load_json(FIXTURES / provider / f"{object_name}.json")["expected"]["acl"],
            )
        )
        for object_name in profile.objects
    ]
    authorization_matrix = {
        actor: tuple(bool(set(principals) & acl) for acl in object_acls)
        for actor, principals in config.binding.actor_principals.items()
    }
    assert all(sum(decisions) == 1 for decisions in authorization_matrix.values())
    assert len(set(authorization_matrix.values())) == len(authorization_matrix)
    assert config.server.environment_refs
    assert all(_ENV_REFERENCE.fullmatch(value) for value in config.server.environment_refs.values())
    serialized = json.dumps(config.model_dump(mode="json"), sort_keys=True)
    assert "secret" not in serialized.casefold()
    assert all("test-token" not in value.casefold() for value in _walk_strings(payload))
