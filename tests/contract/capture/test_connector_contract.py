"""Reusable behavioral contract for deterministic local connectors."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from intent_engineering.capture.base import Connector, SourceObject


@pytest.mark.anyio
async def assert_connector_is_stable(connector: Connector) -> Sequence[SourceObject]:
    """Assert repeated discovery and version-addressed normalization are stable."""
    first = await connector.discover(cursor=None)
    second = await connector.discover(cursor=None)

    assert first == second
    for source in first:
        raw = await connector.fetch(source.external_object_id, source.external_version)
        evidence = connector.normalize(raw)

        assert evidence.external_object_id == source.external_object_id
        assert evidence.external_version == source.external_version
        assert evidence.content_hash.startswith("sha256:")

    return first
