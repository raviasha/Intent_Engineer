"""Fixed, provider-neutral MCP profile fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from intent_engineering.capture.mcp.profile_loader import load_profile
from intent_engineering.capture.mcp.profile_models import ProviderProfile
from intent_engineering.core.models import JsonValue


def valid_profile_payload() -> dict[str, JsonValue]:
    """Return a complete fixed profile with one tool and one resource capability."""
    return {
        "id": "example-provider",
        "version": "1",
        "display_name": "Example Provider",
        "operations": {
            "discover_records": {
                "semantic_name": "discover_records",
                "kind": "tool",
                "pagination": "cursor",
                "arguments": {"cursor": {"source": "cursor"}},
                "item_selector": {"path": "$.items"},
                "next_cursor_selector": {"path": "$.next_cursor", "required": False},
            },
            "fetch_record": {
                "semantic_name": "fetch_record",
                "kind": "resource",
                "pagination": "none",
                "arguments": {"record_id": {"source": "object_id"}},
                "item_selector": {"path": "$"},
            },
        },
        "objects": {
            "record": {
                "discover_operation": "discover_records",
                "fetch_operation": "fetch_record",
                "external_id": {"path": "$.id", "transforms": ["string"]},
                "external_version": {"path": "$.version", "transforms": ["string"]},
                "author": {"path": "$.author", "transforms": ["string"]},
                "observed_at": {"path": "$.observed_at", "transforms": ["iso_datetime"]},
                "locator": {"path": "$.url", "transforms": ["string"]},
                "content": {"text": {"path": "$.text", "transforms": ["string"]}},
            }
        },
        "writes": {
            "update_record": {
                "semantic_name": "update_record",
                "target_id": {"path": "$.id", "transforms": ["string"]},
                "before_version": {"path": "$.version", "transforms": ["string"]},
                "allowed_fields": ["text"],
                "arguments": {
                    "record_id": {"source": "target_id"},
                    "version": {"source": "before_version"},
                    "text": {"source": "field", "field": "text"},
                },
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                "result_version": {"path": "$.version", "transforms": ["string"]},
            }
        },
        "redacted_paths": ["$.token"],
    }


def write_yaml(path: Path, payload: dict[str, JsonValue]) -> Path:
    path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return path


@pytest.fixture
def profile_payload() -> Callable[[], dict[str, JsonValue]]:
    return valid_profile_payload


@pytest.fixture
def yaml_writer() -> Callable[[Path, dict[str, JsonValue]], Path]:
    return write_yaml


@pytest.fixture
def profile(tmp_path: Path) -> ProviderProfile:
    return load_profile(write_yaml(tmp_path / "profile.yaml", valid_profile_payload()))
