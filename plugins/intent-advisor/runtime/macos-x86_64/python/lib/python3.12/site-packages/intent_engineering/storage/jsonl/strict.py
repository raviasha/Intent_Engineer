"""Strict JSON decoding shared by storage and deep validation boundaries."""

from __future__ import annotations

import json
from typing import cast


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def loads_strict_json(value: str) -> object:
    """Decode standards-compliant JSON while rejecting duplicate object keys."""
    return json.loads(
        value,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def loads_strict_object(value: str) -> dict[str, object]:
    """Decode one strict JSON object rather than an arbitrary JSON value."""
    loaded = loads_strict_json(value)
    if not isinstance(loaded, dict):
        raise TypeError("JSON value must be an object")
    return cast(dict[str, object], loaded)
