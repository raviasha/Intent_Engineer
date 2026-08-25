"""Integration coverage for deterministic Markdown evidence capture."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

import pytest

from intent_engineering.capture.base import ConnectorError
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
    cursor = connector.next_checkpoint(second_sources)
    assert cursor is not None
    assert cursor.startswith("markdown:v1:")
    assert await connector.discover(cursor) == ()
    checkpoint = checkpoint_after_discovery(
        connector,
        second_sources,
        second_evidence.observed_at,
    )
    assert checkpoint.connector_id == "markdown"
    assert checkpoint.cursor == cursor


@pytest.mark.anyio
async def test_markdown_manifest_cursor_returns_only_changed_documents_and_migrates_legacy_hash(
    tmp_path: Path,
) -> None:
    """A complete versioned manifest makes no-op scans empty and old cursors safely rescan."""
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n", encoding="utf-8")
    second.write_text("# Second\n", encoding="utf-8")
    connector = MarkdownConnector(
        tmp_path,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )

    initial = await connector.discover(None)
    cursor = connector.next_checkpoint(initial)
    second.write_text("# Second revised\n", encoding="utf-8")
    changed = await connector.discover(cursor)
    updated_cursor = connector.next_checkpoint(changed)
    legacy = initial[-1].external_version

    assert [source.locator for source in initial] == ["first.md", "second.md"]
    assert [source.locator for source in changed] == ["second.md"]
    assert updated_cursor is not None and updated_cursor.startswith("markdown:v1:")
    assert await connector.discover(updated_cursor) == ()
    assert await connector.discover(legacy) == await connector.discover(None)


@pytest.mark.anyio
async def test_markdown_manifest_cursor_rejects_noncanonical_entries_before_filtering(
    tmp_path: Path,
) -> None:
    """Duplicate, unsorted, or malformed rows must rescan instead of hiding source versions."""
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("# First\n", encoding="utf-8")
    second.write_text("# Second\n", encoding="utf-8")
    connector = MarkdownConnector(
        tmp_path,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )
    initial = await connector.discover(None)
    versions = {source.locator: source.external_version for source in initial}

    def cursor(files: list[list[str]]) -> str:
        return "markdown:v1:" + json.dumps({"files": files}, separators=(",", ":"), sort_keys=True)

    malformed = (
        cursor(
            [
                ["first.md", versions["first.md"]],
                ["first.md", versions["first.md"]],
                ["second.md", versions["second.md"]],
            ]
        ),
        cursor(
            [
                ["second.md", versions["second.md"]],
                ["first.md", versions["first.md"]],
            ]
        ),
        cursor([["./first.md", versions["first.md"]], ["second.md", versions["second.md"]]]),
        cursor([["first.md", "sha256:not-a-digest"], ["second.md", versions["second.md"]]]),
    )

    for invalid_cursor in malformed:
        assert await connector.discover(invalid_cursor) == initial


@pytest.mark.anyio
async def test_markdown_connector_rejects_symlinked_paths_outside_the_project(tmp_path: Path) -> None:
    """An in-project symlink must not expose source bytes outside the configured root."""
    outside = tmp_path.parent / "outside.md"
    outside.write_text("# Secret\n", encoding="utf-8")
    link = tmp_path / "linked.md"
    link.symlink_to(outside)
    connector = MarkdownConnector(
        tmp_path,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )

    assert await connector.discover(cursor=None) == ()

    version = f"sha256:{sha256(outside.read_bytes()).hexdigest()}"
    with pytest.raises(ConnectorError, match="outside configured root"):
        await connector.fetch("path:linked.md", version)


@pytest.mark.anyio
async def test_markdown_connector_wraps_operational_discovery_and_fetch_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sync boundary must receive a safe connector error for local I/O failures."""
    connector = MarkdownConnector(
        tmp_path,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )

    def fail_discovery() -> tuple[object, ...]:
        raise OSError("private local filesystem detail")

    monkeypatch.setattr(connector, "_discover_sync", fail_discovery)
    with pytest.raises(ConnectorError, match="Markdown discovery failed") as discovery_error:
        await connector.discover(cursor=None)
    assert "private local filesystem detail" not in str(discovery_error.value)

    with pytest.raises(ConnectorError, match="Markdown fetch failed") as fetch_error:
        await connector.fetch("path:missing.md", "sha256:missing")
    assert "missing.md" not in str(fetch_error.value)


@pytest.mark.anyio
async def test_markdown_connector_wraps_content_version_mismatch(tmp_path: Path) -> None:
    """A file changed after discovery is a safe connector-boundary failure."""
    path = tmp_path / "intent.md"
    path.write_text("# Initial\n", encoding="utf-8")
    connector = MarkdownConnector(
        tmp_path,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )
    source = (await connector.discover(cursor=None))[0]
    path.write_text("# Changed\n", encoding="utf-8")

    with pytest.raises(ConnectorError, match="Markdown fetch failed") as error:
        await connector.fetch(source.external_object_id, source.external_version)

    assert "intent.md" not in str(error.value)


@pytest.mark.anyio
async def test_markdown_connector_rejects_hardlinked_source_files(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-hardlink.md"
    outside.write_text("# Secret\n", encoding="utf-8")
    os.link(outside, tmp_path / "linked.md")
    connector = MarkdownConnector(
        tmp_path,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )

    with pytest.raises(ConnectorError, match="Markdown discovery failed"):
        await connector.discover(None)


@pytest.mark.anyio
async def test_markdown_fetch_rejects_a_swapped_parent_even_with_identical_bytes(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    source_path = docs / "intent.md"
    source_path.write_text("# Stable bytes\n", encoding="utf-8")
    connector = MarkdownConnector(
        tmp_path,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )
    source = (await connector.discover(None))[0]

    docs.rename(tmp_path / "original-docs")
    docs.mkdir()
    (docs / "intent.md").write_text("# Stable bytes\n", encoding="utf-8")

    with pytest.raises(ConnectorError, match="Markdown fetch failed"):
        await connector.fetch(source.external_object_id, source.external_version)


@pytest.mark.anyio
async def test_markdown_connector_reads_from_its_held_root_after_root_path_swap(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "intent.md").write_text("# Original\n", encoding="utf-8")
    connector = MarkdownConnector(
        project,
        ProjectConfig(project_id="capture-test", local_actor="tester"),
    )
    source = (await connector.discover(None))[0]

    held = tmp_path / "held-project"
    project.rename(held)
    attacker = tmp_path / "attacker-project"
    attacker.mkdir()
    (attacker / "intent.md").write_text("# Attacker\n", encoding="utf-8")
    project.symlink_to(attacker, target_is_directory=True)

    record = await connector.fetch(source.external_object_id, source.external_version)

    assert record.payload["content"] == "# Original\n"
