"""Deterministic local Markdown evidence connector."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from hashlib import sha256
from pathlib import Path, PurePosixPath

import anyio

from intent_engineering.capture.base import (
    ConnectorError,
    RawSourceObject,
    SourceObject,
    normalize_raw_source,
)
from intent_engineering.core.models import EvidenceRecord, ProjectConfig


def _content_hash(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


_MANIFEST_CURSOR_PREFIX = "markdown:v1:"


def _manifest_cursor(manifest: Sequence[tuple[str, str]]) -> str:
    """Encode a complete sorted path/version snapshot in a stable, versioned cursor."""
    payload = {"files": [[path, version] for path, version in manifest]}
    return _MANIFEST_CURSOR_PREFIX + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _parse_manifest_cursor(cursor: str | None) -> dict[str, str] | None:
    """Read a valid manifest cursor; legacy or malformed values deliberately rescan."""
    if cursor is None or not cursor.startswith(_MANIFEST_CURSOR_PREFIX):
        return None
    try:
        payload = json.loads(cursor.removeprefix(_MANIFEST_CURSOR_PREFIX))
        files = payload["files"]
        if not isinstance(files, list):
            return None
        manifest: dict[str, str] = {}
        for item in files:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or not item[0]
            ):
                return None
            manifest[item[0]] = item[1]
        return manifest
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


class MarkdownConnector:
    """Capture Markdown files as content-addressed local evidence."""

    connector_id = "markdown"

    def __init__(self, root: Path, config: ProjectConfig) -> None:
        self.root = root.resolve()
        self.config = config
        self._last_manifest: tuple[tuple[str, str], ...] = ()

    def _is_excluded(self, relative: PurePosixPath) -> bool:
        path = relative.as_posix()
        return any(relative.match(pattern) or fnmatchcase(path, pattern) for pattern in self.config.source_exclusions)

    def _is_within_root(self, path: Path) -> bool:
        """Return whether a resolved candidate stays inside the configured root."""
        return path.resolve().is_relative_to(self.root)

    def _discover_sync(self) -> tuple[SourceObject, ...]:
        sources: list[SourceObject] = []
        for path in self.root.rglob("*.md"):
            if not path.is_file():
                continue
            if not self._is_within_root(path):
                continue
            relative = PurePosixPath(path.relative_to(self.root).as_posix())
            if self._is_excluded(relative):
                continue
            content = path.read_bytes()
            relative_path = relative.as_posix()
            sources.append(
                SourceObject(
                    external_object_id=f"path:{relative_path}",
                    external_version=_content_hash(content),
                    locator=relative_path,
                )
            )
        return tuple(sorted(sources, key=lambda source: source.locator))

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        """Discover only source versions absent from the prior full manifest cursor."""
        try:
            sources = await anyio.to_thread.run_sync(self._discover_sync)
        except (OSError, UnicodeError) as error:
            raise ConnectorError("Markdown discovery failed") from error
        self._last_manifest = tuple((source.locator, source.external_version) for source in sources)
        prior_manifest = _parse_manifest_cursor(cursor)
        if prior_manifest is None:
            return sources
        return tuple(
            source
            for source in sources
            if prior_manifest.get(source.locator) != source.external_version
        )

    def _fetch_sync(self, object_id: str, version: str) -> RawSourceObject:
        if not object_id.startswith("path:"):
            raise ValueError(f"unsupported Markdown object ID: {object_id}")
        relative_path = object_id.removeprefix("path:")
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid Markdown object ID: {object_id}")
        path = self.root.joinpath(*relative.parts)
        if not self._is_within_root(path):
            raise ConnectorError("Markdown fetch rejected a path outside configured root")
        content = path.read_bytes()
        content_hash = _content_hash(content)
        if version != content_hash:
            raise ValueError(f"Markdown object changed before fetch: {object_id}")
        stat = path.stat()
        return RawSourceObject(
            connector_type=self.connector_id,
            external_object_id=object_id,
            external_version=version,
            author=self.config.local_actor,
            observed_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            source_locator=relative_path,
            content_hash=content_hash,
            payload={"path": relative_path, "content": content.decode("utf-8")},
        )

    async def fetch(self, object_id: str, version: str) -> RawSourceObject:
        """Read one discovered Markdown version beyond the async event loop boundary."""
        try:
            return await anyio.to_thread.run_sync(self._fetch_sync, object_id, version)
        except ConnectorError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            raise ConnectorError("Markdown fetch failed") from error

    def normalize(self, raw: RawSourceObject) -> EvidenceRecord:
        """Normalize raw local file data into the shared evidence vocabulary."""
        return normalize_raw_source(raw)

    def next_checkpoint(self, discovered: Sequence[SourceObject]) -> str | None:
        """Commit the full snapshot scanned by the latest discovery, even for an empty delta."""
        del discovered
        return _manifest_cursor(self._last_manifest)
