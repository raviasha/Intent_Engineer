"""Deterministic local Markdown evidence connector."""

from __future__ import annotations

import json
import re
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
from intent_engineering.storage.secure import SecureDirectory, SecureRead, UnsafePathError


def _content_hash(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


_MANIFEST_CURSOR_PREFIX = "markdown:v1:"
_CONTENT_VERSION = re.compile(r"sha256:[0-9a-f]{64}\Z")


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
        entries: list[tuple[str, str]] = []
        previous_path: str | None = None
        for item in files:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not all(isinstance(value, str) for value in item)
                or not _valid_manifest_path(item[0])
                or _CONTENT_VERSION.fullmatch(item[1]) is None
                or (previous_path is not None and item[0] <= previous_path)
            ):
                return None
            entries.append((item[0], item[1]))
            previous_path = item[0]
        if _manifest_cursor(entries) != cursor:
            return None
        return dict(entries)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _valid_manifest_path(path: str) -> bool:
    """Accept only the normalized non-empty relative POSIX path shape the scanner emits."""
    relative = PurePosixPath(path)
    return (
        path != "."
        and "\x00" not in path
        and not relative.is_absolute()
        and ".." not in relative.parts
        and relative.as_posix() == path
    )


class MarkdownConnector:
    """Capture Markdown files as content-addressed local evidence."""

    connector_id = "markdown"

    def __init__(self, root: Path | SecureDirectory, config: ProjectConfig) -> None:
        self._root_directory = (
            root.duplicate() if isinstance(root, SecureDirectory) else SecureDirectory.open(root)
        )
        self.root = self._root_directory.path
        self.config = config
        self._last_manifest: tuple[tuple[str, str], ...] = ()
        self._discovered: dict[str, SecureRead] = {}

    def _is_excluded(self, relative: PurePosixPath) -> bool:
        path = relative.as_posix()
        return any(relative.match(pattern) or fnmatchcase(path, pattern) for pattern in self.config.source_exclusions)

    def _discover_sync(self) -> tuple[SourceObject, ...]:
        sources: list[SourceObject] = []
        snapshots: dict[str, SecureRead] = {}
        for relative, snapshot in self._root_directory.walk_regular_files(
            ".md",
            excluded=self._is_excluded,
        ):
            relative_path = relative.as_posix()
            object_id = f"path:{relative_path}"
            sources.append(
                SourceObject(
                    external_object_id=object_id,
                    external_version=_content_hash(snapshot.content),
                    locator=relative_path,
                )
            )
            snapshots[object_id] = snapshot
        self._discovered = snapshots
        return tuple(sorted(sources, key=lambda source: source.locator))

    async def discover(self, cursor: str | None) -> Sequence[SourceObject]:
        """Discover only source versions absent from the prior full manifest cursor."""
        try:
            sources = await anyio.to_thread.run_sync(self._discover_sync)
        except (OSError, UnicodeError, ValueError) as error:
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
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != relative_path
            or relative_path == "."
        ):
            raise ValueError(f"invalid Markdown object ID: {object_id}")
        discovered = self._discovered.get(object_id)
        try:
            snapshot = self._root_directory.read_relative(
                relative,
                expected_identities=discovered.identities if discovered is not None else None,
            )
        except UnsafePathError:
            if discovered is None and self._root_directory.final_is_symlink(relative):
                raise ConnectorError(
                    "Markdown fetch rejected a path outside configured root"
                ) from None
            raise
        content = snapshot.content
        content_hash = _content_hash(content)
        if version != content_hash:
            raise ValueError(f"Markdown object changed before fetch: {object_id}")
        return RawSourceObject(
            connector_type=self.connector_id,
            external_object_id=object_id,
            external_version=version,
            author=self.config.local_actor,
            observed_at=datetime.fromtimestamp(snapshot.modified_ns / 1_000_000_000, tz=UTC),
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
