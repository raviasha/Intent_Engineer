"""Pure, bounded selector access, transforms, and argument materialization."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from types import MappingProxyType
from typing import cast

from intent_engineering.capture.mcp.profile_models import ArgumentBinding, Selector
from intent_engineering.core.models import JsonValue

_INTEGER_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_MISSING = object()
_ACCESS = "access"
_MISSING_PATH = "missing"
_TRANSFORM = "transform"
_BINDING = "binding"

type _Result = tuple[bool, JsonValue | None | str]


class SelectorError(ValueError):
    """A fixed, redacted public selector or binding failure."""


class TransformError(ValueError):
    """A fixed, redacted public transform failure."""


def _invalid_transform() -> TransformError:
    return TransformError("invalid selector transform")


def _copy_external_json(value: object) -> JsonValue:
    """Detach only exact external JSON values; no user-defined protocol is invoked."""
    if type(value) is dict:
        copied: dict[str, JsonValue] = {}
        for key, item in cast(dict[object, object], value).items():
            if type(key) is not str:
                raise ValueError
            copied[key] = _copy_external_json(item)
        return copied
    if type(value) is list:
        return [_copy_external_json(item) for item in cast(list[object], value)]
    if value is None or type(value) in {str, int, bool}:
        return cast(JsonValue, value)
    if type(value) is float and isfinite(value):
        return value
    raise ValueError


def _thaw_trusted_constant(value: object) -> JsonValue:
    """Thaw only immutable values created by ArgumentBinding's own validator."""
    if type(value) is MappingProxyType:
        frozen: MappingProxyType[str, object] = value
        return {
            key: _thaw_trusted_constant(item)
            for key, item in frozen.items()
        }
    if type(value) is tuple:
        return [_thaw_trusted_constant(item) for item in cast(tuple[object, ...], value)]
    if value is None or type(value) in {str, int, bool}:
        return cast(JsonValue, value)
    if type(value) is float and isfinite(value):
        return value
    raise ValueError


def _to_string(value: object) -> JsonValue:
    if type(value) is str:
        return value
    if type(value) is int:
        return str(value)
    if type(value) is float and isfinite(value):
        return str(value)
    raise _invalid_transform()


def _to_integer(value: object) -> JsonValue:
    if type(value) is int:
        return value
    if type(value) is str and _INTEGER_TEXT.fullmatch(value):
        return int(value)
    raise _invalid_transform()


def _to_iso_datetime(value: object) -> JsonValue:
    if type(value) is not str:
        raise _invalid_transform()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid_transform() from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_transform()
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_string_list(value: object) -> JsonValue:
    if type(value) is not list or any(type(item) is not str for item in value):
        raise _invalid_transform()
    return list(cast(list[str], value))


def _to_canonical_json(value: object) -> JsonValue:
    return json.dumps(cast(JsonValue, value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _to_sha256(value: object) -> JsonValue:
    if type(value) is str:
        encoded = value.encode("utf-8")
    else:
        encoded = json.dumps(
            cast(JsonValue, value), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


TRANSFORMS: dict[str, Callable[[object], JsonValue]] = {
    "string": _to_string,
    "integer": _to_integer,
    "iso_datetime": _to_iso_datetime,
    "string_list": _to_string_list,
    "canonical_json": _to_canonical_json,
    "sha256": _to_sha256,
}


def _apply_transform_result(transform: object, value: object) -> _Result:
    """Return an inert success/error result without putting untrusted values in a traceback."""
    try:
        if type(transform) is not str:
            return False, _TRANSFORM
        function = TRANSFORMS.get(transform)
        if function is None:
            return False, _TRANSFORM
        return True, function(_copy_external_json(value))
    except Exception:  # noqa: BLE001 - provider data must not enter public tracebacks
        return False, _TRANSFORM


def _raise_transform() -> None:
    raise TransformError("invalid selector transform")


def apply_transform(transform: str, value: object) -> JsonValue:
    """Apply one transform while deleting provider-controlled inputs before raising publicly."""
    result = _apply_transform_result(transform, value)
    del transform, value
    if result[0]:
        return result[1]
    del result
    _raise_transform()
    raise AssertionError("unreachable")


def _select_result(payload: object, selector: Selector) -> _Result:
    """Select internally without allowing provider data onto a public exception traceback."""
    try:
        current: object = _copy_external_json(payload)
        if selector.path != "$":
            for segment in selector.path[2:].split("."):
                if type(current) is dict:
                    current = cast(dict[str, JsonValue], current).get(segment, _MISSING)
                elif type(current) is list:
                    if not segment.isdecimal():
                        return False, _ACCESS
                    index = int(segment)
                    if index >= len(current):
                        return False, _ACCESS
                    current = cast(list[JsonValue], current)[index]
                else:
                    return False, _ACCESS
                if current is _MISSING:
                    return False, _MISSING_PATH if selector.required else "optional-missing"
        value = cast(JsonValue, current)
        for transform in selector.transforms:
            transformed = _apply_transform_result(transform, value)
            if not transformed[0]:
                return False, _TRANSFORM
            value = transformed[1]
        return True, value
    except Exception:  # noqa: BLE001 - provider data must not enter public tracebacks
        return False, _ACCESS


def _raise_selector(error: str) -> None:
    if error == _TRANSFORM:
        raise TransformError("invalid selector transform")
    if error == _MISSING_PATH:
        raise SelectorError("required selector path is missing")
    raise SelectorError("invalid selector access")


def select_value(payload: object, selector: Selector) -> JsonValue | None:
    """Select a detached JSON value, preserving explicit null distinct from absent mapping paths."""
    result = _select_result(payload, selector)
    del payload, selector
    if result[0]:
        return result[1]
    error = cast(str, result[1])
    del result
    if error == "optional-missing":
        return None
    _raise_selector(error)
    raise AssertionError("unreachable")


def _bind_arguments_result(bindings: object, context: object) -> _Result:
    """Materialize arguments internally, returning only inert error codes on failure."""
    try:
        if type(bindings) not in {dict, MappingProxyType} or type(context) is not dict:
            return False, _BINDING
        safe_context = _copy_external_json(context)
        assert type(safe_context) is dict
        arguments: dict[str, JsonValue] = {}
        for name, binding in cast(
            dict[object, object] | MappingProxyType[object, object], bindings
        ).items():
            if type(name) is not str or type(binding) is not ArgumentBinding:
                return False, _BINDING
            if binding.source == "constant":
                value = _thaw_trusted_constant(binding.value)
            elif binding.source == "field":
                if binding.field is None:
                    return False, _BINDING
                fields = safe_context.get("fields", _MISSING)
                if type(fields) is not dict or binding.field not in fields:
                    return False, _BINDING
                value = fields[binding.field]
            else:
                if binding.source not in safe_context:
                    return False, _BINDING
                value = safe_context[binding.source]
            arguments[name] = _copy_external_json(value)
        return True, arguments
    except Exception:  # noqa: BLE001 - context values must not enter public tracebacks
        return False, _BINDING


def _raise_binding() -> None:
    raise SelectorError("invalid argument binding")


def bind_arguments(bindings: object, context: object) -> dict[str, JsonValue]:
    """Resolve only declared sources and clear all caller-controlled locals before public errors."""
    result = _bind_arguments_result(bindings, context)
    del bindings, context
    if result[0]:
        return cast(dict[str, JsonValue], result[1])
    del result
    _raise_binding()
    raise AssertionError("unreachable")
