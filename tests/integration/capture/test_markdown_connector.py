"""Integration coverage for deterministic Markdown evidence capture."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from intent_engineering.capture.checkpoints import checkpoint_after_discovery
from intent_engineering.capture.markdown.connector import MarkdownConnector
from intent_engineering.core.models import ProjectConfig
from tests.contract.capture.test_connector_contract import assert_connector_is_stable


@pytest.mark.anyio
async def test_markdown_connector_excludes_configured_globs_and_versions_content(tmp_path: Path) -> None:
    """A path or content-hash regression must change the captured evidence."""
    (tmp_path / "notes").mkdir()
    included = tmp_path / "notes" / "included.md"
    included.write_text("# Initial intent\n", encoding="utf-8")
    (tmp_path / "notes" / "private.md").write_text("# Ignore me\n", encoding="utf-8")
    config = ProjectConfig(
        project_id="capture-test",
        local_actor="tester",
        source_exclusions=("notes/private.md",),
    )
    connector = MarkdownConnector(tmp_path, config)

    first_sources = await assert_connector_is_stable(connector)

    assert len(first_sources) == 1
    assert first_sources[0].external_object_id == "path:notes/included.md"
    assert first_sources[0].locator == "notes/included.md"
    first_evidence = connector.normalize(
        await connector.fetch(
            first_sources[0].external_object_id,
            first_sources[0].external_version,
        )
    )
    original_version = first_evidence.external_version

    stat = included.stat()
    os.utime(included, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert await connector.discover(cursor=None) == first_sources

    included.write_text("# Revised intent\n", encoding="utf-8")
    second_sources = await connector.discover(cursor=None)
    second_evidence = connector.normalize(
        await connector.fetch(
            second_sources[0].external_object_id,
            second_sources[0].external_version,
        )
    )

    assert second_sources[0].external_object_id == "path:notes/included.md"
    assert second_evidence.external_version.startswith("sha256:")
    assert second_evidence.external_version != original_version
    assert second_evidence.id != first_evidence.id
    assert connector.next_checkpoint(second_sources) == second_evidence.external_version
    checkpoint = checkpoint_after_discovery(
        connector,
        second_sources,
        second_evidence.observed_at,
    )
    assert checkpoint.connector_id == "markdown"
    assert checkpoint.cursor == second_evidence.external_version
