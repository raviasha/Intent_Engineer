"""Behavioral contracts for strict MCP provider profiles and bindings."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError

from intent_engineering.capture.mcp.profile_loader import ProfileValidationError, load_profile
from intent_engineering.capture.mcp.profile_models import (
    ArgumentBinding,
    BindingValidationError,
    ProviderBinding,
    ProviderProfile,
    profile_schema_bytes,
)
from intent_engineering.core.models import JsonValue


def test_profile_rejects_write_without_version_precondition(
    tmp_path: Path,
    profile_payload: Callable[[], dict[str, JsonValue]],
    yaml_writer: Callable[[Path, dict[str, JsonValue]], Path],
) -> None:
    """Catches removing the optimistic-version selector from a write mapping."""
    payload = profile_payload()
    payload["writes"]["update_record"]["before_version"] = None  # type: ignore[index]

    with pytest.raises(ProfileValidationError, match="invalid MCP provider profile") as caught:
        load_profile(yaml_writer(tmp_path / "profile.yaml", payload))

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("id",), " \t"),
        (("version",), "1\n2"),
        (("operations", "discover_records", "semantic_name"), "discover\x00records"),
        (("operations", "discover_records", "arguments", "cursor"), {"source": "cursor", "x": 1}),
        (("writes", "update_record", "allowed_fields"), []),
        (("writes", "update_record", "arguments", "text", "field"), " "),
    ],
)
def test_profile_rejects_blank_control_and_invalid_write_contract_values(
    tmp_path: Path,
    path: tuple[str, ...],
    value: JsonValue,
    profile_payload: Callable[[], dict[str, JsonValue]],
    yaml_writer: Callable[[Path, dict[str, JsonValue]], Path],
) -> None:
    """Catches permissive identifiers, argument names, fields, and write allowlists."""
    payload = profile_payload()
    target: dict[str, JsonValue] = payload
    for part in path[:-1]:
        target = target[part]  # type: ignore[assignment,index]
    target[path[-1]] = value

    with pytest.raises(ProfileValidationError, match="invalid MCP provider profile"):
        load_profile(yaml_writer(tmp_path / "profile.yaml", payload))


def test_profile_rejects_object_operation_references_and_wrong_capability_kind(
    tmp_path: Path,
    profile_payload: Callable[[], dict[str, JsonValue]],
    yaml_writer: Callable[[Path, dict[str, JsonValue]], Path],
) -> None:
    """Catches profiles whose object operations cannot be fulfilled by their declared operation."""
    payload = profile_payload()
    payload["objects"]["record"]["fetch_operation"] = "missing"  # type: ignore[index]

    with pytest.raises(ProfileValidationError, match="invalid MCP provider profile"):
        load_profile(yaml_writer(tmp_path / "profile.yaml", payload))


def test_profile_is_frozen_extra_forbidden_and_deeply_detaches_input(
    profile_payload: Callable[[], dict[str, JsonValue]],
) -> None:
    """Catches mutable profile state and acceptance of undeclared public fields."""
    payload = profile_payload()
    profile = ProviderProfile.model_validate(payload)
    payload["operations"]["discover_records"]["arguments"]["cursor"]["source"] = "scope"  # type: ignore[index]

    assert isinstance(profile.operations, MappingProxyType)
    assert profile.operations["discover_records"].arguments["cursor"].source == "cursor"
    with pytest.raises(TypeError):
        profile.operations["new"] = profile.operations["discover_records"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        ProviderProfile.model_validate({**profile_payload(), "unexpected": True})


def test_binding_requires_every_profile_capability(profile: ProviderProfile) -> None:
    """Catches accepting a binding that omits profile operations or writes."""
    binding = ProviderBinding(
        profile_id=profile.id,
        profile_version=profile.version,
        tools={},
        resources={},
        actor_principals={},
    )

    with pytest.raises(BindingValidationError, match="missing operation"):
        binding.validate_against(profile)


def test_binding_requires_exact_version_and_rejects_unknown_or_wrong_kind_mapping(
    profile: ProviderProfile,
) -> None:
    """Catches version drift and mappings that silently widen or misclassify capabilities."""
    binding = ProviderBinding(
        profile_id=profile.id,
        profile_version="2",
        tools={"discover_records": "discover", "update_record": "update", "extra": "extra"},
        resources={"fetch_record": "record://{id}", "discover_records": "not-a-resource"},
        actor_principals={"local": frozenset({"principal"})},
    )

    with pytest.raises(BindingValidationError, match="profile version"):
        binding.validate_against(profile)

    matching_version = binding.model_copy(update={"profile_version": profile.version})
    with pytest.raises(BindingValidationError, match="unknown operation"):
        matching_version.validate_against(profile)


def test_binding_is_frozen_extra_forbidden_and_deeply_detaches_input() -> None:
    """Catches bindings retaining mutable local mapping or principal collections."""
    tools = {"discover_records": "discover", "update_record": "update"}
    principals = {"local": {"principal"}}
    binding = ProviderBinding(
        profile_id="example-provider",
        profile_version="1",
        tools=tools,
        resources={"fetch_record": "record://{id}"},
        actor_principals=principals,
    )
    tools["discover_records"] = "changed"
    principals["local"].add("changed")

    assert isinstance(binding.tools, MappingProxyType)
    assert binding.tools["discover_records"] == "discover"
    assert binding.actor_principals["local"] == frozenset({"principal"})
    with pytest.raises(ValidationError):
        ProviderBinding.model_validate(
            {
                "profile_id": "example-provider",
                "profile_version": "1",
                "tools": {},
                "resources": {},
                "actor_principals": {},
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    "body",
    [
        "id: example\nid: duplicate\n",
        "id: &identity example\nversion: *identity\n",
        "id: !custom example\n",
        "1: value\n",
        "id: .nan\n",
        "- not-a-mapping\n",
    ],
)
def test_loader_rejects_unsafe_or_non_mapping_yaml_with_one_redacted_error(
    tmp_path: Path,
    body: str,
) -> None:
    """Catches unsafe YAML parser features and non-profile documents at the file boundary."""
    path = tmp_path / "profile.yaml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ProfileValidationError) as caught:
        load_profile(path)

    assert str(caught.value) == "invalid MCP provider profile"
    assert caught.value.args == ("invalid MCP provider profile",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_loader_rejects_symlink_hardlink_and_redacts_secret_context(
    tmp_path: Path,
    profile_payload: Callable[[], dict[str, JsonValue]],
    yaml_writer: Callable[[Path, dict[str, JsonValue]], Path],
) -> None:
    """Catches bypasses around descriptor-safe files and accidental source-text disclosure."""
    secret = "profile-secret-value"
    outside = yaml_writer(tmp_path / "outside.yaml", profile_payload())
    symlink = tmp_path / "symlink.yaml"
    symlink.symlink_to(outside)
    hardlink = tmp_path / "hardlink.yaml"
    os.link(outside, hardlink)
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text(f"id: {secret}\nunexpected: true\n", encoding="utf-8")

    for path in (symlink, hardlink, malformed):
        with pytest.raises(ProfileValidationError) as caught:
            load_profile(path)
        assert str(caught.value) == "invalid MCP provider profile"
        assert secret not in repr(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


def test_checked_in_profile_schema_is_canonical_and_byte_identical() -> None:
    """Catches schema artifact drift from the public ProviderProfile contract."""
    path = Path("schemas/mcp-provider-profile.schema.json")
    expected = json.dumps(
        ProviderProfile.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"

    assert profile_schema_bytes() == expected
    assert path.read_bytes() == expected


@pytest.mark.parametrize(
    ("source", "kwargs", "expected"),
    [
        ("constant", {"value": None}, {"source": "constant", "value": None}),
        ("scope", {}, {"source": "scope"}),
        ("cursor", {}, {"source": "cursor"}),
        ("object_id", {}, {"source": "object_id"}),
        ("object_version", {}, {"source": "object_version"}),
        ("target_id", {}, {"source": "target_id"}),
        ("before_version", {}, {"source": "before_version"}),
        ("field", {"field": "text"}, {"source": "field", "field": "text"}),
    ],
)
def test_argument_binding_round_trips_every_source_without_irrelevant_defaults(
    source: str,
    kwargs: dict[str, JsonValue],
    expected: dict[str, JsonValue],
) -> None:
    """Catches serializers emitting default fields forbidden by source-shape validation."""
    binding = ArgumentBinding(source=source, **kwargs)  # type: ignore[arg-type]

    serialized = binding.model_dump()

    assert serialized == expected
    assert ArgumentBinding.model_validate(serialized) == binding


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "cursor", "value": None},
        {"source": "cursor", "field": None},
        {"source": "field", "field": "text", "value": None},
        {"source": "constant", "value": None, "field": None},
    ],
)
def test_argument_binding_rejects_explicit_irrelevant_null_fields(
    payload: dict[str, JsonValue],
) -> None:
    """Catches null-valued irrelevant fields bypassing strict source-shape validation."""
    with pytest.raises(ValidationError):
        ArgumentBinding.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("id", " "),
        lambda payload: payload["operations"]["discover_records"].__setitem__(
            "item_selector", {"path": "$.items[0]"}
        ),  # type: ignore[index]
        lambda payload: payload["objects"]["record"]["content"]["text"].__setitem__(
            "transforms", ["unknown"]
        ),  # type: ignore[index]
        lambda payload: payload.__setitem__("operations", {}),
        lambda payload: payload["writes"]["update_record"].__setitem__("allowed_fields", []),  # type: ignore[index]
        lambda payload: payload["writes"]["update_record"]["before_version"].__setitem__(  # type: ignore[index]
            "required", False
        ),
    ],
)
def test_profile_schema_rejects_representative_values_the_model_rejects(
    mutate: Callable[[dict[str, JsonValue]], None],
    profile_payload: Callable[[], dict[str, JsonValue]],
) -> None:
    """Catches a checked-in schema that is weaker than the public profile contract."""
    payload = profile_payload()
    mutate(payload)
    validator = Draft202012Validator(ProviderProfile.model_json_schema())

    with pytest.raises(JsonSchemaValidationError):
        validator.validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"source": "cursor", "value": None},
        {"source": "field", "field": "text", "value": None},
        {"source": "constant", "value": None, "field": None},
    ],
)
def test_argument_binding_schema_rejects_irrelevant_source_shape_fields(
    payload: dict[str, JsonValue],
) -> None:
    """Catches a schema accepting source shapes that public Pydantic validation forbids."""
    profile_schema = ProviderProfile.model_json_schema()
    schema = {"$defs": profile_schema["$defs"], "$ref": "#/$defs/ArgumentBinding"}
    validator = Draft202012Validator(schema)

    with pytest.raises(JsonSchemaValidationError):
        validator.validate(payload)


def test_loader_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    """Catches opening a FIFO in blocking mode before descriptor kind authentication."""
    path = tmp_path / "profile.yaml"
    os.mkfifo(path)
    program = """\\
from pathlib import Path
from intent_engineering.capture.mcp.profile_loader import (
    ProfileValidationError, load_profile,
)
try:
    load_profile(Path(sys.argv[1]))
except ProfileValidationError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", f"import sys\n{program}", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"FIFO profile load did not finish: {error}")
    finally:
        path.unlink(missing_ok=True)

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_loader_bounds_recursion_and_redacts_parser_locals(tmp_path: Path) -> None:
    """Catches a deep document escaping as a parser traceback carrying its source text."""
    secret = "deep-profile-secret"
    path = tmp_path / "profile.yaml"
    nested = "".join(f"{'  ' * depth}node_{depth}:\n" for depth in range(80))
    path.write_text(f"{nested}{'  ' * 80}value: {secret}\n", encoding="utf-8")

    with pytest.raises(ProfileValidationError) as caught:
        load_profile(path)

    traceback_exception = traceback.TracebackException.from_exception(
        caught.value, capture_locals=True
    )
    rendered = "\n".join(
        str(frame.locals)
        for frame in traceback_exception.stack or ()
        if "intent_engineering/capture/mcp/profile_loader.py" in frame.filename
    )
    assert caught.value.args == ("invalid MCP provider profile",)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert secret not in rendered


def test_profile_schema_bounds_selector_depth_and_redacted_paths(
    profile_payload: Callable[[], dict[str, JsonValue]],
) -> None:
    """Catches schema acceptance of unbounded selector paths and unsafe redaction selectors."""
    too_deep = "$." + ".".join(["x"] * 33)
    validator = Draft202012Validator(ProviderProfile.model_json_schema())
    deep_profile = profile_payload()
    deep_profile["operations"]["discover_records"]["item_selector"] = {"path": too_deep}  # type: ignore[index]
    unsafe_redaction = profile_payload()
    unsafe_redaction["redacted_paths"] = ["$.items[0]"]

    assert list(validator.iter_errors(deep_profile))
    assert list(validator.iter_errors(unsafe_redaction))
