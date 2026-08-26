"""Descriptor-safe, fail-closed YAML loading for MCP provider profiles."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError
from yaml.events import (  # type: ignore[import-untyped]
    AliasEvent,
    CollectionEndEvent,
    CollectionStartEvent,
    DocumentStartEvent,
    NodeEvent,
)

from intent_engineering.capture.mcp.profile_models import ProviderProfile
from intent_engineering.storage.secure import SecureDirectory, UnsafePathError

_ERROR_MESSAGE = "invalid MCP provider profile"
_MAX_PROFILE_BYTES = 1_048_576
_MAX_YAML_DEPTH = 64


class ProfileValidationError(ValueError):
    """A fixed error that never retains profile text or parser/provider context."""

    def __init__(self) -> None:
        super().__init__(_ERROR_MESSAGE)


class _UniqueKeyLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Safe YAML loader that refuses duplicate or non-string mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[str, object]:
        mapping: dict[str, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if type(key) is not str or key in mapping:
                raise ValueError("unsafe YAML mapping")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _reject_unsafe_yaml_events(content: str) -> None:
    document_count = 0
    depth = 0
    for event in yaml.parse(content):
        if isinstance(event, DocumentStartEvent):
            document_count += 1
        if isinstance(event, AliasEvent):
            raise TypeError("unsafe YAML alias")
        if isinstance(event, CollectionStartEvent):
            depth += 1
            if depth > _MAX_YAML_DEPTH:
                raise ValueError("profile YAML is too deeply nested")
        if isinstance(event, CollectionEndEvent):
            depth -= 1
        if isinstance(event, NodeEvent) and event.tag is not None and not event.tag.startswith(
            "tag:yaml.org,2002:"
        ):
            raise ValueError("unsafe YAML tag")
    if document_count != 1:
        raise ValueError("profile requires one document")


def _read_profile_bytes(path: Path) -> bytes:
    absolute = Path(os.path.abspath(path))
    directory = SecureDirectory.open(absolute.parent, create=False)
    try:
        profile_file = directory.file(absolute.name)
        try:
            return profile_file.read_bytes_nonblocking()
        finally:
            profile_file.close()
    finally:
        directory.close()


def _load_profile_mapping(content: bytes) -> Mapping[str, object]:
    if len(content) > _MAX_PROFILE_BYTES:
        raise ValueError("profile YAML is too large")
    text = content.decode("utf-8")
    _reject_unsafe_yaml_events(text)
    loaded = yaml.load(text, Loader=_UniqueKeyLoader)
    if type(loaded) is not dict:
        raise ValueError("profile must be a mapping")
    return loaded


def _load_profile_result(path: Path) -> ProviderProfile | None:
    """Keep parser/source locals out of the public failure traceback."""
    try:
        mapping = _load_profile_mapping(_read_profile_bytes(path))
        return ProviderProfile.model_validate(mapping)
    except (
        OSError,
        TypeError,
        UnicodeError,
        UnsafePathError,
        ValidationError,
        ValueError,
        RecursionError,
        yaml.YAMLError,
    ):
        return None


def load_profile(path: Path) -> ProviderProfile:
    """Load one profile while exposing only a fixed, context-free public failure."""
    profile = _load_profile_result(path)
    del path
    if profile is None:
        raise ProfileValidationError()
    return profile
